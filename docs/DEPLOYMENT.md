# Deploying the whole system

*One playbook, one host, everything behind TLS. This page says what you need
before you start, what the single command does, and what to check afterwards.*

The map of the pieces is [`ARCHITECTURE-SYSTEM.md`](ARCHITECTURE-SYSTEM.md);
this is the operational half. The playbook itself lives in the
[`heriverse-ansible`](../../heriverse-ansible) repository.

---

## What comes up

Nine containers behind one Caddy, which terminates TLS and routes by path:

| public route | container | what it serves |
|---|---|---|
| `/` | heriverse | the 3D front end |
| `/server/*` | heriverse-server | Heriverse's own API |
| `/auth/*` | keycloak | identity — one realm for everything |
| `/couchdb/*` | couchdb | documents (Heriverse's scenes, and the Catalog's index) |
| `/em/*` | **em-server** | rooms: the live graph, its assets, the WebSocket |
| `/catalog/*` | **em-catalog** | the studies as published: search, HDT view, "open in…" |
| `/iiif/*` | cantaloupe | the IIIF Image API, straight out of the bucket |
| `/assets/*` | minio | the object store's own API |

Caddy obtains and renews the certificate itself. That single origin is not
cosmetic: it is what removes mixed-content errors, and it is what makes a token
in a query string defensible for a viewer that cannot set a header.

---

## Prerequisites — the part that is ops, and is yours

1. **A host.** Linux, Docker installed (the role does not install it), enough
   disk for the object store. This is where the studies will live.
2. **A domain pointing at it.** Caddy needs a name it can prove, over ports 80
   and 443. Without a real name there is no public certificate, and everything
   below still works but only over an internal CA nobody trusts.
3. **The images.** `em-server` and `em-catalog` are built from the `Dockerfile`
   in each repo and pushed to the registry the defaults name
   (`git.3dresearch.it:5050/stratigraph/…`). Change `em_server_image` /
   `em_catalog_image` if they live somewhere else.
4. **The secrets, from outside the repository.** MinIO's root credentials and
   CouchDB's. There are **no defaults** for them, deliberately: a default
   password is the same password on every deployment. Put them in the inventory,
   in an Ansible Vault file, or in the environment.

> Verified: rendering the compose template **without** the CouchDB credentials
> fails at that task with `'couchdb_user' is undefined`. That is the intended
> behaviour — the alternative is a catalogue that comes up with a password
> somebody could read in a git history.

---

## The command

```bash
ansible-playbook -i <inventory> playbook/heriverse.yml
```

It creates the service user, clones the Heriverse compose repository, writes the
**rendered** `docker-compose.yml` and `Caddyfile` over the ones in it, brings the
stack up and runs the entrypoint.

Two notes for whoever runs it:

* the repository has `role/` (singular), so the playbook cannot resolve the role
  `heriverse` without help. Point `ANSIBLE_ROLES_PATH` at a directory containing
  it under that name, or add an `ansible.cfg`. *Reported, not changed: it is the
  repository's own arrangement, not a typo of ours.*
* the role uses `community.docker.docker_compose_v2`, so that collection must be
  installed (`ansible-galaxy collection install community.docker`).

### Variables worth knowing

| variable | default | what it decides |
|---|---|---|
| `server_name` | `heriverse.example.com` | **the** domain. Everything public is derived from it |
| `em_server_enabled` / `em_catalog_enabled` | `true` | whether the StratiGraph services come up at all |
| `minio_enabled` | `true` | object store on. Off ⇒ em-server falls back to a directory (single instance only) |
| `iiif_enabled` | `true` | Cantaloupe on. Needs MinIO |
| `em_catalog_couchdb_enabled` | `true` | the Catalog's index in CouchDB. Off ⇒ SQLite on a volume |
| `em_server_audience` / `em_catalog_audience` | `em-server` | the audience each service requires in a token |
| `minio_root_user` / `_password`, `couchdb_user` / `_password` | **none** | from the inventory or the Vault |

The URL topology is **already correct** in the templates: em-server writes
`https://<server_name>/iiif/3` into its manifests and dials
`http://cantaloupe:8182/iiif/3` to measure images; em-catalog writes
`https://<server_name>` into its "open in…" answers and reaches MinIO and CouchDB
by service name. Nothing about it has to be assembled by hand — see
[`URL-TOPOLOGY.md`](URL-TOPOLOGY.md) for why the two forms are two variables.

---

## Checking it without deploying

Everything below was **executed** while writing this page, against the templates
as they stand. None of it needs a host.

Render the compose template with example values and let Docker judge it:

```bash
ansible-playbook render.yml -e tpl_src=role/templates/docker-compose.yml.j2 -e tpl_dest=/tmp/dc.yml
docker-compose -f /tmp/dc.yml config
```

Measured: **VALID**, nine services —
`caddy cantaloupe couchdb em-catalog em-server heriverse heriverse-server
keycloak minio` — and the volumes `couchdb_config couchdb_data em_catalog_data
em_data keycloak_data minio_data`.

With the Catalog on its SQLite index and no object store
(`em_catalog_couchdb_enabled: false`, `minio_enabled: false`,
`iiif_enabled: false`): **VALID**, seven services, and the em-catalog block
carries `EM_CATALOG_DB` instead of `COUCHDB_URL`. The config gating works in
both directions, which is the point of testing the *off* case at all.

Let Caddy judge its own file:

```bash
docker run --rm -v "$PWD:/work:ro" caddy:2-alpine caddy validate --config /work/Caddyfile --adapter caddyfile
```

Measured: **Valid configuration**, with all four StratiGraph routes present —
`/em/*`, `/catalog/*`, `/iiif/*`, `/assets/*`.

And the playbook itself:

```bash
ansible-galaxy collection install community.docker
ANSIBLE_ROLES_PATH=<dir containing role as "heriverse"> ansible-playbook --syntax-check playbook/heriverse.yml
```

Measured: **green**.

---

## After a deploy

```bash
curl -s https://<server_name>/em/v1/health | jq
curl -s https://<server_name>/catalog/health | jq
```

What to look for, in both answers:

* `"auth": "keycloak"` — **not** `dev-no-auth`. If it says the latter, the
  service did not get its OIDC environment and is answering to anybody;
* em-server: `"asset_store": "minio (…)"` — if it says `memory`, uploads are
  living in a process and will vanish with it;
* em-catalog: `"container_store": "minio (…)"` and `"index": "couchdb (…)"`.

Then the one that proves the architecture rather than the wiring:

```bash
curl -s -X POST https://<server_name>/catalog/reindex -H "Authorization: Bearer $TOKEN" | jq
```

It rebuilds the catalogue's index from the containers in the bucket. If it
answers with the number of studies you expect, the index is a projection and the
bucket is the truth — which is the property that makes the backup policy simple:
**back up the bucket** (and CouchDB, for Heriverse's own documents). The Catalog
index is derivable; the studies are not.

---

## What this page does not cover

* **Turning the host on** — provisioning the machine, the DNS record, the
  firewall. That is ops, and it is deliberately outside the playbook.
* **The production Catalog's own features** — PID/DOI minting, FAIR publication,
  the browse UI. Those are 3DR's, on the WP6 infrastructure; what deploys here is
  our reference implementation of the contract.
* **Backups and monitoring.** Named, not solved: the bucket is the study data,
  `em_data` is the room documents, CouchDB holds documents for two systems.
