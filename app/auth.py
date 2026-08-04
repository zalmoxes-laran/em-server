"""P1 — OIDC bearer-token auth against the shared Keycloak realm.

em-server is a **resource server**: it never logs anybody in. A client obtains an
access token from Keycloak (Heriverse-Server already does the authorization-code
dance for its own users) and presents it here as ``Authorization: Bearer <jwt>``.
This module answers one question per request — *is this token good, and is it for
us* — by checking the signature against the realm's published JWKS.

**Why a resource server and not a login flow.** Two services validating the same
realm's tokens share their users, groups and revocations for free, with no session
store on either side. The moment em-server kept a session it would stop being
horizontally scalable, which is the 12-factor property `main.py` exists to defend.

**Configuration reuses what 3DR already deploys.** Heriverse-Docker sets
``TOKEN_ENDPOINT`` (``…/realms/<realm>/protocol/openid-connect/token``) and
``CLIENT_ID_<app>``. Rather than asking for a second spelling of the same realm
URL — the classic way two configs drift until one is wrong — the issuer and the
JWKS URI are DERIVED from that endpoint when they are not given explicitly. A
deployment that already runs Heriverse can point em-server at the same realm
without inventing a variable.

**Three states, and the middle one is the point:**

* **enforcing** — issuer and audience known: every ``/v1`` route needs a valid
  token.
* **dev / no-auth** — *nothing* OIDC is configured: P0 keeps working locally, with
  a loud warning in the log AND in ``/health``, because a warning nobody reads is
  not a warning.
* **misconfigured** — *some* of it is set and some is not: the process **refuses to
  start**. This is the state that matters. A half-configured deployment that fell
  back to open would be a service everyone believes is protected, and that failure
  is silent, permanent, and discovered by the wrong person.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

log = logging.getLogger("em-server.auth")

#: How long a fetched JWKS is trusted before it is fetched again. Keycloak key
#: rotation is rare, so this is generous; an UNKNOWN `kid` also forces an
#: immediate refetch (see `_JwksCache.key_for`), which is what actually handles a
#: rotation — the TTL only bounds how long a withdrawn key stays usable.
JWKS_TTL_SECONDS = 3600

#: Signature algorithms accepted. An allow-list, not a read of the token's own
#: `alg` header: honouring that header is how `alg: none` and HMAC-confusion
#: attacks work — the token would get to choose how it is verified.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")

_TOKEN_SUFFIX = "/protocol/openid-connect/token"


def _env(*names: str) -> str:
    """First non-empty value among *names*. The order is the precedence: an
    explicit em-server variable wins over one inherited from Heriverse's realm
    configuration."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass
class OidcSettings:
    """What this process will enforce, resolved from the environment once."""

    issuer: str = ""
    jwks_uri: str = ""
    audience: str = ""
    #: Space-separated scope that a token must carry, when required.
    required_scope: str = ""
    #: True when the operator explicitly asked for the open local mode.
    anon_declared: bool = False

    @property
    def enforcing(self) -> bool:
        return bool(self.issuer and self.jwks_uri and self.audience)

    def describe(self) -> str:
        """For `/health` — never includes a secret, because there is none here:
        an issuer and a client id are public by construction."""
        if self.enforcing:
            return "keycloak"
        return "dev-no-auth"


def load_settings(environ: Optional[Dict[str, str]] = None) -> OidcSettings:
    """Resolve the OIDC configuration, or raise on a half-configured one.

    Raises ``RuntimeError`` when the environment is partially set. That is
    deliberate and it is the most important line in this file: falling back to
    "open" on a typo'd variable produces a service that everybody believes is
    authenticated.
    """
    if environ is not None:
        # Tests hand in a mapping instead of mutating os.environ, so a failed
        # assertion cannot leak configuration into the next test.
        saved, os.environ = os.environ, environ  # type: ignore[assignment]
        try:
            return load_settings()
        finally:
            os.environ = saved  # type: ignore[assignment]

    issuer = _env("OIDC_ISSUER")
    token_endpoint = _env("TOKEN_ENDPOINT")
    if not issuer and token_endpoint:
        # Derived, not asked for twice. Keycloak's token endpoint is the issuer
        # plus a fixed suffix, so one variable configures both and they cannot
        # disagree. Works whichever base path the deployment uses (Keycloak ≤16
        # served `/auth/realms/…`, 17+ serves `/realms/…`) because the suffix is
        # what is stripped, not the prefix that is guessed.
        if token_endpoint.endswith(_TOKEN_SUFFIX):
            issuer = token_endpoint[: -len(_TOKEN_SUFFIX)]
        else:
            raise RuntimeError(
                f"TOKEN_ENDPOINT does not look like a Keycloak token endpoint "
                f"(expected it to end with {_TOKEN_SUFFIX!r}): "
                f"{token_endpoint!r}. Set OIDC_ISSUER explicitly instead.")

    jwks_uri = _env("OIDC_JWKS_URI")
    if not jwks_uri and issuer:
        jwks_uri = f"{issuer}/protocol/openid-connect/certs"

    # `CLIENT_ID_em` follows Heriverse's own `CLIENT_ID_<app>` convention, so the
    # shared realm's env file gains one line rather than a new naming scheme.
    audience = _env("OIDC_AUDIENCE", "CLIENT_ID_em", "CLIENT_ID_emserver")
    required_scope = _env("OIDC_REQUIRED_SCOPE")
    anon = _env("EM_SERVER_ALLOW_ANON") in ("1", "true", "yes", "on")

    settings = OidcSettings(issuer=issuer, jwks_uri=jwks_uri, audience=audience,
                            required_scope=required_scope, anon_declared=anon)

    # The partial-configuration refusal.
    present = {"issuer": bool(issuer), "audience": bool(audience)}
    if any(present.values()) and not all(present.values()):
        missing = ", ".join(k for k, v in present.items() if not v)
        given = ", ".join(k for k, v in present.items() if v)
        raise RuntimeError(
            f"OIDC is half-configured: {given} set, {missing} missing. Refusing "
            f"to start rather than falling back to no-auth — an unprotected "
            f"server that looks protected is worse than one that will not boot. "
            f"Set OIDC_AUDIENCE (or CLIENT_ID_em) and OIDC_ISSUER (or "
            f"TOKEN_ENDPOINT), or unset both for the local dev mode.")

    if settings.enforcing:
        log.info("auth: enforcing Keycloak tokens (issuer=%s audience=%s)",
                 issuer, audience)
        if anon:
            # Both set is a contradiction, and guessing which one the operator
            # meant is not this module's job. Enforcement wins; the log says so.
            log.warning("auth: EM_SERVER_ALLOW_ANON is set but OIDC is "
                        "configured — enforcing tokens and IGNORING the flag")
    elif anon:
        log.warning("auth: DEV MODE — anonymous access allowed by "
                    "EM_SERVER_ALLOW_ANON. Every /v1 route is OPEN.")
    else:
        log.warning(
            "auth: DEV MODE — no OIDC configuration found, so every /v1 route "
            "is OPEN. This is correct for a local P0 run and wrong for "
            "anything reachable: set TOKEN_ENDPOINT (or OIDC_ISSUER) and "
            "CLIENT_ID_em (or OIDC_AUDIENCE). /health reports auth=\"dev-no-auth\".")
    return settings


@dataclass
class _JwksCache:
    """The realm's public keys, fetched on demand and kept for a while."""

    uri: str
    ttl: int = JWKS_TTL_SECONDS
    _keys: Dict[str, Any] = field(default_factory=dict)
    _fetched_at: float = 0.0

    def _fetch(self) -> None:
        try:
            with urllib.request.urlopen(self.uri, timeout=10) as response:
                payload = response.read()
        except (urllib.error.URLError, OSError) as exc:
            # 503 and not 500: the realm being unreachable is an upstream outage,
            # and a client should retry rather than treat its token as bad.
            raise HTTPException(
                status_code=503,
                detail=f"cannot reach the identity provider's JWKS: {exc}",
            ) from None
        import json

        from jwt import PyJWK

        try:
            document = json.loads(payload)
            keys = {}
            for entry in document.get("keys", []):
                kid = entry.get("kid")
                if not kid:
                    continue
                try:
                    keys[kid] = PyJWK.from_dict(entry).key
                except Exception:
                    # One unusable key (an unsupported curve, say) must not cost
                    # the whole key set.
                    continue
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"the identity provider's JWKS is unreadable: {exc}",
            ) from None
        if not keys:
            raise HTTPException(
                status_code=503,
                detail="the identity provider published no usable signing keys")
        self._keys = keys
        self._fetched_at = time.monotonic()

    def key_for(self, kid: str) -> Any:
        """The signing key for *kid*, refetching once if it is unknown.

        The refetch on an unknown `kid` is what actually handles key rotation: a
        token signed with a key minted five minutes ago must verify, and waiting
        out a one-hour TTL would reject every request in between.
        """
        expired = (time.monotonic() - self._fetched_at) > self.ttl
        if not self._keys or expired:
            self._fetch()
        if kid not in self._keys:
            self._fetch()
        key = self._keys.get(kid)
        if key is None:
            raise HTTPException(
                status_code=401,
                detail="the token is signed with a key this realm does not "
                       "publish")
        return key


class Authenticator:
    """Holds the settings and the key cache; produces the FastAPI dependency."""

    def __init__(self, settings: Optional[OidcSettings] = None):
        self.settings = settings if settings is not None else load_settings()
        self._jwks: Optional[_JwksCache] = (
            _JwksCache(self.settings.jwks_uri) if self.settings.enforcing
            else None)

    # ── the dependency ────────────────────────────────────────────────────────
    def require_token(self, request: Request) -> Dict[str, Any]:
        """Validate the request's bearer token; return its claims.

        In dev mode returns a marker principal instead, so a handler that wants
        to know who is calling gets the same shape either way and does not have
        to branch on whether auth is on.
        """
        if not self.settings.enforcing:
            return {"sub": "anonymous", "em_dev_mode": True}

        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            # 401 with the challenge: a client that gets this knows to go and get
            # a token, which a bare 403 would not tell it.
            raise HTTPException(
                status_code=401,
                detail="missing bearer token — send "
                       "'Authorization: Bearer <access token>'",
                headers={"WWW-Authenticate": "Bearer"})
        return self.verify(token.strip())

    def verify(self, token: str) -> Dict[str, Any]:
        """Signature, expiry, issuer, audience, scope — in that order.

        Split out from :meth:`require_token` so the checks are testable without
        constructing a request.
        """
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise HTTPException(status_code=401,
                                detail=f"not a JWT: {exc}",
                                headers={"WWW-Authenticate": "Bearer"}) from None
        algorithm = header.get("alg", "")
        if algorithm not in ALLOWED_ALGORITHMS:
            # Refused BEFORE looking up a key: this is the `alg: none` /
            # HMAC-confusion guard, and it only works if the token never gets to
            # influence how it is verified.
            raise HTTPException(
                status_code=401,
                detail=f"unacceptable token signature algorithm {algorithm!r}",
                headers={"WWW-Authenticate": "Bearer"})
        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401,
                                detail="the token has no 'kid' header, so the "
                                       "signing key cannot be identified",
                                headers={"WWW-Authenticate": "Bearer"})
        assert self._jwks is not None  # enforcing implies a cache
        key = self._jwks.key_for(kid)

        try:
            claims = jwt.decode(
                token, key, algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="the token has expired",
                                headers={"WWW-Authenticate": "Bearer"}) from None
        except jwt.InvalidAudienceError:
            # 403, not 401: the token is genuine and the caller is who they say
            # they are — it was simply issued for a different service. Getting a
            # new token would not help, so telling them to re-authenticate would
            # send them round a loop.
            raise HTTPException(
                status_code=403,
                detail=f"the token's audience does not include "
                       f"{self.settings.audience!r} — it was issued for another "
                       f"client. Add an audience mapper for this client in the "
                       f"realm.") from None
        except jwt.InvalidIssuerError:
            raise HTTPException(
                status_code=401,
                detail=f"the token was not issued by {self.settings.issuer!r}",
                headers={"WWW-Authenticate": "Bearer"}) from None
        except jwt.InvalidSignatureError:
            raise HTTPException(status_code=401,
                                detail="the token's signature does not verify",
                                headers={"WWW-Authenticate": "Bearer"}) from None
        except jwt.MissingRequiredClaimError as exc:
            raise HTTPException(status_code=401,
                                detail=f"the token is missing a required claim: "
                                       f"{exc}",
                                headers={"WWW-Authenticate": "Bearer"}) from None
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail=f"invalid token: {exc}",
                                headers={"WWW-Authenticate": "Bearer"}) from None

        if self.settings.required_scope:
            granted = set((claims.get("scope") or "").split())
            needed = set(self.settings.required_scope.split())
            if not needed <= granted:
                raise HTTPException(
                    status_code=403,
                    detail=f"the token lacks the required scope(s): "
                           f"{' '.join(sorted(needed - granted))}")
        return claims


#: One authenticator per process. Built at import so a misconfiguration fails at
#: STARTUP — the moment an operator is watching — instead of on the first request,
#: when nobody is.
authenticator = Authenticator()


def require_token(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: attach to a router to protect everything under it."""
    return authenticator.require_token(request)


#: Ready to spend as `dependencies=[AuthDependency]` on a router.
AuthDependency = Depends(require_token)
