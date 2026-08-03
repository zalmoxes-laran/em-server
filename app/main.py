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
v1 = APIRouter(prefix="/v1")


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


@v1.get("/health", response_model=Health, tags=["meta"])
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
    )


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


app.include_router(v1)


@app.get("/health", response_model=Health, tags=["meta"],
         summary="Unversioned probe alias — same payload as /v1/health")
def health_probe() -> Health:
    """The orchestrator's health check.

    Deliberately outside `/v1`: a Docker HEALTHCHECK, a k8s probe or a Caddy
    upstream check is infrastructure, and it must not need editing when the API
    version moves. Same function, same payload — there is nothing to keep in sync.
    """
    return health()


# ── what P0 deliberately does NOT do ──────────────────────────────────────────
# No write op, no upload, no asset store, no auth, no WebSocket. Each is a phase
# of its own with a decision attached (which identity provider, which bucket
# layout, which conflict policy), and shipping a placeholder for any of them would
# be worse than the absence: a stub endpoint gets called.
#
#   P1 — Keycloak + ORCID (with 3DR, on the shared Heriverse-Docker infra)
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
