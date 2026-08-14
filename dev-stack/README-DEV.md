# The dev stack — MinIO + Keycloak + em-server on a laptop (Colima)

**What this is.** A local stack for *our* development, not a second deployment
path. The real server — and a local FCN and an institutional node are the same
service, only differently addressed — is provisioned with the production
Ansible/compose (`heriverse-ansible`). What runs here is the **same em-server
image**, the **same wire**, and the **same MinIO implementation of the
AssetStore**. The only thing this directory adds is the two things em-server
depends on, next to it, so the promotion arc can be exercised against a **real
object store** without a remote host.

**What it is not.** Not a place for production secrets, and not a different code
path. `.env.dev.example` holds `minioadmin`/`minioadmin` and a realm called
`em-dev`: every value in it would be a vulnerability on anything reachable.

---

## Prerequisites (once)

```bash
brew install colima docker docker-compose
```

**Colima** is the alternative to Docker Desktop: it runs a small Linux VM and
exposes a Docker socket. `docker` is only the client — without Colima (or Docker
Desktop) there is no daemon for it to talk to.

```bash
colima start --cpu 4 --memory 8 --disk 30
docker context use colima   # point the `docker` client at Colima's socket
docker ps                   # if this answers, everything below will work
```

The `docker context` line is the part people miss: it is what makes `docker` and
`docker compose` speak to the VM instead of looking for a socket that is not
there. Check it any time with `docker context ls` — the active one has a `*`.

> **`docker compose` or `docker-compose`?** Recent Docker ships compose as a
> *plugin* (`docker compose`, two words). A Homebrew `docker` sometimes does not,
> and then only the standalone binary exists (`docker-compose`, hyphen). Both
> take the same arguments. This machine has the standalone one, so the commands
> below are written with the hyphen; drop it if `docker compose version` answers
> on yours.

---

## Up

```bash
cd dev-stack
cp .env.dev.example .env.dev
docker-compose --env-file .env.dev -f docker-compose.dev.yml up -d --build
```

That builds em-server from its own `Dockerfile` and starts four things: MinIO, a
one-shot that **creates the bucket**, Keycloak with the **dev realm imported**,
and em-server pointed at both. First run takes a few minutes (the image build);
after that it is seconds.

| what | where | credentials |
|---|---|---|
| em-server | <http://localhost:8000/v1/health> | a bearer token (below) |
| MinIO console | <http://localhost:9001> | `minioadmin` / `minioadmin` |
| MinIO API | <http://localhost:9000> | idem |
| Keycloak | <http://localhost:8085> | `admin` / `admin` |

**Why 8085 and not 8080.** 8080 is a busy port on a developer's Mac — this one
already had a Moodle container on it. Every port is a variable in `.env.dev`;
change it there if one collides, nothing else needs to know.

Check what came up:

```bash
docker-compose --env-file .env.dev -f docker-compose.dev.yml ps
curl -s http://localhost:8000/v1/health | jq
```

A healthy stack answers `"auth": "keycloak"` and
`"asset_store": "minio (http://minio:9000, bucket em-assets)"`. If it says
`dev-no-auth` or `memory`, em-server did not get its environment — look at
`docker logs em-dev-server`.

---

## A token, without clicking

The dev realm (`keycloak/realm-em-dev.json`, explained in
[`keycloak/README.md`](keycloak/README.md)) seeds a client and a user, so a token
is one `curl`:

```bash
curl -s -X POST http://localhost:8085/realms/em-dev/protocol/openid-connect/token \
  -d grant_type=password -d client_id=em-server -d client_secret=em-dev-secret \
  -d username=dev -d password=dev | jq -r .access_token
```

`client_credentials` works too (the client has service accounts enabled), but the
password grant is what a human wants: the token then carries the dev user's
**ORCID iD**, so what you publish is *signed* rather than merely dated.

```bash
TOKEN=$(curl -s -X POST http://localhost:8085/realms/em-dev/protocol/openid-connect/token \
  -d grant_type=password -d client_id=em-server -d client_secret=em-dev-secret \
  -d username=dev -d password=dev | jq -r .access_token)

curl -s -X PUT "http://localhost:8000/v1/rooms/demo/asset?media_type=model/gltf-binary" \
  -H "Authorization: Bearer $TOKEN" --data-binary @model.glb | jq
```

The answer's `ref` is `sha256:<hex>` — **the digest of the bytes you sent**. That
is the object's name in the bucket (visible in the MinIO console under
`em-assets`), which is what makes the reference verifiable and dedup free.

---

## The smoke test — the proof that MinIO is real

```bash
python dev-stack/smoke.py       # from the repo root
```

It takes a token, uploads an asset, **opens the bucket itself** to check the
object is there (em-server's own word is not proof), downloads it, checks that
an unauthenticated request is refused, and finally runs
`s3dgraphy.api.promote_resource` against the URL em-server serves — verifying
that the URL written into the graph really serves those bytes and that they hash
to the checksum the graph recorded.

Anything it cannot measure is printed as `SKIPPED` with the reason. A skip means
"not measured", never "passed".

---

## Down

```bash
docker-compose --env-file .env.dev -f docker-compose.dev.yml down -v   # -v drops the volumes too
colima stop                                                            # frees the VM's CPU/RAM
```

`-v` throws away the bucket and the room documents. Leave it off to keep them
between sessions.

---

## When something is wrong

| symptom | cause |
|---|---|
| `Cannot connect to the Docker daemon` | Colima is not running, or the context is `default` — `colima start`, `docker context use colima` |
| Keycloak exits at boot with a Jackson error | an unknown key in `realm-em-dev.json`. The importer is strict: **no comments in that file** (that is why its notes live in `keycloak/README.md`) |
| `403 … the token's audience does not include 'em-server'` | the realm's audience mapper is missing. It is the single most common reason a correct-looking token is refused |
| `401` with a token that looks fine | the issuer. A token minted through `localhost:8085` says `iss: http://localhost:8085/...`; em-server is configured with exactly that spelling and fetches the JWKS over the internal `keycloak:8080`. Change one and you must change the other |
| em-server exits saying the store is *half configured* | three of the four `MINIO_*` variables. It refuses rather than falling back to a local directory nobody backs up |
| a port is already taken | change it in `.env.dev` — every port in the compose file is a variable |

---

## Versions this was verified on

Colima 0.10.3 · Docker 29.6.1 · docker-compose 5.3.0 (standalone) ·
MinIO (latest, 2026-08) · Keycloak 24.0.4 · macOS aarch64.
