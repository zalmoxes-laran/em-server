"""STEP 1 — the asset store: a room provides a graph AND the bytes it points at.

Four claims:

  1  ROUNDTRIP   what goes in comes out, byte for byte
  2  DEDUP       the reference IS the digest, so the same bytes are one object
  3  AUTH        publishing needs a token; the uploader is the TOKEN's identity
  4  NO LOGIC    the asset module is transport — it computes nothing about a graph
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest
from fastapi.testclient import TestClient

_REPO = pathlib.Path(__file__).resolve().parent.parent

from app import assets                                            # noqa: E402
from app import main as main_module                              # noqa: E402
from app.assets import (DirectoryAssetStore, InMemoryAssetStore,  # noqa: E402
                        asset_ref_valid, content_id)
from app.main import app                                          # noqa: E402

MODEL = b"glTF\x02\x00\x00\x00fake binary model bytes"
OTHER = b"a different model"


@pytest.fixture(autouse=True)
def fresh_assets(monkeypatch):
    store = InMemoryAssetStore()
    monkeypatch.setattr(main_module, "ASSET_STORE", store)
    return store


@pytest.fixture
def client():
    return TestClient(app)


# ── 1 · roundtrip ───────────────────────────────────────────────────────────

def test_1_what_goes_in_comes_out_byte_for_byte(client, fresh_assets):
    put = client.put("/v1/rooms/scavo/asset?media_type=model/gltf-binary",
                     content=MODEL)
    assert put.status_code == 200
    info = put.json()
    assert info["size"] == len(MODEL)
    assert info["media_type"] == "model/gltf-binary"
    assert info["created"] is True

    got = client.get(f"/v1/rooms/scavo/asset/{info['ref']}")
    assert got.status_code == 200
    assert got.content == MODEL, "the bytes are the bytes"
    assert got.headers["content-type"].startswith("model/gltf-binary")


def test_1b_the_reference_is_the_digest_so_a_client_can_verify(client, fresh_assets):
    info = client.put("/v1/rooms/scavo/asset", content=MODEL).json()
    assert info["ref"] == f"sha256:{hashlib.sha256(MODEL).hexdigest()}", \
        "the name of an asset IS what it contains"
    assert info["sha256"] == hashlib.sha256(MODEL).hexdigest()
    # …which is the whole point: whoever fetches it can check, alone
    got = client.get(f"/v1/rooms/scavo/asset/{info['ref']}")
    assert hashlib.sha256(got.content).hexdigest() == info["sha256"]


def test_1c_an_unknown_or_malformed_reference_is_refused_cleanly(client, fresh_assets):
    missing = client.get(f"/v1/rooms/scavo/asset/{content_id(b'never stored')}")
    assert missing.status_code == 404
    # a reference that could never have been minted here does not reach the store:
    # a directory-backed implementation would be one `../` from somebody's file
    for bad in ("../../etc/passwd", "sha256:zz", "md5:abc"):
        assert client.get(f"/v1/rooms/scavo/asset/{bad}").status_code in (400, 404)
    assert not asset_ref_valid("../../etc/passwd")
    assert asset_ref_valid(content_id(b"x"))


def test_1d_an_empty_body_is_a_bad_request_not_an_empty_object(client, fresh_assets):
    assert client.put("/v1/rooms/scavo/asset", content=b"").status_code == 400


# ── 2 · dedup ───────────────────────────────────────────────────────────────

def test_2_the_same_bytes_are_one_object(client, fresh_assets):
    first = client.put("/v1/rooms/scavo/asset", content=MODEL).json()
    second = client.put("/v1/rooms/scavo/asset", content=MODEL).json()
    assert first["ref"] == second["ref"]
    assert first["created"] is True and second["created"] is False, \
        "the second upload is not an error and not a duplicate — it is the same object"
    assert fresh_assets.count() == 1, "one object in the store"

    client.put("/v1/rooms/scavo/asset", content=OTHER)
    assert fresh_assets.count() == 2, "different bytes are a different object"


def test_2b_a_room_does_not_own_the_bytes_only_names_them(client, fresh_assets):
    """Content-addressing means two rooms that publish the same model share it.
    Stated as a test because it is a property somebody could 'fix' by accident."""
    a = client.put("/v1/rooms/scavo/asset", content=MODEL).json()
    b = client.put("/v1/rooms/altro-scavo/asset", content=MODEL).json()
    assert a["ref"] == b["ref"] and fresh_assets.count() == 1


def test_2c_the_directory_store_writes_bytes_and_dedups_too(tmp_path):
    store = DirectoryAssetStore(tmp_path)
    first = store.put(MODEL, "model/gltf-binary")
    second = store.put(MODEL, "model/gltf-binary")
    assert first["ref"] == second["ref"]
    assert first["created"] and not second["created"]
    assert store.get(first["ref"]) == MODEL
    assert store.head(first["ref"])["media_type"] == "model/gltf-binary"
    files = [p for p in pathlib.Path(tmp_path).rglob("*") if p.is_file()]
    assert len([f for f in files if f.suffix != ".type"]) == 1, "one object on disk"


# ── 3 · auth ────────────────────────────────────────────────────────────────

def test_3_publishing_needs_a_token(monkeypatch, client, fresh_assets):
    class Enforcing:
        enforcing = True

    monkeypatch.setattr(main_module.authenticator, "settings", Enforcing())
    refused = client.put("/v1/rooms/scavo/asset", content=MODEL)
    assert refused.status_code == 401, "no token, no publishing"
    assert fresh_assets.count() == 0, "…and nothing was stored on the way to the 401"


def test_3b_the_uploader_is_the_token_not_a_field(monkeypatch, client, fresh_assets):
    class Enforcing:
        enforcing = True

    monkeypatch.setattr(main_module.authenticator, "settings", Enforcing())
    monkeypatch.setattr(main_module.authenticator, "verify",
                        lambda token: {"orcid": "0000-0002-1825-0097"})
    info = client.put("/v1/rooms/scavo/asset", content=MODEL,
                      headers={"Authorization": "Bearer whatever"}).json()
    assert info["author"] == "0000-0002-1825-0097", \
        "who published is what the token says, not what the client claimed"


# ── 4 · the repo's rule ─────────────────────────────────────────────────────

def test_4_the_asset_module_adds_no_logic():
    """Rule 1, extended to the newest module. There is nothing to COMPUTE about
    a blob beyond hashing it, and the hashing belongs to the store."""
    source = (_REPO / "app" / "assets.py").read_text(encoding="utf-8")
    for forbidden in ("from s3dgraphy", "import s3dgraphy"):
        assert forbidden not in source, \
            "assets.py is a byte store: it must not know what a graph is"


def test_4b_the_production_store_is_named_and_honest():
    source = (_REPO / "app" / "assets.py").read_text(encoding="utf-8")
    assert "class MinioAssetStore" in source, "the deployment target is named"
    assert "NotImplementedError" in source, "…and it fails with a sentence"


def test_4c_health_says_where_the_bytes_live(client):
    payload = client.get("/health").json()
    assert "asset_store" in payload
    assert payload["asset_store"].startswith(("memory", "directory", "minio"))


def test_an_object_store_it_cannot_serve_stops_the_process():
    """STEP 5 · the deploy declares MinIO before the implementation exists.

    A process that read that configuration and quietly wrote to a local
    directory instead would look healthy while putting the institution's assets
    somewhere nobody backs up — silent until it is expensive.
    """
    with pytest.raises(NotImplementedError) as exc:
        assets.asset_store_from_env({"EM_ASSET_S3_ENDPOINT": "http://minio:9000",
                                     "EM_ASSET_DIR": "/srv/em-data/assets"})
    assert "not implemented" in str(exc.value)
    assert "EM_ASSET_DIR" in str(exc.value)      # …and says what to do instead
