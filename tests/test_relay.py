"""P4.2 — the relay, proved with two WebSocket clients and no browser.

The eight claims the design asks for before anybody builds a client on top:

  1  FAN-OUT        an op from A reaches B, and does not echo back to A
  2  CONVERGENCE    concurrent ops → both copies land on the SAME canonical digest,
                    and it is the same answer the offline merge gives (same code)
  3  LATE-JOIN      a third client gets the snapshot + what it missed, and converges
  4  SNAPSHOT + GC  a forced snapshot is written to the store, the GC compacted it
                    WITHOUT changing the observable state, and the op-log is cut
  5  AUTH           no token → refused; with a token → the op's author is the
                    TOKEN's identity, not the one the client wrote
  6  PRESENCE       A joins → B sees it; A leaves → it is gone (ephemeral)
  7  THE RULES      the relay contains no convergence/GC logic (it calls the
                    library), and the durable truth is not the process's disk
  8  s3Dgraphy      its own suite is unchanged (measured there, not here)
  9  TOMBSTONES     the snapshot KEEPS the deletions (a client that joins without
                    them cannot converge, only resurrect) — the KEEP half of the
                    per-surface policy in `s3dgraphy.dissemination`

The three fences of P4.2 are asserted, not assumed: truth outside the process
(4, 7), convergence in the library (2, 7), presence ephemeral (6).

**One thing about the harness, because it looks like a bug and is not.**
Starlette's `TestClient` drives the app from the calling thread: the server task
that produced a frame only advances while the test is talking to *that* socket.
So after A sends an operation the test reads **A's `op_result` first** — which is
what lets A's handler finish, fan-out included — and only then B's copy. Against
a real uvicorn there is no such ordering; here, reading B first blocks forever on
a frame the server has not been given a chance to write.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

_REPO = pathlib.Path(__file__).resolve().parent.parent

from app import ws as ws_module                      # noqa: E402
from app.main import app                             # noqa: E402
from app.rooms import RoomRegistry                   # noqa: E402
from app.store import DirectorySnapshotStore, InMemorySnapshotStore  # noqa: E402
from app.wire import WIRE                             # noqa: E402

from s3dgraphy import api as em                      # noqa: E402

ANNA = "0000-0002-1825-0097"
BRUNO = "0000-0001-5109-3700"
T1 = "2026-08-13T10:00:00Z"
T2 = "2026-08-13T11:00:00Z"
T3 = "2026-08-13T12:00:00Z"


@pytest.fixture(autouse=True)
def fresh_rooms(monkeypatch):
    """A clean relay per test: a store nobody else wrote and no live rooms."""
    store = InMemorySnapshotStore()
    registry = RoomRegistry(store)
    monkeypatch.setattr(ws_module, "SNAPSHOT_STORE", store)
    monkeypatch.setattr(ws_module, "ROOMS", registry)
    return registry


@pytest.fixture
def client():
    return TestClient(app)


def _drain_join(socket):
    """The three frames every client gets on joining: host_info, snapshot,
    presence. Returns the snapshot document."""
    host = socket.receive_json()
    assert host["type"] == "host_info"
    snapshot = socket.receive_json()
    assert snapshot["type"] == "snapshot"
    presence = socket.receive_json()
    assert presence["type"] == "presence"
    # WIRE 2 · every message is `{v, type, source, payload}` — the body is
    # nested, so no field of it can collide with a word of the envelope
    return snapshot["payload"]["doc"]


def _op(field, value, ts, node_id="US1", **extra):
    """One `op` message, WIRE 2: envelope outside, the operation inside."""
    return {"v": WIRE, "type": "op", "source": "test",
            "payload": {"op": "update_field", "node_id": node_id,
                        "field": field, "value": value, "ts": ts, **extra}}


def _seed(registry, room_id="scavo"):
    """A room with one node, written straight into the working copy.

    Through the STORE, not through the socket: this is the fixture, and a test
    that had to send operations to build its own preconditions would be testing
    the setup instead of the claim.
    """
    registry.store.put(room_id, {
        "header": {"format": "em.json", "version": "1.0"},
        "graphs": {room_id: {"graph_id": room_id, "name": room_id,
                             "nodes": [{"id": "US1", "node_type": "US", "name": "US1",
                                        "data": {"created_at": T1, "created_by": ANNA}}],
                             "edges": []}},
        "active_graph_id": room_id,
    })


def _digest(document):
    return em.content_digest(document)


# ── 1 · fan-out ─────────────────────────────────────────────────────────────

def test_1_an_op_reaches_the_others_and_does_not_echo(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()          # A is told that B joined (presence)

        a.send_json(_op("description", "muro in opus", T2))
        # A gets its own ACK — and NOT the operation back
        ack = a.receive_json()
        assert ack["type"] == "op_result" and ack["payload"]["applied"] is True
        got = b.receive_json()
        assert got["type"] == "op" and got["payload"]["value"] == "muro in opus"
        assert got["source"] == "em-server"


def test_1b_a_stale_op_is_not_propagated_as_news(client, fresh_rooms):
    """The CRDT says the op is a regression; the relay must not hand the others
    a regression to re-apply. It answers the sender instead."""
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()

        a.send_json(_op("description", "recente", T3))
        assert a.receive_json()["payload"]["applied"] is True
        assert b.receive_json()["payload"]["value"] == "recente"      # B receives the fresh one

        a.send_json(_op("description", "vecchia", T1))
        result = a.receive_json()
        assert result["payload"]["applied"] is False and result["payload"]["reason"] == "stale"
        # …and B was told NOTHING about it: the next thing it hears is a NEW op
        a.send_json(_op("data.nota", "seguito", T3))
        assert a.receive_json()["payload"]["applied"] is True
        assert b.receive_json()["payload"]["field"] == "data.nota"


# ── 2 · convergenza ─────────────────────────────────────────────────────────

def test_2_concurrent_ops_converge_and_agree_with_the_offline_merge(client, fresh_rooms):
    """Two clients write different fields of the same node at once.

    The relay's copy must land where the OFFLINE merge lands — and it does,
    because it is the same library code. That equality is the whole reason a
    relay is enough: nothing here reconciles anything.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()

        a.send_json(_op("description", "muro in opus", T2))
        assert a.receive_json()["payload"]["applied"] is True
        assert b.receive_json()["type"] == "op"
        b.send_json(_op("data.dating", "II sec. d.C.", T3))
        assert b.receive_json()["payload"]["applied"] is True
        assert a.receive_json()["type"] == "op"

        room = fresh_rooms.peek("scavo")
        node = room.document["graphs"]["scavo"]["nodes"][0]
        assert node["description"] == "muro in opus"      # both kept: P4.1b
        assert node["data"]["dating"] == "II sec. d.C."

        # the same two operations applied offline, in the other order
        offline = fresh_rooms.store.get("scavo")
        section = offline["graphs"]["scavo"]
        em.apply_op(section, {"op": "update_field", "node_id": "US1",
                              "field": "data.dating", "value": "II sec. d.C.",
                              "ts": T3})
        em.apply_op(section, {"op": "update_field", "node_id": "US1",
                              "field": "description", "value": "muro in opus",
                              "ts": T2})
        assert _digest(offline) == _digest(room.document), \
            "the live relay and the offline merge must land on the same state"


# ── 3 · late join ───────────────────────────────────────────────────────────

def test_3_a_late_client_gets_the_snapshot_and_what_it_missed(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json(_op("description", "muro in opus", T2))
        assert a.receive_json()["payload"]["applied"] is True

        with client.websocket_connect("/v1/rooms/scavo/ws") as late:
            doc = _drain_join(late)
            node = doc["graphs"]["scavo"]["nodes"][0]
            assert node["description"] == "muro in opus", \
                "the snapshot a late client receives is the CURRENT state"
            assert _digest(doc) == _digest(fresh_rooms.peek("scavo").document)


def test_3b_a_client_that_says_where_it_stopped_gets_the_replay(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json(_op("description", "prima", T2))
        a.receive_json()
        a.send_json(_op("data.nota", "poi", T3))
        a.receive_json()

        with client.websocket_connect(f"/v1/rooms/scavo/ws?since={T2}") as late:
            _drain_join(late)
            replayed = late.receive_json()
            assert replayed["type"] == "op" and replayed["payload"]["ts"] == T3, \
                "only what happened after the point the client names"


# ── 4 · snapshot + GC ───────────────────────────────────────────────────────

def test_4_a_snapshot_writes_compacts_and_truncates(client, fresh_rooms):
    """The GC runs in s3Dgraphy, on a watermark this relay can justify.

    Measured on both sides of the compaction: the bookkeeping shrinks and the
    observable state does not move.
    """
    room_id = "gc"
    fresh_rooms.store.put(room_id, {
        "header": {"format": "em.json", "version": "1.0"},
        "graphs": {room_id: {"graph_id": room_id, "nodes": [
            # a deletion nobody can still contradict…
            {"id": "morto", "node_type": "US", "name": "morto",
             "data": {"created_at": T1, "removed": {"ts": T1, "by": ANNA}}},
            # …a live node carrying an old field clock…
            {"id": "US1", "node_type": "US", "name": "US1", "description": "viva",
             "data": {"created_at": T1,
                      "field_clocks": {"description": {"ts": T1, "by": ANNA}}}},
        ], "edges": []}, },
        "active_graph_id": room_id,
    })
    with client.websocket_connect(f"/v1/rooms/{room_id}/ws") as a:
        _drain_join(a)
        # an op moves the watermark forward: the client has now been brought past T2
        a.send_json(_op("description", "aggiornata", T2))
        assert a.receive_json()["payload"]["applied"] is True

        room = fresh_rooms.peek(room_id)
        before_stats = em.crdt_stats(room.document)
        before_live = len(em.live_nodes(room.document["graphs"][room_id]))
        before_values = [n.get("description") for n in
                         em.live_nodes(room.document["graphs"][room_id])]
        assert before_stats["node_tombstones"] == 1

        a.send_json({"v": WIRE, "type": "request_save", "payload": {}})
        written = a.receive_json()
        assert written["type"] == "snapshot_written"

        after_stats = em.crdt_stats(room.document)
        after_live = em.live_nodes(room.document["graphs"][room_id])
        assert after_stats["node_tombstones"] == 0, "the settled deletion is gone"
        assert written["payload"]["compaction"]["nodes_dropped"] == 1
        # …and NOTHING observable changed
        assert len(after_live) == before_live
        assert [n.get("description") for n in after_live] == before_values

        # the truth is in the STORE, not only in RAM
        stored = fresh_rooms.store.get(room_id)
        assert stored is not None
        assert _digest(stored) == _digest(room.document)
        # …and the op-log was cut back to what the snapshot does not contain
        assert len(room.oplog) == 0


def test_4b_a_cold_room_is_rebuilt_from_the_store(client, fresh_rooms):
    """The fence in one test: forget the working copy, and the study is still
    there — because it was never living in this process."""
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json(_op("description", "muro", T2))
        a.receive_json()
        a.send_json({"v": WIRE, "type": "request_save", "payload": {}})
        a.receive_json()

    fresh_rooms.forget("scavo")               # the process "restarts"
    assert fresh_rooms.peek("scavo") is None
    with client.websocket_connect("/v1/rooms/scavo/ws") as b:
        doc = _drain_join(b)
        assert doc["graphs"]["scavo"]["nodes"][0]["description"] == "muro"


def test_4c_the_directory_store_actually_writes_bytes(tmp_path):
    """Not a mock: a snapshot store is only a store if something is on the other
    side of it. (The production one is MinIO — declared, not wired.)"""
    store = DirectorySnapshotStore(tmp_path)
    store.put("scavo", {"graphs": {"scavo": {"nodes": [], "edges": []}}})
    written = list(pathlib.Path(tmp_path).glob("*.em.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["graphs"]["scavo"] == {"nodes": [], "edges": []}
    assert store.get("scavo") is not None
    assert store.get("mai-scritta") is None


# ── 5 · auth ────────────────────────────────────────────────────────────────

def test_5_without_a_token_the_handshake_is_refused(monkeypatch, client, fresh_rooms):
    """In enforcing mode a socket with no token is closed with a reason, not
    left open in a half-authenticated state."""
    class Enforcing:
        enforcing = True

    monkeypatch.setattr(ws_module.authenticator, "settings", Enforcing())
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/rooms/scavo/ws") as socket:
            socket.receive_json()


def test_5b_the_author_is_the_token_not_what_the_client_says(monkeypatch, client,
                                                             fresh_rooms):
    """The stamp is what the merge trusts (P4.1b), so it cannot be self-declared:
    a client that names somebody else is not lying to the relay, it is lying to
    everyone downstream."""
    class Enforcing:
        enforcing = True

    monkeypatch.setattr(ws_module.authenticator, "settings", Enforcing())
    monkeypatch.setattr(ws_module.authenticator, "verify",
                        lambda token: {"orcid": ANNA, "name": "Anna"})
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws?token=whatever") as a:
        _drain_join(a)
        # the client claims to be Bruno…
        a.send_json(_op("description", "scritta da chi?", T2, author=BRUNO))
        assert a.receive_json()["payload"]["applied"] is True
        node = fresh_rooms.peek("scavo").document["graphs"]["scavo"]["nodes"][0]
        # …and the graph records the TOKEN's identity
        assert node["data"]["modified_by"] == ANNA
        assert node["data"]["field_clocks"]["description"]["by"] == ANNA


# ── 6 · presence ────────────────────────────────────────────────────────────

def test_6_presence_appears_and_disappears(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        with client.websocket_connect("/v1/rooms/scavo/ws") as b:
            _drain_join(b)
            arrival = a.receive_json()
            assert arrival["type"] == "presence"
            assert len(arrival["payload"]["members"]) == 2, "A is told that B is here"
        departure = a.receive_json()
        assert departure["type"] == "presence"
        assert len(departure["payload"]["members"]) == 1, "…and that B has gone"


def test_6b_a_selection_is_awareness_and_not_a_lock(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a, \
         client.websocket_connect("/v1/rooms/scavo/ws") as b:
        _drain_join(a)
        _drain_join(b)
        a.receive_json()

        b.send_json({"v": WIRE, "type": "select", "payload": {"node_id": "US1"}})
        seen = a.receive_json()
        assert seen["type"] == "select" and seen["payload"]["node_ids"] == ["US1"]

        # …and A can still write that very node: awareness, never a lock
        a.send_json(_op("description", "scrivo lo stesso", T2))
        assert a.receive_json()["payload"]["applied"] is True
        assert any(m["selection"] == ["US1"] for m in
                   fresh_rooms.peek("scavo").presence())


def test_6c_presence_is_not_written_anywhere(client, fresh_rooms):
    """The third fence: ephemeral means ephemeral. Nothing about who was
    connected reaches the store."""
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json({"v": WIRE, "type": "request_save", "payload": {}})
        a.receive_json()
    stored = json.dumps(fresh_rooms.store.get("scavo"))
    for word in ("presence", "connection_id", "joined_at"):
        assert word not in stored


# ── 7 · the repo's rules, as tests ──────────────────────────────────────────

def test_7_the_relay_adds_no_logic():
    """The rule of this repo, extended to the module that gained state.

    The relay must reach s3Dgraphy only through `api`: the moment it imports
    `crdt` or `container` directly, a convergence rule can be written here — and
    the next person to change it will change one of the two copies.
    """
    for name in ("ws.py", "rooms.py"):
        source = (_REPO / "app" / name).read_text(encoding="utf-8")
        for forbidden in ("from s3dgraphy.crdt", "from s3dgraphy.container",
                          "from s3dgraphy.nodes", "from s3dgraphy.graph",
                          "from s3dgraphy.exporter", "from s3dgraphy.importer"):
            assert forbidden not in source, \
                f"{name}: {forbidden} — the relay goes through s3dgraphy.api only"


def test_7b_no_convergence_or_gc_rule_is_written_in_the_relay():
    """A heuristic with teeth: the words a merge rule needs. Clock arithmetic,
    tombstone decisions and compaction do not appear in the transport."""
    for name in ("ws.py", "rooms.py"):
        source = (_REPO / "app" / name).read_text(encoding="utf-8")
        for forbidden in ("compare_clocks", "field_clock", "merge_payloads",
                          "compact_section", "is_removed("):
            assert forbidden not in source, \
                f"{name}: {forbidden} — that decision belongs to s3Dgraphy"


def test_7c_the_durable_truth_is_an_interface_not_a_path():
    """Fence 1, as a test: the relay writes through the store abstraction, and
    never opens a file of its own."""
    for name in ("ws.py", "rooms.py"):
        source = (_REPO / "app" / name).read_text(encoding="utf-8")
        assert "open(" not in source, f"{name}: the relay must not touch the disk"
        assert "pathlib" not in source or name == "store.py"
    store_source = (_REPO / "app" / "store.py").read_text(encoding="utf-8")
    assert "class MinioSnapshotStore" in store_source, \
        "the production target is named, so the swap is configuration"
    assert "NotImplementedError" in store_source, \
        "…and it fails with a sentence rather than pretending to work"


def test_7d_the_health_route_says_where_the_truth_is(client):
    """An operator must be able to ask, without reading the deployment, whether
    their snapshots survive a restart."""
    payload = client.get("/health").json()
    assert "snapshot_store" in payload
    assert payload["snapshot_store"].startswith(("memory", "directory", "minio"))


def test_7e_an_edge_op_keeps_its_endpoints(client, fresh_rooms):
    """The wire's `source` (who sent this) and an edge's `source` (where it
    starts) are the same word for two different things.

    Found by the IIIF smoke, not by reading: the relay stripped `source` from
    every op — correct for the origin tag, fatal for an edge, which then landed
    with `source: None`. It applied, it was broadcast, and the only trace was a
    load warning about an edge whose ends do not exist, much later and somewhere
    else.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json({"v": WIRE, "type": "op", "source": "emstudio",
                     "payload": {"op": "add_node", "ts": T2,
                                 "node": {"id": "reg-1",
                                          "node_type": "annotation_region",
                                          "name": "regione"}}})
        assert a.receive_json()["payload"]["applied"] is True
        # The edge carries `source`/`target` as its ENDPOINTS. In WIRE 1 they sat
        # at the top level beside the envelope's own `source`, and the relay ate
        # them. Nested, the collision cannot happen — and the envelope below
        # still says `emstudio`, so both words are present and neither is
        # ambiguous.
        a.send_json({"v": WIRE, "type": "op", "source": "emstudio",
                     "payload": {"op": "add_edge", "ts": T2, "id": "e-1",
                                 "source": "reg-1", "target": "US1",
                                 "edge_type": "is_on_resource"}})
        assert a.receive_json()["payload"]["applied"] is True

    room = fresh_rooms.peek("scavo")
    section = room.document["graphs"][next(iter(room.document["graphs"]))]
    edge = next(e for e in section["edges"] if e["id"] == "e-1")
    assert edge["source"] == "reg-1", "the edge starts where the client said"
    assert edge["target"] == "US1"
    assert edge["edge_type"] == "is_on_resource"


# ── 9 · the snapshot KEEPS the tombstones ───────────────────────────────────
#
# The other half of the per-surface policy stated in `s3dgraphy.dissemination`:
# a dissemination surface (GraphML, Heriverse, published RDF) must not carry a
# deletion, and the surfaces where somebody still has to MERGE must. The room
# snapshot is the strictest of those three — a client that joins without the
# tombstones cannot converge, it can only resurrect what the others deleted,
# and it would do so silently.

def test_9_a_late_client_receives_the_deletions_too(client, fresh_rooms):
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        a.send_json({"v": WIRE, "type": "op", "source": "test",
                     "payload": {"op": "remove_node", "id": "US1", "ts": T2}})
        assert a.receive_json()["payload"]["applied"] is True

        with client.websocket_connect("/v1/rooms/scavo/ws") as late:
            doc = _drain_join(late)

    nodes = {n["id"]: n for n in doc["graphs"]["scavo"]["nodes"]}
    assert "US1" in nodes, \
        "a snapshot without the dead is a snapshot that resurrects them"
    assert nodes["US1"]["data"]["removed"]["ts"] == T2
    assert em.crdt_stats(doc)["node_tombstones"] == 1


def test_9b_a_saved_room_keeps_a_deletion_somebody_has_not_seen(client,
                                                                fresh_rooms):
    """GC is the one thing allowed to drop a tombstone, and only past an instant
    every participant has passed. A client whose watermark sits BEFORE the
    deletion holds it back — otherwise "save" would quietly mean "forget who was
    deleted" for whoever is still catching up.

    The `ack` is not decoration: it is how a member says where it has got to,
    and the compaction takes the MINIMUM across the connected members.
    """
    _seed(fresh_rooms)
    with client.websocket_connect("/v1/rooms/scavo/ws") as a:
        _drain_join(a)
        # A declares it has only applied up to T1 — before the deletion below
        a.send_json({"v": WIRE, "type": "ack", "source": "test",
                     "payload": {"ts": T1}})
        a.send_json({"v": WIRE, "type": "op", "source": "test",
                     "payload": {"op": "remove_node", "id": "US1", "ts": T3}})
        assert a.receive_json()["payload"]["applied"] is True
        a.send_json({"v": WIRE, "type": "request_save", "source": "test",
                     "payload": {}})
        assert a.receive_json()["type"] == "snapshot_written"

    stored = fresh_rooms.store.get("scavo")
    nodes = {n["id"]: n for n in stored["graphs"]["scavo"]["nodes"]}
    assert "US1" in nodes and "removed" in nodes["US1"]["data"], \
        "the deletion was compacted away while a member was still behind it"
