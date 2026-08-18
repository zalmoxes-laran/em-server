"""The RESIDENT corpus: the documentation em-server can read, so the rights bite.

The hole these tests close was measured on 17 Aug against the live stack: an
asset was uploaded to MinIO and its licence declared in a per-project corpus
**file**, and `GET …/asset/<ref>` still answered `x-em-license: null` — the
enforcement reads the rights out of a document the server holds, and a member of
somebody's em.json is not one.

What is defended here:

* **A · the register** — one corpus per instance, held by a store with the same
  discipline as a room snapshot: the three library acts append to it, `GET
  /v1/corpus` reads it, a **slice** by sha256 returns only what speaks about
  those files, the version is the CONTENT digest, and it survives a restart
  because the truth is in the store and not in the process;
* **A′ · the promote** — a project's file corpus **merges** into the resident one,
  per UUID, idempotently: offline is not a lesser mode;
* **B · the payment** — a licence declared in the resident corpus comes back in
  `x-em-license` (this is the header that was null), and an embargo declared
  there **refuses a viewer with the date** and serves an editor. Which is exactly
  what the file corpus could not do.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ws as ws_module                          # noqa: E402
from app.access import Acl, InMemoryAclStore             # noqa: E402
from app.corpus import (InMemoryCorpusStore,             # noqa: E402
                        ResidentCorpus, canonical_digest, empty_corpus,
                        DirectoryCorpusStore, slice_for)
from app.main import app                                 # noqa: E402
from app.rooms import RoomRegistry                       # noqa: E402
from app.store import InMemorySnapshotStore              # noqa: E402

ANNA = "0000-0002-1825-0097"     # owner of the room
CARLA = "0000-0003-1415-9265"    # editor
BRUNO = "0000-0001-5109-3700"    # viewer

PIXELS = b"\x89PNG\r\n\x1a\n" + b"the north elevation, as pixels"
OTHER = b"a second file, whose rights nobody declared"

HEAD = {"Authorization": "Bearer t"}


# ── the harness ──────────────────────────────────────────────────────────────

@pytest.fixture()
def resident(monkeypatch):
    """An empty resident corpus, in memory, that the app actually uses."""
    from app import main as main_module

    store = InMemoryCorpusStore()
    corpus = ResidentCorpus(store)
    monkeypatch.setattr(main_module, "RESIDENT", corpus)
    monkeypatch.setattr(main_module, "CORPUS_STORE", store)
    return corpus


@pytest.fixture()
def assets(monkeypatch):
    """Two real assets in a store the app uses, and a room that owns nothing."""
    from app.assets import InMemoryAssetStore
    from app import main as main_module

    store = InMemoryAssetStore()
    refs = {"picture": store.put(PIXELS, "image/png")["ref"],
            "other": store.put(OTHER, "text/plain")["ref"]}
    monkeypatch.setattr(main_module, "ASSET_STORE", store)

    # A room WITHOUT any rights in its graph: everything these tests measure has
    # to come from the corpus, or it proves nothing about the corpus.
    snapshots = InMemorySnapshotStore()
    snapshots.put("scavo", {"header": {"format": "em.json", "version": "1.0",
                                       "visibility": "restricted", "owner": ANNA},
                            "graphs": {"scavo": {"graph_id": "scavo", "nodes": [],
                                                 "edges": []}},
                            "active_graph_id": "scavo"})
    acls = InMemoryAclStore()
    acls.put("scavo", Acl(owner=ANNA,
                          members={CARLA: "editor", BRUNO: "viewer"}).as_dict())
    monkeypatch.setattr(ws_module, "SNAPSHOT_STORE", snapshots)
    monkeypatch.setattr(ws_module, "ROOMS", RoomRegistry(snapshots))
    monkeypatch.setattr(ws_module, "ACL_STORE", acls)
    return refs


@pytest.fixture()
def whoever(monkeypatch):
    """Tokens are enforced, and we choose whose they are.

    Patches the authenticator **`app.main` is actually holding**, and `app.ws`'s
    too when they are not the same object. They can differ: `test_auth` reloads
    `app.auth` and `app.main` to check the real wiring, which rebinds main's
    reference and leaves ws's pointing at the module-load original. Patching only
    one of them made these tests pass alone and fail in the suite — measured, and
    the kind of green that is worse than red.
    """
    from app import main as main_module

    class Enforcing:
        enforcing = True

        def describe(self):
            return "keycloak"

    targets = {id(main_module.authenticator): main_module.authenticator,
               id(ws_module.authenticator): ws_module.authenticator}
    for auth in targets.values():
        monkeypatch.setattr(auth, "settings", Enforcing())

    def be(orcid):
        claims = {"orcid": orcid} if orcid else {}
        for auth in targets.values():
            monkeypatch.setattr(auth, "verify", lambda token: dict(claims))
            monkeypatch.setattr(auth, "require_token",
                                lambda request: dict(claims))
    return be


@pytest.fixture()
def client():
    return TestClient(app)


def resource_node(digest: str, name: str = "IMG_0001.png") -> dict:
    return {"id": name.replace(".", "_"), "node_type": "resource", "name": name,
            "data": {"checksum": digest, "residency": "resident",
                     "media_type": "image/png"}}


# ── A · the register ─────────────────────────────────────────────────────────

def test_an_instance_with_no_documentation_has_an_empty_corpus_not_a_missing_one(
        client, resident):
    answer = client.get("/v1/corpus", headers=HEAD)
    assert answer.status_code == 200
    body = answer.json()
    assert body["nodes"] == 0 and body["edges"] == 0
    assert body["graph"]["data"]["em_collection"] == "DTCCorpus", \
        "the marker is what makes it a corpus rather than a graph called dtc"
    assert body["version"] == canonical_digest(empty_corpus())


def test_a_file_is_registered_only_if_this_instance_HOLDS_it(client, resident,
                                                             assets):
    """The door into the register — and the check that keeps it honest.

    The other acts all speak ABOUT a file (`enrich_asset_dtc` refuses to invent
    one), so a client that has just uploaded bytes registers them first. That is
    also the moment em-server can look in its own store: a digest nobody uploaded
    would sit in the corpus carrying a licence for bytes that are not there.
    """
    ok = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "resource", "checksum": assets["picture"], "name": "IMG_0001.png",
        "media_type": "image/png"})
    assert ok.status_code == 200
    assert ok.json()["report"]["created"] is True

    again = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "resource", "checksum": assets["picture"], "name": "another name"})
    assert again.json()["report"]["created"] is False
    assert again.json()["report"]["resource_id"] == ok.json()["report"]["resource_id"], \
        "the id comes from the DIGEST, so two clients converge on one entry"
    names = [n["name"] for n in resident.read()["nodes"]
             if n.get("node_type") == "resource"]
    assert names == ["IMG_0001.png"], \
        "fields are filled in, never overwritten: the first description stands"

    ghost = "sha256:" + "99" * 32
    refused = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "resource", "checksum": ghost})
    assert refused.status_code == 404
    assert "after uploading it" in refused.json()["detail"]

    # …and a file whose bytes live elsewhere is legitimate, declared as such
    elsewhere = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "resource", "checksum": ghost, "residency": "reference",
        "url": "https://zenodo.org/record/1/files/plan.tif"})
    assert elsewhere.status_code == 200


def test_the_three_acts_append_and_the_corpus_shows_them(client, resident, assets):
    # a file the corpus knows, so the acquisition has something to bucket
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)

    lot = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "acquisition", "resources": [assets["picture"]],
        "name": "Volo 2026-03", "metadata": {"camera": "DJI P4"}}).json()
    assert lot["report"]["count"] == 1, "the file was bucketed"
    assert lot["report"]["created"] is True

    # …and a derivation off that lot
    section = resident.read()
    section["nodes"].append(resource_node(assets["other"], "ortofoto.tif"))
    resident.store.put(section)
    chain = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "derivation", "output": assets["other"],
        "inputs": [lot["report"]["acquisition_id"]], "tool": "Metashape"}).json()
    assert chain["report"]["process_id"]

    whole = client.get("/v1/corpus", headers=HEAD).json()
    types = sorted({n["node_type"] for n in whole["graph"]["nodes"]})
    assert "dtc_acquisition" in types and "dtc_process" in types
    assert whole["version"] != lot["version"], \
        "the version follows the CONTENT: a second act is a different corpus"


def test_the_same_act_twice_is_one_lot(client, resident, assets):
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    body = {"act": "acquisition", "resources": [assets["picture"]],
            "name": "Volo 2026-03"}
    first = client.post("/v1/corpus/append", headers=HEAD, json=body).json()
    second = client.post("/v1/corpus/append", headers=HEAD, json=body).json()

    assert second["report"]["acquisition_id"] == first["report"]["acquisition_id"]
    assert second["report"]["created"] is False
    assert second["version"] == first["version"], \
        "nothing changed, so the version did not move — it measures the corpus"
    lots = [n for n in client.get("/v1/corpus", headers=HEAD).json()["graph"]["nodes"]
            if n["node_type"] == "dtc_acquisition"]
    assert len(lots) == 1


def test_an_act_nobody_implements_is_a_400_that_says_what_is_accepted(
        client, resident):
    answer = client.post("/v1/corpus/append", headers=HEAD,
                         json={"act": "teleport"})
    assert answer.status_code == 400
    detail = answer.json()["detail"]
    assert "acquisition" in detail and "derivation" in detail and "attribution" in detail


def test_the_attributor_is_the_TOKEN_never_a_field_in_the_body(
        client, resident, assets, whoever):
    whoever(CARLA)
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    answer = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "license": "CC-BY-4.0", "author": ANNA,
        # a caller claiming to BE somebody else: the attributor still comes from
        # the token, because who says so is not a field anybody gets to fill in
        "attributor": BRUNO}).json()
    assert answer["by"] == CARLA
    report = answer["report"]
    assert report["attributor"] == CARLA, \
        "signed by the token, not by the `attributor` the body tried to claim"
    assert report["changed"] == {"author": "declared", "license": "declared"}


def test_an_attribution_with_nobody_to_sign_it_is_refused(client, resident,
                                                          assets):
    """Dev mode has no identities, and the library will not let an attribution be
    unsigned: "an attribution nobody signs is a rumour". So on a laptop with no
    OIDC the register takes acquisitions and derivations, and refuses to record
    who owns what — which is the honest answer, not a missing feature."""
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    answer = client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"], "license": "CC-BY-4.0"})
    assert answer.status_code == 400
    assert "attributor" in answer.json()["detail"]


def test_a_slice_answers_about_those_files_and_keeps_their_chain(
        client, resident, assets, whoever):
    whoever(ANNA)
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    section["nodes"].append(resource_node(assets["other"], "unrelated.txt"))
    resident.store.put(section)
    assert client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "acquisition", "resources": [assets["picture"]],
        "name": "Volo"}).status_code == 200
    assert client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "license": "CC-BY-4.0"}).status_code == 200

    whole = client.get("/v1/corpus", headers=HEAD).json()
    part = client.get("/v1/corpus", headers=HEAD,
                      params={"sha256": assets["picture"]}).json()

    assert part["sliced"] is True and whole["sliced"] is False
    names = {n.get("name") for n in part["graph"]["nodes"]}
    assert "IMG_0001.png" in names, "the file asked about"
    assert "Volo" in names, "…and the acquisition that brought it in"
    assert "CC-BY-4.0" in names, "…and its licence, or the slice answers wrongly"
    assert "unrelated.txt" not in names, "…and nothing else"
    assert part["version"] == whole["version"], \
        "a slice still reports the version of the register it came out of"


def test_the_corpus_survives_a_restart_because_the_truth_is_in_the_store(tmp_path):
    """A directory store, two `ResidentCorpus` objects: the second one is what a
    restarted process has."""
    store = DirectoryCorpusStore(tmp_path)
    first = ResidentCorpus(store)
    section = first.read()
    section["nodes"].append(resource_node("sha256:" + "ab" * 32))
    store.put(section)
    first.append("acquisition", {"resources": ["sha256:" + "ab" * 32],
                                 "name": "Volo di ieri"}, author=ANNA)

    assert (tmp_path / "corpus.em.json").is_file(), "the bytes are on disk"
    reborn = ResidentCorpus(DirectoryCorpusStore(tmp_path))
    lots = [n for n in reborn.read()["nodes"]
            if n.get("node_type") == "dtc_acquisition"]
    assert [n.get("name") for n in lots] == ["Volo di ieri"]
    assert reborn.version() == first.version()


def test_a_slice_of_a_digest_nobody_documented_is_empty_not_the_whole_corpus():
    section = empty_corpus()
    section["nodes"].append(resource_node("sha256:" + "cd" * 32))
    empty = slice_for(section, ["sha256:" + "ef" * 32])
    assert empty["nodes"] == [] and empty["edges"] == []
    assert empty["data"]["em_collection"] == "DTCCorpus", "still a corpus"


# ── A′ · the promote: a file corpus comes home ───────────────────────────────

def test_a_project_corpus_merges_into_the_resident_one_without_duplicating(
        client, resident):
    """Offline is not a lesser mode: what was documented on a laptop arrives."""
    incoming = empty_corpus()
    incoming["nodes"].append(resource_node("sha256:" + "11" * 32, "laptop.jpg"))
    incoming["nodes"].append({"id": "acq_laptop", "node_type": "dtc_acquisition",
                              "name": "Volo offline",
                              "data": {"dtc_kind": "local_import"}})
    incoming["edges"].append({"id": "e1", "source": "acq_laptop",
                              "target": "laptop_jpg",
                              "edge_type": "dtc_had_output"})

    first = client.post("/v1/corpus/merge", headers=HEAD, json=incoming)
    assert first.status_code == 200
    names = {n.get("name") for n in resident.read()["nodes"]}
    assert {"laptop.jpg", "Volo offline"} <= names

    before = resident.version()
    again = client.post("/v1/corpus/merge", headers=HEAD, json=incoming).json()
    assert again["report"]["added_nodes"] == 0, "promoting twice adds nothing"
    assert resident.version() == before, "…so the version does not move either"


def test_a_container_can_be_posted_and_its_corpus_member_is_found_by_the_marker(
        client, resident):
    incoming = empty_corpus()
    incoming["nodes"].append(resource_node("sha256:" + "22" * 32, "dal_file.jpg"))
    container = {"header": {"format": "em.json", "version": "1.0"},
                 "graphs": {"basilica": {"graph_id": "basilica", "nodes": [],
                                         "edges": []},
                            "dtc": incoming},
                 "active_graph_id": "basilica"}
    assert client.post("/v1/corpus/merge", headers=HEAD,
                       json=container).status_code == 200
    assert "dal_file.jpg" in {n.get("name") for n in resident.read()["nodes"]}

    without = {"header": {}, "graphs": {"basilica": {"graph_id": "basilica",
                                                     "nodes": [], "edges": []}}}
    refused = client.post("/v1/corpus/merge", headers=HEAD, json=without)
    assert refused.status_code == 400
    assert "marker" in refused.json()["detail"]


# ── B · the payment: the rights bite when the asset is served ────────────────

def test_a_licence_declared_in_the_RESIDENT_corpus_reaches_the_asset_header(
        client, resident, assets, whoever):
    """THE MEASURED HOLE. With the licence in a file corpus this header was
    `null`; with the corpus resident it is the licence."""
    whoever(CARLA)
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)

    before = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert before.status_code == 200
    # the corpus knows the FILE but nobody has declared anything about it: what
    # comes back is the DEFAULT, and it says so — a reader can tell a decision
    # from a fallback
    assert before.headers.get("X-EM-License-Default") == "true"

    assert client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "license": "CC-BY-4.0", "author": ANNA,
        "author_name": "Anna"}).status_code == 200

    after = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert after.status_code == 200 and after.content == PIXELS
    assert after.headers["X-EM-License"] == "CC-BY-4.0", \
        "THE HEADER THAT WAS NULL: declared in the resident corpus, served here"
    assert not after.headers.get("X-EM-License-Default")
    assert ANNA in (after.headers.get("X-EM-Author") or "")


def test_an_embargo_in_the_resident_corpus_refuses_a_viewer_and_serves_an_editor(
        client, resident, assets, whoever):
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    whoever(ANNA)
    client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "license": "CC-BY-4.0", "embargo": "2099-01-01",
        "reason": "in corso di studio"})

    whoever(BRUNO)                                   # viewer in the room
    refused = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert refused.status_code == 403
    assert "2099-01-01" in refused.json()["detail"], \
        "the refusal names the date: 'no' with a reason is actionable"

    whoever(CARLA)                                   # editor
    served = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert served.status_code == 200 and served.content == PIXELS

    whoever(ANNA)                                    # owner
    assert client.get(f"/v1/rooms/scavo/asset/{assets['picture']}",
                      headers=HEAD).status_code == 200


def test_an_expired_embargo_in_the_corpus_is_over_for_everybody(
        client, resident, assets, whoever):
    """The verdict is the DATE at request time, never a stored flag."""
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    whoever(ANNA)
    client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "embargo": "2000-01-01"})

    whoever(BRUNO)
    answer = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert answer.status_code == 200, "an embargo that ended in 2000 is over"


def test_a_file_the_corpus_says_nothing_about_is_served_exactly_as_before(
        client, resident, assets, whoever):
    whoever(BRUNO)
    answer = client.get(f"/v1/rooms/scavo/asset/{assets['other']}", headers=HEAD)
    assert answer.status_code == 200 and answer.content == OTHER
    assert not answer.headers.get("X-EM-License")


def test_the_corpus_embargo_cannot_be_walked_around_by_naming_another_room(
        client, resident, assets, whoever):
    """The register is per-instance and content-addressed, so the door does not
    change the answer — which is the bug the room walk was fixed for, one level
    up."""
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    whoever(ANNA)
    client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "embargo": "2099-01-01"})

    whoever(BRUNO)
    # a room nobody has ever opened: the viewer has no role there either
    answer = client.get(f"/v1/rooms/altrove/asset/{assets['picture']}", headers=HEAD)
    assert answer.status_code == 403


def test_the_author_of_the_file_is_not_locked_out_of_their_own_embargo(
        client, resident, assets, whoever):
    """The embargo protects the people who made the file; refusing them their own
    photograph while it is under study would be the gate working against the
    person it works for."""
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    whoever(ANNA)
    client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "author": BRUNO, "author_name": "Bruno", "embargo": "2099-01-01"})

    # BRUNO is only a VIEWER in the room — and the author of the file
    whoever(BRUNO)
    assert client.get(f"/v1/rooms/scavo/asset/{assets['picture']}",
                      headers=HEAD).status_code == 200

    # …while somebody who is neither is refused
    whoever("0000-0009-0009-0009")
    assert client.get(f"/v1/rooms/scavo/asset/{assets['picture']}",
                      headers=HEAD).status_code == 403


def test_an_invented_room_grants_nothing_even_though_it_would_be_owned(
        client, resident, assets, whoever):
    """`ws.authorize` makes the first authenticated arrival the OWNER of an
    unowned room — right when somebody opens a room they made, and a loophole as
    an authorisation check. The gate reads the grants and claims none."""
    section = resident.read()
    section["nodes"].append(resource_node(assets["picture"]))
    resident.store.put(section)
    whoever(ANNA)
    client.post("/v1/corpus/append", headers=HEAD, json={
        "act": "attribution", "checksum": assets["picture"],
        "embargo": "2099-01-01"})

    whoever(BRUNO)
    invented = client.get(f"/v1/rooms/una-stanza-inventata/asset/{assets['picture']}",
                          headers=HEAD)
    assert invented.status_code == 403, \
        "a room name typed into a URL is not a role"


def test_a_corpus_that_will_not_read_fails_CLOSED(client, resident, assets,
                                                  whoever, monkeypatch):
    """'I cannot read the documentation' must never answer 'yes'."""
    class Broken:
        def get(self):
            raise OSError("the object store is not answering")

        def put(self, document):    # pragma: no cover — never reached
            raise OSError("nor writing")

    from app import main as main_module
    monkeypatch.setattr(main_module, "RESIDENT", ResidentCorpus(Broken()))
    whoever(CARLA)
    answer = client.get(f"/v1/rooms/scavo/asset/{assets['picture']}", headers=HEAD)
    assert answer.status_code == 503
    assert "documentation" in answer.json()["detail"]


def test_health_says_where_the_documentation_lives(client, resident):
    body = client.get("/v1/health").json()
    assert "memory" in body["corpus_store"], \
        "an operator who reads 'memory' knows the rights they declared die with " \
        "the process, instead of finding out"
