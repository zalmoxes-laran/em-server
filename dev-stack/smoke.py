#!/usr/bin/env python3
"""End-to-end smoke of the dev stack — the proof that MinIO is REAL.

Everything below could be faked by a server that kept the bytes in a dict, and
that is exactly what this script is written to rule out. So it does not only ask
em-server whether it stored something: it **opens the bucket itself** and looks.

    python dev-stack/smoke.py            # after `docker compose … up -d`

What it checks, in order, and what each one would catch:

1. **a token** from the dev realm — if this fails the realm import is wrong, and
   nothing after it means anything;
2. **PUT** an asset → the reference is the sha256 of the bytes we sent (a store
   that invented its own name would show up here);
3. **the object is in MinIO**, verified against the bucket directly, and its size
   and content-type are read with a `head` that does not download it;
4. **GET** returns the same bytes, byte for byte;
5. **no token → refused**, and nothing is stored;
6. **promotion** (DP-76): `s3dgraphy.api.promote_resource` with the URL em-server
   serves → the ResourceNode becomes a `reference` carrying that url + checksum,
   **and that url really serves those bytes**. This is the arc: the model leaves
   the .blend, lands in the object store, and the graph points at it.

Exit code 0 = everything measured. Anything skipped is printed as SKIPPED with
the reason — a smoke test that hides what it could not do is worse than none.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent

# s3Dgraphy from the checkout beside this repo, when it is there: the promotion
# step exercises the library as it is NOW, not a pinned wheel.
_CHECKOUT = HERE.parent.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir():
    sys.path.insert(0, str(_CHECKOUT))

FAILURES: list[str] = []
SKIPS: list[str] = []


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_env_file(path: pathlib.Path) -> None:
    """Read `.env.dev` the way compose does — so the script and the stack agree
    on the ports and the realm without being told twice."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def ok(label: str, condition: bool, detail: str = "") -> bool:
    mark = "  ok  " if condition else " FAIL "
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)
    return condition


def skip(label: str, why: str) -> None:
    print(f"[ SKIP ] {label} — {why}")
    SKIPS.append(f"{label}: {why}")


def request(url: str, *, method: str = "GET", data: bytes | None = None,
            headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return answer.status, answer.read(), _headers(answer)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc)
    except urllib.error.URLError as exc:
        # nothing listening: a stack that is down is the most likely reason
        # somebody runs this, and a traceback would tell them nothing
        return 0, str(exc.reason).encode(), {}


def _headers(answer) -> dict:
    """Header names, lower-cased. HTTP says they are case-insensitive and uvicorn
    sends them lower-case, so a `dict(...)` lookup for "Content-Type" silently
    finds nothing — which reads exactly like a server that forgot to set it."""
    return {k.lower(): v for k, v in answer.headers.items()}


def main() -> int:
    load_env_file(HERE / ".env.dev")
    load_env_file(HERE / ".env.dev.example")     # fallback: the defaults

    keycloak = f"http://localhost:{env('KEYCLOAK_PORT', '8085')}"
    server = f"http://localhost:{env('EM_SERVER_PORT', '8000')}"
    realm = env("DEV_REALM", "em-dev")
    client_id = env("DEV_CLIENT_ID", "em-server")
    room = "smoke-room"

    print(f"em-server : {server}")
    print(f"keycloak  : {keycloak}/realms/{realm}")
    print()

    # ── 0 · the service is up and says what it is using ─────────────────────
    status, body, _ = request(f"{server}/v1/health")
    if status != 200:
        why = f"status {status}" if status else f"not reachable ({body.decode()})"
        print(f"em-server is not answering on {server} ({why}). "
              f"Is the stack up? From dev-stack/: "
              f"`docker compose --env-file .env.dev "
              f"-f docker-compose.dev.yml up -d` (or `docker-compose`, see "
              f"README-DEV.md)")
        return 2
    health = json.loads(body)
    ok("em-server is enforcing tokens", health.get("auth") == "keycloak",
       str(health.get("auth")))
    ok("the asset store is MinIO", "minio" in str(health.get("asset_store")),
       str(health.get("asset_store")))

    # ── 1 · a token from the dev realm ──────────────────────────────────────
    form = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": env("DEV_CLIENT_SECRET", "em-dev-secret"),
        "username": env("DEV_USER", "dev"),
        "password": env("DEV_PASSWORD", "dev"),
    }).encode()
    status, body, _ = request(
        f"{keycloak}/realms/{realm}/protocol/openid-connect/token",
        method="POST", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        print(f"could not get a token ({status}): {body[:200]!r}")
        return 2
    token = json.loads(body)["access_token"]
    ok("a dev token was issued", bool(token))
    auth = {"Authorization": f"Bearer {token}"}

    # ── 2 · PUT: the reference IS the digest of what we sent ─────────────────
    payload = b"glTF\x02\x00\x00\x00smoke-test-asset-" + os.urandom(8)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    status, body, _ = request(
        f"{server}/v1/rooms/{room}/asset?media_type=model/gltf-binary",
        method="PUT", data=payload,
        headers={**auth, "Content-Type": "model/gltf-binary"})
    if status != 200:
        print(f"the upload was refused ({status}): {body[:300]!r}")
        return 2
    info = json.loads(body)
    ok("the reference is the sha256 of the bytes", info["ref"] == digest,
       info["ref"])
    ok("the uploader is the token's identity", bool(info.get("author")),
       str(info.get("author")))
    ok("size reported matches", info.get("size") == len(payload),
       f"{info.get('size')} B")

    # ── 3 · and it is REALLY in the bucket ───────────────────────────────────
    try:
        from minio import Minio
    except ImportError:
        skip("the object is in the MinIO bucket",
             "the `minio` client is not installed in this interpreter "
             "(pip install minio) — em-server's own answer is not proof")
    else:
        client = Minio(f"localhost:{env('MINIO_API_PORT', '9000')}",
                       access_key=env("MINIO_ROOT_USER", "minioadmin"),
                       secret_key=env("MINIO_ROOT_PASSWORD", "minioadmin"),
                       secure=False)
        bucket = env("MINIO_BUCKET", "em-assets")
        key = digest.split(":", 1)[1]
        stat = client.stat_object(bucket, key)
        ok("the object is in the MinIO bucket, keyed by its digest",
           stat.size == len(payload), f"{bucket}/{key[:16]}… {stat.size} B")
        ok("…with the media type it was uploaded with",
           stat.content_type == "model/gltf-binary", str(stat.content_type))
        response = client.get_object(bucket, key)
        try:
            in_bucket = response.read()
        finally:
            response.close()
            response.release_conn()
        ok("…and the bytes IN THE BUCKET are the bytes we sent",
           in_bucket == payload)

        # a second upload of the same bytes is the same object
        status, body, _ = request(
            f"{server}/v1/rooms/{room}/asset?media_type=model/gltf-binary",
            method="PUT", data=payload,
            headers={**auth, "Content-Type": "model/gltf-binary"})
        again = json.loads(body)
        ok("the same bytes twice are ONE object (dedup)",
           again["ref"] == digest and again.get("created") is False,
           f"created={again.get('created')}")

    # ── 4 · GET returns the same bytes ───────────────────────────────────────
    asset_url = f"{server}/v1/rooms/{room}/asset/{digest}"
    status, fetched, headers = request(asset_url, headers=auth)
    ok("GET returns the asset", status == 200, f"status {status}")
    ok("…byte for byte", fetched == payload, f"{len(fetched)} B")
    ok("…with its media type",
       headers.get("content-type", "").startswith("model/gltf-binary"),
       headers.get("content-type", ""))

    # ── 5 · without a token, nothing ─────────────────────────────────────────
    status, _, _ = request(
        f"{server}/v1/rooms/{room}/asset?media_type=text/plain",
        method="PUT", data=b"i should not be stored")
    ok("an upload without a token is refused", status in (401, 403),
       f"status {status}")
    status, _, _ = request(asset_url)
    ok("a download without a token is refused too", status in (401, 403),
       f"status {status}")

    # ── 6 · the promotion (DP-76) ────────────────────────────────────────────
    try:
        from s3dgraphy import api
        from s3dgraphy.graph import Graph
        from s3dgraphy.nodes import ResourceNode
    except ImportError as exc:
        skip("the promotion writes a verifiable reference",
             f"s3dgraphy is not importable ({exc})")
    else:
        if not hasattr(api, "promote_resource"):
            skip("the promotion writes a verifiable reference",
                 "this s3dgraphy has no promote_resource (pre-DP-76)")
        else:
            graph = Graph(graph_id="smoke")
            node = ResourceNode("res-smoke", name="US smoke (working mesh)",
                                url="//local/US.blend")
            node.set_residency("resident")
            graph.add_node(node)
            result = api.promote_resource(
                graph, "res-smoke", url=asset_url, sha256=info["sha256"],
                media_type="model/gltf-binary", author=info.get("author"))
            promoted = graph.find_node_by_id("res-smoke")
            ok("the resource became a reference",
               promoted.data["residency"] == "reference")
            ok("…carrying the url and the checksum",
               promoted.data["url"] == asset_url
               and promoted.data["checksum"] == digest)
            event = graph.find_node_by_id(result["process_id"])
            ok("…and a dated DTC event records who published it",
               event is not None and event.node_type == "dtc_process"
               and bool(event.data.get("created_at")),
               str(event.data.get("created_by")))

            # THE point of the whole arc: the url in the graph is not a claim.
            status, served, _ = request(promoted.data["url"], headers=auth)
            ok("the url written in the graph really serves those bytes",
               status == 200 and served == payload)
            ok("…and hashes to the checksum the graph recorded",
               "sha256:" + hashlib.sha256(served).hexdigest()
               == promoted.data["checksum"])

    print()
    if SKIPS:
        print(f"{len(SKIPS)} check(s) SKIPPED:")
        for line in SKIPS:
            print(f"  · {line}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for line in FAILURES:
            print(f"  · {line}")
        return 1
    print("smoke: everything measured passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
