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
    """A registry of its own — ONE place to patch, now.

    This used to have to be set in two modules: `main.py` did
    `from .ws import ROOMS`, which binds the object rather than the name, so
    patching only `ws.ROOMS` left the HTTP routes talking to the original
    registry. The footgun is gone — `main.rooms()` resolves `ws.ROOMS` when it
    is asked — and this fixture is one line shorter for it.
    """
    registry = RoomRegistry(InMemorySnapshotStore())
    previous_ws = ws_module.ROOMS
    ws_module.ROOMS = registry
    try:
        yield registry
    finally:
        ws_module.ROOMS = previous_ws


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
    previous_ws = ws_module.ROOMS
    ws_module.ROOMS = registry
    try:
        with TestClient(app) as client:
            answer = client.get("/v1/rooms/scavo/iiif/img-1/manifest")
    finally:
        ws_module.ROOMS = previous_ws

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


# ── 3 · no request parameter may drive the INTERNAL fetch ───────────────────
#
# The same class of bug as the wire's `source`: one name doing two jobs. Here
# the two jobs are "which URL goes into the document" (public, and a caller may
# legitimately have an opinion) and "which host this process connects to"
# (internal, and a caller may not). `?image_base=` did both, and a deployment
# with no IIIF configured would therefore dial whatever host the request named.
# These tests are the enforcement: the parameter keeps its document job and
# loses the other one.

class _FakeInfo:
    """Just enough of an HTTP response for `_measure_images`."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture()
def dialled(monkeypatch):
    """Record every URL the process actually opens — the real seam, not a stub
    of `_measure_images`: what is under test is precisely that nothing between
    the query string and the socket rewrites the host."""
    import urllib.request

    seen: list[str] = []

    def fake_urlopen(url, *_args, **_kwargs):
        seen.append(str(url))
        return _FakeInfo(b'{"width": 1024, "height": 768}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _manifest(client, room, query=""):
    return client.get(f"/v1/rooms/{room}/iiif/img-1/manifest{query}")


def test_3a_a_hostile_image_base_does_not_move_the_internal_fetch(
        client, fresh_rooms, monkeypatch, dialled):
    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "IIIF_INTERNAL", "http://cantaloupe:8182/iiif/3")
    fresh_rooms.store.put("mostra", _document("mostra", "public"))

    answer = _manifest(client, "mostra",
                       "?image_base=http://attacker.example/iiif/3")
    assert answer.status_code == 200, answer.text

    assert dialled, "the server did measure the image"
    for url in dialled:
        assert url.startswith("http://cantaloupe:8182/iiif/3"), \
            f"the request chose where the server connects: {url}"
        assert "attacker.example" not in url

    manifest = answer.json()
    painted = manifest["items"][0]["items"][0]["items"][0]["body"]["id"]
    assert painted.startswith("http://attacker.example/iiif/3"), \
        "the parameter keeps its ONLY job: the URL written into the document"
    canvas = manifest["items"][0]
    assert (canvas["width"], canvas["height"]) == (1024, 768), \
        "…and the size still comes from the internal measurement"


def test_3b_with_nothing_configured_it_dials_nothing_at_all(
        client, fresh_rooms, monkeypatch, dialled):
    """The removed fallback, pinned. `internal = IIIF_INTERNAL or base` looked
    harmless — it meant an unconfigured deployment used the caller's host."""
    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "")
    monkeypatch.setattr(main_module, "IIIF_INTERNAL", "")
    fresh_rooms.store.put("mostra", _document("mostra", "public"))

    answer = _manifest(client, "mostra",
                       "?image_base=http://attacker.example/iiif/3")
    assert answer.status_code == 200, answer.text
    assert dialled == [], "no configured address is a fact, not a gap to fill"


def test_3c_measure_images_refuses_an_unconfigured_base(dialled):
    """Second lock on the same door: even called directly, with the base that a
    caller-supplied value would have produced, it does not dial."""
    assert main_module._measure_images(object(), "") == {}
    assert main_module._measure_images(object(), None) == {}
    assert dialled == []


def test_3d_the_only_outbound_calls_read_their_address_from_config():
    """The audit, kept as a tripwire.

    Two places in this service open a socket to another one: the JWKS fetch and
    the `info.json` measurement. Both take their address from settings read at
    startup. A third one arriving is not forbidden — it just has to arrive with
    this test updated, which is the point.
    """
    import re

    app_dir = _REPO / "app"
    callers = {}
    for path in sorted(app_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"urlopen\(|httpx\.|requests\.(get|post)\(", source):
            callers[path.name] = source
    assert set(callers) == {"auth.py", "main.py"}, \
        f"a new outbound call site appeared: {sorted(callers)}"

    # the JWKS URI is a setting, and `kid` (which DOES come from the token)
    # only picks a key out of what that setting returned
    assert "_JwksCache(self.settings.jwks_uri)" in callers["auth.py"]
    # and the image measurement is handed the config value, with no fallback
    assert "_measure_images(graph, IIIF_INTERNAL) if IIIF_INTERNAL else {}" \
        in callers["main.py"]
    # comments stripped: the removed fallback is NAMED in the comment that
    # explains why it went, and a tripwire that trips on its own explanation
    # teaches people to delete the explanation
    code = "\n".join(line for line in callers["main.py"].splitlines()
                     if not line.lstrip().startswith("#"))
    assert "IIIF_INTERNAL or base" not in code
