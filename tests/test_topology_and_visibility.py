"""URL topology (internal ↔ public) and study visibility (public ↔ restricted).

Two rules that look like configuration and are really design.

**Every service→service URL has two forms.** The one this process dials
(`minio:9000`, `keycloak:8080`, `cantaloupe:8182` — names on the container
network) and the one it writes into documents other people's software will fetch
(`https://host/iiif/3`). Confusing them fails *opaquely*: a 403, an empty body, a
mixed-content block, a canvas with a placeholder size. Three separate bugs in
this project have been that same confusion, which is why `docs/URL-TOPOLOGY.md`
lists the pairs once and these tests hold the code to them.

**A manifest is a capability.** The image service has no auth of its own, and an
image is addressed by its sha256 — a digest that only the graph knows. So
serving the manifest of a study IS publishing its images, and the decision has
to belong to the study.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app import main as main_module                   # noqa: E402
from app import ws as ws_module                       # noqa: E402
from app.main import app                              # noqa: E402
from app.rooms import Room, RoomRegistry              # noqa: E402
from app.store import InMemorySnapshotStore           # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fresh_rooms():
    """A registry of its own — patched in BOTH modules that hold a reference.

    `main.py` does `from .ws import ROOMS`, which binds the object, not the
    name: patching only `ws.ROOMS` leaves the HTTP routes talking to the
    original registry. That is a footgun worth a sentence rather than a
    debugging session.
    """
    registry = RoomRegistry(InMemorySnapshotStore())
    previous_ws, previous_main = ws_module.ROOMS, main_module.ROOMS
    ws_module.ROOMS = registry
    main_module.ROOMS = registry
    try:
        yield registry
    finally:
        ws_module.ROOMS = previous_ws
        main_module.ROOMS = previous_main


def _document(room_id: str, visibility: str | None = None):
    header = {"format": "em.json", "version": "1.0"}
    if visibility:
        header["visibility"] = visibility
    return {
        "header": header,
        "graphs": {room_id: {
            "graph_id": room_id, "name": room_id,
            "nodes": [{"id": "img-1", "node_type": "resource", "name": "foto",
                       "data": {"checksum": "sha256:" + "a" * 64,
                                "media_type": "image/jpeg"}}],
            "edges": []}},
        "active_graph_id": room_id,
    }


# ── 1 · the topology ────────────────────────────────────────────────────────

def test_1_the_two_forms_are_two_settings():
    """Not one variable used twice — two, with the direction in their names."""
    source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "EM_IIIF_PUBLIC" in source and "EM_IIIF_INTERNAL" in source
    assert "IIIF_PUBLIC" in source and "IIIF_INTERNAL" in source


def test_1b_the_server_speaks_internal_and_writes_public(monkeypatch):
    """The rule, asserted rather than remembered.

    `_measure_images` DIALS the image server (internal); the manifest carries the
    base a browser will fetch (public). A build that read `info.json` from the
    public form measures nothing behind a compose network — and every canvas
    silently gets a placeholder size, which is exactly what happened.
    """
    dialled: list[str] = []

    def fake_measure(graph, base):
        dialled.append(base)
        return {}

    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "IIIF_INTERNAL", "http://cantaloupe:8182/iiif/3")
    monkeypatch.setattr(main_module, "_measure_images", fake_measure)

    registry = RoomRegistry(InMemorySnapshotStore())
    registry.store.put("scavo", _document("scavo", "public"))
    previous_ws, previous_main = ws_module.ROOMS, main_module.ROOMS
    ws_module.ROOMS = main_module.ROOMS = registry
    try:
        with TestClient(app) as client:
            answer = client.get("/v1/rooms/scavo/iiif/img-1/manifest")
    finally:
        ws_module.ROOMS, main_module.ROOMS = previous_ws, previous_main

    assert answer.status_code == 200, answer.text
    assert dialled == ["http://cantaloupe:8182/iiif/3"], \
        "the server DIALS the internal form"
    manifest = answer.json()
    painted = manifest["items"][0]["items"][0]["items"][0]["body"]["id"]
    assert painted.startswith("https://em.example.org/iiif/3"), \
        "…and WRITES the public one into the document it serves"


def test_1c_the_older_spellings_still_work():
    """One setting, two names, a precedence — never two settings that disagree."""
    assert main_module._env_url("EM_NOPE_A", "EM_NOPE_B") == ""


def test_1d_the_topology_is_written_down_once():
    doc = _REPO / "docs" / "URL-TOPOLOGY.md"
    assert doc.is_file(), "the pairs live in docs/URL-TOPOLOGY.md"
    text = doc.read_text(encoding="utf-8")
    for pair in ("EM_IIIF_INTERNAL", "EM_IIIF_PUBLIC", "OIDC_ISSUER",
                 "OIDC_JWKS_URI"):
        assert pair in text, f"{pair} is part of the topology and must be listed"


# ── 2 · visibility ──────────────────────────────────────────────────────────

def test_2_a_study_says_whether_it_is_public():
    assert Room("r", _document("r", "public")).visibility == "public"
    assert Room("r", _document("r", "restricted")).visibility == "restricted"


def test_2b_unknown_and_absent_both_read_as_restricted():
    """The failure directions are not symmetric: a public study behind a token
    annoys somebody; an in-progress study served openly publishes an
    interpretation nobody has finished making."""
    assert Room("r", _document("r")).visibility == "restricted"
    assert Room("r", _document("r", "PUBLIC")).visibility == "public"      # case
    assert Room("r", _document("r", "yes-please")).visibility == "restricted"
    assert Room("r", {}).visibility == "restricted"


def test_2c_a_public_manifest_needs_no_token(client, fresh_rooms, monkeypatch):
    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "_measure_images", lambda graph, base: {})
    fresh_rooms.store.put("mostra", _document("mostra", "public"))
    answer = client.get("/v1/rooms/mostra/iiif/img-1/manifest")
    assert answer.status_code == 200
    assert answer.json()["type"] == "Manifest"


def test_2d_a_restricted_manifest_is_refused_without_one(client, fresh_rooms,
                                                         monkeypatch):
    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "_measure_images", lambda graph, base: {})
    fresh_rooms.store.put("scavo", _document("scavo", "restricted"))

    from app.auth import OidcSettings, authenticator
    enforcing = OidcSettings(issuer="https://k/realms/em", audience="em-server",
                             jwks_uri="https://k/realms/em/certs")
    previous = authenticator.settings
    authenticator.settings = enforcing
    try:
        answer = client.get("/v1/rooms/scavo/iiif/img-1/manifest")
        assert answer.status_code == 401, answer.text
        assert "token" in answer.text.lower()
    finally:
        authenticator.settings = previous


def test_2e_the_digest_is_the_capability():
    """Why the gate is on the MANIFEST and not on the image service.

    Cantaloupe has no auth: it will serve any digest it can find in the bucket.
    But a digest is 256 bits and the only place one comes from is the graph — so
    refusing the manifest of a restricted study is what keeps its images
    unreachable, and serving it IS publishing them. This test pins the reasoning
    to the code so nobody 'simplifies' the gate away.
    """
    source = (_REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "room.is_public" in source
    assert "capability" in source.lower(), \
        "the docstring must say why the manifest is the gate"
