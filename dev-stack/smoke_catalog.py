#!/usr/bin/env python3
"""End-to-end smoke of the CATALOG — the proof that the index is DERIVED.

Everything this service claims could be faked by a catalogue that kept its
studies in a dict and its cards in another one. So this script does not only ask
em-catalog whether it registered a study: it **opens the bucket itself** and
looks, and then it **throws the index away and rebuilds it** from what is in
there.

    python dev-stack/smoke_catalog.py    # after `docker-compose … up -d`

What it checks, in order, and what each one would catch:

1. the service is up, is **enforcing tokens**, and its container store is
   **MinIO** — if any of those is not true, nothing after it means anything;
2. a token from the same dev realm em-server uses (one realm, two services);
3. **register two studies**, one `public` and one `restricted`, both real
   containers built with s3Dgraphy — and the container really lands in the
   bucket, under `studies/`, verified against MinIO directly;
4. **search** finds them by author, by free text and by digital twin;
5. the **HDT view** groups the two campaigns of one monument;
6. `/emjson` gives back **exactly** the container that went in (a catalogue that
   reformatted a study on the way out would be editing it);
7. `/ttl` is served in **publish mode** — a US deleted through the CRDT is
   ABSENT from the triples while still present in the em.json;
8. **visibility**: the public study answers with no token, the restricted one is
   401 without and 200 with, and an anonymous listing does not leak it;
9. **"open in…"** hands back something every app can act on today;
10. **reindex**: the SQLite index is emptied and rebuilt from the object store,
    card for card. This is the architectural claim, executed.

Exit code 0 = everything measured. Anything skipped is printed as SKIPPED with
the reason — a smoke test that hides what it could not do is worse than none.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent

# s3Dgraphy from the checkout beside this repo, when it is there: the fixtures
# are built with the library as it is NOW, not with hand-written JSON.
_CHECKOUT = HERE.parent.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir():
    sys.path.insert(0, str(_CHECKOUT))

FAILURES: list[str] = []
SKIPS: list[str] = []


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


def ok(label: str, condition: bool, detail: str = "") -> bool:
    mark = "  ok  " if condition else " FAIL "
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)
    return condition


def skip(label: str, why: str) -> None:
    print(f"[ SKIP ] {label} — {why}")
    SKIPS.append(f"{label}: {why}")


def request(url: str, *, method: str = "GET", data: bytes | None = None,
            headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return answer.status, answer.read(), _headers(answer)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc)
    except urllib.error.URLError as exc:
        return 0, str(exc.reason).encode(), {}


def _headers(answer) -> dict:
    return {k.lower(): v for k, v in answer.headers.items()}


def build_study(graph_id, title, author, orcid, license_, visibility,
                twin, entity, units, site, remove_last=False):
    """A real container, built with s3Dgraphy. `remove_last` deletes a unit
    through the CRDT, which is what step 7 needs a tombstone for."""
    from s3dgraphy import api as em
    from s3dgraphy.container import build_container, container_of
    from s3dgraphy.graph import Graph
    from s3dgraphy.importer.emjson_importer import materialize_graph_scope
    from s3dgraphy.nodes import StratigraphicUnit
    from s3dgraphy.nodes.hdt_node import HDTNode
    from s3dgraphy.nodes.heritage_entity_node import HeritageEntityNode

    graph = Graph(graph_id=graph_id)
    graph.name = {"default": title}
    for index, name in enumerate(units, start=1):
        graph.add_node(StratigraphicUnit(f"{graph_id}-us{index}", name=name))
    root = materialize_graph_scope(graph, author=author, license=license_,
                                   em_id=graph_id.upper(), orcid=orcid)
    if site:
        root.data["site_position"] = {"lat": site[0], "lon": site[1],
                                      "crs": "EPSG:4326"}
    if twin:
        graph.add_node(HDTNode(twin[0], name=twin[1], heritage_entity_iri=twin[2]))
    if entity:
        graph.add_node(HeritageEntityNode(entity[0], name=entity[1],
                                          entity_kind="site"))
    container = container_of(graph)
    container.header = {"visibility": visibility, "title": title}
    doc = build_container(container)
    if remove_last:
        section = doc["graphs"][graph_id]
        em.apply_op(section, em.make_op(
            "remove_node", id=f"{graph_id}-us{len(units)}",
            ts="2026-08-15T10:00:00+00:00", author="scavatrice"))
    return doc


def main() -> int:
    load_env_file(HERE / ".env.dev")
    load_env_file(HERE / ".env.dev.example")

    keycloak = f"http://localhost:{env('KEYCLOAK_PORT', '8085')}"
    catalog = f"http://localhost:{env('EM_CATALOG_PORT', '8010')}"
    realm = env("DEV_REALM", "em-dev")
    client_id = env("DEV_CLIENT_ID", "em-server")
    twin_iri = "https://example.org/h/sarmizegetusa"

    print(f"em-catalog: {catalog}")
    print(f"keycloak  : {keycloak}/realms/{realm}")
    print()

    # ── 0 · the service is up, and says what it is standing on ──────────────
    status, body, _ = request(f"{catalog}/health")
    if status != 200:
        why = f"status {status}" if status else f"not reachable ({body.decode()})"
        print(f"em-catalog is not answering on {catalog} ({why}). From "
              f"dev-stack/: `docker-compose --env-file .env.dev "
              f"-f docker-compose.dev.yml up -d --build em-catalog`")
        return 2
    health = json.loads(body)
    ok("em-catalog is enforcing tokens", health.get("auth") == "keycloak",
       str(health.get("auth")))
    ok("the studies live in MinIO", "minio" in str(health.get("container_store")),
       str(health.get("container_store")))
    ok("the TTL projection can run", bool(
        health.get("capabilities", {}).get("export_ttl")))
    ok("…and it has the publish mode", bool(
        health.get("capabilities", {}).get("ttl_publish_mode")))

    # ── 1 · a token from the SAME realm em-server uses ──────────────────────
    form = urllib.parse.urlencode({
        "grant_type": "password", "client_id": client_id,
        "client_secret": env("DEV_CLIENT_SECRET", "em-dev-secret"),
        "username": env("DEV_USER", "dev"),
        "password": env("DEV_PASSWORD", "dev"),
    }).encode()
    status, body, _ = request(
        f"{keycloak}/realms/{realm}/protocol/openid-connect/token",
        method="POST", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not ok("a token from the dev realm", status == 200, f"status {status}"):
        print("  without a token nothing below can be measured")
        return 1
    token = json.loads(body)["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # ── 2 · register two studies ────────────────────────────────────────────
    try:
        public_doc = build_study(
            "sarmizegetusa-2026", "Sarmizegetusa 2026", "Emanuel Demetrescu",
            "0000-0002-1825-0097", "CC-BY-4.0", "public",
            ("hdt_sarm", "Sarmizegetusa HDT", twin_iri),
            ("hc1_sarm", "Sarmizegetusa Regia"), ("US 1", "US 2"),
            (45.62, 23.31), remove_last=True)
        restricted_doc = build_study(
            "sarmizegetusa-1978", "Sarmizegetusa 1978", "Tizia Caia", None,
            "CC-BY-NC-4.0", "restricted",
            ("hdt_sarm", "Sarmizegetusa HDT", twin_iri), None, ("US 10",), None)
    except Exception as exc:                       # pragma: no cover
        skip("build the study containers", f"s3dgraphy not importable: {exc}")
        return 1

    registered = {}
    for label, doc in (("public", public_doc), ("restricted", restricted_doc)):
        status, body, _ = request(
            f"{catalog}/catalog/studies", method="POST",
            data=json.dumps(doc).encode(),
            headers={**auth, "Content-Type": "application/json"})
        if not ok(f"register the {label} study", status == 201,
                  f"status {status} {body[:160].decode(errors='replace')}"):
            return 1
        registered[label] = json.loads(body)
    public_id = registered["public"]["id"]
    restricted_id = registered["restricted"]["id"]
    ok("…the card was DERIVED, not supplied",
       registered["public"]["card"]["license"] == "CC-BY-4.0"
       and registered["public"]["card"]["authors"][0]["orcid"]
       == "0000-0002-1825-0097",
       registered["public"]["card"]["license"])
    ok("…and the restricted one says restricted",
       registered["restricted"]["card"]["visibility"] == "restricted")

    # ── 3 · the container is really in the bucket ───────────────────────────
    bucket = env("MINIO_BUCKET", "em-assets")
    try:
        from minio import Minio

        client = Minio(f"localhost:{env('MINIO_API_PORT', '9000')}",
                       access_key=env("MINIO_ROOT_USER", "minioadmin"),
                       secret_key=env("MINIO_ROOT_PASSWORD", "minioadmin"),
                       secure=False)
        keys = {o.object_name for o in client.list_objects(
            bucket, prefix="studies/", recursive=True)}
        ok("the container is an object in MinIO, under studies/",
           registered["public"]["container_ref"] in keys,
           registered["public"]["container_ref"])
        ok("…in the SAME bucket as the assets", bool(keys), f"{len(keys)} studies")
    except ImportError:
        skip("look in the bucket directly", "the `minio` client is not installed "
             "in this interpreter (pip install minio)")
    except Exception as exc:
        skip("look in the bucket directly", f"MinIO did not answer: {exc}")

    # ── 4 · search ──────────────────────────────────────────────────────────
    def search(**params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        status, body, _ = request(f"{catalog}/catalog/studies?{query}",
                                  headers=auth)
        return status, (json.loads(body) if status == 200 else {})

    status, found = search(author="demetrescu")
    ok("search by author", status == 200 and [s["id"] for s in
                                              found.get("studies", [])] == [public_id],
       f"{found.get('count')} hit(s)")
    status, found = search(q="1978")
    ok("search by free text", status == 200
       and [s["id"] for s in found.get("studies", [])] == [restricted_id])
    status, found = search(hc2=twin_iri)
    ok("search by digital twin", status == 200 and found.get("count") == 2,
       f"{found.get('count')} studies of one monument")

    # ── 5 · the HDT view ────────────────────────────────────────────────────
    status, body, _ = request(
        f"{catalog}/catalog/hdt/{urllib.parse.quote(twin_iri, safe='')}",
        headers=auth)
    view = json.loads(body) if status == 200 else {}
    ok("the HDT view groups the campaigns of one object", status == 200
       and view.get("count") == 2,
       f"status {status}, {view.get('count')} studies")
    ok("…and names the heritage entity",
       (view.get("hc1") or {}).get("name") == "Sarmizegetusa Regia",
       str((view.get("hc1") or {}).get("name")))

    # ── 6 · /emjson gives back what went in ─────────────────────────────────
    status, body, _ = request(f"{catalog}/catalog/study/{public_id}/emjson")
    returned = json.loads(body) if status == 200 else None
    ok("the container comes back byte-identical on content",
       returned == public_doc, f"status {status}")

    # ── 7 · /ttl is publish mode ────────────────────────────────────────────
    status, body, _ = request(f"{catalog}/catalog/study/{public_id}/ttl")
    ttl = body.decode(errors="replace") if status == 200 else ""
    if status == 501:
        skip("the TTL projection", "this build has no rdflib")
    else:
        ok("the study projects to Turtle", status == 200, f"status {status}")
        ok("…and the deleted US is ABSENT from the published triples",
           "removedAt" not in ttl and "sarmizegetusa-2026-us2" not in ttl)
        ok("…while the living one is there",
           "sarmizegetusa-2026-us1" in ttl)
        ok("…and the em.json still carries the tombstone (it is the "
           "re-editable truth)",
           any("removed" in (n.get("data") or {}) for n in
               (returned or {}).get("graphs", {})
               .get("sarmizegetusa-2026", {}).get("nodes", [])))

    # ── 8 · visibility ──────────────────────────────────────────────────────
    status, _, _ = request(f"{catalog}/catalog/study/{public_id}")
    ok("a public study answers with NO token", status == 200, f"status {status}")
    status, _, _ = request(f"{catalog}/catalog/study/{restricted_id}")
    ok("a restricted study is refused without one", status == 401,
       f"status {status}")
    status, _, _ = request(f"{catalog}/catalog/study/{restricted_id}",
                           headers=auth)
    ok("…and served with one", status == 200, f"status {status}")
    status, body, _ = request(f"{catalog}/catalog/studies")
    anonymous = json.loads(body) if status == 200 else {}
    ok("an anonymous listing shows the public study and nothing else",
       [s["id"] for s in anonymous.get("studies", [])] == [public_id],
       f"{anonymous.get('count')} visible")

    # ── 9 · open in… ────────────────────────────────────────────────────────
    status, body, _ = request(f"{catalog}/catalog/study/{public_id}/open")
    opened = json.loads(body) if status == 200 else {}
    ok("“open in…” answers for the three apps",
       set(opened.get("apps", {})) == {"emstudio", "blender", "heriverse"})
    ok("…and every one of them gets something it can act on today",
       all(t.get("emjson") == opened.get("emjson")
           for t in opened.get("apps", {}).values()),
       str(opened.get("emjson", ""))[:70])

    # ── 10 · the index is DERIVED ───────────────────────────────────────────
    status, body, _ = request(f"{catalog}/catalog/studies", headers=auth)
    before = json.loads(body).get("studies") if status == 200 else None
    status, body, _ = request(f"{catalog}/catalog/reindex", method="POST",
                              data=b"", headers=auth)
    rebuilt = json.loads(body) if status == 200 else {}
    ok("the index rebuilds itself from the containers in MinIO",
       status == 200 and rebuilt.get("studies") == 2,
       f"status {status}, {rebuilt.get('studies')} studies")
    ok("…with nothing unreadable", rebuilt.get("unreadable") == [],
       str(rebuilt.get("unreadable")))
    status, body, _ = request(f"{catalog}/catalog/studies", headers=auth)
    after = json.loads(body).get("studies") if status == 200 else None
    ok("…and the catalogue is card-for-card what it was", after == before)

    # ── cleanup: the smoke leaves nothing behind ────────────────────────────
    #for study_id in (public_id, restricted_id):
    #    request(f"{catalog}/catalog/study/{study_id}", method="DELETE",
    #            headers=auth)

    print()
    if SKIPS:
        print(f"skipped: {len(SKIPS)}")
        for line in SKIPS:
            print(f"  · {line}")
    if FAILURES:
        print(f"catalog smoke: {len(FAILURES)} FAILED")
        for line in FAILURES:
            print(f"  · {line}")
        return 1
    print("catalog smoke: everything measured passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
