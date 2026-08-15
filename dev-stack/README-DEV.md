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

## The catalogue — studies beside the rooms

`em-catalog` publishes **studies** (em.json containers) out of the same bucket,
under the `studies/` prefix, against the same realm. It is a **reference**
implementation of the contract 3DR will build the production Catalog against —
see `em-catalog/README.md`.

```bash
docker-compose --env-file .env.dev -f docker-compose.dev.yml up -d --build em-catalog
curl -s http://localhost:8010/health | python3 -m json.tool
```

**Register a study** (any em.json container; the card is derived from it, and
`visibility` is read from its header — it is not a parameter):

```bash
TOKEN=$(curl -s -X POST http://localhost:8085/realms/em-dev/protocol/openid-connect/token \
  -d grant_type=password -d client_id=em-server -d client_secret=em-dev-secret \
  -d username=dev -d password=dev | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST http://localhost:8010/catalog/studies \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @my-study.em.json | python3 -m json.tool
```

**Search it**, in either view:

```bash
curl -s 'http://localhost:8010/catalog/studies?q=sarmizegetusa' | python3 -m json.tool
curl -s 'http://localhost:8010/catalog/studies?view=hdt'        | python3 -m json.tool
```

A study whose header says `visibility: public` answers **without** a token; a
restricted one is 401 without and 200 with. An anonymous listing shows the public
studies rather than a 401 — discovery is the point — and does not leak the rest.

**The smoke**, which is where the architecture is actually proved:

```bash
python dev-stack/smoke_catalog.py
```

It registers two studies, verifies the containers are objects in MinIO by opening
the bucket itself, searches by author / text / digital twin, checks the HDT view
groups two campaigns of one monument, fetches the container back byte-identical,
checks that `/ttl` is in **publish mode** (a deleted US is absent from the
triples while still present in the em.json), exercises the visibility rule, and
finally **empties the index and rebuilds it from the object store** — the claim
that the index is a projection, executed rather than asserted.

To exercise the **deploy** index instead of the dev one, bring up CouchDB and
uncomment the three `COUCHDB_*` lines in the `em-catalog` service:

```bash
docker-compose --env-file .env.dev -f docker-compose.dev.yml --profile couchdb up -d
```

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

---

## The image layer (IIIF)

The stack also runs **Cantaloupe**, a IIIF Image API server, reading the **same
MinIO bucket** with the asset's `sha256` as the identifier. Nothing is copied,
nothing is imported, and there is no thumbnail pipeline: a thumbnail is a size
request, a deep-zoom tile is a region request, and the crop of an annotated
region is a URL.

```bash
HEX=<the sha256 of an uploaded image>            # the `sha256` field of the PUT answer
curl -s http://localhost:8182/iiif/3/$HEX/info.json | jq '{width, height, profile}'
open  "http://localhost:8182/iiif/3/$HEX/full/!400,400/0/default.jpg"   # thumbnail
open  "http://localhost:8182/iiif/3/$HEX/pct:10,10,30,20/max/0/default.jpg"  # a region
```

Two spellings that are **measured, not guessed** (both cost an afternoon):

* the size `full` is deprecated in Image API 3 — Cantaloupe answers **400**. Use
  `max`;
* any size **above** the source is also a 400, including the `!w,h` confine form
  and the `^` upscale form. So a thumbnail asks `!400,400` (at most this big) and
  a viewer asks `max` until `info.json` tells it how big the image really is.

```bash
python dev-stack/smoke_iiif.py     # the whole arc, measured
```

It uploads an image, checks Cantaloupe serves `info.json` / a thumbnail / a
region crop for it, puts a graph with two annotation regions into a room through
the op stream, and asks em-server for the **IIIF Presentation manifest** — whose
canvas is sized from the real `info.json` and whose annotations are the graph's
regions as **W3C Web Annotations**.

**In EMStudio**: Settings → *Immagini (IIIF)* → `http://localhost:8182/iiif/3`.
The shelf and the resource cards then show thumbnails, and the annotator loads
the picture through the Image API.

---

## HTTPS in locale (profilo `https`) — la stessa forma dello staging

Due prove end-to-end erano bloccate non dalla logica ma da `http://localhost`: un
viewer IIIF ospitato non può leggere un manifest in chiaro da una pagina https
(mixed-content), e un token in query si difende solo dietro TLS. Il dev-stack
prende quindi **la stessa forma del deploy**: un host, TLS terminato da **Caddy**,
rotte per path.

```bash
cd dev-stack
docker compose --profile https --env-file .env.dev -f docker-compose.dev.yml up -d
```

| rotta | va a | uguale a produzione? |
|---|---|---|
| `https://em.localhost:8443/em/*` | em-server | sì (`handle_path`) |
| `https://em.localhost:8443/iiif/*` | Cantaloupe | sì (`handle` — la versione sta nel path) |
| `https://em.localhost:8443/assets/*` | MinIO | sì |
| `https://em.localhost:8443/auth/*` | Keycloak | sì |

Il certificato lo emette la **CA interna di Caddy**. È l'unico gesto che questo
stack non può fare da sé, perché tocca il portachiavi di sistema:

```bash
# 1. estrai la radice (il volume la conserva fra i riavvii)
docker cp em-dev-caddy:/data/caddy/pki/authorities/local/root.crt /tmp/caddy-root.crt

# 2. senza fidarsi di nulla: curl la accetta su richiesta
curl --cacert /tmp/caddy-root.crt https://em.localhost:8443/em/v1/health

# 3. per il BROWSER serve la fiducia di sistema (chiede la password)
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain /tmp/caddy-root.crt
```

Senza il passo 3 `curl --cacert` funziona e i browser no — ed è esattamente il
confine: **un host di staging con un certificato pubblico non ha questo
problema**, ed è la stessa identica configurazione (stesso Caddy, stesse rotte,
`heriverse-ansible/role/templates/Caddyfile.j2`) con un dominio vero. Accendere
quell'host è un'azione ops; il percorso è provato qui.

Nota: `em.localhost` risolve a 127.0.0.1 su macOS. Se la tua rete se lo mangia,
`--resolve em.localhost:8443:127.0.0.1` per curl, o una riga in `/etc/hosts`.
