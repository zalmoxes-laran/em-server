#!/usr/bin/env python3
"""Fill the field node with data that STAYS — for a demo, and for a real look around.

    python dev-stack/seed-demo.py

**Why this exists, and why it is not a smoke test.** The smokes clean up after
themselves, deliberately: a test that leaves things behind is a test you can only
run once. The consequence is that five minutes after proving the stack works, the
catalogue is empty again — so there is nothing to show somebody, and nothing for
EMStudio to open against the room.

This is the other half: a **populator**. It asserts nothing about itself, it
leaves everything it makes, and it prints the URLs to click. Two properties make
that safe:

* **idempotent** — keyed on each study's `em_id`. Run it ten times and there are
  still two studies. A demo you are afraid to re-run is a demo that rots;
* **no cleanup, ever** — the whole point. `fcn-down.sh --wipe` is how you get
  back to nothing, and the last line says so.

What it leaves behind, chosen so that each of the four surfaces has something
real to show: a **public study with a narrative** (the reader has prose and
embeds to render), a **second study of the same monument** (the HDT view has two
campaigns to group), and a **real image in the object store, referenced by a
unit and annotated** (IIIF has pixels to serve, not a placeholder).

The data is invented — a plausible portico, not an excavation. It says so on the
studies themselves.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = pathlib.Path(__file__).resolve().parent

# s3Dgraphy from the checkout beside this repo: the containers are built with
# the library as it is NOW, the same way the smokes do it.
_CHECKOUT = HERE.parent.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir():
    sys.path.insert(0, str(_CHECKOUT))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_env_file(path: pathlib.Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def say(what: str, detail: str = "") -> None:
    print(f"  · {what}{(' — ' + detail) if detail else ''}")


def request(url: str, *, method: str = "GET", data: bytes | None = None,
            headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc.reason).encode()


# ── the demonstration image ─────────────────────────────────────────────────
#
# Generated rather than shipped: a binary in a repository is a binary somebody
# has to review, and what IIIF needs is real pixels of a known size, not a
# photograph. 1024×768, a gradient, a valid PNG — Cantaloupe reads it and
# reports its dimensions, which is the whole point of putting it there.

def demo_png(width: int = 1024, height: int = 768) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # filter byte: none
        for x in range(width):
            raw += bytes(((x * 255) // width,
                          (y * 255) // height,
                          ((x + y) * 255) // (width + height)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# ── the studies ─────────────────────────────────────────────────────────────

TWIN = ("hdt_portico", "Portico di Saggio B · HDT",
        "https://example.org/hdt/portico-saggio-b")
ENTITY = ("hc1_portico", "Portico di Saggio B")


def build_study(*, graph_id, title, author, orcid, units, chapters,
                image_digest=None, with_entity=True, site=(41.89, 12.49)):
    """One study as an em.json CONTAINER, built with s3Dgraphy.

    The same construction the smokes use, plus a **NarrativeNode with real
    chapters and embeds** — because a reader with nothing to render proves
    nothing, and "the reader works" was one of the things this seed exists to
    let somebody see.
    """
    from s3dgraphy.container import build_container, container_of
    from s3dgraphy.graph import Graph
    from s3dgraphy.importer.emjson_importer import materialize_graph_scope
    from s3dgraphy.nodes import StratigraphicUnit
    from s3dgraphy.nodes.hdt_node import HDTNode
    from s3dgraphy.nodes.heritage_entity_node import HeritageEntityNode
    from s3dgraphy.nodes.narrative_node import NarrativeNode

    graph = Graph(graph_id=graph_id)
    graph.name = {"default": title}

    for unit_id, label, description in units:
        graph.add_node(StratigraphicUnit(unit_id, name=label,
                                         description=description))
    # a stratigraphic relation, so the matrix embed has something to draw
    if len(units) >= 2:
        graph.add_edge(f"{units[1][0]}__is_after__{units[0][0]}",
                       units[1][0], units[0][0], "is_after")

    root = materialize_graph_scope(graph, author=author, license="CC-BY-4.0",
                                   em_id=graph_id.upper(), orcid=orcid)
    if site:
        root.data["site_position"] = {"lat": site[0], "lon": site[1],
                                      "crs": "EPSG:4326"}
    graph.add_node(HDTNode(TWIN[0], name=TWIN[1], heritage_entity_iri=TWIN[2]))
    if with_entity:
        graph.add_node(HeritageEntityNode(ENTITY[0], name=ENTITY[1],
                                          entity_kind="site"))

    # the image, when this study has one: a resource the graph POINTS at
    # (reference + checksum), annotated, so IIIF has both an image and regions
    if image_digest:
        from s3dgraphy.nodes.resource_node import ResourceNode

        from s3dgraphy.nodes.annotation_region_node import AnnotationRegionNode

        # Built through the CONSTRUCTORS, not by writing a `data` dict that
        # looks like one: the classes validate (a rect region without a rect is
        # refused, and rightly), and a hand-written dict would drift from
        # whatever the class decides next release.
        photo = ResourceNode("img-prospetto", name="Prospetto nord",
                             checksum=image_digest, residency="reference")
        photo.data = dict(getattr(photo, "data", None) or {})
        photo.data["media_type"] = "image/png"
        graph.add_node(photo)
        for region_id, label, rect in (
            ("reg-muro", "il muro", [0.10, 0.12, 0.34, 0.26]),
            ("reg-soglia", "la soglia", [0.55, 0.58, 0.22, 0.24]),
        ):
            graph.add_node(AnnotationRegionNode(
                region_id, name=label, shape_kind="rect", rect=rect,
                page=0, resource_id="img-prospetto"))
            graph.add_edge(f"{region_id}__is_on_resource__img-prospetto",
                           region_id, "img-prospetto", "is_on_resource")
        graph.add_edge(f"{units[0][0]}__has_linked_resource__img-prospetto",
                       units[0][0], "img-prospetto", "has_linked_resource")

    graph.add_node(NarrativeNode.from_payload(
        f"{graph_id}-narrativa", f"{title} — il racconto",
        data={"chapters": chapters}))

    container = container_of(graph)
    container.header = {"visibility": "public", "title": title,
                        "description": "Dato di DIMOSTRAZIONE, non uno scavo "
                                       "reale: serve a far vedere le superfici."}
    return build_container(container)


def chapters_for(units, *, with_image: bool, activity_ref: str):
    """Prose and embeds, so the reader has something to render.

    Every embed points at something this study actually holds — an embed that
    resolved to nothing would teach a visitor the wrong thing about the tool.
    """
    blocks = [
        {"block_type": "prose",
         "text": "Il portico è documentato in due fasi. **La matrice** che segue "
                 "mostra le unità di questo studio."},
        {"block_type": "embed", "ref": activity_ref, "view_type": "matrix"},
        {"block_type": "prose",
         "text": "Le stesse unità, lette su un *asse temporale*:"},
        {"block_type": "embed", "ref": activity_ref, "view_type": "timeline"},
        {"block_type": "prose", "text": "E interrogate adesso, come tabella:"},
        {"block_type": "embed", "ref": activity_ref, "view_type": "table"},
    ]
    if units:
        blocks += [
            {"block_type": "prose",
             "text": "La prima unità, con la sua certezza d'esistenza:"},
            {"block_type": "embed", "ref": units[0][0], "view_type": "us"},
        ]
    if with_image:
        blocks += [
            {"block_type": "prose",
             "text": "Il prospetto nord, con le regioni annotate:"},
            {"block_type": "embed", "ref": "img-prospetto",
             "view_type": "document"},
        ]
    return [
        {"title": "Le fasi del portico", "blocks": blocks},
        {"title": "Cosa resta da capire", "blocks": [
            {"block_type": "prose",
             "text": "La relazione fra il colonnato e la soglia non è ancora "
                     "risolta: manca un saggio sul lato est.\n\n"
                     "*Questo è testo di dimostrazione.*"},
        ]},
    ]


# ── the seed ────────────────────────────────────────────────────────────────

def existing_by_em_id(catalog: str, em_id: str, auth: dict) -> str | None:
    """The study this seed already made, if it did. **This is the idempotence.**

    Keyed on `em_id` — which the container carries and the catalogue's card
    exposes — rather than on a study id the catalogue mints: the whole point is
    that a second run recognises its own earlier work, and it cannot do that by
    an identity it did not choose.
    """
    status, body = request(f"{catalog}/catalog/studies", headers=auth)
    if status != 200:
        return None
    for study in json.loads(body).get("studies", []):
        if study.get("em_id") == em_id:
            return study.get("id")
    return None


def main() -> int:
    load_env_file(HERE / ".env.dev")
    load_env_file(HERE / ".env.dev.example")

    keycloak = f"http://localhost:{env('KEYCLOAK_PORT', '8085')}"
    server = f"http://localhost:{env('EM_SERVER_PORT', '8000')}"
    catalog = f"http://localhost:{env('EM_CATALOG_PORT', '8010')}"
    realm = env("DEV_REALM", "em-dev")
    domain = env("EM_DEV_DOMAIN", "em.localhost")
    https_port = env("HTTPS_PORT", "8443")
    public = f"https://{domain}:{https_port}"

    print("seed-demo — popolo il nodo di dato che RESTA\n")

    # ── 1 · a token ─────────────────────────────────────────────────────────
    form = urllib.parse.urlencode({
        "grant_type": "password", "client_id": env("DEV_CLIENT_ID", "em-server"),
        "client_secret": env("DEV_CLIENT_SECRET", "em-dev-secret"),
        "username": env("DEV_USER", "dev"), "password": env("DEV_PASSWORD", "dev"),
    }).encode()
    status, body = request(
        f"{keycloak}/realms/{realm}/protocol/openid-connect/token",
        method="POST", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        print(f"non riesco a ottenere un token dal realm ({status}). "
              f"La stack è su? ./fcn-up.sh")
        return 2
    token = json.loads(body)["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    say("token dal realm dev", "ok")

    # ── 2 · the image, into the object store ────────────────────────────────
    png = demo_png()
    status, body = request(
        f"{server}/v1/rooms/demo/asset?media_type=image/png",
        method="PUT", data=png,
        headers={**auth, "Content-Type": "image/png"})
    if status != 200:
        print(f"l'immagine non è stata accettata ({status}): "
              f"{body[:200].decode(errors='replace')}")
        return 1
    asset = json.loads(body)
    digest = asset["ref"]
    # Content-addressed, so re-running does not make a second object: the same
    # bytes are the same object, and the answer says `created: false`.
    say("immagine nell'object store",
        f"{digest[:20]}… · {asset['size']} B · "
        f"{'nuova' if asset.get('created') else 'già presente'}")

    # ── 3 · the two studies ─────────────────────────────────────────────────
    studies = [
        {
            "em_id": "PORTICO-2026",
            "doc": build_study(
                graph_id="portico-2026", title="Portico · campagna 2026",
                author="Emanuel Demetrescu", orcid="0000-0002-1825-0097",
                units=[("US1", "US 1 · muro",
                        "Muro in opus mixtum, due filari conservati."),
                       ("US2", "US 2 · soglia",
                        "Soglia in travertino, consumata al centro.")],
                chapters=chapters_for(
                    [("US1", "US 1 · muro", "")], with_image=True,
                    activity_ref="portico-2026_graphroot"),
                image_digest=digest),
        },
        {
            # The same monument, another campaign — so the HDT view has two
            # studies to group, which is the thing that view exists to show.
            "em_id": "PORTICO-1978",
            "doc": build_study(
                graph_id="portico-1978", title="Portico · campagna 1978",
                author="Tizia Caia", orcid=None,
                units=[("US10", "US 10 · crollo",
                        "Strato di crollo, laterizi e malta.")],
                chapters=chapters_for(
                    [("US10", "US 10 · crollo", "")], with_image=False,
                    activity_ref="portico-1978_graphroot"),
                with_entity=False, site=(41.89, 12.49)),
        },
    ]

    registered = []
    for study in studies:
        already = existing_by_em_id(catalog, study["em_id"], auth)
        query = f"?study_id={urllib.parse.quote(already)}" if already else ""
        status, body = request(
            f"{catalog}/catalog/studies{query}", method="POST",
            data=json.dumps(study["doc"]).encode(),
            headers={**auth, "Content-Type": "application/json"})
        if status != 201:
            print(f"lo studio {study['em_id']} non è stato registrato "
                  f"({status}): {body[:200].decode(errors='replace')}")
            return 1
        answer = json.loads(body)
        registered.append(answer)
        say(f"studio {study['em_id']}",
            f"{answer['id']} · {'aggiornato' if already else 'nuovo'}")

    public_study = registered[0]["id"]
    twin = TWIN[2]
    # The IIIF identifier is the **bare hex**, without the `sha256:` prefix —
    # measured, not assumed, and it is the one thing here that looks wrong and
    # is right. Cantaloupe uses the identifier AS the object key, and the key in
    # the bucket is the hex; asking for `sha256%3A<hex>` makes it look for an
    # object of that name and answer 404 about an image that is plainly there
    # (`NoSuchFileException … key: sha256:58ad…`, which reads like the image is
    # missing rather than like the name is).
    iiif_id = digest.split(":", 1)[-1]

    # ── what somebody should open ───────────────────────────────────────────
    print(f"""
Fatto. Da aprire nel browser (host di Caddy):

  catalogo            {public}/catalog/studies
  studio (em.json)    {public}/catalog/study/{public_study}/emjson
  il RACCONTO         {public}/catalog/study/{public_study}/narrative
  vista HDT           {public}/catalog/hdt/{urllib.parse.quote(twin, safe='')}
  IIIF info.json      {public}/iiif/3/{iiif_id}/info.json
  miniatura           {public}/iiif/3/{iiif_id}/full/!240,240/0/default.jpg

Senza Caddy, sulle porte host: catalog {catalog} · em-server {server} ·
immagini http://localhost:{env('CANTALOUPE_PORT', '8182')}/iiif/3

Il certificato di Caddy è di una CA interna: se il browser protesta,
./fcn-trust-ca.sh (chiede la password).

Questi dati RESTANO. Per azzerare: ./fcn-down.sh --wipe  (e ri-lancia questo).""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
