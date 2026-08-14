"""The relay: `/v1/rooms/{id}/ws` — em-server as "just another host".

It speaks **the wire that already exists** (ADR-002, the `v:1` messages EMStudio
and EMtools use: `snapshot`, `op`, `host_info`, `select`, `command`). That is the
whole trick of this step: when EMStudio points at an em-server in P4.3 it will
not need a new protocol, because the relay is a host that happens to have several
clients instead of one.

What the relay does with an operation is **apply it through s3Dgraphy and pass it
on**. It does not transform, order or reconcile anything: the CRDT of P4.1
converges whatever the order, which is precisely why a *relay* is enough and an
operational-transform server is not needed. If this file ever grows a merge rule,
the rule is in the wrong repository.

Authentication: the connection carries a token, verified with the same
`app/auth.py` the HTTP routes use. The **author of every operation is the token's
identity**, never what the client wrote in the message — an author a client can
declare is an author anybody can borrow, and P4.1b made the stamp the thing the
merge trusts.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .auth import authenticator
from .rooms import RoomRegistry, now_iso
from .store import store_from_env

#: This process's rooms and the store behind them. Built at import so a
#: misconfigured store fails when the process starts, not at the first join.
SNAPSHOT_STORE = store_from_env()
ROOMS = RoomRegistry(SNAPSHOT_STORE)

#: The wire version. Same number as the bridge's, because it is the same wire.
WIRE = 1

#: What this host calls itself in `host_info` — a client shows it in its footer.
HOST_TOOL = "em-server (relay)"

ws_router = APIRouter(prefix="/v1")


def _identity(claims: Dict[str, Any]) -> Optional[str]:
    """The author to stamp operations with, out of the token's claims.

    ORCID first, because in this ecosystem the ORCID iD **is** the identity
    (AUDIT1/ORCID batch); then the realm's preferred username, then the subject.
    In dev mode there is no token and therefore no author — and the honest
    answer is None, which the stamp treats as "unknown" rather than inventing
    somebody.
    """
    if claims.get("em_dev_mode"):
        return None
    for key in ("orcid", "ORCID", "preferred_username", "sub"):
        value = claims.get(key)
        if value:
            return str(value)
    return None


async def _authenticate(websocket: WebSocket, token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verify the handshake, or close the socket with a reason.

    The token may arrive in `Authorization` (a native client can set headers) or
    as `?token=` (a browser cannot). Both are accepted deliberately: refusing the
    query parameter would mean no browser could ever connect, and inventing a
    second auth mechanism for browsers would be worse than a URL a TLS
    connection already protects.
    """
    if not authenticator.settings.enforcing:
        # dev / no-auth: the same door the HTTP routes leave open on a laptop,
        # and `/v1/health` says so out loud rather than letting anyone assume.
        return {"sub": "anonymous", "em_dev_mode": True}
    header = websocket.headers.get("authorization") or ""
    scheme, _, from_header = header.partition(" ")
    raw = from_header.strip() if scheme.lower() == "bearer" else (token or "")
    if not raw:
        await websocket.close(code=4401, reason="missing bearer token")
        return None
    try:
        return authenticator.verify(raw)
    except Exception as exc:  # HTTPException from the verifier, or anything else
        detail = getattr(exc, "detail", None) or str(exc)
        await websocket.close(code=4401, reason=f"token refused: {detail}"[:120])
        return None


@ws_router.websocket("/rooms/{room_id}/ws")
async def room_socket(websocket: WebSocket, room_id: str,
                      token: Optional[str] = Query(default=None),
                      since: Optional[str] = Query(default=None)) -> None:
    """Join a room, receive its snapshot, then live on the op stream."""
    await websocket.accept()
    claims = await _authenticate(websocket, token)
    if claims is None:
        return
    author = _identity(claims)

    room = await ROOMS.get(room_id)
    connection_id = uuid.uuid4().hex[:12]
    member = room.join(connection_id, websocket, author,
                       display=str(claims.get("name") or author or "anon"))

    # ── the join: who you are, what the room is, what you missed ─────────────
    await _send(websocket, {"v": WIRE, "type": "host_info", "source": "em-server",
                            "tool": HOST_TOOL, "file": room_id,
                            "room": room_id, "connection_id": connection_id,
                            "author": author,
                            # P4.3 · the compaction point this room has passed.
                            # A client whose own base is OLDER than this cannot
                            # safely replay its history — what it would re-assert
                            # has already been settled and forgotten here — so it
                            # is told the number and decides to re-sync instead.
                            # Announcing it is the difference between a gap that
                            # is handled and one that is discovered.
                            "gc_watermark": room.compacted_upto,
                            "accepts_commands": False})
    await _send(websocket, {"v": WIRE, "type": "snapshot", "source": "em-server",
                            "doc": room.document,
                            "gc_watermark": room.compacted_upto,
                            "host": {"tool": HOST_TOOL, "file": room_id}})
    # presence closes the JOIN — three frames, always the same three, so a client
    # knows when it has arrived without counting
    await _broadcast_presence(room)
    # …and only then the replay: what a late arrival missed comes as the stream
    # it would have received had it been here, not as part of the handshake
    for op in room.replay_since(since):
        # wrapped like any other op frame: what a client missed must arrive in
        # the SAME shape it would have had live, or a replay needs its own reader
        await _send(websocket, {"v": WIRE, "type": "op", "source": "em-server", **op})
    member.watermark = room.last_op_at or now_iso()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            try:
                await _handle(room, member, websocket, message, author)
            except Exception as exc:      # noqa: BLE001
                # A relay that dies on one bad message takes the room's other
                # clients down with it. The connection survives and the sender is
                # told — a silent drop would look exactly like a network problem.
                await _send(websocket, {"v": WIRE, "type": "error",
                                        "source": "em-server",
                                        "detail": f"{type(exc).__name__}: {exc}"})
    except WebSocketDisconnect:
        pass
    finally:
        room.leave(connection_id)
        # presence is ephemeral: leaving removes it, and nothing is written down
        await _broadcast_presence(room)


async def _handle(room, member, websocket: WebSocket, message: Dict[str, Any],
                  author: Optional[str]) -> None:
    kind = str(message.get("type") or "")

    if kind == "op":
        # THE AUTHOR IS THE TOKEN'S, always. A client that names somebody else is
        # not lying to the relay, it is lying to everyone downstream — the stamp
        # is what the merge trusts (P4.1b), so it cannot be self-declared.
        op = {k: v for k, v in message.items()
              if k not in ("type", "v", "source", "author", "graph_id")}
        if author:
            op["author"] = author
        op.setdefault("ts", now_iso())
        async with room.lock:
            result = room.apply(op, message.get("graph_id"))
            if not result.get("applied"):
                # stale / idempotent / refused: it is NOT news, and re-broadcasting
                # it would hand the other clients a regression to re-apply
                await _send(websocket, {"v": WIRE, "type": "op_result",
                                        "source": "em-server",
                                        "applied": False,
                                        "reason": result.get("reason", ""),
                                        "op": op})
                return
            room.record(op)
            outbound = {"v": WIRE, "type": "op", "source": "em-server", **op}
            if message.get("graph_id"):
                outbound["graph_id"] = message["graph_id"]
        await _fanout(room, outbound, skip=member.connection_id)
        await _send(websocket, {"v": WIRE, "type": "op_result", "source": "em-server",
                                "applied": True, "reason": result.get("reason", ""),
                                "op": op})
        return

    if kind == "select":
        # awareness, soft and never a lock (design P4 §6)
        ids = message.get("node_ids") or ([message["node_id"]]
                                          if message.get("node_id") else [])
        member.selection = [str(i) for i in ids]
        await _fanout(room, {"v": WIRE, "type": "select", "source": "em-server",
                             "connection_id": member.connection_id,
                             "author": author,
                             "node_id": message.get("node_id"),
                             "node_ids": member.selection},
                      skip=member.connection_id)
        # NO presence broadcast here: the `select` frame IS the awareness
        # message, and sending the roster after every click would be noise the
        # clients have to filter. The selection travels with the next presence.
        return

    if kind == "request_snapshot":
        await _send(websocket, {"v": WIRE, "type": "snapshot", "source": "em-server",
                                "doc": room.document,
                                "gc_watermark": room.compacted_upto,
                                "host": {"tool": HOST_TOOL, "file": room.room_id}})
        return

    if kind == "request_save":
        # the client asks the host to persist: for a relay that IS the snapshot
        async with room.lock:
            info = room.snapshot(SNAPSHOT_STORE)
        await _fanout(room, {"v": WIRE, "type": "snapshot_written",
                             "source": "em-server", **info})
        return

    if kind == "ack":
        # "I have applied everything up to here" — the watermark that makes
        # compaction safe. A client that never acks simply holds the GC back,
        # which is the failure direction we want.
        member.watermark = str(message.get("ts") or member.watermark or now_iso())
        return


async def _send(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:      # a socket that died mid-write is a disconnect
        pass


async def _fanout(room, payload: Dict[str, Any], *, skip: Optional[str] = None) -> None:
    """Send to everybody but the origin — the echo suppression the bridge already
    does, for the same reason: a client must not have to recognise its own work
    coming back."""
    for connection_id, socket in list(room.sockets.items()):
        if connection_id == skip:
            continue
        await _send(socket, payload)
        member = room.members.get(connection_id)
        if member is not None and payload.get("type") == "op":
            member.watermark = str(payload.get("ts") or member.watermark or "")


async def _broadcast_presence(room) -> None:
    payload = {"v": WIRE, "type": "presence", "source": "em-server",
               "room": room.room_id, "members": room.presence()}
    for socket in list(room.sockets.values()):
        await _send(socket, payload)
