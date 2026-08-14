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
    """The deployment implementation: one bucket, one object per digest.

    **This is the production store**, not a development double. A local FCN and
    an institutional node are the same service with different addresses, so they
    run the same class; `dev-stack/` only puts a MinIO next to it on a laptop.

    The object KEY is the digest itself (`<hex>`, without the `sha256:` prefix —
    the prefix belongs to the reference, not to the bucket's namespace). That is
    what buys the three properties the interface promises, and it buys them from
    S3 rather than from code we would have to keep correct:

    * **dedup** — the same bytes have the same key, so a second upload overwrites
      an identical object instead of making a second one;
    * **immutability in practice** — an object cannot change under a reference
      that still points at it, because a different content is a different key;
    * **verifiability** — anybody can hash what they fetched and check.

    The `minio` client is an OPTIONAL dependency (`pip install em-server[s3]`).
    A build without it fails **here**, at construction, with a sentence — not at
    the first upload with a stack trace from inside a request.
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, secure: bool = True) -> None:
        try:
            from minio import Minio  # type: ignore
        except ImportError as exc:   # pragma: no cover — depends on the build
            raise RuntimeError(
                "the MinIO asset store needs the `minio` client, which this "
                "build does not have: install em-server[s3] (or unset the MinIO "
                "environment and use EM_ASSET_DIR)") from exc

        self.bucket = bucket
        self.endpoint = endpoint
        # the client wants host:port WITHOUT a scheme, and takes the scheme as a
        # flag — passing the URL whole is the first thing that goes wrong here
        host = endpoint.split("://", 1)[-1].rstrip("/")
        self._client = Minio(host, access_key=access_key, secret_key=secret_key,
                             secure=secure)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """The bucket must exist before the first upload, and saying WHY it does
        not is the whole value of checking at start-up.

        Created when missing, because a room that cannot store its first asset is
        not a room. If creation fails the process refuses to start with the two
        causes named — a bucket that is not there and credentials that may not be
        allowed to make one look identical from a stack trace.
        """
        from minio.error import S3Error  # type: ignore

        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        except S3Error as exc:
            raise RuntimeError(
                f"the asset bucket '{self.bucket}' is not usable at "
                f"{self.endpoint}: {exc.code}. Either it does not exist and these "
                f"credentials may not create it, or they may not read it — "
                f"create the bucket in MinIO, or give the key access to it") from exc
        except Exception as exc:      # network, DNS, TLS: not an S3 answer at all
            raise RuntimeError(
                f"the object store at {self.endpoint} did not answer: {exc}. "
                f"em-server will not start without the store it is configured "
                f"to write to") from exc

    # ── the interface ────────────────────────────────────────────────────────

    def _key(self, ref: str) -> str:
        return ref.split(":", 1)[-1]

    def put(self, data: bytes, media_type: str) -> Dict[str, Any]:
        import io

        from minio.error import S3Error  # type: ignore

        ref = content_id(data)
        key = self._key(ref)
        existed = False
        try:
            self._client.stat_object(self.bucket, key)
            existed = True
        except S3Error as exc:
            if exc.code not in ("NoSuchKey", "NoSuchObject", "NotFound"):
                raise
        if not existed:
            self._client.put_object(self.bucket, key, io.BytesIO(data), len(data),
                                    content_type=media_type)
        return {"ref": ref, "sha256": key, "media_type": media_type,
                "size": len(data), "created": not existed}

    def get(self, ref: str) -> Optional[bytes]:
        from minio.error import S3Error  # type: ignore

        response = None
        try:
            response = self._client.get_object(self.bucket, self._key(ref))
            return response.read()
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject", "NotFound"):
                return None
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def head(self, ref: str) -> Optional[Dict[str, Any]]:
        """Size and type **without moving the bytes** — a `HEAD`, not a `GET`.

        Worth its own method precisely because an asset can be a hundred
        megabytes: asking "is it there, how big is it" must not cost a download.
        """
        from minio.error import S3Error  # type: ignore

        try:
            stat = self._client.stat_object(self.bucket, self._key(ref))
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject", "NotFound"):
                return None
            raise
        return {"ref": ref, "sha256": self._key(ref),
                "media_type": stat.content_type or "application/octet-stream",
                "size": stat.size}


#: The MinIO settings, in the two spellings that exist in the wild: the client's
#: own names (`MINIO_*`, what the dev-stack and MinIO's own docs use) and the
#: em-server-prefixed ones the Ansible role writes. Read in this order, like
#: `auth.py` reads the issuer — one setting, two names, and a precedence, rather
#: than two settings that will one day disagree.
_MINIO_KEYS = {
    "endpoint": ("MINIO_ENDPOINT", "EM_ASSET_S3_ENDPOINT"),
    "access_key": ("MINIO_ACCESS_KEY", "EM_ASSET_S3_ACCESS_KEY"),
    "secret_key": ("MINIO_SECRET_KEY", "EM_ASSET_S3_SECRET_KEY"),
    "bucket": ("MINIO_BUCKET", "EM_ASSET_S3_BUCKET"),
}


def _minio_settings(env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The MinIO configuration, or None when there is none at all.

    Raises when it is HALF there. That is the whole point of this function: a
    deployment that believes it writes to the shared bucket and quietly writes
    to a local directory instead is a data-loss story, and it is silent until it
    is expensive — the same rule `auth.py` applies to a half-configured realm.
    """
    found = {field: next((env[name].strip() for name in names
                          if (env.get(name) or "").strip()), "")
             for field, names in _MINIO_KEYS.items()}
    given = [field for field, value in found.items() if value]
    if not given:
        return None
    missing = [field for field, value in found.items() if not value]
    if missing:
        raise RuntimeError(
            "the MinIO asset store is half configured: "
            f"{', '.join(sorted(given))} given, {', '.join(sorted(missing))} "
            "missing. em-server will not fall back to a local directory the "
            "operator did not ask for — set all four "
            "(MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / "
            "MINIO_BUCKET) or none of them.")
    secure_raw = (env.get("MINIO_SECURE") or "").strip().lower()
    if secure_raw:
        secure = secure_raw in ("1", "true", "yes", "on")
    else:
        # not stated: believe the endpoint. `http://` means somebody chose plain
        # HTTP (a laptop, a private network); anything else defaults to TLS,
        # because guessing wrong in that direction is the safe way to be wrong.
        secure = not found["endpoint"].startswith("http://")
    found["secure"] = secure
    return found


def asset_store_from_env(environ: Optional[Dict[str, str]] = None) -> AssetStore:
    """The asset store this process should use, chosen by configuration.

    Precedence, and each step is a deliberate answer to "what did the operator
    actually ask for?":

    1. **MinIO** when its settings are there — the deployment store, durable and
       outside this process (rule #2). Half-configured **refuses to start**.
    2. `EM_ASSET_DIR` — a directory. Honest for one instance, wrong for replicas.
    3. nothing — in-memory, which dies with the process. Loudly wrong for a
       deployment, which is why `/v1/health` reports which one is in use.
    """
    env = environ if environ is not None else os.environ
    minio = _minio_settings(env)
    if minio:
        return MinioAssetStore(endpoint=minio["endpoint"],
                               access_key=minio["access_key"],
                               secret_key=minio["secret_key"],
                               bucket=minio["bucket"], secure=minio["secure"])
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
    if isinstance(store, MinioAssetStore):
        # the bucket and the host, never the key: `/v1/health` is public enough
        # that a credential printed there is a credential in a screenshot
        return f"minio ({store.endpoint}, bucket {store.bucket})"
    return {
        "InMemoryAssetStore": "memory (not durable — dies with the process)",
        "DirectoryAssetStore": "directory (local only — not for replicas)",
    }.get(type(store).__name__, type(store).__name__)
