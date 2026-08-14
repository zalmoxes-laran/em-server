#!/usr/bin/env python3
"""End-to-end smoke of the IMAGE layer: MinIO → Cantaloupe → manifest.

    python dev-stack/smoke_iiif.py       # after `docker compose … up -d`

The arc it measures, and every step of it is a real request to a real service:

1. an image is uploaded through em-server and lands in **MinIO**;
2. **Cantaloupe** serves `info.json` for it — from the object store, with the
   asset's sha256 as the identifier, so nothing was copied or imported;
3. a **thumbnail** is a size request (no thumbnail pipeline exists in this
   project, and none should);
4. the **crop of an annotated region** is a region request, computed from the
   region's normalised [0,1] coordinates — no pixel arithmetic anywhere;
5. a graph is built **in a room**, through the ordinary op stream: the image
   resource and two annotation regions;
6. em-server returns a **IIIF Presentation 3 manifest** for it, with the canvas
   sized from the real `info.json` and the two regions projected as **W3C Web
   Annotations**.

The two-tier claim is what step 6 demonstrates: the annotation is a node in the
em.json — the authoring truth, stamped and versioned — and the manifest is a
VIEW of it that any viewer in the world can open.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import socket
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from smoke import (FAILURES, SKIPS, load_env_file, ok, request,  # noqa: E402
                   skip)

_CHECKOUT = HERE.parent.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir():
    sys.path.insert(0, str(_CHECKOUT))


# ── a minimal WebSocket client, because the op stream is the write path ─────
#
# Fifty lines rather than a dependency: this script must run with the
# interpreter that is there. The one detail that is not optional is MASKING —
# an unmasked client frame is a protocol error and uvicorn closes the socket,
# which looks exactly like the room refusing you.

class MiniWs:
    def __init__(self, url: str, token: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.sock.sendall("\r\n".join([
            f"GET {path} HTTP/1.1",
            f"Host: {parsed.hostname}:{parsed.port}",
            "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
            f"Authorization: Bearer {token}", "", ""]).encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += self.sock.recv(4096)
        if b" 101 " not in raw.split(b"\r\n")[0]:
            raise RuntimeError(f"the room refused the socket: "
                               f"{raw.split(b'\r\n')[0].decode()}")
        self.buf = bytearray(raw.split(b"\r\n\r\n", 1)[1])

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask +
                          bytes(data[i] ^ mask[i % 4] for i in range(n)))

    def recv(self, timeout: float = 5.0) -> dict | None:
        self.sock.settimeout(timeout)
        while True:
            if len(self.buf) >= 2:
                length = self.buf[1] & 0x7F
                offset = 2
                if length == 126:
                    length = struct.unpack(">H", self.buf[2:4])[0]
                    offset = 4
                elif length == 127:
                    length = struct.unpack(">Q", self.buf[2:10])[0]
                    offset = 10
                if len(self.buf) >= offset + length:
                    payload = bytes(self.buf[offset:offset + length])
                    del self.buf[:offset + length]
                    if self.buf[:0] is not None and payload:
                        try:
                            return json.loads(payload)
                        except ValueError:
                            continue
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def make_png(width: int, height: int) -> bytes:
    """A gradient PNG, built here so the smoke needs no fixture on disk."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row += bytes(((x * 255) // width, (y * 255) // height, 128))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
            + chunk(b"IEND", b""))


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """The dimensions a JPEG declares — so "a thumbnail arrived" can be a
    measurement rather than a byte count."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        if data[i + 1] in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (width, height)
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


WIDTH, HEIGHT = 1024, 768
ROOM = "iiif-smoke-5"
REGIONS = {                       # normalised [0,1] — the truth in the graph
    "reg-muro": [0.10, 0.10, 0.30, 0.20],
    "reg-soglia": [0.55, 0.60, 0.25, 0.25],
}


def main() -> int:
    load_env_file(HERE / ".env.dev")
    load_env_file(HERE / ".env.dev.example")
    server = f"http://localhost:{os.environ.get('EM_SERVER_PORT', '8000')}"
    keycloak = f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8085')}"
    iiif = f"http://localhost:{os.environ.get('CANTALOUPE_PORT', '8182')}/iiif/3"
    realm = os.environ.get("DEV_REALM", "em-dev")

    print(f"em-server  : {server}")
    print(f"iiif       : {iiif}\n")

    status, body, _ = request(f"{iiif}/", timeout=5)
    if status == 0:
        print(f"Cantaloupe is not answering on {iiif}. Is the stack up? "
              f"(dev-stack/README-DEV.md)")
        return 2

    # ── a token ─────────────────────────────────────────────────────────────
    form = urllib.parse.urlencode({
        "grant_type": "password", "client_id": os.environ.get("DEV_CLIENT_ID", "em-server"),
        "client_secret": os.environ.get("DEV_CLIENT_SECRET", "em-dev-secret"),
        "username": os.environ.get("DEV_USER", "dev"),
        "password": os.environ.get("DEV_PASSWORD", "dev")}).encode()
    status, body, _ = request(f"{keycloak}/realms/{realm}/protocol/openid-connect/token",
                              method="POST", data=form,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        print(f"could not get a token ({status})")
        return 2
    token = json.loads(body)["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # ── 1 · the image goes into MinIO through em-server ─────────────────────
    png = make_png(WIDTH, HEIGHT)
    digest = "sha256:" + hashlib.sha256(png).hexdigest()
    status, body, _ = request(f"{server}/v1/rooms/{ROOM}/asset?media_type=image/png",
                              method="PUT", data=png,
                              headers={**auth, "Content-Type": "image/png"})
    if status != 200:
        print(f"the upload was refused ({status}): {body[:200]!r}")
        return 2
    asset = json.loads(body)
    ok("the image is in the object store", asset["ref"] == digest, asset["ref"])
    identifier = asset["sha256"]

    # ── 2 · Cantaloupe serves it FROM THE BUCKET, by its digest ─────────────
    status, body, _ = request(f"{iiif}/{identifier}/info.json")
    ok("info.json is served for it", status == 200, f"status {status}")
    if status == 200:
        info = json.loads(body)
        ok("…and the image server measured the real image",
           (info.get("width"), info.get("height")) == (WIDTH, HEIGHT),
           f"{info.get('width')}×{info.get('height')}")
        ok("…at IIIF Image API level 2 (regions and sizes on demand)",
           info.get("profile") == "level2", str(info.get("profile")))

    # ── 3 · a thumbnail is a SIZE REQUEST ───────────────────────────────────
    status, thumb, headers = request(f"{iiif}/{identifier}/full/200,/0/default.jpg")
    ok("a thumbnail is served", status == 200 and thumb[:2] == b"\xff\xd8",
       f"{len(thumb)} B")
    ok("…at the size asked for, aspect kept", jpeg_size(thumb) == (200, 150),
       str(jpeg_size(thumb)))

    # ── 4 · the crop of a region, from normalised coordinates ───────────────
    try:
        from s3dgraphy.iiif import region_url
        from s3dgraphy.nodes import ResourceNode
        from s3dgraphy.nodes.annotation_region_node import AnnotationRegionNode
    except ImportError as exc:
        skip("the crop of a region is served", f"s3dgraphy not importable ({exc})")
        crop_url = None
    else:
        resource = ResourceNode("img-1", name="Foto di scavo",
                                url=f"{server}/v1/rooms/{ROOM}/asset/{digest}",
                                checksum=digest)
        resource.data["media_type"] = "image/png"
        region = AnnotationRegionNode("reg-muro", "muro", "rect",
                                      rect=REGIONS["reg-muro"], resource_id="img-1")
        crop_url = region_url(resource, region, iiif)
        status, crop, _ = request(crop_url)
        expected = (round(0.30 * WIDTH), round(0.20 * HEIGHT))
        ok("the crop of an annotated region is served",
           status == 200 and crop[:2] == b"\xff\xd8", f"{len(crop)} B")
        ok("…and it is the region the graph describes, in pixels",
           jpeg_size(crop) == expected,
           f"{jpeg_size(crop)} (expected {expected})")

    # ── 5 · the graph goes into the room, through the op stream ─────────────
    ws_url = f"ws://localhost:{os.environ.get('EM_SERVER_PORT', '8000')}/v1/rooms/{ROOM}/ws"
    try:
        room = MiniWs(ws_url, token)
    except Exception as exc:                       # noqa: BLE001
        skip("the room holds the image and its regions", f"could not join: {exc}")
        room = None
    if room is not None:
        for _ in range(3):                         # host_info, snapshot, presence
            room.recv()
        applied = 0
        # `node_type`, not `type`: it is what the em.json schema says, and the
        # loader SKIPS a node it cannot type — with a warning nobody sees if the
        # only thing checked is that the op was applied
        ops = [{"op": "add_node", "node": {
            "id": "img-1", "node_type": "resource", "name": "Foto di scavo",
            "data": {"url": f"{server}/v1/rooms/{ROOM}/asset/{digest}",
                     "checksum": digest, "media_type": "image/png",
                     "residency": "reference"}}}]
        for node_id, rect in REGIONS.items():
            ops.append({"op": "add_node", "node": {
                "id": node_id, "node_type": "annotation_region", "name": node_id,
                "data": {"shape_kind": "rect", "rect": rect, "page": 0,
                         "resource_id": "img-1"}}})
            # an `add_edge` op is FLAT — the relay reads `source`/`target`/
            # `edge_type` off the op itself. Nested under an "edge" key they all
            # read as None, the edge is stored as None__None__None, and the only
            # sign of it is a load warning nobody looks at.
            ops.append({"op": "add_edge", "id": f"e-{node_id}",
                        "source": node_id, "target": "img-1",
                        "edge_type": "is_on_resource"})
        for index, op in enumerate(ops):
            room.send({"v": 1, "type": "op", "ts": f"2026-08-14T14:00:{index:02d}Z",
                       **op})
        accepted = 0
        for _ in ops:
            answer = room.recv()
            if not answer or answer.get("type") != "op_result":
                continue
            if answer.get("applied"):
                applied += 1
                accepted += 1
            elif answer.get("reason") == "idempotent":
                # a second run of this smoke re-sends the same nodes: the CRDT
                # says "already here", which is the room HOLDING them, not a
                # failure. Counting it as one would make the check pass only on
                # a fresh stack, which is the least useful moment to test.
                accepted += 1
        ok("the room holds the image and its two regions",
           accepted == len(ops),
           f"{applied} applied, {accepted - applied} already there")

        # ── 6 · the manifest: the graph, as any viewer sees it ──────────────
        status, body, _ = request(
            f"{server}/v1/rooms/{ROOM}/iiif/img-1/manifest", headers=auth)
        ok("em-server builds a manifest for it", status == 200, f"status {status}")
        if status == 200:
            manifest = json.loads(body)
            ok("…a IIIF Presentation 3 Manifest",
               manifest.get("type") == "Manifest"
               and "presentation/3" in manifest.get("@context", ""))
            canvases = manifest.get("items", [])
            ok("…with one canvas for the image", len(canvases) == 1)
            if canvases:
                canvas = canvases[0]
                ok("…sized from the real info.json, not a placeholder",
                   (canvas["width"], canvas["height"]) == (WIDTH, HEIGHT),
                   f"{canvas['width']}×{canvas['height']}")
                ok("…and no assumption had to be declared",
                   "em:warnings" not in manifest,
                   str(manifest.get("em:warnings", ""))[:80])
                annotations = canvas.get("annotations", [{}])[0].get("items", [])
                ok("…carrying the two regions as Web Annotations",
                   len(annotations) == 2, f"{len(annotations)}")
                if len(annotations) == 2:
                    selector = annotations[0]["target"]["selector"]
                    ok("…with a pixel FragmentSelector a viewer understands",
                       selector["type"] == "FragmentSelector"
                       and selector["value"].startswith("xywh="),
                       selector["value"])
                    value = selector["value"][len("xywh="):]
                    if value.startswith("percent:"):
                        value = value[len("percent:"):]
                    x, y, w, h = (int(round(float(v))) for v in value.split(","))
                    want = REGIONS[annotations[0]["id"]]
                    ok("…and the numbers are the normalised region, scaled",
                       (x, y, w, h) == (round(want[0] * WIDTH), round(want[1] * HEIGHT),
                                        round(want[2] * WIDTH), round(want[3] * HEIGHT)),
                       f"{(x, y, w, h)}")
                painting = canvas["items"][0]["items"][0]["body"]
                status, painted, _ = request(painting["id"])
                ok("…and the image the canvas paints really resolves",
                   status == 200 and painted[:2] == b"\xff\xd8",
                   f"{len(painted)} B")
                ok("…through an Image API service the viewer can zoom with",
                   painting["service"][0]["type"] == "ImageService3")
        room.close()

    print()
    for line in SKIPS:
        print(f"  · SKIPPED {line}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for line in FAILURES:
            print(f"  · {line}")
        return 1
    print("iiif smoke: everything measured passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
