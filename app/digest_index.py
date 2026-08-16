"""digest → embargo, for the hottest path there is.

Every IIIF tile is a request, and every request asks the same question: *is the
picture with this sha256 under embargo, and may this caller have it?* Answering
it honestly means reading the graph — and, because a IIIF identifier carries no
room, reading **every room this instance holds**. That is O(rooms) per tile, on
the one path a viewer hits hundreds of times while somebody pans an image.

So: an index. And the whole design of it is in one decision —

    it is invalidated by the WRITE, never by a clock.

A cache with a TTL would be a *cache of an embargo*: for up to N seconds it
would answer with a rule that is no longer the rule, which is exactly what the
asset gate promises not to do ("the store consults the graph; it does not keep a
second copy of it"). A room bumps `Room.revision` when its document changes; this
index remembers the revision it read and rebuilds the moment it differs. There is
no window in which it can be stale — the invalidation is the event, not the
timer.

It is **derived**, in the same sense the Catalog's index is: throw it away and it
comes back from the documents. Nothing depends on it being warm, and a miss is
not an error — it is a read.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from s3dgraphy import api as em


class DigestIndex:
    """Which rooms say something about which digest, kept in step with the rooms.

    The entry per room is `{digest: rights}` — the rights dict `asset_rights`
    returns, minus nothing: storing a boolean "embargoed" would freeze a verdict
    that is computed from today's date, and an embargo that expires tomorrow
    would still be refusing the file. What is cached is what the GRAPH SAYS; the
    *verdict* is recomputed on every question.
    """

    def __init__(self) -> None:
        # keyed by room id → (which working copy, which revision, what it says)
        self._by_room: Dict[str, Tuple[str, int, Dict[str, Dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def rooms_for(self, digest: str, rooms: List[Tuple[str, Any]]
                  ) -> List[str]:
        """Which of these rooms mention this digest at all.

        `rooms` is `[(room_id, room_or_None)]` — a live `Room` when the instance
        holds one. A room that is not live is not indexed and is reported as a
        candidate, because "I have not read it" must never read as "it says
        nothing": the caller then does the real read, which is the fallback this
        index is allowed to have.
        """
        wanted = _norm(digest)
        out: List[str] = []
        for room_id, room in rooms:
            if room is None:
                out.append(room_id)         # unread: ask properly
                continue
            entry = self._entry(room_id, room)
            if entry is None or wanted in entry:
                out.append(room_id)
        return out

    def rights(self, room_id: str, room: Any, digest: str
               ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """`(known, rights)` for this digest in this room.

        `known=False` means the index could not answer and the caller must read
        the graph — a miss, not a "no". Distinguishing the two is the difference
        between an index and a hole in the gate.
        """
        entry = self._entry(room_id, room)
        if entry is None:
            return False, None
        return True, entry.get(_norm(digest))

    def forget(self, room_id: str) -> None:
        with self._lock:
            self._by_room.pop(room_id, None)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"rooms": len(self._by_room),
                    "digests": sum(len(e) for _i, _rev, e in self._by_room.values())}

    # ── the build, and the one condition that triggers it ────────────────────

    def _entry(self, room_id: str, room: Any) -> Optional[Dict[str, Dict[str, Any]]]:
        revision = getattr(room, "revision", None)
        instance = getattr(room, "instance", None)
        if revision is None or instance is None:
            return None                     # not a room we can track
        with self._lock:
            cached = self._by_room.get(room_id)
            # BOTH halves of the key: the revision says the document changed,
            # the instance says it is a different working copy altogether. A
            # room rebuilt from the store starts again at revision 0, and
            # keying on the number alone hands it the previous room's answers.
            if cached is not None and cached[0] == instance and cached[1] == revision:
                return cached[2]
        # Built OUTSIDE the lock: it walks a document, and holding a lock across
        # that would serialise every tile request behind one room's scan.
        try:
            built = _scan(room.document)
        except Exception:                   # noqa: BLE001 — unreadable: no index
            return None
        with self._lock:
            self._by_room[room_id] = (instance, revision, built)
        return built


def _scan(document: Any) -> Dict[str, Dict[str, Any]]:
    """Every digest this document mentions, with what it says about it.

    One walk per room per revision, instead of one walk per room per REQUEST.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for section in _sections(document):
        for node in section.get("nodes") or []:
            data = node.get("data") if isinstance(node, dict) else None
            digest = _norm((data or {}).get("checksum"))
            if not digest or digest in out:
                continue
            rights = em.asset_rights(document, digest)
            if rights is not None:
                out[digest] = rights
    return out


def _sections(document: Any) -> List[Dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    graphs = document.get("graphs")
    if isinstance(graphs, dict):
        return [g for g in graphs.values() if isinstance(g, dict)]
    return [document] if "nodes" in document else []


def _norm(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    return (text.rsplit(":", 1)[-1] if ":" in text else text).lower()


#: One index per process. Derived state, so it is created here rather than
#: configured: there is nothing to choose, and losing it costs a rebuild.
INDEX = DigestIndex()
