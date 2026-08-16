"""Who may enter a room, and who may write in it.

Until this, a valid token was a key to every room and to every operation in it.
These tests are the description of the door that replaced that, and each one
names a failure that would otherwise be invisible from the outside:

* a **viewer that can write** looks like a working room until somebody's edit
  arrives in a study they were only shown;
* a **refusal without a word** looks exactly like a dropped connection, so the
  room gets blamed for a rule it applied correctly;
* a **restricted study whose viewer may be anonymous** is a restricted study in
  name only;
* an **admin who may touch an owner** is an admin who may take the room.

The relay is driven through the real `TestClient`, with the authenticator forced
into enforcing mode and `verify` returning the claims we choose — everything but
the signature check is the production path, which is where the interesting
mistakes live.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import access                                   # noqa: E402
from app import ws as ws_module                          # noqa: E402
from app.access import (Acl, DirectoryAclStore, InMemoryAclStore,  # noqa: E402
                        Role, may_assign, role_of)
from app.main import app                                 # noqa: E402
from app.rooms import RoomRegistry                       # noqa: E402
from app.store import InMemorySnapshotStore              # noqa: E402
from app.wire import WIRE                                 # noqa: E402

ANNA = "0000-0002-1825-0097"     # the owner in the fixtures below
BRUNO = "0000-0001-5109-3700"    # somebody with no grant
CARLA = "0000-0003-1415-9265"    # somebody who gets one


def document(room_id: str, *, visibility: str, owner: str | None = ANNA,
             embargo: str | None = None) -> dict:
    header = {"format": "em.json", "version": "1.0", "visibility": visibility}
    if owner:
        header["owner"] = owner
    if embargo:
        header["embargo"] = embargo
    return {"header": header,
            "graphs": {room_id: {"graph_id": room_id, "name": room_id,
                                 "nodes": [], "edges": []}},
            "active_graph_id": room_id}


@pytest.fixture(autouse=True)
def relay(monkeypatch):
    """A clean relay with its own snapshot store AND its own ACL store."""
    store = InMemorySnapshotStore()
    store.put("mostra", document("mostra", visibility="public"))
    store.put("scavo", document("scavo", visibility="restricted"))
    store.put("orfana", document("orfana", visibility="restricted", owner=None))
    store.put("embargata", document("embargata", visibility="public",
                                    embargo="2099-01-01"))
    acls = InMemoryAclStore()
    monkeypatch.setattr(ws_module, "SNAPSHOT_STORE", store)
    monkeypatch.setattr(ws_module, "ROOMS", RoomRegistry(store))
    monkeypatch.setattr(ws_module, "ACL_STORE", acls)
    return acls


@pytest.fixture
def enforcing(monkeypatch):
    """Tokens are checked, and we choose whose they are."""
    class Enforcing:
        enforcing = True

        def describe(self):
            return "keycloak"

    monkeypatch.setattr(ws_module.authenticator, "settings", Enforcing())

    def be(orcid):
        monkeypatch.setattr(ws_module.authenticator, "verify",
                            lambda token: ({"orcid": orcid} if orcid else {}))
    return be


@pytest.fixture
def client():
    return TestClient(app)


def _join(socket):
    host = socket.receive_json()
    assert host["type"] == "host_info"
    assert socket.receive_json()["type"] == "snapshot"
    assert socket.receive_json()["type"] == "presence"
    return host["payload"]


OP = {"op": "add_node", "node": {"id": "US1", "node_type": "US", "name": "US 1"},
      "id": "US1", "ts": "2026-08-16T10:00:00Z"}


def _send_op(socket, op=None):
    socket.send_json({"v": WIRE, "type": "op", "source": "test",
                      "payload": op or OP})


# ── the resolution, without a socket ─────────────────────────────────────────

def test_the_roles_are_ordered_and_that_is_what_permission_asks():
    assert Role.OWNER.at_least(Role.EDITOR) and not Role.VIEWER.at_least(Role.EDITOR)
    assert Role.EDITOR.can_write and not Role.VIEWER.can_write
    assert Role.ADMIN.can_manage and not Role.EDITOR.can_manage


def test_a_public_study_gives_reading_to_anybody_including_nobody():
    acl = Acl(owner=ANNA)
    assert role_of(acl, None, "public") is Role.VIEWER
    assert role_of(acl, BRUNO, "public") is Role.VIEWER
    # …and reading is ALL it gives: publishing is not a grant to edit
    assert not role_of(acl, BRUNO, "public").can_write


def test_a_restricted_study_gives_nothing_without_a_grant():
    acl = Acl(owner=ANNA, members={CARLA: "viewer"})
    assert role_of(acl, None, "restricted") is None
    assert role_of(acl, BRUNO, "restricted") is None
    assert role_of(acl, CARLA, "restricted") is Role.VIEWER
    assert role_of(acl, ANNA, "restricted") is Role.OWNER


def test_an_embargo_makes_a_public_study_behave_as_restricted():
    acl = Acl(owner=ANNA)
    assert role_of(acl, BRUNO, "public", embargo="2099-01-01") is None
    assert role_of(acl, BRUNO, "public", embargo="2001-01-01") is Role.VIEWER
    # …and it never takes the room from the people who have a grant
    assert role_of(acl, ANNA, "public", embargo="2099-01-01") is Role.OWNER


def test_the_two_refusals_are_different_facts():
    """4401 says "log in", 4403 says "ask for access". Answering 4401 to
    somebody already signed in sends them back to a login they have done."""
    assert access.refusal_code(None) == 4401
    assert access.refusal_code(BRUNO) == 4403


def test_an_orcid_is_the_same_person_written_as_a_url():
    acl = Acl(owner=f"https://orcid.org/{ANNA}", members={f"orcid.org/{CARLA}": "editor"})
    assert role_of(acl, ANNA, "restricted") is Role.OWNER
    assert role_of(acl, f"https://orcid.org/{CARLA}", "restricted") is Role.EDITOR


def test_a_group_grant_expands_through_the_seam_and_the_strongest_wins():
    """The resolver, on its own: handed an expander it takes the STRONGEST
    grant. The registry that provides that expander is tested further down; here
    the point is that the resolution is a pure function of what it is given."""
    acl = Acl(owner=ANNA, groups={"scavo-2026": "editor", "ospiti": "viewer"})
    assert role_of(acl, BRUNO, "restricted") is None, "no expander: no grant"
    assert role_of(acl, BRUNO, "restricted",
                   groups_of=lambda who: ["ospiti", "scavo-2026"]) is Role.EDITOR


def test_the_owner_comes_from_the_study_and_bootstraps_only_once():
    doc = document("x", visibility="restricted", owner=None)
    assert access.owner_from_document(doc) is None
    assert access.claim_owner(doc, BRUNO) is True
    assert doc["header"]["owner"] == BRUNO
    assert access.claim_owner(doc, CARLA) is False, "an owner is claimed ONCE"
    assert access.owner_from_document(doc) == BRUNO


def test_an_admin_may_not_touch_an_owner_or_another_admin():
    assert may_assign(Role.OWNER, Role.ADMIN, Role.OWNER) is None
    assert may_assign(Role.ADMIN, None, Role.EDITOR) is None
    assert "only the owner" in (may_assign(Role.ADMIN, None, Role.ADMIN) or "")
    assert "another admin" in (may_assign(Role.ADMIN, Role.ADMIN, Role.VIEWER) or "")
    assert "admin or owner" in (may_assign(Role.EDITOR, None, Role.VIEWER) or "")


def test_the_acl_survives_the_process(tmp_path):
    """The grants are not in the relay's memory: a restart must not re-open a
    room. Measured on the bytes, not on a mock."""
    store = DirectoryAclStore(tmp_path)
    store.put("scavo", Acl(owner=ANNA, members={CARLA: "editor"}).as_dict())
    written = json.loads((tmp_path / "scavo.acl.json").read_text())
    assert written == {"owner": ANNA, "members": {CARLA: "editor"}, "groups": {}}
    again = Acl.from_dict(DirectoryAclStore(tmp_path).get("scavo"))
    assert again.role_for(CARLA) is Role.EDITOR


# ── the door ─────────────────────────────────────────────────────────────────

def test_the_owner_joins_as_owner_and_writes(client, enforcing):
    enforcing(ANNA)
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as a:
        host = _join(a)
        assert (host["role"], host["can_write"]) == ("owner", True)
        _send_op(a)
        answer = a.receive_json()
        assert answer["type"] == "op_result" and answer["payload"]["applied"] is True


def test_on_a_public_study_a_stranger_reads_and_is_refused_the_write(client,
                                                                     enforcing):
    """The case the whole gate exists for: joining is not permission to edit."""
    enforcing(BRUNO)
    with client.websocket_connect("/v1/rooms/mostra/ws?token=t") as b:
        host = _join(b)
        assert (host["role"], host["can_write"]) == ("viewer", False)
        _send_op(b)
        answer = b.receive_json()
        assert answer["type"] == "denied", "a refusal is SAID, never a silence"
        assert answer["payload"]["verb"] == "op"
        assert answer["payload"]["role"] == "viewer"
        assert "read-only" in answer["payload"]["reason"]
        # …and nothing was written
        assert not ws_module.ROOMS.peek("mostra").document["graphs"]["mostra"]["nodes"]


def test_a_viewer_may_still_be_present_and_select(client, enforcing):
    """Awareness is not writing. A viewer nobody could see would be a ghost."""
    enforcing(BRUNO)
    with client.websocket_connect("/v1/rooms/mostra/ws?token=t") as b:
        _join(b)
        b.send_json({"v": WIRE, "type": "select", "source": "test",
                     "payload": {"node_id": "US1"}})
        b.send_json({"v": WIRE, "type": "request_snapshot", "source": "test",
                     "payload": {}})
        assert b.receive_json()["type"] == "snapshot", \
            "reading the document again is what a viewer does"


def test_a_restricted_room_refuses_the_signed_in_stranger_and_the_anonymous(
        client, enforcing):
    enforcing(BRUNO)                       # authenticated, no grant → 4403
    with pytest.raises(Exception) as refused:
        with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as b:
            b.receive_json()
    # the CODE, not the text: the two refusals are two different instructions
    # to the client, and only the number carries that
    assert refused.value.code == 4403
    assert "not a member" in (refused.value.reason or "")

    # …and with NO token the refusal comes one step earlier, from
    # authentication, with the code that means "sign in" rather than "ask for
    # access". That difference is the whole of "a restricted viewer is login +
    # grant, never anonymous".
    with pytest.raises(Exception) as anonymous:
        with client.websocket_connect("/v1/rooms/scavo/ws") as b:
            b.receive_json()
    assert anonymous.value.code == 4401

    # A token that names nobody is the same fact as no token: `_identity`
    # answers None, and an identity nobody can check is not one to grant to.
    enforcing(None)
    with pytest.raises(Exception) as nameless:
        with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as b:
            b.receive_json()
    assert nameless.value.code == 4401


def test_a_grant_lets_somebody_in_and_a_revocation_takes_it_back(client,
                                                                 enforcing,
                                                                 relay):
    """The full round trip, because the interesting bug is in the second half:
    a system that grants and cannot revoke is not access control."""
    relay.put("scavo", Acl(owner=ANNA, members={CARLA: "editor"}).as_dict())
    enforcing(CARLA)
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
        assert _join(c)["can_write"] is True
        _send_op(c)
        assert c.receive_json()["payload"]["applied"] is True

    relay.put("scavo", Acl(owner=ANNA).as_dict())        # revoked
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
            c.receive_json()


def test_an_ownerless_room_is_claimed_by_the_first_person_through_the_door(
        client, enforcing, relay):
    enforcing(BRUNO)
    with client.websocket_connect("/v1/rooms/orfana/ws?token=t") as b:
        assert _join(b)["role"] == "owner"
    assert Acl.from_dict(relay.get("orfana")).owner == BRUNO
    # …and the STUDY carries it too, so it survives being moved elsewhere
    assert ws_module.ROOMS.peek("orfana").document["header"]["owner"] == BRUNO
    # the second arrival does not become a second owner
    enforcing(CARLA)
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/rooms/orfana/ws?token=t") as c:
            c.receive_json()


def test_an_embargoed_public_study_is_shut_until_the_date(client, enforcing):
    enforcing(BRUNO)
    with pytest.raises(Exception):
        with client.websocket_connect("/v1/rooms/embargata/ws?token=t") as b:
            b.receive_json()
    enforcing(ANNA)              # the owner keeps their room throughout
    with client.websocket_connect("/v1/rooms/embargata/ws?token=t") as a:
        assert _join(a)["role"] == "owner"


# ── the REST that hands the roles out ────────────────────────────────────────

def test_the_owner_grants_and_the_relay_obeys_the_grant(client, enforcing, relay):
    enforcing(ANNA)
    answer = client.put(f"/v1/rooms/scavo/members/{CARLA}",
                        json={"role": "editor"}, headers={"Authorization": "Bearer t"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["members"] == [{"orcid": CARLA, "role": "editor"}]
    assert Acl.from_dict(relay.get("scavo")).members[CARLA] is Role.EDITOR

    enforcing(CARLA)             # the grant is real at the door, not just in the file
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
        assert _join(c)["can_write"] is True

    enforcing(ANNA)
    gone = client.delete(f"/v1/rooms/scavo/members/{CARLA}",
                         headers={"Authorization": "Bearer t"})
    assert gone.status_code == 200 and gone.json()["members"] == []


def test_a_grant_is_idempotent(client, enforcing, relay):
    enforcing(ANNA)
    head = {"Authorization": "Bearer t"}
    for _ in range(3):
        answer = client.put(f"/v1/rooms/scavo/members/{CARLA}",
                            json={"role": "viewer"}, headers=head)
        assert answer.status_code == 200
    assert answer.json()["members"] == [{"orcid": CARLA, "role": "viewer"}]
    # …and so is a revocation of somebody who has nothing
    assert client.delete(f"/v1/rooms/scavo/members/{BRUNO}",
                         headers=head).status_code == 200


def test_the_boundaries_are_403_with_a_reason(client, enforcing, relay):
    relay.put("scavo", Acl(owner=ANNA, members={BRUNO: "admin",
                                                CARLA: "editor"}).as_dict())
    head = {"Authorization": "Bearer t"}

    enforcing(BRUNO)             # an admin
    refused = client.put(f"/v1/rooms/scavo/members/{CARLA}",
                         json={"role": "admin"}, headers=head)
    assert refused.status_code == 403 and "only the owner" in refused.json()["detail"]
    assert client.put(f"/v1/rooms/scavo/members/{ANNA}", json={"role": "viewer"},
                      headers=head).status_code == 403, "nor demote the owner"
    # …but an admin does manage editors and viewers
    assert client.put(f"/v1/rooms/scavo/members/{CARLA}", json={"role": "viewer"},
                      headers=head).status_code == 200

    enforcing(CARLA)             # now a viewer: manages nobody, sees nothing
    assert client.get("/v1/rooms/scavo/members", headers=head).status_code == 403
    assert client.put(f"/v1/rooms/scavo/members/{BRUNO}", json={"role": "viewer"},
                      headers=head).status_code == 403


def test_ownership_is_transferred_and_never_duplicated(client, enforcing, relay):
    enforcing(ANNA)
    head = {"Authorization": "Bearer t"}
    answer = client.put(f"/v1/rooms/scavo/members/{CARLA}", json={"role": "owner"},
                        headers=head)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["owner"] == CARLA
    # the previous owner is left an admin: losing the room should not lock the
    # person who built it out of it
    assert {"orcid": ANNA, "role": "admin"} in body["members"]
    enforcing(ANNA)
    assert client.put(f"/v1/rooms/scavo/members/{BRUNO}", json={"role": "owner"},
                      headers=head).status_code == 403


def test_an_unknown_role_is_a_400_not_a_silent_viewer(client, enforcing):
    enforcing(ANNA)
    answer = client.put(f"/v1/rooms/scavo/members/{CARLA}", json={"role": "boss"},
                        headers={"Authorization": "Bearer t"})
    assert answer.status_code == 400 and "expected viewer" in answer.json()["detail"]


# ── groups: a name for a set of people, above the per-ORCID ACL ──────────────

def test_a_group_grant_and_an_individual_one_resolve_to_the_MAXIMUM(client,
                                                                    enforcing,
                                                                    relay,
                                                                    monkeypatch):
    """The rule that makes groups safe to add: the strongest grant wins.

    If the individual grant always won, adding somebody to the excavation team
    would silently DEMOTE them — a viewer grant from last month would keep
    beating the editor role the team has now."""
    from app import ws as ws_mod
    from app.access import Group, Groups, InMemoryGroupStore

    store = InMemoryGroupStore()
    monkeypatch.setattr(ws_mod, "GROUP_STORE", store)
    Groups(store).put(Group("scavo-2026", "Scavo 2026", [BRUNO, CARLA]))

    # the room grants EDITOR to the group, and viewer to Bruno personally
    relay.put("scavo", Acl(owner=ANNA, members={BRUNO: "viewer"},
                           groups={"scavo-2026": "editor"}).as_dict())

    enforcing(BRUNO)
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as b:
        host = _join(b)
        assert host["role"] == "editor", "the group's grant beats the weaker one"
        assert host["can_write"] is True

    enforcing(CARLA)                    # only in the group
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
        assert _join(c)["role"] == "editor"


def test_leaving_the_group_takes_the_role_with_it(client, enforcing, relay,
                                                  monkeypatch):
    from app import ws as ws_mod
    from app.access import Group, Groups, InMemoryGroupStore

    store = InMemoryGroupStore()
    monkeypatch.setattr(ws_mod, "GROUP_STORE", store)
    registry = Groups(store)
    registry.put(Group("scavo-2026", "Scavo 2026", [BRUNO, CARLA]))
    relay.put("scavo", Acl(owner=ANNA, groups={"scavo-2026": "editor"}).as_dict())

    enforcing(CARLA)
    with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
        assert _join(c)["can_write"] is True

    group = registry.get("scavo-2026")
    group.remove(CARLA)
    registry.put(group)
    # …and the room is shut at the next door: a restricted study with no other
    # grant leaves nothing behind
    with pytest.raises(Exception) as refused:
        with client.websocket_connect("/v1/rooms/scavo/ws?token=t") as c:
            c.receive_json()
    assert refused.value.code == 4403


def test_the_group_registry_survives_the_process(tmp_path):
    from app.access import DirectoryGroupStore, Group, Groups

    Groups(DirectoryGroupStore(tmp_path)).put(
        Group("scavo-2026", "Scavo 2026", [BRUNO], owner=ANNA))
    again = Groups(DirectoryGroupStore(tmp_path)).get("scavo-2026")
    assert again.members == [BRUNO] and again.owner == ANNA
    assert (tmp_path / "groups.json").is_file(), "on disk, not in a memory nobody kept"


def test_the_rest_manages_groups_and_only_the_owner_may(client, enforcing,
                                                        monkeypatch):
    from app import ws as ws_mod
    from app.access import InMemoryGroupStore

    monkeypatch.setattr(ws_mod, "GROUP_STORE", InMemoryGroupStore())
    head = {"Authorization": "Bearer t"}

    enforcing(ANNA)
    made = client.put("/v1/groups/scavo-2026",
                      json={"id": "scavo-2026", "name": "Scavo 2026",
                            "members": [BRUNO]}, headers=head)
    assert made.status_code == 200, made.text
    assert made.json()["owner"] == ANNA and made.json()["members"] == [BRUNO]

    added = client.put(f"/v1/groups/scavo-2026/members/{CARLA}", headers=head)
    assert added.json()["members"] == [BRUNO, CARLA]
    # idempotent: adding twice is one membership
    assert client.put(f"/v1/groups/scavo-2026/members/{CARLA}",
                      headers=head).json()["members"] == [BRUNO, CARLA]

    enforcing(BRUNO)                     # a member, not the owner
    refused = client.put(f"/v1/groups/scavo-2026/members/{ANNA}", headers=head)
    assert refused.status_code == 403
    assert "managed by whoever created it" in refused.json()["detail"]
    # …but reading the list is not a secret
    assert client.get("/v1/groups", headers=head).status_code == 200

    enforcing(ANNA)
    assert client.delete(f"/v1/groups/scavo-2026/members/{CARLA}",
                         headers=head).json()["members"] == [BRUNO]
    gone = client.delete("/v1/groups/scavo-2026", headers=head)
    assert gone.status_code == 200 and gone.json()["ok"] is True
    assert client.get("/v1/groups", headers=head).json() == []
