"""An asset is served if the GRAPH says it may be — embargo, licence, authors.

`GET /rooms/{id}/asset/{ref}` had no gate: anybody holding a digest downloaded
the bytes. The digest is not a secret worth relying on either — it travels in
manifests and in documents — so "unguessable" was never the protection anybody
thought it was.

What these tests defend:

* an asset under a **running embargo** is refused to a viewer and to a stranger,
  and served to the people working on the study (**editor and above**);
* the verdict is computed from the DATE **at request time**, never from a stored
  flag: an embargo that expired this morning is over this morning;
* an asset the graph says nothing about is served **exactly as before** — a gate
  that changed the behaviour of every other file would have been a regression
  wearing a feature's clothes;
* the **licence travels with the bytes**, including the default, because a
  reader who has downloaded a file has been told what they may do with it;
* an embargoed image is **left out of the IIIF manifest**, which is what actually
  withholds it: the manifest is where a digest comes from.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ws as ws_module                          # noqa: E402
from app.access import Acl, InMemoryAclStore, Role       # noqa: E402
from app.main import app                                 # noqa: E402
from app.rooms import RoomRegistry                       # noqa: E402
from app.store import InMemorySnapshotStore              # noqa: E402

ANNA = "0000-0002-1825-0097"     # owner
CARLA = "0000-0003-1415-9265"    # editor
BRUNO = "0000-0001-5109-3700"    # nobody

PIXELS = b"\x89PNG\r\n\x1a\n" + b"fake but stable"
FREE = b"a file nobody said anything about"


def document(room_id: str, *, embargoed: str, plain: str, until: str = "2099-01-01",
             license_value: str | None = "CC-BY-4.0") -> dict:
    """One room: an embargoed image with a licence and an author, and a file the
    graph mentions with no rights at all."""
    nodes = [
        {"id": "img", "node_type": "resource", "name": "Prospetto nord",
         "data": {"checksum": embargoed, "media_type": "image/png"}},
        {"id": "libero", "node_type": "resource", "name": "Nota",
         "data": {"checksum": plain}},
        {"id": "emb", "node_type": "embargo", "name": until,
         "data": {"embargo_end": until, "reason": "in corso di studio"}},
        {"id": "aut", "node_type": "author", "name": "Anna",
         "data": {"orcid": ANNA}},
    ]
    edges = [
        {"id": "e1", "source": "img", "target": "emb", "edge_type": "has_embargo"},
        {"id": "e2", "source": "img", "target": "aut", "edge_type": "has_author"},
    ]
    if license_value:
        nodes.append({"id": "lic", "node_type": "license", "name": license_value,
                      "data": {"license_type": license_value}})
        edges.append({"id": "e3", "source": "img", "target": "lic",
                      "edge_type": "has_license"})
    return {"header": {"format": "em.json", "version": "1.0",
                       "visibility": "restricted", "owner": ANNA},
            "graphs": {room_id: {"graph_id": room_id, "name": room_id,
                                 "nodes": nodes, "edges": edges}},
            "active_graph_id": room_id}


@pytest.fixture()
def room(monkeypatch):
    """A room with two real assets in the store and the rights in its graph."""
    from app.assets import InMemoryAssetStore
    from app import main as main_module

    assets = InMemoryAssetStore()
    embargoed = assets.put(PIXELS, "image/png")["ref"]
    plain = assets.put(FREE, "text/plain")["ref"]
    monkeypatch.setattr(main_module, "ASSET_STORE", assets)

    store = InMemorySnapshotStore()
    store.put("scavo", document("scavo", embargoed=embargoed, plain=plain))
    acls = InMemoryAclStore()
    acls.put("scavo", Acl(owner=ANNA, members={CARLA: "editor",
                                               BRUNO: "viewer"}).as_dict())
    monkeypatch.setattr(ws_module, "SNAPSHOT_STORE", store)
    monkeypatch.setattr(ws_module, "ROOMS", RoomRegistry(store))
    monkeypatch.setattr(ws_module, "ACL_STORE", acls)
    return {"embargoed": embargoed, "plain": plain}


@pytest.fixture()
def whoever(monkeypatch):
    """Tokens are enforced, and we choose whose they are."""
    class Enforcing:
        enforcing = True

        def describe(self):
            return "keycloak"

    monkeypatch.setattr(ws_module.authenticator, "settings", Enforcing())

    def be(orcid):
        monkeypatch.setattr(ws_module.authenticator, "verify",
                            lambda token: ({"orcid": orcid} if orcid else {}))
        monkeypatch.setattr(ws_module.authenticator, "require_token",
                            lambda request: ({"orcid": orcid} if orcid else {}))
    return be


@pytest.fixture()
def client():
    return TestClient(app)


HEAD = {"Authorization": "Bearer t"}


def test_an_embargoed_asset_is_refused_to_a_viewer_and_served_to_an_editor(
        client, room, whoever):
    whoever(BRUNO)                              # viewer
    refused = client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}", headers=HEAD)
    assert refused.status_code == 403
    assert "embargo" in refused.json()["detail"]
    assert "2099-01-01" in refused.json()["detail"], \
        "the refusal names the date: 'no' with a reason is actionable, 'no' is not"

    whoever(CARLA)                              # editor
    served = client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}", headers=HEAD)
    assert served.status_code == 200 and served.content == PIXELS

    whoever(ANNA)                               # owner
    assert client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}",
                      headers=HEAD).status_code == 200


def test_an_expired_embargo_is_over_for_everybody(client, room, whoever,
                                                  monkeypatch):
    """Computed from the date at REQUEST time. A flag written when the study was
    indexed would keep the file buried the morning after it was released."""
    from app import ws as ws_mod

    store = InMemorySnapshotStore()
    store.put("scavo", document("scavo", embargoed=room["embargoed"],
                                plain=room["plain"], until="2001-01-01"))
    monkeypatch.setattr(ws_mod, "ROOMS", RoomRegistry(store))
    whoever(BRUNO)
    assert client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}",
                      headers=HEAD).status_code == 200


def test_an_asset_the_graph_says_nothing_about_is_served_as_before(client, room,
                                                                   whoever):
    whoever(BRUNO)
    answer = client.get(f"/v1/rooms/scavo/asset/{room['plain']}", headers=HEAD)
    assert answer.status_code == 200 and answer.content == FREE
    # …with the default licence stated: silence in the graph is not silence to
    # the person downloading it
    assert answer.headers["X-EM-License"] == "CC-BY-SA-4.0"
    assert answer.headers["X-EM-License-Default"] == "true"


def test_the_licence_and_the_author_travel_with_the_bytes(client, room, whoever):
    whoever(CARLA)
    answer = client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}", headers=HEAD)
    assert answer.status_code == 200
    assert answer.headers["X-EM-License"] == "CC-BY-4.0"
    assert "X-EM-License-Default" not in answer.headers
    assert answer.headers["X-EM-Embargo"] == "2099-01-01"
    assert answer.headers["X-EM-Author"] == ANNA


def test_a_stranger_without_a_token_is_refused_the_embargoed_asset(client, room,
                                                                   whoever):
    whoever(None)
    assert client.get(f"/v1/rooms/scavo/asset/{room['embargoed']}"
                      ).status_code in (401, 403)


def test_the_manifest_leaves_out_an_embargoed_image(client, room, whoever,
                                                    monkeypatch):
    """The manifest is where a digest comes from, so omitting the canvas is what
    actually withholds the picture from a viewer."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "_measure_images", lambda graph, base: {})
    whoever(ANNA)
    answer = client.get("/v1/rooms/scavo/iiif/img/manifest", headers=HEAD)
    assert answer.status_code == 200, answer.text
    manifest = answer.json()
    assert manifest["items"] == [], "the embargoed image has no canvas"
    assert manifest["em:withheld"] == [{"resource": "img", "embargo": "2099-01-01"}]
    assert any("embargo" in w for w in manifest["em:warnings"])


def test_a_manifest_carries_the_licence_of_what_it_does_show(client, room,
                                                             whoever, monkeypatch):
    from app import main as main_module
    from app import ws as ws_mod

    store = InMemorySnapshotStore()
    store.put("scavo", document("scavo", embargoed=room["embargoed"],
                                plain=room["plain"], until="2001-01-01"))
    monkeypatch.setattr(ws_mod, "ROOMS", RoomRegistry(store))
    monkeypatch.setattr(main_module, "IIIF_PUBLIC", "https://em.example.org/iiif/3")
    monkeypatch.setattr(main_module, "_measure_images", lambda graph, base: {})
    whoever(ANNA)
    manifest = client.get("/v1/rooms/scavo/iiif/img/manifest", headers=HEAD).json()
    assert len(manifest["items"]) == 1, "the embargo has expired: the image shows"
    assert manifest["requiredStatement"]["value"]["none"] == ["CC-BY-4.0"]
    assert manifest["em:authors"] == [ANNA]
