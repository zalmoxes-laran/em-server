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
from pydantic import BaseModel, Field

from fastapi import Request

from .assets import ASSET_STORE, asset_ref_valid
from .assets import describe as asset_describe
from .auth import AuthDependency, authenticator
from .store import describe as snapshot_describe
from .ws import ROOMS, SNAPSHOT_STORE, ws_router

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
        snapshot_store=snapshot_describe(SNAPSHOT_STORE),
        asset_store=asset_describe(ASSET_STORE),
        rooms=len(ROOMS.rooms()),
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
def get_asset(room_id: str, ref: str) -> Response:
    """Fetch an asset by reference. The caller can verify what it got: the
    reference IS the digest."""
    if not asset_ref_valid(ref):
        raise HTTPException(status_code=400,
                            detail=f"not an asset reference: {ref!r} "
                                   f"(expected 'sha256:<hex>')")
    data = ASSET_STORE.get(ref)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no asset {ref}")
    meta = ASSET_STORE.head(ref) or {}
    return Response(content=data,
                    media_type=str(meta.get("media_type") or "application/octet-stream"),
                    headers={"ETag": f'"{ref}"'})


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

    room = await ROOMS.get(room_id)
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
    internal = IIIF_INTERNAL or base
    sizes = _measure_images(graph, internal)
    try:
        manifest = em.iiif_manifest(graph, target_id, image_base=base,
                                    manifest_id=str(request.url).split("?")[0],
                                    sizes=sizes)
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
    """
    import json as _json
    import urllib.error
    import urllib.request

    from s3dgraphy.iiif import image_identifier, is_image

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
