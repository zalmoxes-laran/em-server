"""The asset store: a room provides a graph AND the bytes it refers to.

P4.2 gave a room its graph (the snapshot store). This is the other half: a study
is not only a set of assertions, it is also the models and the photographs those
assertions point at, and a room that could hold the first without the second
would push every collaborator back to sending files by hand.

Same shape as `store.py`, and for the same reason: **the durable truth is not on
the process's disk.** The interface is three methods, the implementation for
tests keeps bytes in memory, and the production one is MinIO — a line of
configuration, not a rewrite.

**Content-addressed.** The reference IS the digest of the content
(`sha256:<hex>`), which buys three things at once and no cleverness:

* **dedup is free** — the same bytes put twice are one object, because they have
  the same name. Two people who promote the same model do not fill a bucket.
* **the reference is verifiable** — a client that fetches an asset can check what
  it got, without asking anybody. That is what makes `checksum` on a ResourceNode
  a fact rather than a hope.
* **immutability** — an object cannot be replaced under a reference that still
  points at it. A citation stays a citation.

The digest travels WITH its algorithm (`sha256:…`), the same rule the shelf's
checksum follows: a bare hex string is unreadable in two years.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import threading
from typing import Any, Dict, Optional, Protocol


#: What a room's assets are addressed by. The prefix is not decoration: it is
#: what lets a reader know how to verify, and what lets us change algorithm one
#: day without every old reference becoming ambiguous.
DIGEST_PREFIX = "sha256"


def content_id(data: bytes) -> str:
    """`sha256:<hex>` of the bytes — the name an asset has for ever."""
    return f"{DIGEST_PREFIX}:{hashlib.sha256(data).hexdigest()}"


class AssetStore(Protocol):
    """Put bytes, get bytes, ask about bytes. Nothing else lives here."""

    def put(self, data: bytes, media_type: str) -> Dict[str, Any]:
        """Store `data`; return `{ref, sha256, media_type, size, created}`.

        `created` is False when the object was already there — a caller that
        wants to report "dedup" can, and one that does not can ignore it.
        """

    def get(self, ref: str) -> Optional[bytes]:
        """The bytes behind a reference, or None."""

    def head(self, ref: str) -> Optional[Dict[str, Any]]:
        """`{ref, sha256, media_type, size}` without moving the bytes."""


class InMemoryAssetStore:
    """For tests and a single-process laptop run — and it says so.

    Not the deployment target: it dies with the process, which is exactly the
    property the MinIO implementation exists to remove.
    """

    def __init__(self) -> None:
        self._blobs: Dict[str, bytes] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, media_type: str) -> Dict[str, Any]:
        ref = content_id(data)
        with self._lock:
            existed = ref in self._blobs
            if not existed:
                self._blobs[ref] = bytes(data)
                self._meta[ref] = {"ref": ref, "sha256": ref.split(":", 1)[1],
                                   "media_type": media_type, "size": len(data)}
        info = dict(self._meta[ref])
        info["created"] = not existed
        return info

    def get(self, ref: str) -> Optional[bytes]:
        with self._lock:
            return self._blobs.get(ref)

    def head(self, ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            meta = self._meta.get(ref)
        return dict(meta) if meta else None

    def count(self) -> int:
        """How many distinct objects — the number a dedup test measures."""
        with self._lock:
            return len(self._blobs)


class DirectoryAssetStore:
    """A directory of content-addressed files. Local runs and tests that want to
    see the bytes; explicitly not the answer for replicas."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> pathlib.Path:
        digest = ref.split(":", 1)[-1]
        safe = "".join(c for c in digest if c.isalnum())
        # two levels of fan-out: a directory with a hundred thousand entries in
        # it is a directory nothing can list
        return self.root / safe[:2] / safe[2:4] / safe

    def put(self, data: bytes, media_type: str) -> Dict[str, Any]:
        ref = content_id(data)
        path = self._path(ref)
        existed = path.is_file()
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)                      # a reader never sees half a file
            path.with_suffix(".type").write_text(media_type, encoding="utf-8")
        return {"ref": ref, "sha256": ref.split(":", 1)[1], "media_type": media_type,
                "size": len(data), "created": not existed}

    def get(self, ref: str) -> Optional[bytes]:
        path = self._path(ref)
        return path.read_bytes() if path.is_file() else None

    def head(self, ref: str) -> Optional[Dict[str, Any]]:
        path = self._path(ref)
        if not path.is_file():
            return None
        type_file = path.with_suffix(".type")
        media = type_file.read_text(encoding="utf-8").strip() if type_file.is_file() \
            else "application/octet-stream"
        return {"ref": ref, "sha256": ref.split(":", 1)[1], "media_type": media,
                "size": path.stat().st_size}


class MinioAssetStore:
    """The deployment target — **not wired tonight, and it says so.**

    The shape is fixed by the interface: a bucket, one object per digest,
    `put`/`get`/`head`. What it needs is the bucket and the credentials the
    shared infrastructure provides (STEP 5 of this runner writes the Ansible that
    creates them) plus the `minio` client as an optional dependency — so a build
    without it fails at construction with a sentence, not at the first upload
    with a stack trace.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "the MinIO asset store is the deployment target and is not wired "
            "yet: it needs the shared bucket + credentials (see the Ansible "
            "role). Use InMemoryAssetStore (tests) or DirectoryAssetStore.")


def asset_store_from_env(environ: Optional[Dict[str, str]] = None) -> AssetStore:
    """The asset store this process should use, chosen by configuration.

    `EM_ASSET_DIR` picks the directory store. Nothing set means in-memory —
    honest for a laptop and loudly wrong for a deployment, which is why
    `/v1/health` reports which one is in use.

    `EM_ASSET_S3_ENDPOINT` (the deployment's object store) is READ but not yet
    served: the process **refuses to start** rather than quietly falling back to
    a directory nobody backs up. Same rule as the half-configured auth in
    `auth.py` — a deployment that believes it is writing to the shared bucket
    and is not is a data-loss story, and it is silent until it is expensive.
    """
    env = environ if environ is not None else os.environ
    if env.get("EM_ASSET_S3_ENDPOINT"):
        raise NotImplementedError(
            "EM_ASSET_S3_ENDPOINT is set, but the MinIO/S3 asset store is not "
            "implemented yet: this process will not pretend to write to "
            f"{env['EM_ASSET_S3_ENDPOINT']}. Unset it and use EM_ASSET_DIR "
            "(a volume) until the object-store implementation ships.")
    directory = env.get("EM_ASSET_DIR")
    if directory:
        return DirectoryAssetStore(directory)
    return InMemoryAssetStore()


#: This process's asset store. Built at import, like the snapshot one, so a
#: misconfiguration fails when the process starts rather than at the first upload.
ASSET_STORE: AssetStore = asset_store_from_env()

_HEX = set("0123456789abcdef")


def asset_ref_valid(ref: str) -> bool:
    """Is this a reference this store could ever have minted?

    Checked before touching the store: a reference comes from a URL, and a
    directory-backed implementation that took an arbitrary string would be one
    `../` away from reading somebody else's file. Content-addressing makes the
    check trivial — a valid reference has exactly one shape.
    """
    prefix, _, digest = str(ref).partition(":")
    return (prefix == DIGEST_PREFIX and len(digest) == 64
            and all(c in _HEX for c in digest.lower()))


def describe(store: AssetStore) -> str:
    return {
        "InMemoryAssetStore": "memory (not durable — dies with the process)",
        "DirectoryAssetStore": "directory (local only — not for replicas)",
        "MinioAssetStore": "minio",
    }.get(type(store).__name__, type(store).__name__)
