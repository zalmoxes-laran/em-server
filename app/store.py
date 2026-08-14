"""Where a room's durable truth lives — outside this process.

P4.2 introduces the first component of em-server that **holds state**, and this
module is one of the three fences that keep that introduction honest (the others
are in `rooms.py`: convergence stays in the library, presence stays ephemeral).

The fence here: **the durable truth is not on the process's disk.** A room's
snapshot goes to an object store — MinIO in the deployment — and what em-server
keeps in RAM is a *working copy* that can be rebuilt from it. The process may die
and come back; the study does not live inside it. That is what keeps rule 2 of
this repo (stateless, 12-factor) meaningful in the presence of a relay: the state
is *held*, not *owned*.

The interface is deliberately two methods. A snapshot store is a key-value store
of documents, and anything richer here would be logic — which belongs in the
library, not in the transport.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import threading
from typing import Any, Dict, Optional, Protocol


class SnapshotStore(Protocol):
    """Get and put a room's container document. Nothing else."""

    def get(self, room_id: str) -> Optional[Dict[str, Any]]:
        """The last snapshot of this room, or None if it was never written."""

    def put(self, room_id: str, document: Dict[str, Any]) -> None:
        """Replace this room's snapshot."""


class InMemorySnapshotStore:
    """For tests and for a single-process laptop run.

    It is NOT the deployment target, and saying so matters: an in-memory store
    dies with the process, which is precisely the property the MinIO
    implementation exists to remove. Copies on the way in and out, so a caller
    that keeps mutating its working copy cannot rewrite history.
    """

    def __init__(self) -> None:
        self._data: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, room_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            raw = self._data.get(room_id)
        return json.loads(raw) if raw is not None else None

    def put(self, room_id: str, document: Dict[str, Any]) -> None:
        blob = json.dumps(document, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        with self._lock:
            self._data[room_id] = blob

    def rooms(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


class DirectorySnapshotStore:
    """A directory of `<room>.em.json` files.

    For a **local** run and for tests that want to see the bytes. Explicitly not
    the production answer: a path on the process's filesystem is exactly the
    thing rule 2 warns about, and a deployment that used this would have two
    replicas with two different truths. It exists because "run it on a laptop"
    is a real use, and because a test that can open the file proves the snapshot
    was actually written rather than remembered.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, room_id: str) -> pathlib.Path:
        # room ids come from a URL; keep them from walking out of the directory
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in room_id)
        return self.root / f"{safe}.em.json"

    def get(self, room_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(room_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, room_id: str, document: Dict[str, Any]) -> None:
        path = self._path(room_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)     # atomic: a reader never sees half a snapshot


class MinioSnapshotStore:
    """The deployment target — **not implemented tonight, and it says so.**

    The shape is already fixed by the interface above: a bucket, one object per
    room, `get`/`put`. What it needs is the bucket and the credentials the shared
    infrastructure will provide (the mail-spec to Romano: realm + bucket +
    routing), and the `minio` client as an optional dependency — so a build
    without it fails at construction with a sentence, not at the first snapshot
    with a stack trace.

    Left as a class rather than a comment because the point of the interface is
    that the swap is a line of configuration, and an empty class makes that
    concrete: when the bucket exists, this is where it goes.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "the MinIO snapshot store is the P4.2 deployment target and is not "
            "wired yet: it needs the shared bucket + credentials. Use "
            "InMemorySnapshotStore (tests) or DirectorySnapshotStore (local).")


def store_from_env(environ: Optional[Dict[str, str]] = None) -> SnapshotStore:
    """The store this process should use, chosen by configuration.

    `EM_SNAPSHOT_DIR` picks the directory store (a local run). Nothing set means
    in-memory — honest for a laptop, and loudly wrong for a deployment, which is
    why `/v1/health` reports which one is in use rather than leaving an operator
    to guess whether their snapshots survive a restart.
    """
    env = environ if environ is not None else os.environ
    directory = env.get("EM_SNAPSHOT_DIR")
    if directory:
        return DirectorySnapshotStore(directory)
    return InMemorySnapshotStore()


def describe(store: SnapshotStore) -> str:
    """A word for `/v1/health`: which store is holding the truth."""
    return {
        "InMemorySnapshotStore": "memory (not durable — dies with the process)",
        "DirectorySnapshotStore": "directory (local only — not for replicas)",
        "MinioSnapshotStore": "minio",
    }.get(type(store).__name__, type(store).__name__)


def deep_copy(document: Dict[str, Any]) -> Dict[str, Any]:
    """A working copy that cannot alias the snapshot it came from."""
    return copy.deepcopy(document)
