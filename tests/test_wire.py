"""WIRE 2 · the envelope, and the op-matrix that would have caught the bug.

Two claims, and the second one is the guardrail.

**The envelope and the body are separate namespaces.** Until WIRE 2 a message
was one flat object, so the wire's `source` ("who sent this") and an edge's
`source` ("where it starts") were the same key. The relay stripped one and the
other disappeared with it. Nesting the body under `payload` makes the collision
impossible rather than handled — and these tests say so in the form that would
have failed before: an `add_edge` through the relay, endpoints intact.

**Every verb, end to end.** The crack was not only in the relay: it was that the
convergence tests exercised `add_node` and `update_field` and never an
`add_edge` WITH its endpoints. So this file sends every verb through two real
sockets and compares the received op field by field. A new verb that does not
pass this is not finished.
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
from app.wire import WIRE, WireError, envelope, read  # noqa: E402

T1 = "2026-08-14T10:00:00Z"
T2 = "2026-08-14T11:00:00Z"


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fresh_rooms():
    """A registry of its own per test — rooms are process state."""
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
        "graphs": {room_id: {"graph_id": room_id, "name": room_id,
                             "nodes": [{"id": "US1", "node_type": "US", "name": "US1",
                                        "data": {"created_at": T1}},
                                       {"id": "US2", "node_type": "US", "name": "US2",
                                        "data": {"created_at": T1}}],
                             # an edge to remove: `remove_edge` needs something
                             # that exists, and seeding it keeps the matrix
                             # entries independent of each other's order
                             "edges": [{"id": "e-seed", "source": "US1",
                                        "target": "US2",
                                        "edge_type": "is_after"}]}},
        "active_graph_id": room_id,
    })


def _drain_join(socket):
    for _ in range(3):
        socket.receive_json()


# ── 1 · the envelope itself ─────────────────────────────────────────────────

def test_1_the_envelope_carries_only_the_wire_s_own_words():
    message = envelope("op", {"op": "add_edge", "source": "reg-1",
                              "target": "US1"}, source="emstudio")
    assert set(message) == {"v", "type", "source", "payload"}
    assert message["v"] == WIRE
    assert message["source"] == "emstudio", "the ENVELOPE's source: who sent it"
    assert message["payload"]["source"] == "reg-1", \
        "the PAYLOAD's source: where the edge starts. Two words, two namespaces"


def test_1b_routing_stays_outside_the_payload():
    """`graph_id` is read BY the relay, so it is the wire's word, not the body's."""
    message = envelope("op", {"op": "add_node"}, source="x", graph_id="scavo")
    assert message["graph_id"] == "scavo"
    assert "graph_id" not in message["payload"]


def test_1c_a_message_of_another_version_is_refused_by_name():
    with pytest.raises(WireError) as exc:
        read({"v": 1, "type": "op", "op": "add_edge", "source": "reg-1"})
    assert "v1" in str(exc.value) and "payload" in str(exc.value)


def test_1d_a_message_without_a_payload_reads_as_an_empty_one():
    """`request_snapshot` has no body, and asking for one would be ceremony."""
    kind, payload = read({"v": WIRE, "type": "request_snapshot", "source": "x"})
    assert (kind, payload) == ("request_snapshot", {})


def test_1e_the_relay_no_longer_needs_a_per_verb_exception():
    """The symptom fix is gone because the cause is.

    The previous shape kept `source` for `add_edge`/`remove_edge` and stripped it
    everywhere else — a list of verbs to remember, in the transport, about a
    vocabulary that is not its business.
    """
    source = (_REPO / "app" / "ws.py").read_text(encoding="utf-8")
    assert '"add_edge", "remove_edge"' not in source
    assert "payload" in source, "the relay reads the body from the envelope"


def test_1f_an_old_speaker_is_told_and_the_room_survives(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json({"v": 1, "type": "op", "op": "add_node",
                     "node": {"id": "X", "node_type": "US", "name": "X"}})
        answer = a.receive_json()
        assert answer["type"] == "error"
        assert "v2" in answer["payload"]["detail"] or "v1" in answer["payload"]["detail"]
        # …and the connection is still usable: one bad frame is not a disconnect
        a.send_json(envelope("request_snapshot", source="test"))
        assert a.receive_json()["type"] == "snapshot"


# ── 2 · THE OP MATRIX — every verb, through two real sockets ────────────────

#: One entry per CRDT verb. The payload is what a client sends; `check` names the
#: fields whose survival is the point of the entry.
OP_MATRIX = [
    pytest.param(
        {"op": "add_node", "ts": T2,
         "node": {"id": "US9", "node_type": "US", "name": "US9"}},
        ["op", "ts", "node"], id="add_node"),
    pytest.param(
        {"op": "update_field", "node_id": "US1", "field": "description",
         "value": "muro in opus", "ts": T2},
        ["op", "node_id", "field", "value", "ts"], id="update_field"),
    pytest.param(
        {"op": "update_field", "node_id": "US1", "field": "data.dating",
         "remove": True, "ts": T2},
        ["op", "node_id", "field", "remove", "ts"], id="update_field-remove"),
    pytest.param(
        {"op": "remove_node", "id": "US2", "ts": T2},
        ["op", "id", "ts"], id="remove_node"),
    pytest.param(
        # THE ONE. `source` and `target` are the edge's ends, and they have the
        # same spelling as the envelope's origin tag.
        {"op": "add_edge", "id": "e-1", "source": "US1", "target": "US2",
         "edge_type": "is_before", "ts": T2},
        ["op", "id", "source", "target", "edge_type", "ts"], id="add_edge"),
    pytest.param(
        {"op": "remove_edge", "id": "e-seed", "source": "US1", "target": "US2",
         "edge_type": "is_after", "ts": T2},
        ["op", "id", "source", "target", "edge_type", "ts"], id="remove_edge"),
]


@pytest.mark.parametrize("body,fields", OP_MATRIX)
def test_2_every_verb_arrives_field_for_field(client, fresh_rooms, body, fields):
    """A sends one op; B receives exactly it.

    Not "an op arrives" — the fields, compared one by one. `add_edge` is the
    reason this test exists, and the parametrisation is what stops the next verb
    from being added without the same guarantee.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()                       # A is told B joined (presence)

        a.send_json(envelope("op", body, source="emstudio"))
        acknowledgement = a.receive_json()
        assert acknowledgement["type"] == "op_result"
        assert acknowledgement["payload"]["applied"] is True, \
            f"{body['op']} was refused: {acknowledgement['payload'].get('reason')}"

        received = b.receive_json()
        assert received["type"] == "op"
        got = received["payload"]
        for field in fields:
            assert got[field] == body[field], \
                f"{body['op']}: `{field}` did not survive the relay"


def test_2b_the_regression_the_old_wire_would_have_failed(client, fresh_rooms):
    """The bug, stated as a test.

    Before WIRE 2 the relay stripped the top-level `source` from every op — the
    envelope's origin tag — and an `add_edge` written flat lost its beginning
    with it. The edge applied, was broadcast, and only surfaced later as a load
    warning about an edge whose ends do not exist. Here the edge is checked
    where it ends up: in the room's document.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json(envelope("op", {"op": "add_edge", "id": "e-2",
                                    "source": "US1", "target": "US2",
                                    "edge_type": "is_before", "ts": T2},
                             source="emstudio"))
        assert a.receive_json()["payload"]["applied"] is True

    room = fresh_rooms.peek("scavo")
    section = room.document["graphs"]["scavo"]
    edge = next(e for e in section["edges"] if e["id"] == "e-2")
    assert (edge["source"], edge["target"]) == ("US1", "US2")
    assert edge["source"] != "emstudio", \
        "the envelope's origin tag must never end up as an edge endpoint"


def test_2c_the_author_is_still_the_token_s(client, fresh_rooms):
    """Nesting did not weaken the rule it sits next to: whatever a client writes
    in `author`, the identity the relay stamps is the token's — and in dev mode,
    where there is no token identity, the claim is dropped rather than believed.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()
        a.send_json(envelope("op", {"op": "update_field", "node_id": "US1",
                                    "field": "description", "value": "x",
                                    "ts": T2, "author": "somebody-else"},
                             source="emstudio"))
        assert a.receive_json()["payload"]["applied"] is True
        got = b.receive_json()["payload"]
        assert got.get("author") != "somebody-else"
