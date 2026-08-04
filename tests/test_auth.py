"""P1 — the bearer-token gate, exercised against a locally signed realm.

No Keycloak here, and no mock of the verification either: a real RSA keypair is
generated, a real JWKS document is published (over ``file://``, which
``urllib.request`` fetches exactly the way it fetches ``https://``), and real tokens
are signed. What is faked is the *realm*, not the crypto — because the crypto is
the thing under test.

The end-to-end run against 3DR's actual realm is E.D.'s, and it needs the shared
configuration (see the mail to Romano). Everything that can be pinned without it is
pinned here, and these are the failures worth pinning:

* **the open fallback.** A deployment with a typo in one variable must not come up
  unprotected. Half-configured refuses to boot; that is the single most consequential
  line in `auth.py` and it gets its own test.
* **the algorithm confusion.** A verifier that honours the token's own ``alg``
  header lets the token choose how it is checked — ``alg: none``, or an HMAC signed
  with the public key. Both are refused before a key is even looked up.
* **401 vs 403.** "Go and get a token" and "your token is genuine but not for this
  service" are different instructions. Returning 401 for a wrong audience sends a
  client round a refresh loop that cannot help it.
* **the probe stays reachable.** If `/health` needed a token, a Docker HEALTHCHECK
  would report the service dead the moment auth was switched on.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
# Imported at MODULE level, not inside the fixture, and that matters: this file has
# `from __future__ import annotations`, so every annotation is a string and FastAPI
# resolves it against the defining module's globals. A `Request` visible only in an
# enclosing function scope does not resolve, and FastAPI then reads the parameter as
# a query parameter — a 422 that looks nothing like an auth problem.
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.auth import Authenticator, OidcSettings, load_settings

ISSUER = "https://keycloak.example.org/realms/heriverse"
AUDIENCE = "em"


# ── a realm we control ────────────────────────────────────────────────────────

def _keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_file(tmp_path: Path, *keys) -> str:
    """Publish public keys as a JWKS at a ``file://`` URI.

    A real fetch of a real document: the cache's HTTP path, its JSON parsing and
    its key construction all run. Standing up an HTTPS server would test urllib,
    not this module.
    """
    from jwt.algorithms import RSAAlgorithm

    entries = []
    for kid, key in keys:
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        entries.append(jwk)
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": entries}), encoding="utf-8")
    return f"file://{path}"


def _token(key, *, kid="test-1", audience=AUDIENCE, issuer=ISSUER,
           expires_in=300, algorithm="RS256", scope=None, **extra):
    import jwt

    now = int(time.time())
    claims = {
        "sub": "user-1",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        **extra,
    }
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, key, algorithm=algorithm,
                      headers={"kid": kid} if kid else {})


@pytest.fixture
def realm(tmp_path):
    """An enforcing authenticator plus the key its tokens must be signed with."""
    key = _keypair()
    settings = OidcSettings(issuer=ISSUER,
                            jwks_uri=_jwks_file(tmp_path, ("test-1", key)),
                            audience=AUDIENCE)
    return Authenticator(settings), key


@pytest.fixture
def client(realm):
    """A one-route app behind the real dependency.

    Small on purpose: what is under test here is the HTTP surface of the gate —
    the status codes and the ``WWW-Authenticate`` header as a client sees them —
    and importing `main.py` would drag in s3Dgraphy's endpoints for no gain. The
    real wiring is checked separately, in
    `test_the_real_app_protects_v1_and_leaves_health_open`.
    """
    authenticator, _key = realm

    def guard(request: Request) -> dict:
        return authenticator.require_token(request)

    app = FastAPI()

    @app.get("/v1/thing")
    def thing(claims: dict = Depends(guard)) -> dict:
        return {"claims": claims}

    return TestClient(app, raise_server_exceptions=False)


# ── a good token ──────────────────────────────────────────────────────────────

def test_a_valid_token_is_accepted(realm):
    authenticator, key = realm
    claims = authenticator.verify(_token(key))
    assert claims["sub"] == "user-1"
    assert claims["aud"] == AUDIENCE


def test_the_jwks_is_fetched_once_and_reused(realm):
    """The cache exists so a burst of requests does not become a burst of calls to
    the identity provider — which would make em-server a load source on the thing
    everything else also depends on."""
    authenticator, key = realm
    authenticator.verify(_token(key))
    fetched_at = authenticator._jwks._fetched_at
    authenticator.verify(_token(key))
    assert authenticator._jwks._fetched_at == fetched_at


def test_an_unknown_kid_refetches_so_rotation_works(realm, tmp_path):
    """Key rotation. A token signed with a key minted a minute ago must verify;
    waiting out the TTL would reject every request until it expired."""
    authenticator, old_key = realm
    authenticator.verify(_token(old_key))          # populate the cache

    new_key = _keypair()
    Path(authenticator._jwks.uri[len("file://"):]).write_text(
        json.dumps(json.loads(Path(
            authenticator._jwks.uri[len("file://"):]).read_text())), )
    # republish with BOTH keys, as a rotating realm does
    authenticator._jwks.uri = _jwks_file(
        tmp_path, ("test-1", old_key), ("test-2", new_key))
    claims = authenticator.verify(_token(new_key, kid="test-2"))
    assert claims["sub"] == "user-1"
    # and the old key still verifies, which is the point of overlapping keys
    assert authenticator.verify(_token(old_key))["sub"] == "user-1"


# ── bad tokens, each rejected for its own reason ──────────────────────────────

def test_an_expired_token_is_401(realm):
    from fastapi import HTTPException

    authenticator, key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(_token(key, expires_in=-30))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_a_token_signed_by_a_stranger_is_401(realm):
    """Same kid, different key: the signature must not verify. This is the check
    everything else rests on, so it is asserted directly rather than assumed."""
    from fastapi import HTTPException

    authenticator, _key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(_token(_keypair()))
    assert exc.value.status_code == 401


def test_a_wrong_audience_is_403_not_401(realm):
    """The token is genuine and the caller is who they say they are — it was
    issued for another service. A 401 would tell them to re-authenticate, which
    cannot fix it."""
    from fastapi import HTTPException

    authenticator, key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(_token(key, audience="heriverse"))
    assert exc.value.status_code == 403
    assert "audience" in exc.value.detail


def test_a_wrong_issuer_is_401(realm):
    from fastapi import HTTPException

    authenticator, key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(
            _token(key, issuer="https://evil.example.org/realms/heriverse"))
    assert exc.value.status_code == 401


def test_alg_none_is_refused_before_a_key_is_looked_up(realm):
    """The classic. A verifier that reads the token's `alg` lets the token decide
    how it is verified; `none` then means "do not verify at all"."""
    import jwt
    from fastapi import HTTPException

    authenticator, _key = realm
    unsigned = jwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE,
                           "exp": int(time.time()) + 60},
                          key=None, algorithm=None, headers={"kid": "test-1"})
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(unsigned)
    assert exc.value.status_code == 401
    assert "algorithm" in exc.value.detail


def test_an_hmac_token_is_refused(realm):
    """HMAC confusion: sign with HS256 using the realm's PUBLIC key as the shared
    secret. A verifier that trusts the header's algorithm would validate it — the
    public key is public, so anyone can mint one.

    Assembled by hand rather than with ``jwt.encode``: PyJWT refuses to *sign* an
    HMAC with a PEM public key (a good guard on its own), and the attack this
    describes does not need PyJWT's cooperation to construct.
    """
    import base64
    import hashlib
    import hmac
    import json as _json

    from cryptography.hazmat.primitives import serialization
    from fastapi import HTTPException

    authenticator, key = realm
    secret = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(_json.dumps({"alg": "HS256", "typ": "JWT",
                              "kid": "test-1"}).encode())
    payload = b64(_json.dumps({"sub": "x", "iss": ISSUER, "aud": AUDIENCE,
                               "exp": int(time.time()) + 60}).encode())
    signing_input = header + b"." + payload
    signature = b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(HTTPException) as exc:
        authenticator.verify(forged)
    assert exc.value.status_code == 401
    assert "algorithm" in exc.value.detail


def test_a_token_without_a_kid_is_401(realm):
    from fastapi import HTTPException

    authenticator, key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(_token(key, kid=None))
    assert exc.value.status_code == 401
    assert "kid" in exc.value.detail


def test_garbage_is_401_not_500(realm):
    """A malformed Authorization header is a client error. A 500 here would put a
    scan of the open internet into somebody's error budget."""
    from fastapi import HTTPException

    authenticator, _key = realm
    with pytest.raises(HTTPException) as exc:
        authenticator.verify("not-a-jwt-at-all")
    assert exc.value.status_code == 401


def test_a_missing_scope_is_403(tmp_path):
    from fastapi import HTTPException

    key = _keypair()
    authenticator = Authenticator(OidcSettings(
        issuer=ISSUER, jwks_uri=_jwks_file(tmp_path, ("test-1", key)),
        audience=AUDIENCE, required_scope="em:write"))
    assert authenticator.verify(
        _token(key, scope="openid em:write email"))["sub"] == "user-1"
    with pytest.raises(HTTPException) as exc:
        authenticator.verify(_token(key, scope="openid email"))
    assert exc.value.status_code == 403
    assert "em:write" in exc.value.detail


# ── over HTTP: statuses, headers, and the probe ───────────────────────────────

def test_a_request_without_a_token_is_401_with_a_challenge(client):
    response = client.get("/v1/thing")
    assert response.status_code == 401
    # The challenge is what tells a client to go and authenticate rather than
    # give up.
    assert response.headers.get("www-authenticate") == "Bearer"


def test_a_request_with_a_token_passes(client, realm):
    _authenticator, key = realm
    response = client.get("/v1/thing",
                          headers={"Authorization": f"Bearer {_token(key)}"})
    assert response.status_code == 200
    assert response.json()["claims"]["sub"] == "user-1"


def test_a_non_bearer_scheme_is_401(client, realm):
    _authenticator, key = realm
    response = client.get("/v1/thing",
                          headers={"Authorization": f"Basic {_token(key)}"})
    assert response.status_code == 401


def test_the_real_app_protects_v1_and_leaves_health_open(monkeypatch, tmp_path):
    """The wiring in `main.py`, not a stand-in: `/v1/validate` closed, both health
    paths open. If the probe needed a token, every orchestrator would call the
    service dead the day auth was turned on."""
    key = _keypair()
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URI", _jwks_file(tmp_path, ("test-1", key)))
    import importlib

    from app import auth as auth_module
    importlib.reload(auth_module)
    from app import main as main_module
    importlib.reload(main_module)
    try:
        with TestClient(main_module.app) as probe:
            assert probe.get("/health").status_code == 200
            assert probe.get("/v1/health").status_code == 200
            assert probe.get("/health").json()["auth"] == "keycloak"

            assert probe.post("/v1/validate", json={}).status_code == 401
            ok = probe.post(
                "/v1/validate", json={"graph": {"nodes": [], "edges": []}},
                headers={"Authorization": f"Bearer {_token(key)}"})
            assert ok.status_code in (200, 400), ok.text
    finally:
        # Leave the modules as the rest of the suite expects them: dev mode.
        for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URI"):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(auth_module)
        importlib.reload(main_module)


# ── configuration: the three states ──────────────────────────────────────────

def test_no_configuration_is_dev_mode(caplog):
    """P0 has to keep working on a laptop with no Keycloak."""
    settings = load_settings({})
    assert not settings.enforcing
    assert settings.describe() == "dev-no-auth"


def test_dev_mode_says_so_loudly(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="em-server.auth"):
        load_settings({})
    assert any("OPEN" in r.message or "OPEN" in str(r.args)
               for r in caplog.records), caplog.text


def test_half_configured_refuses_to_start():
    """The one that matters. A typo in one variable must not produce a server
    everybody believes is protected."""
    with pytest.raises(RuntimeError) as exc:
        load_settings({"OIDC_ISSUER": ISSUER})           # no audience
    assert "half-configured" in str(exc.value)

    with pytest.raises(RuntimeError) as exc:
        load_settings({"OIDC_AUDIENCE": AUDIENCE})       # no issuer
    assert "half-configured" in str(exc.value)


def test_the_issuer_is_derived_from_heriverse_s_token_endpoint():
    """The alignment that keeps two services on one realm: 3DR already deploys
    TOKEN_ENDPOINT, so em-server must not require a second spelling of the same
    URL. Two variables for one fact is how configurations drift."""
    settings = load_settings({
        "TOKEN_ENDPOINT":
            "http://keycloak:8080/auth/realms/heriverse/protocol/"
            "openid-connect/token",
        "CLIENT_ID_em": "em",
    })
    assert settings.issuer == "http://keycloak:8080/auth/realms/heriverse"
    assert settings.jwks_uri == ("http://keycloak:8080/auth/realms/heriverse"
                                 "/protocol/openid-connect/certs")
    assert settings.audience == "em"
    assert settings.enforcing


def test_a_keycloak_17_style_path_also_works():
    """Keycloak ≤16 served `/auth/realms/…`, 17+ serves `/realms/…`. The suffix is
    stripped rather than the prefix guessed, so both deployments work and neither
    needs a special case."""
    settings = load_settings({
        "TOKEN_ENDPOINT": "https://kc.example.org/realms/heriverse/protocol/"
                          "openid-connect/token",
        "OIDC_AUDIENCE": "em",
    })
    assert settings.issuer == "https://kc.example.org/realms/heriverse"


def test_a_token_endpoint_that_is_not_one_is_reported():
    """Rather than silently deriving a wrong issuer, which would fail later as a
    signature error and send somebody debugging the wrong thing."""
    with pytest.raises(RuntimeError) as exc:
        load_settings({"TOKEN_ENDPOINT": "https://kc.example.org/whatever",
                       "OIDC_AUDIENCE": "em"})
    assert "token endpoint" in str(exc.value)


def test_an_explicit_issuer_wins_over_the_derived_one():
    settings = load_settings({
        "OIDC_ISSUER": "https://explicit.example.org/realms/x",
        "TOKEN_ENDPOINT": "https://derived.example.org/realms/y/protocol/"
                          "openid-connect/token",
        "OIDC_AUDIENCE": "em",
    })
    assert settings.issuer == "https://explicit.example.org/realms/x"


def test_the_anon_flag_cannot_open_a_configured_server(caplog):
    """A contradiction in the environment resolves towards SAFETY, and says so.
    Guessing which of the two the operator meant is not this module's job."""
    import logging

    with caplog.at_level(logging.WARNING, logger="em-server.auth"):
        settings = load_settings({
            "OIDC_ISSUER": ISSUER,
            "OIDC_AUDIENCE": AUDIENCE,
            "EM_SERVER_ALLOW_ANON": "1",
        })
    assert settings.enforcing
    assert "IGNORING" in caplog.text


def test_dev_mode_hands_a_principal_shaped_like_a_real_one():
    """So a handler that wants to know who is calling reads the same shape in both
    modes instead of branching on whether auth is on."""


    authenticator = Authenticator(OidcSettings())
    request = Request({"type": "http", "headers": []})
    claims = authenticator.require_token(request)
    assert claims["sub"] == "anonymous"
    assert claims["em_dev_mode"] is True
