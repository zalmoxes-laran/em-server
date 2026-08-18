#!/usr/bin/env python3
"""LIVE smoke: the RESIDENT corpus makes the rights bite when an asset is served.

The hole this measures was demonstrated on 17 Aug: an asset in MinIO whose
licence lived in a per-project corpus **file** came back with `x-em-license:
null`, because the enforcement reads the rights out of a document em-server
holds. With the corpus resident (one register per instance, in the same bucket as
the bytes) the header carries the licence and the embargo refuses.

Run it against the dev stack, with two real tokens from Keycloak:

    ./dev-stack/token.sh > /tmp/owner.jwt
    ./dev-stack/token.sh --user viewer > /tmp/viewer.jwt
    ./dev-stack/smoke_corpus_resident.py --owner /tmp/owner.jwt --viewer /tmp/viewer.jwt

Or let it mint them itself (it shells out to `token.sh`):

    ./dev-stack/smoke_corpus_resident.py

What it asserts, in the order the day happens:

1. a fresh asset is uploaded and served with **no rights** — nothing is invented;
2. it is **registered** in the resident corpus (and a digest nobody uploaded is
   refused, so the register describes the store);
3. a **licence + author** are declared → `x-em-license` is that licence. *This is
   the header that was null.*
4. an **embargo** is declared → the **viewer** gets 403 **with the date**, the
   **owner** (editor+ in a room that really grants it) gets 200;
5. an **expired** embargo is over for everybody — the verdict is today's date;
6. a room name **invented in the URL** grants nothing (the owner bootstrap must
   not turn a URL into a role);
7. `GET /v1/corpus?sha256=…` returns the **slice** about that file, with its
   chain — open to any authenticated caller — while the **whole** register is a
   **curation read** (403 with the remedy unless this instance declares curators).

Exit code 0 only if every assertion held. Each line is printed as it is measured,
because a smoke test that only says "ok" is one nobody can debug.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_BASE = os.environ.get("EM_BASE", "http://localhost:8000/v1")
#: a room whose ACL this stack really holds (see `seed_rooms.py`): the override
#: has to come from a grant that exists, not from one the request creates
ROOM = os.environ.get("EM_ROOM", "basilica-demo")

checks = 0
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {what}")
    if not ok:
        failures.append(what)


def call(method: str, url: str, *, token: str | None = None,
         data: bytes | None = None, json_body: dict | None = None,
         media_type: str | None = None) -> tuple[int, dict, bytes]:
    """(status, headers, body). Never raises on an HTTP error — the status IS the
    measurement, and an exception would hide the 403 we are here to see.

    Header names come back LOWERCASED, deliberately: starlette sends them lower
    case, and a `dict(answer.headers)` plus a `get("X-EM-License")` silently
    returned None — which read as "the header is missing" and was the first thing
    this smoke got wrong about itself.
    """
    body = data
    headers = {}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if media_type:
        headers["Content-Type"] = media_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request) as answer:
            return answer.status, _lower(answer.headers), answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers), exc.read()


def _lower(headers) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


def token_for(user: str | None) -> str:
    args = [str(HERE / "token.sh")] + (["--user", user] if user else [])
    out = subprocess.run(args, capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--owner", help="file holding an owner/editor token")
    parser.add_argument("--viewer", help="file holding the viewer's token")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    owner = (pathlib.Path(args.owner).read_text().strip() if args.owner
             else token_for(None))
    viewer = (pathlib.Path(args.viewer).read_text().strip() if args.viewer
              else token_for("viewer"))

    def orcid_of(jwt: str) -> str:
        import base64
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("orcid") or claims.get("preferred_username") or "?"

    who_owner, who_viewer = orcid_of(owner), orcid_of(viewer)
    print(f"base {base} · room {ROOM}")
    print(f"owner {who_owner} · viewer {who_viewer}")

    # ── 1 · upload, and nothing is claimed about it ───────────────────────────
    print("\n1 · a fresh asset, undocumented")
    pixels = b"\x89PNG\r\n\x1a\n" + str(random.random()).encode("ascii")
    status, _, raw = call("PUT", f"{base}/rooms/{ROOM}/asset?media_type=image/png",
                          token=owner, data=pixels, media_type="image/png")
    check(status == 200, f"uploaded ({status})")
    ref = json.loads(raw)["ref"]
    print(f"    ref {ref}")
    status, headers, _ = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}", token=owner)
    check(status == 200, "served")
    check(not headers.get("x-em-license"),
          "no licence header: nothing declared, nothing invented")

    # ── 2 · the register describes the STORE ─────────────────────────────────
    print("\n2 · registered in the resident corpus")
    status, _, raw = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "resource", "checksum": ref, "name": "prospetto_nord.png",
        "media_type": "image/png"})
    check(status == 200, f"the file is in the register ({status})")
    ghost = "sha256:" + "99" * 32
    status, _, raw = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "resource", "checksum": ghost})
    check(status == 404, f"a digest nobody uploaded is refused ({status})")

    # ── 3 · THE HEADER THAT WAS NULL ─────────────────────────────────────────
    print("\n3 · licence + author declared in the corpus")
    status, _, raw = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "attribution", "checksum": ref, "license": "CC-BY-4.0",
        "author": who_owner, "author_name": "chi ha scattato"})
    check(status == 200, f"attributed ({status})")
    report = json.loads(raw)["report"]
    check(report.get("attributor") == who_owner,
          "signed by the token's identity, not by a field in the body")
    status, headers, _ = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}", token=owner)
    check(headers.get("x-em-license") == "CC-BY-4.0",
          f"x-em-license = {headers.get('x-em-license')!r}  ← was null before")
    check(who_owner in (headers.get("x-em-author") or ""),
          f"x-em-author = {headers.get('x-em-author')!r}")

    # ── 4 · the embargo bites ────────────────────────────────────────────────
    print("\n4 · an embargo, and who may pass it")
    status, _, _ = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "attribution", "checksum": ref, "embargo": "2099-01-01",
        "reason": "in corso di studio"})
    check(status == 200, f"embargo declared ({status})")
    status, _, raw = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}", token=viewer)
    check(status == 403, f"the viewer is refused ({status})")
    detail = ""
    try:
        detail = json.loads(raw).get("detail", "")
    except Exception:  # noqa: BLE001 — a body that is not JSON is still a body
        detail = raw.decode("utf-8", "replace")
    check("2099-01-01" in detail, f"…with the date: {detail[:90]}…")
    status, _, _ = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}", token=owner)
    check(status == 200, f"the owner of the room is served ({status})")
    status, _, _ = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}")
    check(status == 401,
          f"a caller with no token gets 401 at the router's door, before the "
          f"embargo is even consulted ({status})")

    # ── 5 · the verdict is TODAY'S date ──────────────────────────────────────
    print("\n5 · an expired embargo is over")
    status, _, _ = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "attribution", "checksum": ref, "embargo": "2000-01-01"})
    check(status == 200, f"re-declared in the past ({status})")
    status, _, _ = call("GET", f"{base}/rooms/{ROOM}/asset/{ref}", token=viewer)
    check(status == 200, f"the viewer is served ({status})")

    # ── 6 · a room name is not a role ────────────────────────────────────────
    print("\n6 · an invented room grants nothing")
    status, _, _ = call("POST", f"{base}/corpus/append", token=owner, json_body={
        "act": "attribution", "checksum": ref, "embargo": "2099-01-01"})
    check(status == 200, "embargo back in the future")
    status, _, _ = call("GET",
                        f"{base}/rooms/stanza-inventata-{random.randint(0, 9999)}"
                        f"/asset/{ref}", token=viewer)
    check(status == 403, f"a room typed into the URL does not open it ({status})")

    # ── 7 · the register reads back: the SLICE for anyone, the WHOLE for a curator
    print("\n7 · GET /v1/corpus")
    status, _, raw = call("GET", f"{base}/corpus?sha256={ref}", token=owner)
    part = json.loads(raw)
    check(status == 200 and part["sliced"] is True,
          f"the slice is open to an authenticated caller ({status})")
    names = [n.get("name") for n in part["graph"]["nodes"]]
    check(bool(names), f"the slice about this file: {names}")
    check(any(str(n).startswith("CC-BY") for n in names),
          "…carries its licence, or the slice would answer the rights wrongly")
    status, _, raw = call("GET", f"{base}/corpus?sha256={ref}", token=viewer)
    check(status == 200, f"…and to a viewer, who cites those files too ({status})")

    status, _, raw = call("GET", f"{base}/corpus", token=viewer)
    if status == 403:
        detail = json.loads(raw).get("detail", "")
        check("curation" in detail and "sha256" in detail,
              f"the WHOLE register is a curation read: 403 that names the remedy")
    else:
        check(status == 200,
              f"the whole register is open on this instance "
              f"(EM_CORPUS_OPEN / a curator list) — {status}")
    status, _, raw = call("GET", f"{base}/corpus", token=owner)
    whole = json.loads(raw) if status == 200 else part
    check(status in (200, 403), f"the whole read, as the owner: {status}")
    if status == 200:
        check(whole["graph"]["data"]["em_collection"] == "DTCCorpus",
              f"the corpus has {whole['nodes']} nodes, {whole['edges']} edges "
              f"and is marked as a corpus")
        check(part["version"] == whole["version"],
              "a slice reports the version of the register it came from")
        print(f"    version {whole['version']}")
        print(f"    store   {whole['store']}")
    else:
        print("    (the owner is not a declared curator here: "
              "set EM_CORPUS_CURATORS to read the lot)")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    for bad in failures:
        print(f"  FAILED: {bad}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
