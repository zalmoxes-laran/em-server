"""The RESIDENT DTC corpus: this instance's documentation, where em-server can read it.

## The hole this closes (measured, 17 Aug 2026)

An asset was uploaded to MinIO and its rights were declared in a **file** corpus —
the per-project `DTCCorpus` member that travels inside an em.json. Serving the
asset, `GET …/asset/<ref>` answered `x-em-license: null`: the enforcement reads
the rights from the **room's document**, and a file on somebody's laptop is not a
document em-server has. The licence was written, and the server could not see it,
so it did not bite.

So the corpus gets a residence. One per **instance** — the shared register of the
documentation, the way the Catalog is the shared register of studies — held with
the same discipline as a room snapshot: put atomically, outside this process,
rebuildable. It is a place em-server can *ask*, which is the whole point.

## Why per-instance and not per-room

A digest is content, and the same photograph is cited by several studies (that is
why `_rights_seen_anywhere` exists at all). The provenance of a file is a fact
about the file, not about the room somebody happened to open — so partitioning
the corpus by room would make one file's licence depend on which door you came
through, which is the bug this closes, re-introduced one level up.

## What it holds

**One em.json graph section**, marked as a corpus
(`data.em_collection = "DTCCorpus"`) — the same shape the per-project member has,
so the two are the same kind of thing and the promote path is a merge and not a
translation. Its nodes are acquisitions, transformations and the resources they
are about; the resources carry their `checksum`, which is what the asset gate
looks them up by.

## Versioning

**Append-mostly, versioned by CONTENT.** Every write recomputes a sha256 over the
canonical JSON, so the version measures what the corpus *says*, not how many
times somebody pressed save — the same rule the project version follows
(`s3dgraphy.container.bump_version`). No history and no branches tonight: those
are a design decision about citing a state of the documentation, not a store
feature, and inventing them here would prejudge it.

## Offline keeps working

The per-project file corpus is NOT replaced: standalone/offline it is the truth,
it travels with the em.json, and at promote/join its DTC **merges** into the
resident one (`s3dgraphy.dtc.corpus.merge_corpus`, per-UUID, additive). Two
places, one shape, and the resident one is what the server can enforce from.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import threading
from typing import Any, Dict, List, Optional, Protocol, Tuple

#: The one object this instance keeps. A name rather than an id, because there is
#: exactly one: a second resident corpus on the same server would be two answers
#: to "what is this file's licence".
CORPUS_KEY = "corpus"


def canonical_digest(document: Dict[str, Any]) -> str:
    """`sha256:<hex>` over the canonical JSON of the corpus.

    Canonical = sorted keys, no spaces: two servers that hold the same corpus
    must compute the same version, or a client cannot tell "changed" from
    "serialised differently".
    """
    blob = json.dumps(document, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class CorpusStore(Protocol):
    """Get and put the instance's corpus document. Nothing else.

    Two methods, like `SnapshotStore` — and for the same reason: anything richer
    would be logic, and the logic of a DTC corpus lives in s3Dgraphy where
    EMStudio and EMtools can reach it too.
    """

    def get(self) -> Optional[Dict[str, Any]]:
        """The corpus as last written, or None when this instance has none yet."""

    def put(self, document: Dict[str, Any]) -> None:
        """Replace the corpus. Atomic: a reader never sees half of it."""


class InMemoryCorpusStore:
    """For tests and a single-process laptop run — and it dies with the process.

    Copies on the way in and out, so a caller that keeps mutating its working
    copy cannot rewrite what was stored (the same fence `InMemorySnapshotStore`
    puts up, for the same reason).
    """

    def __init__(self) -> None:
        self._blob: Optional[str] = None
        self._lock = threading.Lock()

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            raw = self._blob
        return json.loads(raw) if raw is not None else None

    def put(self, document: Dict[str, Any]) -> None:
        blob = json.dumps(document, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        with self._lock:
            self._blob = blob


class DirectoryCorpusStore:
    """A single `corpus.em.json` in a directory.

    For a local run and for tests that want to see the bytes. `tmp.replace` is
    not decoration: the corpus is read by the asset gate on every request, and a
    reader that caught a half-written file would fail closed (503) for no reason
    anybody could reproduce.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{CORPUS_KEY}.em.json"

    def get(self) -> Optional[Dict[str, Any]]:
        if not self.path.is_file():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def put(self, document: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)


class MinioCorpusStore:
    """The deployment implementation: one object in the room bucket.

    The same client and the same bucket as the assets — the corpus is *about*
    those bytes, and giving it a second bucket would be a second thing to
    provision, back up and get wrong. The key is a fixed name (not a digest):
    unlike an asset, this object is meant to be replaced, and a reader must be
    able to find the current one without being told its version.

    The `minio` client is an OPTIONAL dependency: a build without it fails
    **here**, at construction, with a sentence — not at the first append with a
    stack trace from inside a request.
    """

    #: the object key inside the bucket. Prefixed so an operator listing the
    #: bucket can tell the register from the content-addressed blobs beside it.
    OBJECT_KEY = "em/corpus.em.json"

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, secure: bool = True) -> None:
        try:
            from minio import Minio  # type: ignore
        except ImportError as exc:   # pragma: no cover — depends on the build
            raise RuntimeError(
                "the MinIO corpus store needs the `minio` client, which this "
                "build does not have: install em-server[s3] (or unset the MinIO "
                "environment and use EM_CORPUS_DIR)") from exc
        self.bucket = bucket
        self.endpoint = endpoint
        host = endpoint.split("://", 1)[-1].rstrip("/")
        self._client = Minio(host, access_key=access_key, secret_key=secret_key,
                             secure=secure)

    def get(self) -> Optional[Dict[str, Any]]:
        from minio.error import S3Error  # type: ignore

        response = None
        try:
            response = self._client.get_object(self.bucket, self.OBJECT_KEY)
            return json.loads(response.read().decode("utf-8"))
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject", "NotFound"):
                return None
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def put(self, document: Dict[str, Any]) -> None:
        import io

        blob = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
        self._client.put_object(self.bucket, self.OBJECT_KEY, io.BytesIO(blob),
                               len(blob), content_type="application/json")


def corpus_store_from_env(environ: Optional[Dict[str, str]] = None) -> CorpusStore:
    """The corpus store this process should use, chosen by configuration.

    Precedence, and each step answers "what did the operator ask for?":

    1. **MinIO** when the asset store's settings are there — the corpus rides in
       the same bucket as the bytes it describes (`assets._minio_settings`, so a
       half-configured MinIO refuses to start there, once, rather than twice);
    2. `EM_CORPUS_DIR`, or `EM_SNAPSHOT_DIR` when that is not set — a local run
       keeps its corpus beside its snapshots without a second setting to
       remember;
    3. nothing — in memory, which dies with the process and is reported as such
       by `/v1/health`.
    """
    env = environ if environ is not None else os.environ
    from .assets import _minio_settings

    minio = _minio_settings(env)
    if minio:
        return MinioCorpusStore(endpoint=minio["endpoint"],
                                access_key=minio["access_key"],
                                secret_key=minio["secret_key"],
                                bucket=minio["bucket"], secure=minio["secure"])
    directory = env.get("EM_CORPUS_DIR") or env.get("EM_SNAPSHOT_DIR")
    if directory:
        return DirectoryCorpusStore(directory)
    return InMemoryCorpusStore()


def describe(store: CorpusStore) -> str:
    """A word for `/v1/health`: where the documentation lives."""
    if isinstance(store, MinioCorpusStore):
        return f"minio ({store.endpoint}, bucket {store.bucket})"
    return {
        "InMemoryCorpusStore": "memory (not durable — dies with the process)",
        "DirectoryCorpusStore": "directory (local only — not for replicas)",
    }.get(type(store).__name__, type(store).__name__)


# ── the corpus as a document: read, slice, append ────────────────────────────

def empty_corpus() -> Dict[str, Any]:
    """An empty corpus section, marked as one — from the library, not by hand.

    The marker is what makes a graph called `dtc` a corpus rather than a graph
    somebody called dtc (`s3dgraphy.dtc.corpus`), and spelling it here would be
    the second definition of the same thing.
    """
    from s3dgraphy.dtc.corpus import DTC_CORPUS_COLLECTION, DTC_CORPUS_MEMBER_ID

    return {"graph_id": DTC_CORPUS_MEMBER_ID, "name": "Documentation (DTC)",
            "data": {"em_collection": DTC_CORPUS_COLLECTION},
            "nodes": [], "edges": []}


def _digest_of(value: Any) -> str:
    """The bare lowercase hex of a digest, however it was written."""
    return str(value or "").rsplit(":", 1)[-1].strip().lower()


def slice_for(section: Dict[str, Any], digests: List[str]) -> Dict[str, Any]:
    """The part of the corpus that speaks about these files.

    A study cites a handful of assets out of a register that may hold thousands,
    and sending the whole thing to draw four nodes would make the resident corpus
    unusable for exactly the client it exists for.

    What comes back: the resources whose `checksum` is asked for, **everything
    the chain attaches to them** (the acquisitions and transformations that
    produced or consumed them, and those events' own rights), and the edges among
    the nodes kept. Reached by walking the DTC edges outward until nothing new
    joins — an acquisition without its licence node would be a slice that answers
    the rights question wrongly, which is worse than a slice that is too big.
    """
    wanted = {_digest_of(d) for d in digests if _digest_of(d)}
    nodes = [n for n in (section.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (section.get("edges") or []) if isinstance(e, dict)]

    def node_id(node: Dict[str, Any]) -> str:
        return str(node.get("id") or node.get("node_id") or "")

    def checksum(node: Dict[str, Any]) -> str:
        data = node.get("data")
        return _digest_of((data or {}).get("checksum")) if isinstance(data, dict) else ""

    keep = {node_id(n) for n in nodes if checksum(n) in wanted}
    if not keep:
        return {**{k: v for k, v in section.items() if k not in ("nodes", "edges")},
                "nodes": [], "edges": []}

    # walk outward: an edge either way pulls its other end in
    changed = True
    while changed:
        changed = False
        for edge in edges:
            src = str(edge.get("source") or edge.get("edge_source") or "")
            dst = str(edge.get("target") or edge.get("edge_target") or "")
            if src in keep and dst not in keep:
                keep.add(dst)
                changed = True
            elif dst in keep and src not in keep:
                keep.add(src)
                changed = True

    return {**{k: v for k, v in section.items() if k not in ("nodes", "edges")},
            "nodes": [n for n in nodes if node_id(n) in keep],
            "edges": [e for e in edges
                      if str(e.get("source") or e.get("edge_source") or "") in keep
                      and str(e.get("target") or e.get("edge_target") or "") in keep]}


# ── who may read the WHOLE register ──────────────────────────────────────────
#
# The slice is a citation: `?sha256=…` asks about files the caller already holds
# the digests of, and answering is what the register is for. **The whole thing is
# a different question**: it is the provenance of every study on the instance —
# who photographed what, for whom, under which embargo — and an authenticated
# stranger has no business reading the lot just because they can log in.
#
# The rule, and it is deliberately crude (a full role model over the register is
# curation policy, not a line of code):
#
# * **off by default.** The whole corpus is NOT a client path — EMStudio always
#   slices — so nothing legitimate breaks, and an instance that never configures
#   anything never exposes the lot. Defaults decide what most deployments do;
# * `EM_CORPUS_CURATORS` — a comma-separated list of ORCIDs who may read it. That
#   is the curation role: the people who look after the register;
# * `EM_CORPUS_OPEN=1` — the escape hatch for a single-user laptop, stated in one
#   place so it shows up in a config review.
#
# A refusal is a **403 with the reason and the remedy**, never a 500 and never an
# empty answer: "you got nothing" and "there is nothing" must not look alike.

def curators(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """The ORCIDs allowed to read the whole register."""
    env = environ if environ is not None else os.environ
    raw = env.get("EM_CORPUS_CURATORS") or ""
    return [o.strip() for o in raw.split(",") if o.strip()]


def whole_read_open(environ: Optional[Dict[str, str]] = None) -> bool:
    """Is the whole register open to any authenticated caller? (Off by default.)"""
    env = environ if environ is not None else os.environ
    return (env.get("EM_CORPUS_OPEN") or "").strip().lower() in ("1", "true", "yes", "on")


def may_read_whole(orcid: Optional[str], *, dev_mode: bool = False,
                   environ: Optional[Dict[str, str]] = None) -> bool:
    """May this caller read the WHOLE corpus?

    Dev mode is yes for the same reason it is `owner` everywhere else: with no
    OIDC there are no identities to distinguish, and a lock drawn on a door with
    no wall is theatre (`ws.authorize` says it first).
    """
    if dev_mode:
        return True
    if whole_read_open(environ):
        return True
    allowed = curators(environ)
    return bool(orcid) and orcid in allowed


def whole_read_refusal(environ: Optional[Dict[str, str]] = None) -> str:
    """The sentence a refusal carries: what was refused, and what to do instead."""
    named = curators(environ)
    who = (f"the curators of this register ({len(named)} declared)" if named
           else "nobody yet — no curator is configured")
    return ("the WHOLE documentation of this instance is the provenance of every "
            "study on it, and reading it is a curation act: it is open to "
            f"{who}. Ask about the files you hold instead — "
            "GET /v1/corpus?sha256=<digest>,<digest> answers about those, with "
            "their chain. An operator opens the whole read with "
            "EM_CORPUS_CURATORS=<orcid>,… (or EM_CORPUS_OPEN=1 for a single-user "
            "instance).")


def _marked(section: Dict[str, Any]) -> Dict[str, Any]:
    """The section, guaranteed to still SAY it is a corpus.

    `build_emjson` carries `graph.data` through, but a corpus that lost the marker
    would come back as "a graph called dtc" — and the shelf/corpus distinction is
    exactly what the marker exists for.
    """
    from s3dgraphy.dtc.corpus import DTC_CORPUS_COLLECTION

    data = section.setdefault("data", {})
    if not data.get("em_collection"):
        data["em_collection"] = DTC_CORPUS_COLLECTION
    return section


class ResidentCorpus:
    """The instance's corpus, read through a store and written back atomically.

    Holds no truth of its own: `read()` goes to the store every time. The corpus
    is consulted by the asset gate, and a cached copy would be exactly the
    "remembered embargo" that `get_asset` refuses to keep — a licence lives in a
    text people edit.

    The lock serialises **appends**, so two acts that arrive together cannot
    clobber one another (read-modify-write on one object, one process). Two
    replicas appending at the same instant are not covered and it says so:
    that needs a conditional put (S3 preconditions) and is a deployment concern
    to close when there are replicas — declared, not pretended.
    """

    def __init__(self, store: CorpusStore) -> None:
        self.store = store
        self._lock = threading.Lock()

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self) -> Dict[str, Any]:
        """The corpus section — an empty, marked one when nothing was written."""
        document = self.store.get()
        if not isinstance(document, dict) or "nodes" not in document:
            return empty_corpus()
        return document

    def version(self) -> str:
        return canonical_digest(self.read())

    def read_slice(self, digests: Optional[List[str]]) -> Dict[str, Any]:
        section = self.read()
        if not digests:
            return section
        return slice_for(section, digests)

    def rights_for(self, digest: str, *, today: Any = None) -> Optional[Dict[str, Any]]:
        """What the corpus says about these bytes — or None if it never heard of
        them. The same library call the room path uses (`api.asset_rights`), so
        the two cannot drift into two readings of one document."""
        from s3dgraphy import api as em

        return em.asset_rights(self.read(), digest, today=today)

    # ── appending ────────────────────────────────────────────────────────────

    def append(self, act: str, payload: Dict[str, Any], *,
               author: Optional[str]) -> Tuple[Dict[str, Any], str]:
        """Perform one documentation ACT on the resident corpus.

        `act` is one of the three the library already implements — and they are
        the library's, deliberately: `bucket_acquisition`, `declare_derivation`
        and `enrich_asset_dtc` are the protocol EMStudio, EMtools and a field
        ingest all perform, and a server that re-implemented them would be the
        fourth dialect of the same sentence.

        Returns `(report, version)`. The ops are idempotent (the same act twice
        converges), so a client that retries after a timeout does not double
        anything.

        **NOTHING IS LOST IN CONCURRENCY**, and it takes two fences because the
        register is shared:

        * a **lock** serialises the read→apply→write of this process. It is the
          simple, exactly-correct answer for one process over one object, and it
          is why two clients appending at the same instant both land;
        * a **last-moment merge**: after applying, the store is read AGAIN, and if
          it moved under us (another process — a second replica, an operator's
          script) our result is merged into that newer state instead of replacing
          it. Per-UUID, additive, the library's own merge (`merge_corpus`) — so
          the act that arrived meanwhile is not calpestato.

        The declared gap: this is compare-then-write, not compare-and-swap. Two
        replicas can still interleave between the re-read and the put, and closing
        that needs a conditional put (S3 preconditions / `If-Match`). What the
        merge buys is that the *likely* race — an act landing during a slow
        read-modify-write — costs nothing, instead of silently costing an act.

        Raises `ValueError` for an act nobody implements or an argument the
        library refuses — the caller turns that into a 400 with the sentence.
        """
        from s3dgraphy import api as em
        from s3dgraphy.exporter.emjson_exporter import build_emjson
        from s3dgraphy.importer.emjson_importer import parse_emjson

        header = {"header": {"format": "em.json", "version": "1.0"}}
        with self._lock:
            before = self.read()
            base_digest = canonical_digest(before)
            graph, _warnings = parse_emjson({**header,
                                             "graph": copy.deepcopy(before)})
            report = _perform(em, graph, act, payload, author=author)
            written = _marked(build_emjson(graph)["graph"])

            # …did anything land while we were working?
            latest = self.read()
            if canonical_digest(latest) != base_digest:
                mine, _ = parse_emjson({**header, "graph": written})
                theirs, _ = parse_emjson({**header,
                                          "graph": copy.deepcopy(latest)})
                # THEIRS is the base and MINE folds into it: the other act is
                # already durable and this one is the newcomer, so the merge runs
                # in the direction that cannot drop what is already stored.
                from s3dgraphy.dtc.corpus import merge_corpus
                merge_corpus(theirs, mine)
                written = _marked(build_emjson(theirs)["graph"])

            self.store.put(written)
            return report, canonical_digest(written)

    def merge(self, incoming: Dict[str, Any], *,
              author: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        """Take in a **file** corpus — the offline half coming home.

        Additive and per-UUID, the library's own merge
        (`s3dgraphy.dtc.corpus.merge_corpus`), so promoting a project twice does
        not duplicate its documentation and two people who photographed the same
        stone end up with one entry.
        """
        from s3dgraphy.dtc.corpus import merge_corpus
        from s3dgraphy.exporter.emjson_exporter import build_emjson
        from s3dgraphy.importer.emjson_importer import parse_emjson

        header = {"header": {"format": "em.json", "version": "1.0"}}
        with self._lock:
            mine, _ = parse_emjson({**header, "graph": copy.deepcopy(self.read())})
            theirs, _ = parse_emjson({**header, "graph": copy.deepcopy(incoming)})
            report = merge_corpus(mine, theirs)
            written = _marked(build_emjson(mine)["graph"])
            self.store.put(written)
            return report, canonical_digest(written)


#: The acts, kept as data so the endpoint's error message can name what it
#: accepts instead of a caller reading the source.
#:
#: `resource` is the door: the other three all speak ABOUT a file, and
#: `enrich_asset_dtc` refuses to invent one ("enrich the asset after the
#: ResourceNode exists, not instead of it"). So a client that has just uploaded
#: bytes registers them here first — which is also the moment em-server can check
#: that the bytes really are in its store, so the register describes assets this
#: instance holds rather than assets somebody described.
ACTS = ("resource", "acquisition", "derivation", "attribution")

#: The namespace for a resource id derived from its digest. Deterministic and
#: SHARED: two clients registering the same file must land on the same node, or
#: the register grows two entries for one photograph and the second one carries no
#: rights. (Same trick `dtc.ingest` uses for its stable ids.)
_RESOURCE_NAMESPACE = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"


def resource_id_for(digest: str) -> str:
    """The corpus's id for these bytes — derived from the digest, so it converges."""
    import uuid

    return str(uuid.uuid5(uuid.UUID(_RESOURCE_NAMESPACE),
                          f"resource|{_digest_of(digest)}"))


def _perform(em: Any, graph: Any, act: str, payload: Dict[str, Any], *,
             author: Optional[str]) -> Dict[str, Any]:
    """One act, through the library. No logic here beyond the routing."""
    kind = str(act or "").strip().lower()
    if kind == "resource":
        checksum = payload.get("checksum") or payload.get("digest")
        if not checksum:
            raise ValueError("a resource needs its `checksum` (sha256:<hex>)")
        return _register_resource(graph, str(checksum), payload, author=author)
    if kind == "acquisition":
        return em.bucket_acquisition(
            graph, payload.get("resources") or [],
            acquisition_id=payload.get("acquisition_id"),
            name=payload.get("name"),
            dtc_kind=payload.get("dtc_kind"),
            metadata=payload.get("metadata"),
            author=author, at=payload.get("at"))
    if kind == "derivation":
        output = payload.get("output")
        if not output:
            raise ValueError("a derivation needs an `output` (an id or a digest)")
        # NOTE the api surface of `declare_derivation` takes no `dtc_kind` (the
        # process kind is the library's default): passing one would be a keyword
        # this call does not have, and inventing it here is how a thin adapter
        # starts having opinions.
        return em.declare_derivation(
            graph, output, payload.get("inputs") or [],
            tool=payload.get("tool"), process_id=payload.get("process_id"),
            name=payload.get("name"), author=author, at=payload.get("at"))
    if kind == "attribution":
        checksum = payload.get("checksum") or payload.get("digest")
        if not checksum:
            raise ValueError("an attribution needs the `checksum` of the asset")
        # the ATTRIBUTOR is the token's identity, never a field in the body: who
        # says so is not something a caller gets to claim about somebody else
        return em.enrich_asset_dtc(
            graph, str(checksum), attributor=author,
            author=payload.get("author"), license=payload.get("license"),
            embargo=payload.get("embargo"), at=payload.get("at"),
            author_name=payload.get("author_name"),
            reason=payload.get("reason"))
    raise ValueError(f"unknown act {act!r}: this endpoint performs {', '.join(ACTS)}")


def _register_resource(graph: Any, checksum: str, payload: Dict[str, Any], *,
                       author: Optional[str]) -> Dict[str, Any]:
    """Put a file in the register — idempotently, and never overwriting.

    Fields are FILLED IN, not replaced: whoever described the file first said
    something, and a second client that knows less about it must not erase that.
    Same rule as `dtc.corpus.mirror_resource`, applied to a file arriving from an
    upload rather than from a study graph.
    """
    from s3dgraphy.editorial import stamp_created, stamp_modified
    from s3dgraphy.nodes import ResourceNode

    digest = _digest_of(checksum)
    existing = next((n for n in graph.nodes
                     if _digest_of((getattr(n, "data", None) or {}).get("checksum"))
                     == digest), None)
    created = existing is None
    node = existing
    if node is None:
        node = ResourceNode(resource_id_for(digest),
                            name=str(payload.get("name") or f"{digest[:12]}…"),
                            url=str(payload.get("url") or ""))
        graph.add_node(node)
    data = getattr(node, "data", None)
    if not isinstance(data, dict):
        data = {}
        node.data = data
    data.setdefault("checksum", f"sha256:{digest}")
    for key in ("media_type", "residency", "url", "url_type", "scope", "size"):
        value = payload.get(key)
        if value not in (None, "") and data.get(key) in (None, ""):
            data[key] = value
    data.setdefault("residency", "resident")
    if created:
        stamp_created(node, by=author, at=payload.get("at"))
    stamp_modified(node, by=author, at=payload.get("at"))
    return {"resource_id": node.node_id, "digest": digest, "created": created,
            "name": getattr(node, "name", "")}


#: This process's corpus store and the corpus over it. Built at import, like the
#: other two stores, so a misconfiguration fails when the process starts.
CORPUS_STORE: CorpusStore = corpus_store_from_env()
RESIDENT: ResidentCorpus = ResidentCorpus(CORPUS_STORE)
