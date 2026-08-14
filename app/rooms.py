"""The room: a study, its working copy, its op-log, and who is in it.

P4.2 is where em-server stops being stateless, and the honest thing to do is to
say exactly where the state is and why it is allowed. Three fences, and they are
the contract:

1. **The durable truth is outside the process** — `store.py`. What lives here is
   a *working copy*, rebuilt from the snapshot when a cold room is opened. The
   process can die; the study does not.
2. **Convergence lives in s3Dgraphy.** This module calls `s3dgraphy.api` and
   contains no merge, no clock comparison, no tombstone rule. The relay applies
   and re-broadcasts; the CRDT converges regardless of order, which is exactly
   why a relay is enough and a transformer (OT) is not needed.
3. **Presence is ephemeral.** Who is connected lives in RAM, is lost on restart,
   and that is correct: presence is about *now*. Nothing in it is durable, so
   nothing in it needs a store.

Scaling seam, declared and NOT implemented: one instance owns a room (sticky
routing by `room_id`). Several replicas would need the op-log outside the process
— a Redis stream or the object store — and every `broadcast` here would become a
publish. The shape is ready for that; tonight it would be an untested moving part
in a component that just gained state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from s3dgraphy import api as em

from .store import SnapshotStore, deep_copy

#: How many recent operations a room keeps so a late arrival can catch up
#: without a fresh snapshot. Bounded on purpose: an unbounded log is a memory
#: leak with a good excuse.
OPLOG_LIMIT = 512


def now_iso() -> str:
    """The clock this server stamps with — UTC, seconds, the EM spelling."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Member:
    """One connected client. Ephemeral by construction — see fence 3."""

    connection_id: str
    #: the ORCID (or subject) from the TOKEN, never what the client said it was
    author: Optional[str]
    display: str = ""
    #: what this member has selected — the awareness channel, soft, no locks
    selection: List[str] = field(default_factory=list)
    #: the instant of the last operation this member has been sent. The minimum
    #: across members is what makes compaction safe (see `gc_watermark`).
    watermark: Optional[str] = None
    joined_at: str = field(default_factory=now_iso)

    def as_presence(self) -> Dict[str, Any]:
        return {"id": self.connection_id, "author": self.author,
                "display": self.display, "selection": list(self.selection),
                "joined_at": self.joined_at}


class Room:
    """One study, live.

    The working copy is a container document (`{"graphs": {...}}`) — the same
    shape the file on disk has, because the relay must never invent a second
    representation of a project.
    """

    def __init__(self, room_id: str, document: Dict[str, Any]):
        self.room_id = room_id
        self.document = document
        self.oplog: List[Dict[str, Any]] = []
        self.members: Dict[str, Member] = {}
        self.sockets: Dict[str, Any] = {}
        self.lock = asyncio.Lock()
        self.snapshot_at: Optional[str] = None
        self.last_op_at: Optional[str] = None
        #: P4.3 · how far this room has been COMPACTED. Announced to every client
        #: (`host_info`, `snapshot`) because it is the one number a client needs
        #: to know whether its own history is still reconcilable here: below this
        #: point the room no longer holds what a replay would argue with.
        self.compacted_upto: Optional[str] = None

    # ── who may read this study ──────────────────────────────────────────────

    @property
    def visibility(self) -> str:
        """`public` or `restricted` — read from the STUDY, not from a config.

        It belongs in the document's header because it is a fact about the work
        (D2.2 §3.4: dissemination is a *validated* tier, in-progress is not), and
        a study that travels — a file somebody sends, a snapshot restored
        elsewhere — must carry its own answer rather than inherit whatever the
        new server happens to think.

        **Restricted is the default, and unknown reads as restricted.** The
        failure directions are not symmetric: a public study served behind a
        token annoys somebody, an in-progress study served without one publishes
        an interpretation nobody has finished making.
        """
        header = self.document.get("header")
        value = str((header or {}).get("visibility") or "").strip().lower()
        return "public" if value == "public" else "restricted"

    @property
    def is_public(self) -> bool:
        return self.visibility == "public"

    # ── membership ───────────────────────────────────────────────────────────

    def join(self, connection_id: str, socket: Any, author: Optional[str],
             display: str = "") -> Member:
        member = Member(connection_id=connection_id, author=author,
                        display=display or (author or "anon"))
        self.members[connection_id] = member
        self.sockets[connection_id] = socket
        return member

    def leave(self, connection_id: str) -> None:
        self.members.pop(connection_id, None)
        self.sockets.pop(connection_id, None)

    def presence(self) -> List[Dict[str, Any]]:
        return [m.as_presence() for m in self.members.values()]

    # ── the op-log ───────────────────────────────────────────────────────────

    def record(self, op: Dict[str, Any]) -> None:
        self.oplog.append(op)
        if len(self.oplog) > OPLOG_LIMIT:
            del self.oplog[: len(self.oplog) - OPLOG_LIMIT]
        self.last_op_at = str(op.get("ts") or now_iso())

    def replay_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """The operations after `since` — what a late arrival missed.

        With no `since` the caller gets nothing: it is being handed the snapshot,
        which already contains everything, and replaying the log on top would
        only re-apply what is there (harmless, because the ops are idempotent,
        and pointless, which is the better reason not to).
        """
        if not since:
            return []
        return [op for op in self.oplog if str(op.get("ts") or "") > since]

    # ── the operations (the library does the work) ───────────────────────────

    def apply(self, op: Dict[str, Any], graph_id: Optional[str] = None) -> Dict[str, Any]:
        """Apply ONE operation to the working copy, through s3Dgraphy.

        Fence 2 in one line: the relay does not decide anything about the
        operation — `em.apply_op` does, with the same code the offline merge and
        EMStudio's own copy run. A stale operation comes back `applied: False`,
        and the relay does not re-broadcast it as if it were news.
        """
        section = self._section(graph_id)
        if section is None:
            return {"applied": False, "reason": "no such graph in this room"}
        return em.apply_op(section, op)

    def _section(self, graph_id: Optional[str]) -> Optional[Dict[str, Any]]:
        graphs = self.document.get("graphs") or {}
        if graph_id and graph_id in graphs:
            return graphs[graph_id]
        active = self.document.get("active_graph_id")
        if active and active in graphs:
            return graphs[active]
        return next(iter(graphs.values()), None)

    # ── snapshot + GC ────────────────────────────────────────────────────────

    def gc_watermark(self) -> Optional[str]:
        """The instant every connected member has been brought past.

        The safety argument for compaction, and the reason it is a MINIMUM: a
        member still catching up may yet be sent an operation older than the
        others have seen, and compacting past that point would let a late
        operation win against a fallback instead of against the real clock.

        With nobody connected there is nothing to protect — but nothing to
        promise either: an absent client can come back with an old op-log, which
        this cannot know about. That is the declared limit of GC at this stage.
        """
        marks = [m.watermark for m in self.members.values() if m.watermark]
        if not marks:
            return None
        return min(marks)

    def snapshot(self, store: SnapshotStore, *, gc: bool = True) -> Dict[str, Any]:
        """Write the room to the durable store, compact, and truncate the log.

        The order matters: compaction happens on the working copy BEFORE the
        write, so the snapshot is the compacted one and a restart does not
        resurrect the bookkeeping that was just settled.
        """
        report: Dict[str, Any] = {}
        before = self.gc_watermark()
        if gc and before:
            # fence 2 again: the GC rule is s3Dgraphy's, invoked from here
            report = em.compact(self.document, before_ts=before)
            # …and the point is REMEMBERED, because a client that was away has to
            # be able to ask "is my history still worth anything here?"
            self.compacted_upto = before
        store.put(self.room_id, self.document)
        self.snapshot_at = now_iso()
        if before:
            # the log up to the watermark is now inside the snapshot
            self.oplog = [op for op in self.oplog if str(op.get("ts") or "") > before]
        return {"at": self.snapshot_at, "compaction": report,
                "gc_watermark": self.compacted_upto,
                "oplog": len(self.oplog),
                "stats": em.crdt_stats(self.document)}


class RoomRegistry:
    """The rooms this instance owns.

    Sticky by design: one instance owns a room. The seam for horizontal scaling
    is `get`/`broadcast` — a multi-replica deployment would resolve a room
    through a shared op-log instead of this dict. Not tonight (see the module
    docstring), and not precluded.
    """

    def __init__(self, store: SnapshotStore):
        self.store = store
        self._rooms: Dict[str, Room] = {}
        self._lock = asyncio.Lock()

    async def get(self, room_id: str) -> Room:
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is not None:
                return room
            # cold room: the truth comes back from the store, not from memory
            snapshot = self.store.get(room_id)
            document = deep_copy(snapshot) if snapshot else _empty_container(room_id)
            room = Room(room_id, document)
            self._rooms[room_id] = room
            return room

    def peek(self, room_id: str) -> Optional[Room]:
        return self._rooms.get(room_id)

    def forget(self, room_id: str) -> None:
        """Drop the working copy. The next join rebuilds it from the store —
        which is the property that makes this state a cache and not a home."""
        self._rooms.pop(room_id, None)

    def rooms(self) -> List[str]:
        return sorted(self._rooms)


def _empty_container(room_id: str) -> Dict[str, Any]:
    """A room nobody has ever written: an empty container-of-one.

    A container, not a bare graph, because that is what an em.json IS since the
    multigraph decision — a relay that invented a different starting shape would
    hand its first client a document the rest of the ecosystem does not read.
    """
    return {
        "header": {"format": "em.json", "version": "1.0"},
        "graphs": {room_id: {"graph_id": room_id, "name": room_id,
                             "nodes": [], "edges": []}},
        "active_graph_id": room_id,
    }
