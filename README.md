# em-server

**The s3Dgraphy access API, over HTTP.** A thin FastAPI wrapper around the same
operations EMStudio's local bridge already calls — so a browser client can point at
a service instead of a sidecar, with nothing else changed.

Part of the StratiGraph ecosystem (CNR ISPC, Horizon Europe GA 101232855) with
[s3Dgraphy](../s3Dgraphy) (the reference implementation), EMStudio, EM-blender-tools
and EMLab.

> **Status: P0 complete — read-only, local, no auth, everything under `/v1`.**
> The scaffold and the read endpoints, verified in a container against the published
> `s3dgraphy 1.6.0.dev12`. Authentication (Keycloak + ORCID), the asset store
> (MinIO), the op-log WebSocket and the deployment on the shared infrastructure are
> **P1–P4**, and they land with 3DR on Heriverse-Docker. See *Roadmap* below.

## The two rules that shape this repo

**1. FastAPI lives only here.** s3Dgraphy stays a pure library — no web framework,
no transport, no server — and em-server is the one place that knows about HTTP. The
corollary is the useful part: **em-server adds no logic.** Every endpoint is a call
into `s3dgraphy.api`; if an operation needs to compute something, that something
belongs in the library, where it is testable without a server and reusable by
EMStudio, EMtools and EMLab. There is a test that reads `app/main.py` and fails if
it ever reaches past the `api` surface into s3Dgraphy's internals.

**2. Stateless (12-factor).** No session, no upload directory, no database: the
document arrives in the request and leaves in the response. That is what makes
replicas behind a load balancer safe, and it is a property to defend — the first
endpoint that keeps a file on disk breaks it. (`.gitignore` says the same thing:
if it ever needs an entry for uploads, fix the code, not the ignore list.)

### …and the exception, named (P4.2)

A **real-time relay holds connections**: it cannot be stateless, and pretending
otherwise would be worse than saying so. `/v1/rooms/{id}/ws` is where a new *kind*
of state enters this service, and it comes with three fences that keep rule 2
meaningful rather than abandoned:

* **The durable truth is outside the process.** A room's snapshot goes to an
  object store (`app/store.py`; MinIO in the deployment). What lives in RAM is a
  *working copy*, rebuilt from the snapshot when a cold room is opened — the
  process may die, the study does not live inside it. `GET /health` reports
  `snapshot_store`, so "do my snapshots survive a restart?" is one unauthenticated
  GET away.
* **Convergence stays in the library.** The relay applies each operation through
  `s3dgraphy.api` and re-broadcasts it. It does not transform, order or reconcile:
  the CRDT converges whatever the order (P4.1), which is exactly why a *relay* is
  enough and an operational-transform server is not. There is a test that fails if
  `app/ws.py` or `app/rooms.py` ever reaches past `api` — the same guard rule 1
  already had, extended to the stateful part.
* **Presence is ephemeral.** Who is connected, and what they have selected, lives
  in RAM and is lost on restart. That is correct: presence is about *now*, and a
  test asserts that none of it reaches the store.

Rooms are **sticky**: one instance owns a room. Several replicas would need the
op-log outside the process (a Redis stream, or the store) — the seam is written
down in `app/rooms.py` and deliberately not implemented.

## Endpoints (P0)

| endpoint | what it does | needs |
|---|---|---|
| `GET /v1/health` | liveness, version, **and `capabilities`** — which optional ops this build can actually perform, so a client never has to discover a 501 by trying | — |
| `POST /v1/validate` | header/format conformance + stats (`api.validate`); load warnings travel in the response, not into a log the caller cannot read | — |
| `POST /v1/export-ttl` | em.json → Turtle (`api.project_ttl`), `text/turtle` with the same filename the bridge uses | `rdflib` |
| `POST /v1/reproject` | EPSG → EPSG (`api.reproject_many`); `{x, y, …}` or `{points: [[x, y], …]}` — one transformer per batch, capped at 512 points | `pyproj` |
| `GET/POST /v1/resolve-authority` | ranked offline authority candidates; both verbs, like the bridge | authority snapshots |
| `GET /health` | **unversioned probe alias** — same payload as `/v1/health` | — |

### Versioning

**`/v1` is the stable contract.** Route names and payloads under it do not move:
3DR builds against this, and a path is the cheapest promise to keep. The
multi-client WebSocket work of **P3 may introduce a `/v2`**, and it should appear
*beside* `/v1` rather than replace it. An unprefixed API path is a 404, deliberately
— a silently-aliased route is one that vanishes at v2 and takes a client with it.

The one exception is `GET /health`, which also answers unversioned. A health probe
belongs to the infrastructure, not to the API: a Docker `HEALTHCHECK`, a Kubernetes
liveness probe and a Caddy upstream check must not need editing the day the API
version changes. Same function, same payload, nothing to keep in sync.

A missing optional dependency is **501 Not Implemented**, never 500: the request was
valid and this build simply cannot do that op. A client can degrade honestly from a
501 (that is exactly what EMStudio's map does when pyproj is absent) and cannot from
a 500.

`GET /docs` serves the interactive OpenAPI UI; `GET /openapi.json` is the contract
to hand to 3DR for P1.

## Auth (P1)

em-server is a **resource server**: it never logs anybody in. A client gets an
access token from the shared Keycloak realm — the one Heriverse-Server already uses
— and sends it as `Authorization: Bearer <jwt>`. Every request is verified against
the realm's published JWKS; there is no session, which is what keeps the service
horizontally scalable.

**Everything under `/v1` requires a token. Both health routes do not** — a probe is
infrastructure, and a Docker `HEALTHCHECK` must not start failing the day auth is
switched on. `GET /health` reports `auth: "keycloak"` or `auth: "dev-no-auth"`, so
"is this deployment actually protected?" is one unauthenticated GET away.

### Environment

| variable | meaning |
|---|---|
| `TOKEN_ENDPOINT` | the realm's token endpoint, **already deployed by Heriverse-Docker**. The issuer and the JWKS URI are derived from it |
| `CLIENT_ID_em` | the audience this service accepts, following Heriverse's own `CLIENT_ID_<app>` convention |
| `OIDC_ISSUER` | explicit issuer, if you would rather not derive it |
| `OIDC_AUDIENCE` | explicit audience; wins over `CLIENT_ID_em` |
| `OIDC_JWKS_URI` | explicit JWKS URI; defaults to `<issuer>/protocol/openid-connect/certs` |
| `OIDC_REQUIRED_SCOPE` | space-separated scopes a token must carry (optional) |
| `EM_SERVER_ALLOW_ANON` | `1` to state the local open mode deliberately. **Ignored** when OIDC is configured |

The derivation is the alignment that matters: 3DR already sets `TOKEN_ENDPOINT`, so
em-server must not ask for a second spelling of the same realm URL. Two variables
for one fact is how configurations drift until one of them is wrong. It works with
both Keycloak path styles (`/auth/realms/…` for ≤16, `/realms/…` for 17+) because
the endpoint's fixed suffix is stripped rather than the prefix guessed.

```bash
# the shared realm
TOKEN_ENDPOINT=http://keycloak:8080/auth/realms/heriverse/protocol/openid-connect/token
CLIENT_ID_em=em
```

### Three states, and the middle one is the point

* **enforcing** — issuer and audience known: `/v1` needs a valid token. 401 for
  missing / expired / badly signed / wrong issuer; **403** for a wrong audience or a
  missing scope, because that token is genuine and re-authenticating cannot fix it.
* **dev / no-auth** — *nothing* OIDC is set: P0 keeps working on a laptop, with a
  loud warning in the log and `auth: "dev-no-auth"` in `/health`.
* **misconfigured** — *some* of it set: the process **refuses to start**. A
  half-configured deployment that fell back to open would be a service everyone
  believes is protected, and that failure is silent, permanent, and found by the
  wrong person.

### What is still needed from 3DR

The end-to-end run against the real realm needs the shared configuration:
the realm's public URL as reachable from em-server's container, a **client for
em-server** in that realm, and — the one that bites — an **audience mapper** on it,
because Keycloak does not put a client's own id in `aud` by default. Without the
mapper a genuine token arrives with `aud: account` and is correctly refused with a
403. See the mail-spec to Romano (realm + bucket + routing).

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'          # + '.[all]' for TTL and reprojection

# s3dgraphy from PyPI — dev12 is the first release with `api.py` AND the extras
.venv/bin/pip install 's3dgraphy[geo,rdf]==1.6.0.dev12'
# …or from the sibling checkout, while the language and the service move together:
# .venv/bin/pip install -e ../s3Dgraphy

.venv/bin/uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/v1/health | python3 -m json.tool
```

Without s3dgraphy importable the app refuses to start and says how to fix it —
deliberately, because a service that boots and then 500s on every request is worse
than one that does not boot. (For a quick run against a checkout without
installing: `PYTHONPATH=../s3Dgraphy/src .venv/bin/uvicorn app.main:app`.)

### Docker

```bash
docker build -t em-server .
docker run --rm -p 8000:8000 em-server
curl -s localhost:8000/v1/health | python3 -m json.tool
```

Nothing to mount: the image installs `s3dgraphy[geo,rdf]==1.6.0.dev12` from PyPI and
that is sufficient — verified in a real container (see below). To develop against a
checkout instead, mount it and point `PYTHONPATH` at it:

```bash
docker run --rm -p 8000:8000 \
  -v /path/to/s3Dgraphy/src:/opt/s3dgraphy-src:ro \
  -e PYTHONPATH=/opt/s3dgraphy-src em-server
```

The image carries the optional engines by default (rdflib, pyproj): a service whose
`/reproject` answers 501 is a support ticket waiting to happen, and pyproj bundles
PROJ in its wheel so this needs no system GDAL. One uvicorn worker per container —
replicas are the orchestrator's business, and a process count baked into an image
is a decision taken in the wrong place.

> **Why the pin is spelled `s3dgraphy[geo,rdf]==1.6.0.dev12`** — three lessons, each
> learned from a build that failed:
>
> * `s3dgraphy>=1.6` matches **nothing** while 1.6 is a dev series: PEP 440 will not
>   resolve a pre-release unless the specifier names one. The build died at pip.
> * **dev12 is the first release shipping `s3dgraphy/api.py`**, the surface this
>   whole service wraps. Anything older starts and fails the import (it says so, on
>   purpose).
> * The extras are **not** optional here in practice: dev11 predated `[geo]`, so
>   `s3dgraphy[rdf,geo]` installed rdflib and silently skipped pyproj — pip warns
>   about an unknown extra, it does not fail — and the container answered
>   `reproject: false`. dev12 declares both, so the pin alone is now enough and the
>   Dockerfile no longer names rdflib/pyproj separately.

## Tests, and how P0 was verified

```bash
.venv/bin/python -m pytest tests -q      # 15 passed, 2 skipped
```

The tests skip rather than fail when an optional dependency is absent, and the
inverse tests (501 without rdflib / without pyproj) run in that case instead — so
the suite is meaningful in both builds.

Beyond the endpoints, two properties are pinned: that `/export-ttl` returns
**byte-for-byte** what `api.project_ttl` returns (the moment a service reshapes a
result, "the same API over HTTP" stops being true), and that the app imports nothing
from s3Dgraphy but `api`.

**Checked in a real container** built from this Dockerfile, with **nothing mounted**
— PyPI dev12 only:

```
GET  /v1/health      s3dgraphy 1.6.0.dev12, capabilities: validate ✓ export_ttl ✓
                     reproject ✓ resolve_authority ✓
GET  /health         200 — the unversioned probe answers the same
POST /v1/reproject   lon 14.999999999999982, lat 41.99999995659526
POST /reproject      404 — an unprefixed API route is not aliased
POST /v1/export-ttl  7457 bytes, text/turtle, filename="portamarina_lite.ttl"
```

and the same TTL three ways, byte for byte:

```
the endpoint, in the container            7457 bytes
api.project_ttl, in the same container    7457 bytes   → identical (no logic added)
em-bridge, running the s3Dgraphy checkout 7457 bytes   → identical (dev12 == checkout)
```

The reprojection number is worth its own line: easting 500 000 in UTM zone 33 **is**
the zone's central meridian (15°E) by definition, not by table lookup — so `lon
15.000…` from inside the container is also proof that pyproj's PROJ data travelled
with the wheel.

The reprojection number is worth its own line: easting 500 000 in UTM zone 33 **is**
the zone's central meridian (15°E) by definition, not by table lookup — so `lon
15.000…` is also a proof that PROJ found its datum data.

## Roadmap

| phase | what | with |
|---|---|---|
| **P0** | this scaffold: read-only, local, no auth | — |
| **P1** | **Keycloak** bearer-token auth on the shared realm — done, verified against a locally signed realm; the run against the real one needs 3DR's config (see *Auth* above). **ORCID** as user identity is a realm-side concern, so it lands with that config rather than here | 3DR, shared infra |
| P2 | **MinIO assets** — the same stable-ID resolver s3Dgraphy already has (R0–R2) | 3DR |
| P3 | **op-log WebSocket** — ADR-002: one host per session now, CRDT later | — |
| P4 | **deployment** (WP6) on Heriverse-Docker | 3DR |

None of them is stubbed here. A placeholder endpoint gets called, and each phase
carries a real decision — which identity provider, which bucket layout, which
conflict policy — that is not ours alone to make.

The mini-plan (`Regia_EM/em-server-mini-plan.md`) describes the **full** surface this
service will eventually expose: GraphML ↔ em.json, the resource ops (list / resolve /
ingest-minio / presign), DTC detach/inject/bake, `georeference_scene`, and the
narrative generation seam. P0 carries the read-only subset on purpose — everything
else either writes, or needs the asset store, or needs auth, and each of those is a
phase with a decision attached. The list is the roadmap's, not a gap in P0.

## License

GPL-3.0-or-later, as the rest of the EM toolchain.
