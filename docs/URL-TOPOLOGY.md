# URL topology — internal ↔ public, listed once

Every service→service URL in a StratiGraph deployment exists in **two forms**:

* the **internal** one, a service name on the container network
  (`minio:9000`, `keycloak:8080`, `cantaloupe:8182`), which is how one process
  dials another;
* the **public** one, the host a browser can reach
  (`https://<host>/em/…`, `https://<host>/iiif/3/…`), which is what goes into
  anything a client will later fetch.

The rule, in one line:

> **em-server SPEAKS on the internal form and WRITES the public form into the
> documents it serves.**

Confusing the two fails *opaquely* — that is the whole reason this file exists.
Three separate bugs in this project were the same confusion wearing different
clothes:

| what happened | what it looked like |
|---|---|
| the JWKS fetched from the issuer's public URL | `401` on a token that was perfectly good |
| `info.json` fetched from `EM_IIIF_BASE`, which inside the network is em-server itself | every canvas silently sized `1000×1000` (a placeholder), no error anywhere |
| a manifest naming `http://localhost:8000` handed to a hosted Mirador | `Failed to fetch`, mixed-content, nothing on screen |

None of the three raised anything. That is the failure mode this page is against.

---

## The hard half of the rule

The line above says where each form belongs. This says who is allowed to choose
them, and it is not symmetric:

> **The address em-server DIALS comes only from the configuration. A request
> parameter may, at most, change the URL WRITTEN INTO the document it gets
> back.**

The two jobs are easy to conflate because they name the same service. That is
exactly what `?image_base=` did: the manifest route read
`internal = IIIF_INTERNAL or base`, so a deployment with no IIIF configured
dialled whatever host the caller had named. The fallback is gone — unconfigured
now means unmeasured, and the canvases say so — and the rule is asserted rather
than remembered (`tests/test_topology_and_visibility.py` §3: a hostile
`?image_base=` moves what is written into the manifest and does not move the
socket).

Corollary for the next parameter: if it does not have a strong reason to exist
*for the document*, it should not exist at all.

---

## The pairs

| pair | internal (who dials) | public (what is written / fetched by a browser) |
|---|---|---|
| **IIIF image service** | `EM_IIIF_INTERNAL` — em-server → Cantaloupe, to read `info.json` | `EM_IIIF_PUBLIC` — written into every manifest and every image URL a client renders |
| **Identity** | `OIDC_JWKS_URI` — em-server → Keycloak, to fetch the signing keys | `OIDC_ISSUER` — what the TOKEN says, i.e. the address the user's browser got it from |
| **Object store** | `MINIO_ENDPOINT` / `EM_ASSET_S3_ENDPOINT` — em-server → MinIO | `/assets/*` on the public host, for clients that fetch an asset by digest |
| **The room API** | (none: nobody dials em-server from inside) | `/em/*` on the public host — the base EMStudio and EMtools are configured with |

Older spellings are still read, in this order, so nothing breaks on upgrade:
`EM_IIIF_PUBLIC` → `EM_IIIF_BASE`, and `EM_IIIF_INTERNAL` → `EM_IIIF_INTERNAL_BASE`.
One setting with two names and a precedence — never two settings that will one
day disagree.

---

## How each deployment fills them in

**dev-stack** (`em-server/dev-stack/docker-compose.dev.yml`): the "public" form
is `http://localhost:<port>` because the browser really is on the same machine;
the internal form is the compose service name. That difference is exactly why
the dev stack catches these bugs — a stack where both forms were the same string
would prove nothing.

**production** (`heriverse-ansible`): Caddy terminates TLS on one host and
reverse-proxies by path, so every public form is `https://<server_name>/<route>`
and every internal form is a service name:

```
https://<host>/em/*      → em-server:8000     (handle_path: the app's own prefix)
https://<host>/iiif/*    → cantaloupe:8182    (handle: the IIIF version is IN the path)
https://<host>/assets/*  → minio:9000
https://<host>/auth/*    → keycloak:8080
```

`handle` vs `handle_path` is part of the topology and not a detail: `/iiif/3/…`
carries the API version in the path, so stripping the prefix would take the
endpoint with it.

---

## Adding a service

1. write down **both** forms, here, before writing any code;
2. give them **two variables** whose names say the direction — never one
   variable used in both places;
3. dial the internal one; write the public one into documents;
4. if the two are ever equal in some deployment, that is a coincidence of that
   deployment — do not collapse them;
5. no request parameter selects the internal one. Not as a default, not as a
   fallback, not "only when nothing is configured".
