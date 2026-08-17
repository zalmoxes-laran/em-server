#!/usr/bin/env python3
"""The embargo gate, end to end, with a REAL viewer token.

The gate was already written, tested and reasoned about; what could not be
measured was the sentence that matters most:

    an authenticated person who is not editor of the study gets **403**.

Not for lack of a gate — for lack of a **person**. The dev realm seeded one user
(`dev`), who bootstraps as the owner of any room they touch, so the only refusal
reachable from here was the anonymous one (401), which proves the door is locked
and nothing about who holds a key. The realm now seeds a second user, `viewer`:
an ordinary authenticated identity with no membership anywhere. This script is
the arc that user makes possible.

    python dev-stack/smoke_embargo_viewer.py

What it measures, against the LIVE stack, in order:

1. two real tokens — `dev` (who will own the room) and `viewer`;
2. an asset published into a fresh room, and an **embargo declared on it in the
   room's own document** (the graph is where an embargo lives; em-server reads
   it, it does not keep a copy);
3. **403 for the viewer, with the date in the message** — the row somebody has
   to be able to act on;
4. **200 for dev**, the owner: while an embargo runs, the file is for the people
   working on the study;
5. **401 for the anonymous caller** — a different refusal, and it must stay
   different;
6. the embargo **lifted** → 200 for both, which is what makes step 3 a gate and
   not a broken endpoint.

**Requires the realm re-import.** The `viewer` user is in
`dev-stack/keycloak/realm-em-dev.json`; a Keycloak that was started before that
line existed does not have it. From `dev-stack/`:

    docker-compose -f docker-compose.dev.yml --env-file .env.dev \
        up -d --force-recreate keycloak

(`start-dev` keeps the realm in the container's own database, so recreating the
container is what re-imports. New realm keys are minted with it: tokens issued
before the recreate stop verifying, which is expected and not a fault.)

Exit code 0 = everything measured; anything skipped says why.
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
import uuid

HERE = pathlib.Path(__file__).resolve().parent

FAILURES: list[str] = []
SKIPS: list[str] = []


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_env_file(path: pathlib.Path) -> None:
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
            return answer.status, answer.read(), dict(answer.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, str(exc.reason).encode(), {}


def token_for(keycloak: str, realm: str, username: str, password: str):
    """A password-grant token, or None with the reason printed.

    The same grant `token.sh` uses — deliberately, so a failure here is a
    failure there too and there is one thing to fix.
    """
    form = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": env("DEV_CLIENT_ID", "em-server"),
        "client_secret": env("DEV_CLIENT_SECRET", "em-dev-secret"),
        "username": username,
        "password": password,
    }).encode()
    status, body, _ = request(
        f"{keycloak}/realms/{realm}/protocol/openid-connect/token",
        method="POST", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        print(f"        ({username}: {status} {body[:160].decode(errors='replace')})")
        return None
    return json.loads(body)["access_token"]


# ── the room's document, written the way a client writes it ──────────────────
#
# Over the WebSocket, with the op vocabulary the CRDT speaks. Not by editing the
# snapshot on disk: an embargo that only exists in a file nobody has opened is
# not the thing being tested — what is tested is the gate reading the LIVE room.

def send_ops(server: str, room: str, token: str, ops: list) -> tuple[int, list]:
    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError:
        return -1, []
    url = (server.replace("http://", "ws://").replace("https://", "wss://")
           + f"/v1/rooms/{urllib.parse.quote(room)}/ws?token="
           + urllib.parse.quote(token))
    applied = []
    with connect(url) as socket:
        for body in ops:
            socket.send(json.dumps({"v": 2, "type": "op", "source": "smoke",
                                    "payload": body}))
        deadline = len(ops)
        while len(applied) < deadline:
            frame = json.loads(socket.recv(timeout=10))
            if frame.get("type") == "op_result":
                applied.append(frame.get("payload") or {})
    return len(applied), applied


def now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    load_env_file(HERE / ".env.dev")
    load_env_file(HERE / ".env.dev.example")

    keycloak = f"http://localhost:{env('KEYCLOAK_PORT', '8085')}"
    server = f"http://localhost:{env('EM_SERVER_PORT', '8000')}"
    realm = env("DEV_REALM", "em-dev")
    # a room of this run's own, so the ACL bootstrap makes `dev` its owner and
    # `viewer` a stranger — which is the whole point
    room = f"embargo-smoke-{uuid.uuid4().hex[:8]}"

    print(f"em-server : {server}")
    print(f"keycloak  : {keycloak}/realms/{realm}")
    print(f"room      : {room}")
    print()

    status, body, _ = request(f"{server}/v1/health")
    if status != 200:
        print(f"em-server is not answering on {server}. Is the stack up?")
        return 2

    owner_token = token_for(keycloak, realm, env("DEV_USER", "dev"),
                            env("DEV_PASSWORD", "dev"))
    if not owner_token:
        print("could not get the dev token — is the stack up?")
        return 2
    ok("a token for `dev` (the owner-to-be)", True)

    viewer_token = token_for(keycloak, realm, "viewer", "viewer")
    if not viewer_token:
        print("\nthe `viewer` user is not in this Keycloak. The realm JSON has "
              "it; the running container was started before. From dev-stack/:\n"
              "  docker-compose -f docker-compose.dev.yml --env-file .env.dev "
              "up -d --force-recreate keycloak\n")
        return 2
    ok("a token for `viewer` (authenticated, no membership)", True)

    owner = {"Authorization": f"Bearer {owner_token}"}
    viewer = {"Authorization": f"Bearer {viewer_token}"}

    # ── the asset ────────────────────────────────────────────────────────────
    payload = b"glTF\x02\x00\x00\x00embargo-smoke-" + os.urandom(8)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    status, body, _ = request(
        f"{server}/v1/rooms/{room}/asset?media_type=model/gltf-binary",
        method="PUT", data=payload,
        headers={**owner, "Content-Type": "model/gltf-binary"})
    if status != 200:
        print(f"the upload was refused ({status}): {body[:200]!r}")
        return 2
    ok("the asset is published", json.loads(body)["ref"] == digest, digest[:23])

    # before the embargo: everybody with a token may have it. Measured FIRST, so
    # the 403 below cannot be a permission problem wearing an embargo's clothes.
    status, _, _ = request(f"{server}/v1/rooms/{room}/asset/{digest}",
                           headers=viewer)
    ok("before the embargo, the viewer may have it", status == 200, str(status))

    # ── the embargo, declared in the room's document ─────────────────────────
    until = "2099-12-31"
    ops = [
        {"op": "add_node", "ts": now_iso(), "author": "smoke",
         "node": {"id": "res_embargo_smoke", "node_type": "resource",
                  "name": "embargoed.glb", "description": "",
                  "data": {"checksum": digest, "residency": "resident",
                           "url_type": "3d_model"}}},
        {"op": "add_node", "ts": now_iso(), "author": "smoke",
         "node": {"id": "emb_smoke", "node_type": "embargo", "name": until,
                  "description": "",
                  "data": {"embargo_end": until, "reason": "smoke"}}},
        {"op": "add_edge", "ts": now_iso(), "author": "smoke",
         "id": "res_embargo_smoke__has_embargo__emb_smoke",
         "edge_type": "has_embargo", "source": "res_embargo_smoke",
         "target": "emb_smoke"},
    ]
    count, results = send_ops(server, room, owner_token, ops)
    if count < 0:
        skip("the embargo is declared in the room",
             "the `websockets` package is not importable here "
             "(pip install websockets), so the document could not be written")
        print("\nnothing else can be measured without the embargo.")
        return 1 if FAILURES else 0
    ok("the embargo is declared in the room's document",
       all(r.get("applied") for r in results),
       f"{count} op(s), until {until}")

    # ── the three answers ────────────────────────────────────────────────────
    status, body, _ = request(f"{server}/v1/rooms/{room}/asset/{digest}",
                              headers=viewer)
    detail = ""
    try:
        detail = json.loads(body).get("detail", "")
    except Exception:  # noqa: BLE001 — a body that will not parse is still a body
        detail = body[:120].decode(errors="replace")
    ok("VIEWER → 403", status == 403, f"{status} · {detail[:90]}")
    ok("…and the refusal carries the date", until in str(detail), str(detail)[:90])

    status, got, _ = request(f"{server}/v1/rooms/{room}/asset/{digest}",
                             headers=owner)
    ok("OWNER (editor+) → 200", status == 200, str(status))
    ok("…and the bytes are the ones published", got == payload,
       f"{len(got)} B")

    status, _, _ = request(f"{server}/v1/rooms/{room}/asset/{digest}")
    ok("ANONYMOUS → 401 (a different refusal, and it stays different)",
       status == 401, str(status))

    # ── lifted ───────────────────────────────────────────────────────────────
    count, results = send_ops(server, room, owner_token, [
        {"op": "remove_node", "ts": now_iso(), "author": "smoke",
         "id": "emb_smoke"}])
    ok("the embargo is lifted", bool(results and results[0].get("applied")))
    status, _, _ = request(f"{server}/v1/rooms/{room}/asset/{digest}",
                           headers=viewer)
    ok("VIEWER → 200 once it is over", status == 200, str(status))

    print()
    if SKIPS:
        print(f"{len(SKIPS)} skipped:")
        for line in SKIPS:
            print(f"  · {line}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for line in FAILURES:
            print(f"  · {line}")
        return 1
    print("the gate refuses the person, not only the stranger.")
    print(f"(this run left the room `{room}` behind — a fresh one each time, so "
          f"the ACL bootstrap has an owner to make)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
