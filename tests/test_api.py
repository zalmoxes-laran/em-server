"""em-server's endpoints, and the properties that make it a *thin* wrapper.

Two things are worth testing in a service that is deliberately logic-free, and
they are not "does FastAPI work":

* **the answer is the library's.** `/export-ttl` must return exactly what
  `s3dgraphy.api.project_ttl` returns — byte for byte — because the moment the
  service reshapes a result, "the same API over HTTP" stops being true and two
  implementations start drifting. Same reasoning for the reprojection numbers.
* **a missing optional dependency is 501, never 500.** The request was valid and
  the build simply cannot do that op; a client can degrade honestly from a 501 and
  cannot from a 500.
"""

import pathlib
import sys

import pytest

# The import paths (repo root + the s3Dgraphy checkout ahead of any wheel) are set
# by tests/conftest.py, which runs before this module is imported.
_REPO = pathlib.Path(__file__).resolve().parent.parent

pytest.importorskip("fastapi", reason="needs the [dev] extra")
pytest.importorskip("httpx", reason="needs the [dev] extra")
pytest.importorskip("s3dgraphy", reason="s3dgraphy not importable")

from fastapi.testclient import TestClient  # noqa: E402
from s3dgraphy import api as em  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

FIXTURE = (_REPO.parent / "s3Dgraphy" / "tests" / "fixtures"
           / "PortaMarina-lite.em.json")


def _doc():
    import json
    if not FIXTURE.is_file():
        pytest.skip("s3Dgraphy checkout fixture not available")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _has(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


# ── health ────────────────────────────────────────────────────────────────────

def test_health_says_what_this_build_can_do():
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "em-server"
    assert body["version"]
    # capabilities is the useful part: a client learns what works WITHOUT having
    # to discover a 501 by trying.
    caps = body["capabilities"]
    assert caps["validate"] is True
    assert caps["export_ttl"] == _has("rdflib")
    assert caps["reproject"] == _has("pyproj")


# ── validate ──────────────────────────────────────────────────────────────────

def test_validate_returns_the_librarys_report_and_the_warnings():
    doc = _doc()
    response = client.post("/v1/validate", json=doc)
    assert response.status_code == 200
    body = response.json()
    graph, warnings = em.load_emjson(doc)
    assert body["report"] == em.validate(graph)
    # the warnings are about the CALLER's document, so they travel back rather
    # than into a log the caller cannot read
    assert body["warnings"] == list(warnings)


def test_a_malformed_document_is_a_400_not_a_500():
    """It is the client's document, and 500 would send someone reading server
    logs for a bad upload."""
    response = client.post("/v1/validate", json={"not": "an em.json"})
    assert response.status_code == 400
    assert "em.json" in response.json()["detail"]


# ── TTL: the same bytes as the library ────────────────────────────────────────

@pytest.mark.skipif(not _has("rdflib"), reason="rdflib not installed")
def test_export_ttl_is_byte_identical_to_the_library():
    """The property that makes this a wrapper and not a second implementation.
    (Same reason the bridge and em-server agree: both call project_ttl.)"""
    doc = _doc()
    response = client.post("/v1/export-ttl", json=doc)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/turtle")
    graph, _warnings = em.load_emjson(doc)
    assert response.text == em.project_ttl(graph)


@pytest.mark.skipif(not _has("rdflib"), reason="rdflib not installed")
def test_the_download_filename_follows_the_graph_id():
    response = client.post("/v1/export-ttl", json=_doc())
    assert 'filename="portamarina_lite.ttl"' in \
        response.headers["content-disposition"]


@pytest.mark.skipif(_has("rdflib"), reason="only meaningful without rdflib")
def test_without_rdflib_ttl_is_501_and_says_what_is_missing():
    response = client.post("/v1/export-ttl", json=_doc())
    assert response.status_code == 501
    assert "rdflib" in response.json()["detail"]


# ── reprojection ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _has("pyproj"), reason="pyproj not installed")
def test_reproject_matches_the_library_and_names_its_axes():
    """Easting 500 000 in UTM zone 33 IS the zone's central meridian (15°E) — a
    definition, not a table lookup. If this is 15, PROJ found its data."""
    response = client.post("/v1/reproject", json={
        "x": 500000, "y": 4649776.22, "epsg_source": 32633})
    assert response.status_code == 200
    body = response.json()
    assert body["lon"] == pytest.approx(15.0, abs=1e-9)
    assert body["lat"] == pytest.approx(42.0, abs=1e-6)
    assert body["points"][0] == [body["lon"], body["lat"]]
    assert tuple(body["points"][0]) == em.reproject(500000.0, 4649776.22, 32633)


@pytest.mark.skipif(not _has("pyproj"), reason="pyproj not installed")
def test_the_batch_form_takes_a_whole_footprint():
    points = [[291960.0, 4640631.0], [291990.0, 4640631.0], [291990.0, 4640661.0]]
    response = client.post("/v1/reproject", json={
        "points": points, "epsg_source": 32633})
    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 3
    # the single-point axis names are absent in the batch form: there is no one
    # lon/lat to name
    assert "lon" not in body
    assert [tuple(p) for p in body["points"]] == \
        em.reproject_many([(x, y) for x, y in points], 32633)


@pytest.mark.skipif(not _has("pyproj"), reason="pyproj not installed")
def test_an_impossible_request_is_a_400():
    response = client.post("/v1/reproject", json={"x": 1, "y": 2,
                                               "epsg_source": 999999})
    assert response.status_code == 400


def test_a_request_with_neither_point_nor_points_is_a_400():
    response = client.post("/v1/reproject", json={"epsg_source": 32633})
    assert response.status_code == 400


def test_the_batch_is_capped():
    """A footprint is a handful of corners; the cap stops this becoming a
    projection service by accident."""
    response = client.post("/v1/reproject", json={
        "points": [[0, 0]] * 513, "epsg_source": 32633})
    assert response.status_code == 400
    assert "512" in response.json()["detail"]


@pytest.mark.skipif(_has("pyproj"), reason="only meaningful without pyproj")
def test_without_pyproj_reproject_is_501():
    response = client.post("/v1/reproject", json={"x": 1, "y": 2,
                                               "epsg_source": 32633})
    assert response.status_code == 501
    assert "pyproj" in response.json()["detail"]


# ── authority ─────────────────────────────────────────────────────────────────

def test_authority_accepts_both_verbs_like_the_bridge():
    facets = em.authority_facets()
    if not facets:
        pytest.skip("authority resolver unavailable in this build")
    facet = facets[0]
    got = client.get("/v1/resolve-authority", params={"term": "roma", "facet": facet})
    posted = client.post("/v1/resolve-authority", json={"term": "roma", "facet": facet})
    assert got.status_code == posted.status_code == 200
    assert got.json() == posted.json()
    assert got.json()["facet"] == facet.upper()


def test_an_unknown_facet_is_a_400_that_lists_the_real_ones():
    if not em.authority_facets():
        pytest.skip("authority resolver unavailable in this build")
    response = client.get("/v1/resolve-authority",
                          params={"term": "roma", "facet": "NOPE"})
    assert response.status_code == 400
    assert "expected one of" in response.json()["detail"]


# ── the architectural rule, as a test ─────────────────────────────────────────

def test_em_server_adds_no_logic():
    """FastAPI lives only here, and s3Dgraphy stays pure — but the corollary is
    the one worth pinning: this app must not COMPUTE anything.

    Read as a heuristic on the source: every endpoint's real work is a call into
    `em.<op>`, so the module must not import s3dgraphy's internals (its nodes,
    exporters, importers) — only the api surface. The day somebody reaches past
    `api` is the day logic starts leaking into the transport.
    """
    source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    for forbidden in ("from s3dgraphy.nodes", "from s3dgraphy.exporter",
                      "from s3dgraphy.importer", "from s3dgraphy.graph"):
        assert forbidden not in source, \
            f"{forbidden}: em-server must go through s3dgraphy.api only"


def test_it_is_stateless():
    """No endpoint may keep the caller's document. Same request twice → same
    answer, and nothing accumulates."""
    doc = _doc()
    first = client.post("/v1/validate", json=doc).json()
    second = client.post("/v1/validate", json=doc).json()
    assert first == second


def test_the_openapi_schema_is_served():
    """It is the contract 3DR will read for P1, so it has to exist and name the
    endpoints P0 promises."""
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert {"/v1/health", "/v1/validate", "/v1/export-ttl", "/v1/reproject",
            "/v1/resolve-authority"} <= paths
    # P0 was read-only. A ROOM is not: it holds a study's graph and the bytes it
    # points at, so publishing an asset is a PUT — the one write this service
    # has, and it is content-addressed (the same bytes are the same object), so
    # it creates rather than modifies.
    #
    # What stays forbidden is what would make the service a mutable store:
    # PATCH (change something in place) and DELETE (remove it). Nothing here
    # takes anything away — the graph's own deletions are tombstones inside a
    # document, not HTTP verbs.
    writes = {(verb.upper(), path) for path, ops in schema["paths"].items()
              for verb in ops if verb in ("put", "patch", "delete")}
    assert writes == {("PUT", "/v1/rooms/{room_id}/asset")}, \
        f"unexpected write endpoints: {sorted(writes)}"


# ── versioning: the promise the path makes (P0.1) ─────────────────────────────

def test_every_api_route_is_under_v1():
    """`/v1` is the contract 3DR builds against, so nothing may sit beside it by
    accident. The ONE exception is the unversioned health probe, and it is an
    exception on purpose — see the next test."""
    schema = client.get("/openapi.json").json()
    unversioned = [p for p in schema["paths"] if not p.startswith("/v1")]
    assert unversioned == ["/health"], unversioned


def test_the_unversioned_probe_mirrors_the_versioned_one():
    """A Docker HEALTHCHECK, a k8s probe and a Caddy upstream check belong to the
    infrastructure: they must not need editing the day the API version moves. Same
    payload, so there is nothing to keep in sync."""
    probe = client.get("/health")
    versioned = client.get("/v1/health")
    assert probe.status_code == versioned.status_code == 200
    assert probe.json() == versioned.json()


def test_an_unprefixed_api_route_is_a_404():
    """Stated so a client that forgets the prefix hears about it immediately,
    rather than reaching a silently-aliased route that will vanish at v2."""
    assert client.post("/validate", json=_doc()).status_code == 404
    assert client.post("/reproject", json={"x": 1, "y": 2,
                                           "epsg_source": 4326}).status_code == 404
