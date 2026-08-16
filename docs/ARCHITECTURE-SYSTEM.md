# StratiGraph — how the pieces stand together

*One house, several rooms. This page is the map: what each piece is, why it
exists, and where it sits. It assumes nothing — every term that carries weight is
explained the first time it appears.*

Written for the person who has just been handed six repositories and wants to
know which one to open. If you only read one section, read **“The one sentence”**
and then the diagram.

---

## The one sentence

> A **study** is a graph of archaeological reasoning plus the things it points
> at. It lives in ONE portable file — an **em.json container** — and everything
> else in this system is either a way to *edit* that file, a way to *serve* it,
> or a *projection* of it for somebody who only needs to read.

Two consequences follow, and most design arguments in this project are settled by
one of them:

1. **the container is the truth.** RDF/Turtle, GraphML, a IIIF manifest, a
   Heriverse scene, a catalogue card — all **projections**. When a projection and
   the container disagree, the container wins;
2. **the meaning lives in the library.** `s3Dgraphy` knows what a stratigraphic
   unit is, how two versions merge, and what a study says about itself. The
   services transport, store and authorise; they do not decide.

---

## The map

```
                        ┌──────────────────────────────────────────┐
     PEOPLE             │  EMStudio          EM-blender-tools      │
     (the clients)      │  the graph editor  the Blender addon     │
                        │  (web + desktop)   (3D + proxies)        │
                        └───────┬──────────────────────┬───────────┘
                                │   WIRE 2 (WebSocket) │
                                │   ops · presence     │
                                ▼                      ▼
     SERVICES        ┌───────────────────┐   ┌────────────────────────┐
     (the node)      │    em-server      │   │      em-catalog        │
                     │  THE ROOM         │   │  THE REGISTER          │
                     │  live graph,      │   │  studies as published, │
                     │  op relay,        │   │  search, the HDT view, │
                     │  assets, IIIF     │   │  "open in…"            │
                     └─────┬────────┬────┘   └────┬──────────┬────────┘
                           │        │             │          │
     INFRASTRUCTURE   ┌────▼───┐ ┌──▼─────────┐ ┌─▼──────┐ ┌─▼────────┐
     (bought, not     │ MinIO  │ │ Cantaloupe │ │Keycloak│ │ CouchDB  │
      invented)       │ bytes  │ │ IIIF images│ │identity│ │ index    │
                      └────────┘ └────────────┘ └────────┘ └──────────┘

     THE LANGUAGE     ┌──────────────────────────────────────────────┐
     (under all of    │                s3Dgraphy                     │
      the above)      │  the em.json format · the merge algebra ·    │
                      │  every projection (RDF, GraphML, IIIF, …)    │
                      └──────────────────────────────────────────────┘
```

Read it bottom-up: **s3Dgraphy** is the language, the two **services** speak it
over HTTP and WebSocket, the two **clients** are where a person actually works,
and the four **infrastructure** boxes are things we did not invent and do not
maintain.

---

## The pieces, one at a time

### s3Dgraphy — the library, and the domain's only authority

A Python library. It defines the **em.json** format, reads and writes it, merges
two versions of a study, and produces every projection: RDF/Turtle (CIDOC-CRM +
CRMinf + HDT-O), GraphML (for yEd), the Heriverse payload, IIIF manifests,
LaTeX/DOCX narrative.

It has **no web framework, no socket, no UI**, and this is a rule rather than an
accident: everything it does can be proved on a table, without a server, and can
therefore be reused by a desktop addon, by a browser sidecar and by a service
without any of them re-deriving it.

The rule the other repos inherit: *if an endpoint needs to compute something,
that something belongs here.*

**Repo:** [`s3Dgraphy`](../../s3Dgraphy) · installable as `pip install s3dgraphy`

### em-server — the ROOM

The live half. It holds the graph **being edited right now**, relays operations
between the people editing it, keeps the assets that graph points at, and serves
IIIF manifests for its images.

* **rooms + relay** — clients join `/v1/rooms/{id}/ws` and exchange operations.
  Convergence is the library's (a CRDT); this service transports and authorises,
  and contains no merge rule of its own;
* **assets** — bytes are **content-addressed**: an asset's name IS the sha256 of
  its content. Dedup is free, a reference is verifiable, and a citation cannot
  rot under you;
* **IIIF** — an image in the bucket is served by Cantaloupe, addressed by that
  same digest, so there is no second copy of the pixels and no thumbnail
  pipeline;
* **stateless** — no session, no upload directory. The durable truth is the
  object store, never this process's disk.

**Repo:** [`em-server`](../) · API under `/v1` · [`docs/URL-TOPOLOGY.md`](URL-TOPOLOGY.md)

### em-catalog — the REGISTER

The published half. A study that is *finished enough to be found* is registered
here: the container goes into the object store, and a **card** is derived from it
and indexed.

The rule that makes this safe: **the index is a projection**. It is built by
re-reading the containers, and `POST /catalog/reindex` proves it by rebuilding
the whole thing from the bucket. Lose the index, run one command; lose the
bucket, lose the studies. Back up the bucket.

Two views: the **flat catalogue** (search by author, licence, text) and the
**HDT view**, which groups the N studies of one heritage object over time —
Sarmizegetusa in 1978, in 2013, in 2026, three containers, one digital twin.

> This is a **reference** implementation. The production Catalog — PID/DOI, FAIR
> publication, the browse UI, the institutional deployment — is 3DR's, on the WP6
> infrastructure. What we provide is the contract, running.

**Repo:** [`em-catalog`](../../em-catalog) · API under `/catalog`

### EMStudio — the editor

Where the graph is authored. Two views on the same data: the **EM matrix**
(epochs as lanes, time flowing upward — the archaeologist's swimlane) and the
**full knowledge graph** with every relation visible. Runs as a web app and as a
desktop app from one codebase, opens and saves `.em.json` containers, and can
connect **live** to a Blender session or to a room.

**Repo:** [`EMStudio`](../../EMStudio)

### EM-blender-tools (EMtools) — the 3D end

A Blender extension. It is where the *geometry* of the reasoning lives: proxies
for stratigraphic units, reconstruction models, orthophotos, georeferencing. It
reads and writes the same graphs, and publishes models into a room's asset store
so that a `.blend` stops being both the workshop and the archive.

**Repo:** [`EM-blender-tools`](../../EM-blender-tools)

### Reading a study — the two renderings

A study can be *read* as a narrative, and there are two ways to serve one. The
difference is worth stating because it is a decision, not a duplication:

* the **live viewer** (`GET /catalog/study/{id}/narrative`) resolves every embed
  at render time. The 3D is navigable, the matrix says what the graph says today,
  and a unit renamed this morning is renamed here. It obeys the study's own
  visibility: a `public` study opens with no token.
* the **static export** (HTML, Word, LaTeX) is a **snapshot**: the embeds are
  resolved once and frozen, so the file can be e-mailed, archived and cited.

Same NarrativeNode, same rendering engine, two outputs — and each one *says
which it is*, in its own footer. A third rendering built separately would
eventually disagree with the other two about what the study said.

The live viewer is EMStudio's reader bundle, and em-catalog serves it as a
**directory** (`/catalog/reader/`, `EM_CATALOG_READER` pointing at the built
`dist/`) rather than as one file: the reader gave up its single-file build so the
3D engine could be a chunk fetched only when a model appears. A deployment that
bundles no reader answers **501** with the build command — not a blank page,
which reads as an empty study.

### The infrastructure we did not invent

| piece | what it is | why it is there |
|---|---|---|
| **MinIO** | S3-compatible object store | the bytes: models, images, the study containers. Content-addressed for assets, keyed by study id for containers |
| **Cantaloupe** | IIIF Image API server | serves images straight out of the bucket, by digest. Zoom, crops and thumbnails become requests, not files |
| **Keycloak** | OIDC identity provider | one realm, every service. Our services **validate** tokens; none of them logs anybody in |
| **CouchDB** | document database | the Catalog's deploy index, beside Heriverse's own documents. Only the index — never the studies |

---

## The two rules that cross every repo

### WIRE 2 — the message shape

Every message between the clients and the room is an **envelope with a nested
body**:

```json
{ "v": 2, "type": "op", "source": "emstudio", "payload": { … } }
```

The envelope says *what kind of message and who sent it*; `payload` is the body,
and the relay treats it as **opaque**. A message announcing another version is
**refused with a sentence**, never half-understood.

Why nested: in WIRE 1 the body sat beside the envelope, and an edge's own
`source` (its start node) collided with the envelope's `source` (who sent this).
Edges arrived with no ends. The nesting makes that class of bug structurally
impossible, which is worth more than the one bug it fixed.

### URL topology — internal vs public

Every service→service URL exists in **two forms**: the internal one (a service
name on the container network) and the public one (a host a browser can reach).

> **em-server speaks on the internal form and writes the public form into the
> documents it serves.** The address it *dials* comes only from the
> configuration; a request parameter may, at most, change the URL written *into*
> the answer.

Confusing the two fails silently — a 403, an empty body, a canvas with a
placeholder size. All of it is listed once in
[`docs/URL-TOPOLOGY.md`](URL-TOPOLOGY.md).

---

## The three scenarios

The same code, addressed three ways. (The full argument is the D2.2 addendum;
this is the shape.)

**1 · Standalone.** One person, one laptop, no server. EMStudio and EMtools talk
to each other through a local sidecar; a study is a file. Everything works —
merging, versioning, projections — because the algebra needs no coordinator.
*This is not a degraded mode; it is the base case, and the other two add reach
rather than capability.*

**2 · Local FCN** (*Fedora Commons Node* — a working group's own node). The same
services on a machine the team controls: rooms for co-editing, a bucket for the
assets, a catalogue for what is finished. Nothing leaves the institution unless
somebody publishes it.

**3 · Institutional node.** The same services again, on an institutional host
with a real domain and a public certificate, behind one Caddy — and, from there,
into the wider infrastructure (ECCCH, the Collaborative Cloud). *Same images,
same wire, same routes as scenario 2: the difference is the address and who holds
the keys.*

The dev stack (`dev-stack/`) is scenario 2 on a laptop, deliberately: it is the
smallest arrangement in which the internal/public distinction is real, and a
stack where both forms were the same string would prove nothing.

---

## Where to go next

| you want to… | read |
|---|---|
| run the whole thing locally | [`dev-stack/README-DEV.md`](../dev-stack/README-DEV.md) |
| deploy it on a real host | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| know which URL is which | [`URL-TOPOLOGY.md`](URL-TOPOLOGY.md) |
| understand the merge / real-time model | `EMStudio/docs/adr-003-offline-vs-realtime-versioning.md` |
| use the library | [`s3Dgraphy/README.md`](../../s3Dgraphy/README.md) |
| use the API | [`em-server/README.md`](../README.md) · [`em-catalog/README.md`](../../em-catalog/README.md) |
| edit a graph | [`EMStudio/README.md`](../../EMStudio/README.md) |
| work in 3D | [`EM-blender-tools/README.md`](../../EM-blender-tools/README.md) |
