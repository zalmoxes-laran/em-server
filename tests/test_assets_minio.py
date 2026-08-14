"""`MinioAssetStore` against a REAL MinIO — the deployment store, exercised.

Not a mock, and deliberately not `moto`: what can go wrong in this class is the
S3 client's own behaviour (how a missing key is reported, whether a `stat` costs
a download, what `content_type` survives a round trip), and a fake that we wrote
would agree with whatever we assumed. So these tests talk to the MinIO of the
dev stack.

    cd dev-stack && docker compose --env-file .env.dev -f docker-compose.dev.yml up -d minio minio-init
    pytest tests/test_assets_minio.py

They **skip** — loudly, with the reason — when that MinIO is not reachable or the
`minio` client is not installed, so the suite stays runnable on a machine with no
Docker. A skip here means "not measured", never "passed".
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import uuid

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.assets import MinioAssetStore, content_id  # noqa: E402

ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.environ.get("MINIO_ROOT_USER", "minioadmin")
SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")

pytest.importorskip("minio", reason="the `minio` client is not installed "
                                    "(pip install em-server[s3])")


def _reachable() -> bool:
    host, _, port = ENDPOINT.split("://", 1)[-1].partition(":")
    try:
        with socket.create_connection((host, int(port or 9000)), timeout=1.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason=f"no MinIO at {ENDPOINT} — start the dev stack "
           f"(dev-stack/README-DEV.md) to measure this")


@pytest.fixture()
def store():
    """A store on a bucket of its own, removed afterwards.

    Its own bucket because these tests write, and a test that leaves objects in
    the bucket the dev stack uses would make the next run's dedup assertions
    depend on the previous run.
    """
    bucket = f"em-test-{uuid.uuid4().hex[:10]}"
    store = MinioAssetStore(endpoint=ENDPOINT, access_key=ACCESS_KEY,
                            secret_key=SECRET_KEY, bucket=bucket, secure=False)
    try:
        yield store
    finally:
        client = store._client
        for obj in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, obj.object_name)
        client.remove_bucket(bucket)


# ── the interface, against the real thing ───────────────────────────────────

def test_put_then_get_is_byte_exact(store):
    payload = b"\x00\x01glTF binary-ish bytes\xff" * 64
    info = store.put(payload, "model/gltf-binary")
    assert info["created"] is True
    assert store.get(info["ref"]) == payload


def test_the_object_key_is_the_digest(store):
    payload = b"the key is the content"
    info = store.put(payload, "text/plain")
    assert info["ref"] == content_id(payload)
    # …and the object really is under that name in the bucket
    key = info["ref"].split(":", 1)[1]
    assert store._client.stat_object(store.bucket, key).size == len(payload)


def test_the_same_bytes_twice_are_one_object(store):
    payload = b"dedup is a property of the naming, not of a check"
    first, second = store.put(payload, "text/plain"), store.put(payload, "text/plain")
    assert first["ref"] == second["ref"]
    assert second["created"] is False
    assert len(list(store._client.list_objects(store.bucket, recursive=True))) == 1


def test_head_answers_without_downloading(store):
    """A hundred-megabyte asset must not have to travel to answer "is it there"."""
    payload = b"x" * 100_000
    info = store.put(payload, "image/png")
    meta = store.head(info["ref"])
    assert meta["size"] == len(payload)
    assert meta["media_type"] == "image/png"
    assert meta["sha256"] == info["sha256"]


def test_a_reference_nobody_stored_is_absent_not_an_error(store):
    missing = "sha256:" + "0" * 64
    assert store.get(missing) is None
    assert store.head(missing) is None


def test_two_different_contents_are_two_objects(store):
    a = store.put(b"one", "text/plain")
    b = store.put(b"two", "text/plain")
    assert a["ref"] != b["ref"]
    assert store.get(a["ref"]) == b"one" and store.get(b["ref"]) == b"two"


# ── refusing what it cannot honour ──────────────────────────────────────────

def test_a_store_that_cannot_reach_its_bucket_refuses_to_be_built():
    """At construction, with a sentence — not at the first upload, from inside
    somebody's request."""
    with pytest.raises(RuntimeError) as exc:
        MinioAssetStore(endpoint="http://127.0.0.1:1", access_key="k",
                        secret_key="s", bucket="nowhere", secure=False)
    assert "127.0.0.1:1" in str(exc.value)


def test_wrong_credentials_are_a_startup_failure_too(store):
    with pytest.raises(RuntimeError) as exc:
        MinioAssetStore(endpoint=ENDPOINT, access_key="not-the-key",
                        secret_key="not-the-secret", bucket=store.bucket,
                        secure=False)
    message = str(exc.value)
    assert store.bucket in message
    assert "credentials" in message      # names the likely cause, not a code
