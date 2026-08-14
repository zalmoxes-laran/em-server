"""The re-sync, end to end — the branch that was built but never staged.

The claim P4.2/P4.3 made and could not demonstrate: a client whose base is older
than the room's compaction point must NOT replay its history — what it would
re-assert has already been settled and forgotten — and its unconfirmed work must
come back **re-stamped**, including the **emptyings**, so nothing it did is lost
and nothing it deleted comes back.

Staging it needs one thing the UI does not guide, and that is the whole reason
this file exists: `gc_watermark` is the **minimum over CONNECTED members**, so a
member who is present can never fall behind it. Somebody has to be **away while
the room compacts**. Here A leaves, B advances the room, the room compacts past
A's base, and A comes back — which is exactly the sequence a person could only
produce by accident.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app import ws as ws_module                       # noqa: E402
from app.main import app                              # noqa: E402
from app.rooms import RoomRegistry                    # noqa: E402
from app.store import InMemorySnapshotStore           # noqa: E402
from app.wire import envelope                         # noqa: E402

T0 = "2026-08-14T09:00:00Z"      # the seed
T1 = "2026-08-14T10:00:00Z"      # A's last confirmed op — its base
T2 = "2026-08-14T11:00:00Z"      # B moves the room on; the room compacts here
T3 = "2026-08-14T12:00:00Z"      # A comes back and re-sends, re-stamped


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fresh_rooms():
    previous = ws_module.ROOMS
    registry = RoomRegistry(InMemorySnapshotStore())
    ws_module.ROOMS = registry
    try:
        yield registry
    finally:
        ws_module.ROOMS = previous


def _seed(registry, room_id="scavo"):
    registry.store.put(room_id, {
        "header": {"format": "em.json", "version": "1.0"},
        "graphs": {room_id: {
            "graph_id": room_id, "name": room_id,
            "nodes": [{"id": "US1", "node_type": "US", "name": "US1",
                       "description": "prima lettura",
                       "data": {"created_at": T0, "created_by": "anna",
                                "dating": "II sec."}}],
            "edges": []}},
        "active_graph_id": room_id,
    })


def _join(socket):
    """The three arrival frames; returns the `host_info` payload."""
    host = socket.receive_json()
    socket.receive_json()          # snapshot
    socket.receive_json()          # presence
    return host["payload"]


def _op(body):
    return envelope("op", body, source="test")


def _node(registry, room_id="scavo", node_id="US1"):
    room = registry.peek(room_id)
    section = room.document["graphs"][room_id]
    return next(n for n in section["nodes"] if n["id"] == node_id)


def _plan_rejoin(base, watermark):
    """The rule both clients implement (`hub.ts`, `room_session.py`).

    Restated here because this harness is the only place all three sides meet:
    if the two client implementations and this one ever disagree, one of them is
    wrong and the disagreement should be visible in a test rather than in
    somebody's document.
    """
    if not base:
        return "resync"
    if not watermark:
        return "resume"
    return "resume" if str(base) >= str(watermark) else "resync"


# ── the sequence ────────────────────────────────────────────────────────────

def test_a_client_that_was_away_while_the_room_compacted_resyncs(client, fresh_rooms):
    """The whole arc, in one test, because it is one story."""
    _seed(fresh_rooms)

    # ── A and B are in the room; A does some work and stops there ───────────
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _join(a)
        _join(b)
        a.receive_json()                       # A is told B joined

        a.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "description", "value": "muro in opus",
                         "ts": T1}))
        assert a.receive_json()["payload"]["applied"] is True
        assert b.receive_json()["type"] == "op"
        b.send_json(envelope("ack", {"ts": T1}, source="test"))
        # A's base is now T1: the instant of the last operation it saw applied.

    # ── A is AWAY. B moves the room on and the room compacts past T1 ────────
    with client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _join(b)
        b.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "description", "value": "muro in opus mixtum",
                         "ts": T2}))
        assert b.receive_json()["payload"]["applied"] is True
        # B says how far it has applied; it is the only member, so the
        # watermark IS T2 — and this is the step the UI never guides.
        b.send_json(envelope("ack", {"ts": T2}, source="test"))
        b.send_json(envelope("request_save", source="test"))
        written = b.receive_json()
        assert written["type"] == "snapshot_written"

    room = fresh_rooms.peek("scavo")
    assert room.compacted_upto == T2, \
        "the room compacted up to the watermark of the only member present"
    assert _plan_rejoin(T1, room.compacted_upto) == "resync", \
        "A's base is older than the compaction point: replaying it could " \
        "resurrect what the room has already settled"

    # ── A comes back, is TOLD the watermark, and re-sends re-stamped ────────
    with client.websocket_connect("/v1/rooms/scavo/ws?since=" + T1) as a:
        host = _join(a)
        assert host["gc_watermark"] == T2, \
            "the room announces the number A needs to decide — announcing it " \
            "is the difference between a gap that is handled and one that is " \
            "discovered"
        # A's unconfirmed work, re-stamped NOW (T3): a VALUE and an EMPTYING.
        a.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "data.nota", "value": "rivedere in situ",
                         "ts": T3}))
        assert a.receive_json()["payload"]["applied"] is True
        a.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "data.dating", "remove": True, "ts": T3}))
        assert a.receive_json()["payload"]["applied"] is True

    node = _node(fresh_rooms)
    assert node["description"] == "muro in opus mixtum", \
        "the room's own newer value stands: A did not replay over it"
    assert node["data"]["nota"] == "rivedere in situ", \
        "…and A's unconfirmed VALUE survived, re-stamped"
    assert node["data"].get("dating") in (None, ""), \
        "…and A's EMPTYING survived too: the field is empty"
    clocks = node["data"]["field_clocks"]
    assert clocks["data.dating"]["removed"] is True, \
        "…as a TOMBSTONE, not a missing key — which is what stops the older " \
        "value from winning the next merge"
    assert clocks["data.dating"]["ts"] == T3


def test_a_replay_that_is_older_than_the_compaction_point_is_refused(client,
                                                                     fresh_rooms):
    """The other half of the rule: if A had replayed instead of re-sending, the
    room would have said no.

    This is why the client re-stamps rather than replays — and why the refusal
    is not an error but a fact the CRDT reports.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _join(b)
        b.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "description", "value": "muro in opus mixtum",
                         "ts": T2}))
        assert b.receive_json()["payload"]["applied"] is True
        b.send_json(envelope("ack", {"ts": T2}, source="test"))
        b.send_json(envelope("request_save", source="test"))
        b.receive_json()

    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _join(a)
        # the stale replay A must NOT send
        a.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "description", "value": "prima lettura",
                         "ts": T1}))
        answer = a.receive_json()["payload"]
        assert answer["applied"] is False
        assert "stale" in answer["reason"] or "not" in answer["reason"], answer

    assert _node(fresh_rooms)["description"] == "muro in opus mixtum"


def test_the_room_never_replays_what_it_has_compacted(client, fresh_rooms):
    """A late arrival asking `since=T1` gets no stale stream.

    The op-log was truncated at the compaction point, so there is nothing older
    to hand out — the snapshot IS the answer. What must not happen is a replay
    of operations the room can no longer reconcile.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _join(b)
        b.send_json(_op({"op": "update_field", "node_id": "US1",
                         "field": "description", "value": "muro", "ts": T2}))
        b.receive_json()
        b.send_json(envelope("ack", {"ts": T2}, source="test"))
        b.send_json(envelope("request_save", source="test"))
        b.receive_json()

    room = fresh_rooms.peek("scavo")
    assert room.oplog == [], "the log was truncated at the compaction point"

    with client.websocket_connect("/v1/rooms/scavo/ws?since=" + T1) as a:
        host = a.receive_json()
        snapshot = a.receive_json()
        presence = a.receive_json()
        assert (host["type"], snapshot["type"], presence["type"]) == \
            ("host_info", "snapshot", "presence")
        # …and nothing else: no replayed op follows the three arrival frames
        a.send_json(envelope("request_snapshot", source="test"))
        assert a.receive_json()["type"] == "snapshot", \
            "the next frame is the answer to what we JUST asked — so nothing " \
            "was queued behind the join"
