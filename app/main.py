"""em-server — the s3Dgraphy access API, over HTTP.

**P0: read-only, local, no auth.** Auth (Keycloak), assets (MinIO), the WebSocket
op-log and the deployment on the shared infrastructure are P1–P4, and they land
with 3DR. Nothing here anticipates them beyond leaving the seams where they go.

The architectural rule this file exists to honour: **FastAPI lives only in
em-server.** s3Dgraphy stays a pure library — no web framework, no transport — and
this is a *thin adapter* over `s3dgraphy.api`. There is no new logic here, and
there must not be: if an endpoint needs to compute something, that something
belongs in the library, where it is testable without a server and reusable by
EMStudio's local bridge, by EMtools, and by EMLab.

**The contract is em-bridge's.** EMStudio already speaks to a local sidecar
(`EMStudio/tools/em_bridge.py`) with exactly these payloads; em-server answers the
same shapes so the frontend's `bridgeUrl()` can point here instead, with nothing
else changed. Where the two differ it is stated in the endpoint's docstring.

**Stateless (12-factor).** No session, no upload directory, no database: a document
arrives in the request and leaves in the response. That is what makes it safe to
run several replicas behind a load balancer, and it is a property to defend rather
than an accident — the first endpoint that keeps a file on disk breaks it.

**Everything lives under `/v1`.** The prefix is not decoration: 3DR will build
against this contract, and a path is the cheapest promise to keep. `/v1` means the
route names and payloads of P0 do not move; the multi-client WebSocket work of P3
may well need a `/v2`, and it should be able to appear beside this one rather than
replace it.

The single exception is `GET /health`, which exists **unversioned as well**. A
health probe belongs to the infrastructure, not to the API: a Docker HEALTHCHECK, a
Kubernetes liveness probe and a Caddy upstream check should not have to be edited
the day the API version changes. `/v1/health` is the API's answer, `/health` is the
orchestrator's, and they return the same thing.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from fastapi import Request

from .assets import ASSET_STORE, asset_ref_valid
from .assets import describe as asset_describe
from .auth import AuthDependency, authenticator
from .access import Acl, Group, Role, may_assign, parse_role
from .access import describe as acl_describe
from .corpus import CORPUS_STORE, RESIDENT, canonical_digest
from .corpus import describe as corpus_describe
from .digest_index import INDEX as DIGEST_INDEX
from .store import describe as snapshot_describe
from . import ws as _ws
from .ws import (ACL_STORE, SNAPSHOT_STORE, authorize, groups, load_acl,
                 save_acl, ws_router)


def rooms():
    """The relay's room registry, resolved WHEN ASKED and not bound at import.

    `from .ws import ROOMS` binds the object, so a test (or anything else) that
    replaced `ws.ROOMS` left these HTTP routes talking to the original registry
    — a footgun that has cost two debugging sessions and is documented in
    `tests/test_topology_and_visibility.py`. One registry, one place to find it.
    """
    return _ws.ROOMS


def snapshot_store():
    """The snapshot store, resolved when asked — same reason as `rooms()`.

    Binding it at import is how `_rooms_holding` ended up asking a store nobody
    was using: the gate looked correct and let an embargoed asset through in a
    test that had replaced the store. One accessor, one footgun less.
    """
    return _ws.SNAPSHOT_STORE

try:  # the whole point of the service; a clear failure beats a mysterious one
    from s3dgraphy import api as em
except ImportError as exc:  # pragma: no cover — deployment error, not runtime
    raise RuntimeError(
        "em-server needs s3dgraphy importable: pip install s3dgraphy "
        f"(or -e ../s3Dgraphy). {exc}"
    ) from exc

__version__ = "0.1.0.dev0"

app = FastAPI(
    title="em-server",
    version=__version__,
    summary="The s3Dgraphy access API over HTTP — read-only (P0), under /v1.",
    description=__doc__,
)

# ── who may call this from a browser ─────────────────────────────────────────
#
# Until now nothing did, and it showed: EMStudio talks to a room over a
# WebSocket (not subject to CORS) and everything worked — until the first
# ordinary HTTP call from the app. A `PUT` with an `Authorization` header is a
# preflighted request, and with no CORS policy the browser refuses it before
# em-server ever sees it. The symptom is a bare "Failed to fetch", which reads
# like the server is down.
#
# `*` by default, and it is not laxity: **there are no cookies here**. Every
# route is opened by a bearer token the caller attaches deliberately, so a page
# that has no token gets exactly what an anonymous caller gets, whatever its
# origin. `allow_credentials` stays False for the same reason — turning it on is
# what would make `*` dangerous, and nothing here needs it.
#
# `EM_CORS_ORIGINS` (comma-separated) narrows it for a deployment that wants to.
_CORS = [o.strip() for o in (os.environ.get("EM_CORS_ORIGINS") or "*").split(",")
         if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS,
    allow_credentials=False,
    allow_methods=["GET", "PUT", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["ETag", "X-EM-License", "X-EM-License-Default",
                    "X-EM-Embargo", "X-EM-Author", "X-EM-Authz"],
)

#: Every endpoint hangs off this router, so the prefix is declared once and cannot
#: drift between routes. A future v2 is a second router beside it, not an edit.
#:
#: **P1: the router carries the auth dependency**, so protection is a property of
#: the prefix rather than a decoration each route has to remember. A new endpoint
#: is authenticated because of where it is declared — the opposite arrangement,
#: where every handler opts in, is one forgotten line away from a public write op.
v1 = APIRouter(prefix="/v1", dependencies=[AuthDependency])

#: The two health routes live here instead, unauthenticated.
#:
#: Same prefix, no dependency: a probe is infrastructure. `/v1/health` is public
#: for the same reason `/health` is — and, more to the point, because making the
#: two paths of ONE function differ in security is how a misconfiguration hides.
#: Neither leaks anything: a version string and which optional libraries were
#: installed are things `docker inspect` already tells you.
v1_public = APIRouter(prefix="/v1")


# ── how optional dependencies are reported ────────────────────────────────────
# rdflib (TTL) and pyproj (reprojection) are optional in s3Dgraphy and stay
# optional here. A missing one is **501 Not Implemented**, never 500: the request
# was valid and the server simply cannot do that op in this build. It is the same
# mapping em-bridge uses, and it is what lets a client degrade honestly instead of
# showing an error it cannot explain.
def _missing_dependency(exc: Exception, what: str, extra: str) -> HTTPException:
    return HTTPException(
        status_code=501,
        detail=f"{what} unavailable — this build has no {extra} ({exc})",
    )


def _load(doc: Dict[str, Any]):
    """em.json dict → (graph, warnings), with a 400 for a document we cannot read.

    A document the importer refuses is the CLIENT's problem, so it is a 400 with
    the importer's own message — not a 500, which would send someone reading server
    logs for a malformed upload.
    """
    try:
        return em.load_emjson(doc)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"not a readable em.json: {exc}") from exc


# ── health ────────────────────────────────────────────────────────────────────

class Health(BaseModel):
    ok: bool = True
    service: str = "em-server"
    version: str
    s3dgraphy: Optional[str] = None
    #: which optional ops this build can actually perform. A client that reads
    #: this does not have to discover a 501 by trying.
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    #: `keycloak` when tokens are enforced, `dev-no-auth` when every /v1 route is
    #: open. Reported because a warning that only exists in a log is a warning
    #: nobody reads: this way "is this deployment actually protected?" is one
    #: unauthenticated GET away, for the operator and for the client alike.
    auth: str = "dev-no-auth"
    #: P4.2 · WHERE THE DURABLE TRUTH IS. The relay holds a working copy in RAM;
    #: this says what is behind it — and an operator who reads "memory" knows
    #: their snapshots die with the process, instead of finding out.
    snapshot_store: str = "memory"
    #: how many rooms this instance currently owns (sticky routing)
    rooms: int = 0
    #: where a room's ASSET bytes live. Same question as `snapshot_store`, asked
    #: of the other half of what a room provides: the graph AND the models it
    #: points at. An operator who reads "memory" knows their uploads die with the
    #: process, instead of finding out later.
    asset_store: str = "memory"
    #: …and where the per-room ACCESS LISTS live. Same question again, asked of
    #: the third piece of room state: a deployment whose grants die with the
    #: process would silently re-open every restricted room on restart.
    acl_store: str = "memory"
    #: and where the RESIDENT DTC CORPUS lives — the documentation the asset gate
    #: reads a licence and an embargo out of. An operator who reads "memory" knows
    #: the rights they declared die with the process.
    corpus_store: str = "memory"


@v1_public.get("/health", response_model=Health, tags=["meta"])
def health() -> Health:
    """Liveness, version, and what this build can do.

    `capabilities` is the part worth having: it answers "can you export TTL, can
    you reproject" without a request that fails. Probing is done by import, not by
    running an op — cheap, and it cannot have side effects.
    """
    def importable(module: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(module) is not None

    version = None
    try:
        import s3dgraphy
        version = getattr(s3dgraphy, "__version__", None)
    except Exception:  # pragma: no cover
        pass
    return Health(
        version=__version__,
        s3dgraphy=version,
        capabilities={
            "validate": True,
            "export_ttl": importable("rdflib"),
            "reproject": importable("pyproj"),
            "resolve_authority": bool(em.authority_facets()),
        },
        auth=authenticator.settings.describe(),
        snapshot_store=snapshot_describe(snapshot_store()),
        asset_store=asset_describe(ASSET_STORE),
        acl_store=acl_describe(ACL_STORE),
        corpus_store=corpus_describe(CORPUS_STORE),
        rooms=len(rooms().rooms()),
    )


# ── assets (the other half of what a room provides) ──────────────────────────
#
# A room gives a graph and the BYTES its assertions point at. Everything here is
# transport: the store decides what a reference is (the digest of the content),
# and this decides who may ask. No logic, per rule 1 — there is nothing to
# compute about a blob beyond hashing it, and the hashing is the store's.


class AssetInfo(BaseModel):
    ref: str
    sha256: str
    media_type: str
    size: int
    #: False when these exact bytes were already there. Content-addressing makes
    #: dedup automatic; SAYING it lets a client show "already published".
    created: bool = True
    #: who uploaded — the TOKEN's identity, never a field the client filled in
    author: Optional[str] = None


@v1.put("/rooms/{room_id}/asset", response_model=AssetInfo, tags=["assets"])
async def put_asset(room_id: str, request: Request,
                    media_type: str = Query(default="application/octet-stream",
                                            description="the MIME type of the bytes")) -> AssetInfo:
    """Publish bytes into a room's store; the reference is their digest.

    The body is the raw bytes (not multipart): an asset is one object, and a
    form wrapper would only add a boundary to parse. Re-uploading the same bytes
    is not an error and not a duplicate — it is the same object, and the answer
    says `created: false`.
    """
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body: nothing to store")
    info = ASSET_STORE.put(data, media_type)
    principal = authenticator.require_token(request)
    author = None if principal.get("em_dev_mode") else (
        principal.get("orcid") or principal.get("preferred_username")
        or principal.get("sub"))
    return AssetInfo(**info, author=author)


@v1.get("/rooms/{room_id}/asset/{ref:path}", tags=["assets"])
async def get_asset(room_id: str, ref: str, request: Request) -> Response:
    """Fetch an asset by reference — if the graph says you may have it yet.

    The caller can verify what it got: the reference IS the digest.

    **The store consults the graph; it does not duplicate it.** An asset's
    embargo is stated in the document (on its `ResourceNode`, or on the DTC
    chunk that produced it) and read here through
    `s3dgraphy.api.asset_rights` — never cached, because an embargo lives in a
    text people edit and a remembered copy is wrong the first time somebody
    changes their mind.

    The rule, and it is the study's rule applied one level down: while an
    embargo is running, these bytes are for the people who are working on them —
    **editor and above**. A viewer or an anonymous caller gets **403 with the
    date**, not a 404: pretending the file does not exist would be a lie the
    caller can disprove by asking again in March.

    The two embargoes COMPOUND, and the more restrictive wins: a room under a
    study-level embargo already refuses at the door, and an asset may be
    embargoed inside a study that is not.

    An asset the graph says nothing about is served as it always was.
    """
    if not asset_ref_valid(ref):
        raise HTTPException(status_code=400,
                            detail=f"not an asset reference: {ref!r} "
                                   f"(expected 'sha256:<hex>')")
    data = ASSET_STORE.get(ref)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no asset {ref}")

    # THE NAMED ROOM FIRST, then everybody else — because the store is shared.
    #
    # Measured, and it was a hole big enough to make the rest theatre: the same
    # digest requested through a DIFFERENT room came back 200, since the embargo
    # is stated in the graph of the room that holds the picture and the asset
    # store is not partitioned by room. A gate you get past by typing another
    # room name is not a gate.
    rights = await _rights_seen_anywhere(room_id, ref, request)
    # …AND THE RESIDENT CORPUS. This is the hole that was measured: the licence
    # was declared in a corpus, and a corpus em-server does not hold cannot make
    # anything bite. The register speaks about the BYTES, so it speaks whatever
    # room the caller came through.
    rights = await _corpus_gate(ref, request, door=room_id, room_rights=rights)

    meta = ASSET_STORE.head(ref) or {}
    headers = {"ETag": f'"{ref}"'}
    # The licence TRAVELS WITH THE BYTES. Not enforcement — a share-alike cannot
    # be imposed by an HTTP header, and pretending otherwise would be worse than
    # saying nothing. What this does is remove the excuse: whoever downloads
    # this file has been told, in the same breath, what they may do with it.
    if rights:
        headers["X-EM-License"] = str(rights.get("license_effective") or "")
        if rights.get("license_is_default"):
            headers["X-EM-License-Default"] = "true"
        if rights.get("embargo"):
            headers["X-EM-Embargo"] = str(rights["embargo"])
        authors = [a.get("orcid") or a.get("name") for a in rights.get("authors") or []]
        if authors:
            headers["X-EM-Author"] = ", ".join(str(a) for a in authors if a)
    return Response(content=data,
                    media_type=str(meta.get("media_type") or "application/octet-stream"),
                    headers=headers)


async def _asset_rights(room_id: str, ref: str) -> Optional[Dict[str, Any]]:
    """What this room's graph says about these bytes, or None for "nothing".

    **FAIL-CLOSED.** There are three outcomes and only two of them are answers:

    * *(i)* the graph was read and says nothing about this digest → `None`, and
      the caller serves the bytes. No regression: an asset with no DTC behaves
      exactly as it always has;
    * *(ii)* the graph was read and states an embargo → the rights, and the
      caller applies the gate;
    * *(iii)* **the graph could not be read at all** → this raises, and the
      caller answers 503. It used to return `None` here, which meant "nothing
      said", which meant *serve* — so a room whose document was corrupt, or a
      store that was briefly unreachable, published every embargoed file in it.

    (iii) is the whole point of this function existing separately: whether an
    asset is embargoed is **unknowable without the graph**, so "I cannot read
    the graph" must not be allowed to answer the question. The failure of a
    store is a reason to say *not now*, never a reason to say *yes*.

    A room nobody has ever written is case (i), not (iii), and that is
    deliberate: it has no document in which an embargo could be hiding, and the
    asset store is shared across rooms rather than partitioned by them. Making
    it a 404 would break the one path that proves this — `smoke.py` uploads to a
    room it never opens — while protecting nothing, because any room that DOES
    exist would still serve the same bytes.
    """
    room = await rooms().get(room_id)      # may raise: the store is not here
    known, rights = DIGEST_INDEX.rights(room_id, room, _digest_of(ref))
    if known:
        # …from the index, which is rebuilt the moment the room's document
        # changes (`Room.revision`) rather than on a timer. What is cached is
        # what the GRAPH SAYS; whether the embargo is running is computed from
        # today's date every time it is asked, below.
        return _fresh_verdict(rights)
    return em.asset_rights(room.document, ref)


def _fresh_verdict(rights: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Re-decide `embargo_active` NOW, on cached facts.

    The date is the one part of the answer that changes without anybody writing
    anything: an embargo that ends tomorrow is over tomorrow, and an index that
    stored the verdict would go on refusing the file until somebody edited the
    graph. So the FACTS are cached and the VERDICT is not.
    """
    if not rights:
        return rights
    try:
        from s3dgraphy.study import embargo_active
    except ImportError:  # pragma: no cover — the library is a hard dep
        return rights
    return {**rights, "embargo_active": embargo_active(rights.get("embargo"))}


async def _rights_seen_anywhere(room_id: str, ref: str,
                                request: Request) -> Optional[Dict[str, Any]]:
    """The rights to REPORT, having refused if any room forbids this caller.

    Asks the named room first — it is the one the caller meant, and its answer
    is the one whose licence belongs in the headers. Then, only if nothing has
    refused yet, the other rooms this instance holds, because a digest is
    content and the same picture can be cited in several studies; an embargo
    stated in one of them is stated about these bytes.

    Raises 403 (embargoed, and this caller is not editor+ **there** — the role
    is resolved in the room that made the statement, which is the room that can
    grant it) or 503 (a document that will not read: see `_asset_rights`).

    The declared cost: on a big instance this reads every room's document. It is
    bounded by the rooms one instance owns, and a deployment that minds can pass
    the room explicitly. Correct-and-slow beats fast-and-bypassable.
    """
    first: Optional[Dict[str, Any]] = None
    checked: List[str] = []
    for candidate in [room_id] + [r for r in _rooms_holding(_digest_of(ref))
                                  if r != room_id]:
        try:
            rights = await _asset_rights(candidate, ref)
        except Exception as exc:  # noqa: BLE001 — case (iii), fail-closed
            raise HTTPException(
                status_code=503,
                detail=f"cannot verify this asset's rights right now: the room "
                       f"{candidate!r} has a document that will not read "
                       f"({type(exc).__name__}). The bytes are not served until "
                       f"it does — an embargo that cannot be read is not an "
                       f"embargo that can be ignored.") from None
        checked.append(candidate)
        if candidate == room_id:
            first = rights
        if not rights or not rights.get("embargo_active"):
            continue
        role = await _role_in_room(candidate, request)
        if role is not None and role.can_write:
            continue
        raise HTTPException(
            status_code=403,
            detail=f"this asset is under embargo until {rights.get('embargo')} "
                   f"— until then it is readable by the people working on the "
                   f"study (editor and above)")
    return first


async def _corpus_gate(ref: str, request: Request, *, door: Optional[str],
                       room_rights: Optional[Dict[str, Any]]
                       ) -> Optional[Dict[str, Any]]:
    """Apply — and report — what the RESIDENT CORPUS says about these bytes.

    The register is per-instance and content-addressed, so it speaks about the
    file rather than about a room: whichever door the caller came through, a
    licence declared in the documentation is the licence of these bytes, and an
    embargo declared there is an embargo on them.

    **FAIL-CLOSED, like the room walk**: a corpus store that will not read raises
    503. "I cannot read the documentation" must not be allowed to answer "yes" —
    that was the bug `_asset_rights` was rewritten to remove, and a second reader
    with the opposite reflex would put it back.

    **Who overrides an embargo**: editor and above **in the room the request came
    through** — the door is the room the caller is working in, and it is the room
    that can grant them the role. When no room is named (a IIIF request with no
    `?room=`) the rooms citing the digest are asked instead; when nothing cites it
    at all, an embargoed file is refused to everybody until the date passes,
    because there is no room in which anybody could be its editor. (Dev mode has
    no identities and lets everything through, as everywhere else.)

    Returns the rights to REPORT: the room's statement where it made one, filled
    in from the corpus where it did not, and the **more restrictive** embargo of
    the two — the same compounding rule the two embargo levels already follow.
    """
    try:
        corpus_rights = RESIDENT.rights_for(_digest_of(ref))
    except Exception as exc:  # noqa: BLE001 — fail-closed, exactly like a room
        raise HTTPException(
            status_code=503,
            detail=f"cannot verify this asset's rights right now: the resident "
                   f"documentation will not read ({type(exc).__name__}). The bytes "
                   f"are not served until it does — an embargo that cannot be read "
                   f"is not an embargo that can be ignored.") from None
    if not corpus_rights:
        return room_rights

    if corpus_rights.get("embargo_active"):
        allowed = False
        if not authenticator.settings.enforcing:
            allowed = True                     # dev mode has no identities
        else:
            # WHOSE FILE IT IS, first: the people named as its authors are the
            # ones the embargo is protecting, not the ones it is protecting the
            # file from. Refusing them their own photograph while it is under
            # study would be the gate working against the person it works for.
            me = _identity_of(request)
            authors = {str(a.get("orcid") or "").strip()
                       for a in corpus_rights.get("authors") or []}
            if me and me in authors:
                allowed = True
            # …then a room where the caller may WRITE — resolved WITHOUT the
            # owner bootstrap.
            #
            # Measured: naming a room nobody had ever created got 200, because a
            # room with no owner makes the first authenticated arrival its OWNER
            # (`ws.authorize`) — so a viewer walked around a corpus embargo by
            # inventing a room name in the URL. Same shape as the room-shopping
            # hole `_rights_seen_anywhere` exists for, one level up. Reading the
            # grants without claiming any is the precise fix: an invented room has
            # no ACL and its empty document names no owner, so it grants nothing,
            # while a real editor of a room that was never saved still gets in.
            candidates = ([door] if door else []) \
                + [r for r in _rooms_holding(_digest_of(ref)) if r != door]
            for candidate in [c for c in candidates if c and not allowed]:
                role = await _role_without_bootstrap(candidate, me, request)
                if role is not None and role.can_write:
                    allowed = True
                    break
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"this asset is under embargo until "
                       f"{corpus_rights.get('embargo')} — until then it is "
                       f"readable by the people working on the study (editor and "
                       f"above)")
    return _combine_rights(room_rights, corpus_rights)


def _combine_rights(room: Optional[Dict[str, Any]],
                    corpus: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Two statements about one file, reported as one answer.

    The room is the more specific speaker (it is the study the caller opened), so
    what it SAID wins; what it left unsaid the corpus fills in — which is the
    whole point, because "nothing said" is exactly what the file corpus used to
    leave behind. The **embargo compounds**: the later date wins, because a file
    freed in one place and embargoed in another is embargoed.
    """
    if not corpus:
        return room
    if not room:
        return corpus
    out = dict(room)
    if not out.get("license"):
        for key in ("license", "license_effective", "license_is_default"):
            if corpus.get(key) not in (None, ""):
                out[key] = corpus[key]
    if not out.get("authors") and corpus.get("authors"):
        out["authors"] = corpus["authors"]
    mine, theirs = str(out.get("embargo") or ""), str(corpus.get("embargo") or "")
    if theirs and theirs > mine:      # ISO dates compare as strings
        out["embargo"] = corpus["embargo"]
        out["embargo_active"] = corpus.get("embargo_active")
    if not out.get("via") and corpus.get("via"):
        out["via"] = corpus["via"]
    return out


def _digest_of(ref: str) -> str:
    return str(ref).rsplit(":", 1)[-1].lower()


async def _role_in_room(room_id: str, request: Request):
    """The caller's role here, from an OPTIONAL token.

    Optional because an asset URL is fetched by things that cannot log in, and
    because a public study's assets are public. No token is not an error at this
    point — it simply resolves to whatever a stranger gets, which the embargo
    rule then judges.
    """
    header = request.headers.get("authorization") or ""
    token = request.query_params.get("token")
    claims: Dict[str, Any] = {}
    if not authenticator.settings.enforcing:
        claims = {"em_dev_mode": True}
    elif header.lower().startswith("bearer ") or token:
        try:
            claims = (authenticator.require_token(request)
                      if header.lower().startswith("bearer ")
                      else authenticator.verify(str(token).strip()))
        except Exception:  # noqa: BLE001 — a bad token is simply not an identity
            claims = {}
    orcid = None if claims.get("em_dev_mode") else (
        claims.get("orcid") or claims.get("preferred_username") or claims.get("sub"))
    room = await rooms().get(room_id)
    return authorize(room, orcid, dev_mode=bool(claims.get("em_dev_mode")))


# ── the RESIDENT DTC corpus (the documentation this instance can enforce from) ─
#
# The hole it closes was measured on 17 Aug: an asset whose licence lived in a
# per-project corpus FILE was served with `x-em-license: null`, because the
# enforcement reads the rights out of a document em-server holds and a file on a
# laptop is not one. Give the corpus a residence and the rights bite (see
# `app/corpus.py` for why it is one per instance and not one per room).
#
# Transport only, per rule 1: the three acts are `s3dgraphy`'s
# (`bucket_acquisition` / `declare_derivation` / `enrich_asset_dtc`) — the same
# protocol EMStudio and EMtools perform — and the merge is the container's.


class CorpusOut(BaseModel):
    """The corpus, or the slice of it that was asked for."""

    #: the em.json graph section, marked `em_collection: DTCCorpus`
    graph: Dict[str, Any] = Field(default_factory=dict)
    #: sha256 over the canonical JSON of the WHOLE corpus. The version is what
    #: the corpus says, not how many times somebody saved — so a client can tell
    #: "nothing changed" without diffing, and a slice still reports the version
    #: of the register it came out of.
    version: str = ""
    nodes: int = 0
    edges: int = 0
    #: True when `sha256=` narrowed the answer — a client that asked for four
    #: digests and got four nodes should not have to guess whether that is the
    #: whole register.
    sliced: bool = False
    #: where the documentation lives, in the words `/v1/health` uses
    store: str = ""


class CorpusAct(BaseModel):
    """One documentation act to append. `act` picks which, the rest is its body.

    Deliberately ONE endpoint with a named act rather than three routes: the
    three are the same kind of statement about the same register, they are all
    idempotent, and a client that performs two of them in a row should not have
    to learn two shapes.
    """

    act: str = Field(description="acquisition | derivation | attribution")
    #: acquisition: the files (ids **or** digests) and how the lot is described
    resources: Optional[List[str]] = None
    acquisition_id: Optional[str] = None
    name: Optional[str] = None
    dtc_kind: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    #: derivation: what came out, and what went in (an acquisition counts as one)
    output: Optional[str] = None
    inputs: Optional[List[str]] = None
    tool: Optional[str] = None
    process_id: Optional[str] = None
    #: resource: a file the register should know about — `checksum` is the door,
    #: and for a RESIDENT one em-server checks its own store before believing it
    media_type: Optional[str] = None
    residency: Optional[str] = None
    url: Optional[str] = None
    url_type: Optional[str] = None
    scope: Optional[str] = None
    size: Optional[int] = None
    #: attribution: whose file this is and what may be done with it
    checksum: Optional[str] = None
    author: Optional[str] = None
    author_name: Optional[str] = None
    license: Optional[str] = None
    embargo: Optional[str] = None
    reason: Optional[str] = None
    at: Optional[str] = None


class CorpusAppendOut(BaseModel):
    act: str
    #: whatever the library reported — ids, counts, the missing references it
    #: refused to invent, its warnings. Passed through verbatim: this is a thin
    #: adapter, and summarising a report is how a caller stops being able to see
    #: that three of its four files were not there.
    report: Dict[str, Any] = Field(default_factory=dict)
    version: str = ""
    #: who signed the act — the TOKEN's identity, never a field in the body
    by: Optional[str] = None


@v1.get("/corpus", response_model=CorpusOut, tags=["corpus"])
def get_corpus(
    sha256: Optional[List[str]] = Query(
        default=None,
        description="one or more digests: the answer is the part of the corpus "
                    "that speaks about those files (repeat the parameter, or "
                    "pass a comma-separated list)"),
) -> CorpusOut:
    """The instance's documentation — whole, or the slice a study needs.

    A study cites a handful of assets out of a register that may hold thousands,
    so the slice is not an optimisation: sending the whole corpus to draw four
    nodes is what would make the resident corpus unusable for the client it
    exists for. The slice keeps the chain around those files (the acquisition
    that brought them in, the transformation that made them, their rights) —
    reached by walking the DTC edges, because an acquisition without its licence
    node would answer the rights question wrongly.
    """
    digests: List[str] = []
    for raw in sha256 or []:
        digests.extend(part.strip() for part in str(raw).split(",") if part.strip())
    section = RESIDENT.read_slice(digests or None)
    whole = RESIDENT.read()
    return CorpusOut(
        graph=section, version=canonical_digest(whole),
        nodes=len(section.get("nodes") or []),
        edges=len(section.get("edges") or []),
        sliced=bool(digests), store=corpus_describe(CORPUS_STORE))


@v1.post("/corpus/append", response_model=CorpusAppendOut, tags=["corpus"])
def append_corpus(request: Request,
                  act: CorpusAct = Body(...)) -> CorpusAppendOut:
    """Perform one documentation act on the resident corpus.

    **Who may**: any authenticated caller (the router's dependency). The corpus
    is the register of what this instance's rooms are made of, and a person who
    can log in and upload bytes can say where they came from — while the
    ATTRIBUTOR is taken from the token, so nobody signs a statement in somebody
    else's name. A finer rule (a role over the register) is a decision about
    curation, not a line of code, and it is not invented here.

    Idempotent, like the acts themselves: the same acquisition twice is one lot,
    and a client that retries after a timeout does not double anything.
    """
    who = _caller(request)
    # A RESIDENT file is one this instance HOLDS. Checking it here is what makes
    # the register a description of the store rather than of somebody's intention:
    # a digest nobody uploaded would sit in the corpus carrying a licence for
    # bytes that are not there, and the asset gate would look it up for ever.
    if str(act.act).strip().lower() == "resource":
        residency = (act.residency or "resident").strip().lower()
        ref = str(act.checksum or "")
        if residency == "resident":
            if not asset_ref_valid(ref):
                raise HTTPException(
                    status_code=400,
                    detail=f"not an asset reference: {ref!r} (expected "
                           f"'sha256:<hex>')")
            if ASSET_STORE.head(ref) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"no asset {ref} in this instance's store: register a "
                           f"file after uploading it (or declare it "
                           f"`residency: reference` if the bytes live elsewhere)")
    try:
        report, version = RESIDENT.append(act.act,
                                         act.model_dump(exclude_none=True),
                                         author=who)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LookupError as exc:
        # the library's way of saying "that file is not in here"
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return CorpusAppendOut(act=act.act, report=report, version=version, by=who)


@v1.post("/corpus/merge", response_model=CorpusAppendOut, tags=["corpus"])
def merge_corpus_endpoint(request: Request,
                          graph: Dict[str, Any] = Body(...)) -> CorpusAppendOut:
    """Fold a project's **file** corpus into the resident one — the promote path.

    Offline is not a lesser mode: a study documented on a laptop carries its DTC
    inside its own em.json, and joining a room must bring that documentation with
    it. Additive and per-UUID (`s3dgraphy.api.merge_corpus`, the container's own
    merge), so promoting twice does not duplicate and two people who photographed
    the same stone end up with one entry.

    The body is the corpus **member section** (`{graph_id, nodes, edges, data}`),
    or a container from which it is taken.
    """
    who = _caller(request)
    section = graph
    if isinstance(graph, dict) and isinstance(graph.get("graphs"), dict):
        section = next((g for g in graph["graphs"].values()
                        if isinstance(g, dict)
                        and em.is_dtc_corpus(g)), None)
        if section is None:
            raise HTTPException(
                status_code=400,
                detail="this container has no DTC corpus member: nothing to merge "
                       "(a member is a corpus by its `data.em_collection` marker, "
                       "not by being called `dtc`)")
    if not isinstance(section, dict) or "nodes" not in section:
        raise HTTPException(status_code=400,
                            detail="expected a corpus graph section with `nodes`")
    report, version = RESIDENT.merge(section, author=who)
    return CorpusAppendOut(act="merge", report=report, version=version, by=who)


# ── IIIF · the authorisation the image server does not have ───────────────────
#
# Cantaloupe reads MinIO by sha256 and em-server is NOT in the path of a pixel.
# Taking the canvas out of the manifest withholds the digest — which is real,
# because the manifest is where a digest comes from — but somebody who already
# has the hex reaches the image directly. That is the hole this closes.
#
# The shape: the proxy in front of the image server asks em-server first
# (`forward_auth`), for every `/iiif/*` request. em-server stays the authority —
# it is the process that can read the graph, resolve the room's ACL and apply
# exactly the rule the asset route applies. Cantaloupe keeps knowing nothing
# about rights, which is right: an image server should serve images.
#
# The alternative was a Cantaloupe DELEGATE script (Ruby, calling back here).
# Rejected for two reasons: it puts a second implementation of the rule in a
# second language inside a component whose job is not authorisation, and it is
# invisible to anybody reading the routing — the place people look when they ask
# "who can reach this". `forward_auth` is one stanza in the file that already
# decides what reaches what.

@v1_public.get("/iiif-authz", tags=["iiif"], responses={
    200: {"description": "let it through"},
    403: {"description": "under embargo for this caller"},
    503: {"description": "the rights cannot be read right now"},
})
async def iiif_authz(request: Request, response: Response,
                     token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """May the caller of THIS image request have it? For a proxy to ask.

    Answers about the request the proxy is holding, which it hands over in the
    headers it copies (`X-Forwarded-Uri` / `X-Original-Uri`): the IIIF
    identifier is the bare sha256, so the digest is in the path. No body, no
    guessing — the same digest the asset route would be asked for.

    **The same rule as `get_asset`, from the same code.** While an embargo runs
    the image is for editor and above; everybody else gets 403. Two rules for
    one question would be one rule and one bug.

    Unauthenticated by design: it is a question about somebody ELSE's request,
    and the answer for a caller with no token is exactly the answer a caller
    with no token should get.
    """
    digest = _digest_from_iiif_path(
        request.headers.get("x-forwarded-uri")
        or request.headers.get("x-original-uri")
        or request.query_params.get("uri") or "")
    if not digest:
        # Nothing that looks like a digest: this is not an image request we can
        # judge (`info.json` of something else, a static asset). Letting it
        # through is correct — the gate exists for images, and refusing what it
        # cannot read would break the image server for everything else.
        response.headers["X-EM-Authz"] = "not-an-image-request"
        return {"ok": True, "reason": "no digest in the request"}

    # The SAME walk the asset route uses, and deliberately the same function:
    # two implementations of "may this person see this picture" would be one
    # rule and one bug, and the bug would be the one nobody tested.
    room_id = (request.query_params.get("room")
               or request.headers.get("x-em-room") or "").strip()
    if room_id:
        await _rights_seen_anywhere(room_id, digest, request)
        checked = 1
    else:
        rooms_to_ask = _rooms_holding(digest)
        checked = len(rooms_to_ask)
        for candidate in rooms_to_ask:
            await _rights_seen_anywhere(candidate, digest, request)
    # …and the resident documentation, ONCE and even when no room mentions this
    # picture at all: an image published from the register is still an image the
    # register can embargo, and a gate that only looked at rooms would let it
    # through precisely when nobody had opened the study.
    await _corpus_gate(digest, request, door=room_id or None, room_rights=None)
    response.headers["X-EM-Authz"] = "ok"
    return {"ok": True, "digest": digest, "rooms_checked": checked}


def _digest_from_iiif_path(path: str) -> Optional[str]:
    """The sha256 out of a IIIF URL, or None.

    `/iiif/3/<identifier>/full/max/0/default.jpg` — and the identifier is the
    bare hex, because that is what Cantaloupe uses as the object key (measured,
    the night the `sha256%3A` form 404'd). Anything else in that position is not
    ours to judge.
    """
    import re
    import urllib.parse

    for part in urllib.parse.unquote(path).split("/"):
        candidate = part.rsplit(":", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{64}", candidate.lower()):
            return candidate.lower()
    return None


def _identity_of(request: Request) -> Optional[str]:
    """The caller's ORCID from an OPTIONAL token, or None. Never an error: an
    asset URL is fetched by things that cannot log in, and a stranger is a valid
    answer to "who is this"."""
    header = request.headers.get("authorization") or ""
    token = request.query_params.get("token")
    claims: Dict[str, Any] = {}
    try:
        if header.lower().startswith("bearer "):
            claims = authenticator.require_token(request)
        elif token:
            claims = authenticator.verify(str(token).strip())
    except Exception:  # noqa: BLE001 — a bad token is simply not an identity
        claims = {}
    if claims.get("em_dev_mode"):
        return None
    return (claims.get("orcid") or claims.get("preferred_username")
            or claims.get("sub"))


async def _role_without_bootstrap(room_id: str, orcid: Optional[str],
                                  request: Request):
    """The caller's role in a room, READING the grants and claiming none.

    `ws.authorize` also performs the owner bootstrap — the first authenticated
    arrival in an unowned room becomes its owner — which is right when somebody
    opens a room they made and wrong as an authorisation check: it turns a room
    name typed into a URL into a role. This is the read-only half: the ACL as
    stored, plus the owner the STUDY declares, and nothing written back.
    """
    from . import access

    acl = load_acl(room_id)
    room = await rooms().get(room_id)
    if acl.owner is None:
        declared = access.owner_from_document(room.document)
        if declared:
            acl = Acl(owner=declared, members=acl.as_dict().get("members"),
                      groups=acl.as_dict().get("groups"))
    return access.role_of(acl, orcid, room.visibility, embargo=room.embargo,
                          groups_of=groups().expander())


def _rooms_holding(digest: str) -> List[str]:
    """Which rooms might have something to say about this digest.

    A IIIF request carries no room — the identifier is the content itself — so
    the question is asked of every room this instance holds. That is honest for
    a single-instance deployment and it is stated as a limit for a sharded one:
    a room owned by another replica cannot be consulted from here, and the
    remedy is the `?room=` the proxy can be told to pass.

    Live rooms first, then whatever the store has: a room that is open is a room
    somebody is working in, and its document is the most recent.
    """
    live = rooms().rooms()
    store = snapshot_store()
    stored = []
    try:
        stored = [r for r in (store.rooms() if hasattr(store, "rooms") else [])
                  if r not in live]
    except Exception:  # pragma: no cover — a store that cannot list is not fatal
        stored = []
    candidates = list(live) + stored
    # …and the INDEX narrows it. Only the rooms that mention this digest are
    # worth reading; the rest cannot have anything to say about it. A room the
    # index has not read is kept in the list — "not read" must never pass for
    # "says nothing", which is the difference between an index and a hole.
    registry = rooms()
    return DIGEST_INDEX.rooms_for(
        digest, [(room_id, registry.peek(room_id)) for room_id in candidates])


# ── IIIF: the manifest of a room's image or document ──────────────────────────

# ── URL topology · one house for internal↔public ─────────────────────────────
#
# Every service→service URL in this deployment has TWO forms, and confusing them
# fails opaquely — a 403, an empty body, a mixed-content block. The pairs are
# listed once in `docs/URL-TOPOLOGY.md`; the rule is one line:
#
#     em-server SPEAKS on the internal form and WRITES the public form into the
#     documents it serves.
#
# `EM_IIIF_PUBLIC` is what goes into a manifest (other people's viewers fetch
# it, so it must name a host they can reach). `EM_IIIF_INTERNAL` is how this
# process reaches the same image server to read `info.json` — inside a compose
# network `localhost` is em-server itself, so using the public form here
# measures nothing and every canvas silently gets a placeholder size.
#
# The older `EM_IIIF_BASE` / `EM_IIIF_INTERNAL_BASE` spellings are still read, in
# that order: one setting with two names and a precedence, never two settings
# that will one day disagree.
#
# And the rule has a HARD half, which is the one a request can attack:
#
#     the address em-server DIALS comes only from the CONFIG.
#
# A request parameter may, at most, change the URL WRITTEN INTO the document it
# gets back. Never the outgoing call. The two are easy to conflate because they
# name the same service — that conflation is exactly what `?image_base=` did
# (`internal = IIIF_INTERNAL or base`, so a deployment with no IIIF configured
# dialled whatever host the caller named), and it is why this is now stated
# rather than assumed. `_measure_images` refuses an unconfigured base for the
# same reason: no address is a fact to report, not a gap for a caller to fill.
def _env_url(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    return ""


IIIF_PUBLIC = _env_url("EM_IIIF_PUBLIC", "EM_IIIF_BASE")
IIIF_INTERNAL = _env_url("EM_IIIF_INTERNAL", "EM_IIIF_INTERNAL_BASE") or IIIF_PUBLIC

#: Kept as the old name so nothing else in this module has to change spelling.
IIIF_BASE = IIIF_PUBLIC
IIIF_INTERNAL_BASE = IIIF_INTERNAL


# On the router WITHOUT the blanket auth dependency, and doing the check itself:
# a viewer cannot set a header, so the token has to be allowed in the query — and
# a router-level dependency refuses the request before the handler can look.
@v1_public.get("/rooms/{room_id}/iiif/{target_id}/manifest", tags=["iiif"])
async def iiif_manifest(room_id: str, target_id: str, request: Request,
                        response: Response,
                        image_base: Optional[str] = Query(default=None),
                        token: Optional[str] = Query(default=None)
                        ) -> Dict[str, Any]:
    """A IIIF Presentation 3 manifest for an image or a document in this room.

    The graph is the source and the manifest is a view of it — built by
    `s3dgraphy.api.iiif_manifest`, because that is where the logic belongs
    (rule #1). What em-server adds is the two things a library must not do:

    * it knows the deployment's **public** Image API base;
    * it can **fetch `info.json`** to learn each image's pixel size. A library
      that made HTTP calls would be untestable and would break offline; a server
      that refused to would emit canvases with placeholder dimensions.

    **Public or restricted, per STUDY** (the tiers of D2.2 §3.4). A room whose
    document says `header.visibility: "public"` is the *dissemination* tier —
    validated work, meant to be read by anybody — so its manifest is served
    **without a token**: that is what publishing means. Anything else is
    in-progress and stays behind the token, which is also the DEFAULT, because a
    study served too openly cannot be un-served.

    What the gate actually protects is worth stating, because it is not obvious:
    the image service (Cantaloupe) has no auth of its own, but an image is
    addressed by its **sha256**, and the only place a digest comes from is the
    graph. **The manifest is the capability.** Refuse it and a restricted study's
    assets are unguessable; serve it and you have published them — which is why
    the decision lives with the study rather than in a config file.

    The token may arrive in the header (a program) or in the query (a VIEWER):
    Mirador fetches this URL itself and cannot be asked to set a header, and
    refusing the query would mean no IIIF viewer could open one of our manifests.
    """
    # …and a viewer fetching from its own origin needs CORS. Read-only, and only
    # on this route: a manifest exists to be fetched by other people's software.
    response.headers["Access-Control-Allow-Origin"] = "*"
    base = (image_base or IIIF_PUBLIC).rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="this deployment has no IIIF image service configured: set "
                   "EM_IIIF_PUBLIC (e.g. https://host/iiif/3) or pass "
                   "?image_base=. A manifest pointing at nothing would look "
                   "like a broken image rather than a missing service.")

    room = await rooms().get(room_id)
    if not room.is_public:
        _authorise_manifest(request, token)
    graph, warnings = _room_graph(room)
    # SPEAK on the internal form, WRITE the public one into the document.
    #
    # `?image_base=` chooses what goes INTO the manifest — a caller staging a
    # different public host, for instance. It must NOT change how this process
    # dials the image server: the internal address is a property of the
    # deployment, not of the request. Getting that backwards is how a manifest
    # asked for over https ended up with placeholder canvas sizes, because the
    # server tried to reach itself through the public name.
    #
    # CONFIG ONLY — and no `or base` fallback. With one, a deployment that has
    # no IIIF configured would dial whatever host the caller put in
    # `?image_base=`: the request would be choosing where the server connects,
    # which is the thing this must never allow. Unconfigured means unmeasured;
    # the canvases then carry no size and the library says so.
    sizes = _measure_images(graph, IIIF_INTERNAL) if IIIF_INTERNAL else {}
    try:
        # The whole DOCUMENT goes in, not just the graph: the rights of an image
        # may be stated on a DTC chunk in another section of the container, and a
        # manifest that read only the active graph would publish an embargoed
        # picture because the embargo was written next door.
        manifest = em.iiif_manifest(graph, target_id, image_base=base,
                                    manifest_id=str(request.url).split("?")[0],
                                    sizes=sizes, document=room.document)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if warnings:
        manifest.setdefault("em:warnings", []).extend(warnings)
    return manifest


def _authorise_manifest(request: Request, token: Optional[str]) -> None:
    """Bearer header, or `?token=` — the manifest is read by viewers."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer ") or not token:
        authenticator.require_token(request)
        return
    if not authenticator.settings.enforcing:
        return
    try:
        authenticator.verify(token.strip())
    except HTTPException:
        raise
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(status_code=401,
                            detail=f"token refused: {exc}") from None


def _room_graph(room: Any):
    """The room's ACTIVE graph, loaded through the library's own reader."""
    document = room.document
    graphs = document.get("graphs") or {}
    graph_id = document.get("active_graph_id") or next(iter(graphs), None)
    if not graph_id:
        raise HTTPException(status_code=404,
                            detail=f"room {room.room_id!r} holds no graph yet")
    graph, warnings = em.load_emjson({"header": document.get("header", {}),
                                      "graph": graphs[graph_id]})
    return graph, list(warnings)


def _measure_images(graph: Any, base: str) -> Dict[str, Any]:
    """Ask the image server how big each image actually is.

    One `info.json` per image, and a failure is not fatal: an image the service
    cannot answer for gets no entry, and the library then says so in the
    manifest rather than pretending. A missing size costs an aspect ratio; a
    failed request must not cost the whole manifest.

    `base` is the INTERNAL address and comes from the config — the guard below
    is the second lock on the same door as the caller's: this function must not
    become the place where a request-supplied host gets dialled just because
    somebody passed it in one day.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from s3dgraphy.iiif import image_identifier, is_image

    if not base:
        return {}
    sizes: Dict[str, Any] = {}
    for node in graph.nodes:
        if getattr(node, "node_type", "") != "resource" or not is_image(node):
            continue
        identifier = image_identifier(node)
        if not identifier:
            continue
        try:
            with urllib.request.urlopen(f"{base}/{identifier}/info.json",
                                        timeout=4) as answer:
                info = _json.loads(answer.read())
            sizes[node.node_id] = (int(info["width"]), int(info["height"]))
        except (urllib.error.URLError, KeyError, ValueError, OSError):
            continue
    return sizes


# ── validate ──────────────────────────────────────────────────────────────────

@v1.post("/validate", tags=["graph"])
def validate(doc: Dict[str, Any] = Body(..., description="an em.json document")
             ) -> Dict[str, Any]:
    """Header/format conformance plus stats — `api.validate`.

    The load warnings travel in the RESPONSE rather than to a log: they are about
    the caller's document, and a service that keeps them to itself makes the client
    guess why a graph behaves oddly.
    """
    graph, warnings = _load(doc)
    report = em.validate(graph)
    return {"ok": True, "report": report, "warnings": list(warnings)}


# ── RDF / CIDOC projection ────────────────────────────────────────────────────

@v1.post("/export-ttl", tags=["graph"],
         responses={200: {"content": {"text/turtle": {}}}})
def export_ttl(doc: Dict[str, Any] = Body(..., description="an em.json document"),
               base_uri: Optional[str] = Query(None)) -> Response:
    """em.json → Turtle (`api.project_ttl`), as `text/turtle`.

    Same media type and same `Content-Disposition` as em-bridge, so a browser that
    downloads from the sidecar downloads identically from here. 501 without rdflib.
    """
    graph, _warnings = _load(doc)
    try:
        ttl = em.project_ttl(graph, base_uri=base_uri)
    except em.MissingDependency as exc:
        raise _missing_dependency(exc, "TTL export", "rdflib") from exc
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"TTL export failed: {exc}") from exc
    graph_id = (doc.get("graph") or {}).get("graph_id") or "graph"
    filename = f"{graph_id}.ttl".replace("/", "_")
    return Response(
        content=ttl,
        media_type="text/turtle",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── coordinate reprojection ───────────────────────────────────────────────────

class ReprojectRequest(BaseModel):
    """One point or many, exactly as em-bridge accepts them."""
    x: Optional[float] = None
    y: Optional[float] = None
    points: Optional[List[List[float]]] = None
    epsg_source: int
    epsg_target: int = 4326


@v1.post("/reproject", tags=["geo"])
def reproject(req: ReprojectRequest) -> Dict[str, Any]:
    """EPSG → EPSG via `api.reproject_many` (pyproj).

    The batch form builds ONE transformer, which is why a footprint should be sent
    as `points` rather than as five requests. 501 without pyproj — and the caller
    is then expected to refuse honestly rather than guess, which is what EMStudio's
    map does.
    """
    if req.points is not None:
        if not req.points:
            raise HTTPException(status_code=400,
                               detail="'points' must be a non-empty list of [x, y]")
        if len(req.points) > 512:
            # A footprint is a handful of corners. The cap stops this becoming a
            # projection service by accident.
            raise HTTPException(status_code=400,
                                detail="at most 512 points per request")
        try:
            pts = [(float(p[0]), float(p[1])) for p in req.points]
        except (TypeError, ValueError, IndexError) as exc:
            raise HTTPException(
                status_code=400,
                detail="each point must be an [x, y] pair of numbers") from exc
    elif req.x is None or req.y is None:
        raise HTTPException(status_code=400,
                            detail="send {x, y} or {points: [[x, y], …]}")
    else:
        pts = [(req.x, req.y)]

    try:
        out = em.reproject_many(pts, req.epsg_source, req.epsg_target)
    except em.MissingDependency as exc:
        raise _missing_dependency(exc, "reprojection", "pyproj (the [geo] extra)") from exc
    except ValueError as exc:
        # An unknown EPSG, or a point outside the frame's domain: the client asked
        # something impossible and should hear which.
        raise HTTPException(status_code=400, detail=f"reproject failed: {exc}") from exc

    payload: Dict[str, Any] = {
        "ok": True,
        "epsg_source": req.epsg_source,
        "epsg_target": req.epsg_target,
        "points": [[x, y] for x, y in out],
    }
    if req.points is None:
        x, y = out[0]
        # Named axes for the single-point form, so no caller has to remember
        # whether [0] was the longitude (it is).
        if req.epsg_target == 4326:
            payload["lon"], payload["lat"] = x, y
        else:
            payload["x"], payload["y"] = x, y
    return payload


# ── authority resolution ──────────────────────────────────────────────────────

def _resolve_authority(term: str, facet: str) -> Dict[str, Any]:
    facets = em.authority_facets()
    if not facets:
        raise HTTPException(status_code=501,
                            detail="authority resolver unavailable in this build")
    if (facet or "").upper() not in facets:
        raise HTTPException(
            status_code=400,
            detail=f"unknown facet {facet!r}; expected one of {sorted(facets)}")
    return {"ok": True, "term": term, "facet": facet.upper(),
            "candidates": em.resolve_authority(term, facet)}


@v1.get("/resolve-authority", tags=["authority"])
def resolve_authority_get(term: str = Query(...), facet: str = Query(...)
                          ) -> Dict[str, Any]:
    """Ranked offline authority candidates. GET, for a link or a curl."""
    return _resolve_authority(term, facet)


class AuthorityRequest(BaseModel):
    term: str
    facet: str


@v1.post("/resolve-authority", tags=["authority"])
def resolve_authority_post(req: AuthorityRequest) -> Dict[str, Any]:
    """The same op as POST — em-bridge offers both verbs and so does this."""
    return _resolve_authority(req.term, req.facet)


# ── who may work in a room (P5 · access control) ──────────────────────────────
#
# The relay enforces the roles; these routes are how they get handed out. They
# are deliberately small: an ACL is a map from ORCID to role, and every rule
# beyond that lives in `access.py::may_assign`, in one place, so a refusal here
# and a refusal at the door cannot disagree.
#
# Every refusal is a **403 with the reason**. "Forbidden" on its own is what
# makes people file bugs against a working access-control system.

class MemberIn(BaseModel):
    role: str = Field(description="viewer · editor · admin · owner")


class MemberOut(BaseModel):
    orcid: str
    role: str


class Members(BaseModel):
    room: str
    owner: Optional[str] = None
    members: List[MemberOut] = Field(default_factory=list)
    #: what the CALLER may do here — so a UI can draw the right buttons without
    #: a second request, and without guessing from the absence of an error
    your_role: Optional[str] = None


async def _acting_role(room_id: str, request: Request) -> tuple:
    """(acl, room, the caller's role) — or a 403 saying why not.

    Resolved exactly the way the WebSocket door resolves it (same `authorize`),
    because two implementations of "who are you here" is one more than the
    number that can stay right.
    """
    principal = authenticator.require_token(request)
    dev_mode = bool(principal.get("em_dev_mode"))
    orcid = None if dev_mode else (principal.get("orcid")
                                   or principal.get("preferred_username")
                                   or principal.get("sub"))
    room = await rooms().get(room_id)
    role = authorize(room, orcid, dev_mode=dev_mode)
    return load_acl(room_id), room, role, orcid


@v1.get("/rooms/{room_id}/members", response_model=Members, tags=["access"])
async def list_members(room_id: str, request: Request) -> Members:
    """Who may work in this room. Owner and admins only.

    Not public, and not for editors either: a membership list is a list of the
    people working on an unpublished study, which is somebody's business rather
    than everybody's.
    """
    acl, _room, role, _who = await _acting_role(room_id, request)
    if role is None or not role.can_manage:
        raise HTTPException(status_code=403,
                            detail="reading the member list needs admin or owner")
    return Members(room=room_id, owner=acl.owner,
                   members=[MemberOut(orcid=k, role=v.value)
                            for k, v in sorted(acl.members.items())],
                   your_role=role.value)


@v1.put("/rooms/{room_id}/members/{orcid}", response_model=Members, tags=["access"])
async def set_member(room_id: str, orcid: str, body: MemberIn,
                     request: Request) -> Members:
    """Give somebody a role — or move the room to a new owner.

    `owner` is a **transfer**, not a second owner: a room has one, the previous
    owner is left as an admin (losing the room should not lock the person who
    built it out of it), and only the owner may do it.
    """
    acl, room, role, _who = await _acting_role(room_id, request)
    wanted = parse_role(body.role)
    if wanted is None:
        raise HTTPException(status_code=400,
                            detail=f"unknown role {body.role!r}: "
                                   f"expected viewer, editor, admin or owner")
    target = orcid.strip()
    current = acl.members.get(target) or (Role.OWNER if acl.owner == target else None)
    refusal = may_assign(role, current, wanted)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)

    if wanted is Role.OWNER:
        previous = acl.owner
        acl.owner = target
        acl.members.pop(target, None)
        if previous and previous != target:
            acl.members[previous] = Role.ADMIN
        # the study carries its owner too (that is where an owner belongs), so
        # the header follows the ACL rather than drifting behind it
        header = room.document.setdefault("header", {})
        if isinstance(header, dict):
            header["owner"] = target
    else:
        if acl.owner == target:
            raise HTTPException(
                status_code=403,
                detail="the owner cannot be demoted; transfer the room first")
        acl.members[target] = wanted
    save_acl(room_id, acl)
    return Members(room=room_id, owner=acl.owner,
                   members=[MemberOut(orcid=k, role=v.value)
                            for k, v in sorted(acl.members.items())],
                   your_role=role.value)


@v1.delete("/rooms/{room_id}/members/{orcid}", response_model=Members,
           tags=["access"])
async def remove_member(room_id: str, orcid: str, request: Request) -> Members:
    """Revoke a role. Removing somebody who has none is not an error — the
    requested state (this person has no grant here) is the state that results."""
    acl, _room, role, _who = await _acting_role(room_id, request)
    target = orcid.strip()
    if acl.owner == target:
        raise HTTPException(status_code=403,
                            detail="the owner cannot be removed; transfer the "
                                   "room to somebody else first")
    refusal = may_assign(role, acl.members.get(target), None)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    acl.members.pop(target, None)
    save_acl(room_id, acl)
    return Members(room=room_id, owner=acl.owner,
                   members=[MemberOut(orcid=k, role=v.value)
                            for k, v in sorted(acl.members.items())],
                   your_role=role.value)


# ── groups: a name for a set of people ────────────────────────────────────────
#
# A room grants a role to a NAME ("the excavation team") instead of to six
# ORCIDs it would then have to keep in step with reality. The registry is
# instance-wide; the grant stays in each room's ACL, where it belongs.
#
# Who may manage one: **whoever made it**. The same arrangement rooms use, and
# for the same reason — somebody has to be able to fix it, and "everybody" is
# not an answer. There is no global administrator in this service and inventing
# one here would be a bigger decision than a group list deserves.

class GroupIn(BaseModel):
    id: str = Field(description="short stable id, e.g. `scavo-2026`")
    name: str = Field(default="", description="how humans read it")
    members: List[str] = Field(default_factory=list)


class GroupOut(BaseModel):
    id: str
    name: str
    owner: Optional[str] = None
    members: List[str] = Field(default_factory=list)


def _caller(request: Request) -> Optional[str]:
    principal = authenticator.require_token(request)
    if principal.get("em_dev_mode"):
        return None            # dev mode has no identities; see `access.py`
    return (principal.get("orcid") or principal.get("preferred_username")
            or principal.get("sub"))


def _may_manage(group: Group, who: Optional[str], request: Request) -> None:
    """The owner of a group manages it. Dev mode manages everything, because dev
    mode has no identities to distinguish."""
    if not authenticator.settings.enforcing:
        return
    if group.owner and who and group.owner == who:
        return
    raise HTTPException(
        status_code=403,
        detail=f"the group {group.id!r} is managed by whoever created it "
               f"({group.owner or 'nobody recorded'})")


@v1.get("/groups", response_model=List[GroupOut], tags=["access"])
def list_groups(request: Request) -> List[GroupOut]:
    """Every group this instance knows. Readable by any authenticated caller:
    a group is a name for a team, not a secret, and you cannot be granted a role
    through one you are not allowed to see the name of."""
    authenticator.require_token(request)
    return [GroupOut(**g.as_dict()) for g in sorted(groups().all().values(),
                                                    key=lambda g: g.id)]


@v1.put("/groups/{group_id}", response_model=GroupOut, tags=["access"])
def put_group(group_id: str, body: GroupIn, request: Request) -> GroupOut:
    """Create a group, or replace the one you own.

    Idempotent: the same call twice leaves one group with those members. The
    creator becomes the owner, and only they may replace it afterwards.
    """
    who = _caller(request)
    registry = groups()
    existing = registry.get(group_id)
    if existing is not None:
        _may_manage(existing, who, request)
    group = Group(group_id, body.name or (existing.name if existing else group_id),
                  body.members, owner=(existing.owner if existing else who))
    registry.put(group)
    return GroupOut(**group.as_dict())


@v1.put("/groups/{group_id}/members/{orcid}", response_model=GroupOut,
        tags=["access"])
def add_group_member(group_id: str, orcid: str, request: Request) -> GroupOut:
    who = _caller(request)
    registry = groups()
    group = registry.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"no group {group_id!r}")
    _may_manage(group, who, request)
    group.add(orcid)
    registry.put(group)
    return GroupOut(**group.as_dict())


@v1.delete("/groups/{group_id}/members/{orcid}", response_model=GroupOut,
           tags=["access"])
def remove_group_member(group_id: str, orcid: str, request: Request) -> GroupOut:
    """Take somebody out. Removing who is not there is not an error — the state
    asked for is the state that results."""
    who = _caller(request)
    registry = groups()
    group = registry.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"no group {group_id!r}")
    _may_manage(group, who, request)
    group.remove(orcid)
    registry.put(group)
    return GroupOut(**group.as_dict())


@v1.delete("/groups/{group_id}", tags=["access"])
def delete_group(group_id: str, request: Request) -> Dict[str, Any]:
    """Drop the group. The grants that named it stay in the rooms' ACLs and stop
    resolving — reported rather than chased: a server that walked every room to
    scrub a name would be doing a migration nobody asked for, and the grant is
    visible in the room's member list, where somebody can decide."""
    who = _caller(request)
    registry = groups()
    group = registry.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"no group {group_id!r}")
    _may_manage(group, who, request)
    return {"ok": registry.drop(group_id), "group": group_id,
            "note": "grants naming this group stay in the rooms' ACLs and now "
                    "resolve to nothing"}


app.include_router(v1_public)
app.include_router(v1)
#: P4.2 · the relay. A router of its own because it is a different KIND of thing:
#: everything above is stateless request/response, and this holds connections. It
#: authenticates in the handshake rather than through the router dependency —
#: a WebSocket has no place to put a 401 body, so the refusal is a close code.
app.include_router(ws_router)


@app.get("/health", response_model=Health, tags=["meta"],
         summary="Unversioned probe alias — same payload as /v1/health")
def health_probe() -> Health:
    """The orchestrator's health check.

    Deliberately outside `/v1`: a Docker HEALTHCHECK, a k8s probe or a Caddy
    upstream check is infrastructure, and it must not need editing when the API
    version moves. Same function, same payload — there is nothing to keep in sync.
    """
    return health()


# ── what this build deliberately does NOT do ──────────────────────────────────
# No write op, no upload, no asset store, no WebSocket. Each is a phase of its own
# with a decision attached (which bucket layout, which conflict policy), and
# shipping a placeholder for any of them would be worse than the absence: a stub
# endpoint gets called.
#
#   P1 — DONE: Keycloak bearer tokens on the shared realm (`app/auth.py`). ORCID as
#        the user identity is configured in the realm, not here.
#   P2 — MinIO assets: the same stable-ID resolver s3Dgraphy already has
#   P3 — the op-log WebSocket (ADR-002: one host per session, CRDT later)
#   P4 — deployment (WP6)


def main() -> None:  # pragma: no cover — entry point
    """`python -m app.main` for a quick local run; production uses uvicorn."""
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("EM_SERVER_HOST", "127.0.0.1"),
                port=int(os.environ.get("EM_SERVER_PORT", "8000")), reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
