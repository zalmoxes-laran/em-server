"""Who may do what in a room — roles, the per-room ACL, and how a role is resolved.

Until now the relay **authenticated** and stopped there: any valid token entered
any room and wrote to it. Authentication answers *who are you*; this module
answers *what may you do here*, and the two are not the same question. Keycloak
does the first. **em-server does the second** — deliberately, because the
authorisation is about a study this server holds, not about an identity provider.

## The four roles

* **owner** — everything, including the other admins, the transfer of ownership
  and the deletion of the room;
* **admin** — manages editors and viewers and the room's resources, edits;
  never touches the owner or another admin;
* **editor** — reads and writes the graph, adds resources, bakes; manages nobody;
* **viewer** — reads. Snapshot and presence yes, operations **refused with a
  reason** — a silent drop looks exactly like a network fault.

They are ordered, so a permission question is a comparison
(``role >= Role.EDITOR``) rather than a list of names to keep in sync.

## Where each half lives, and why it is a split

The **owner is intrinsic to the study**: it comes from the container's header,
travels with the file, and survives being restored on another server. The
**collaboration roles are operational**: who may edit *this room on this server*
is not a scientific fact about the excavation, and writing it into the em.json
would put an access-control list inside the record of what was found. So:

* owner → `header.owner` (an ORCID), with the first authoring ORCID as the
  fallback the study already carries;
* everybody else → an ACL **beside** the snapshot, keyed by ORCID, with the same
  discipline `store.py` uses: atomic write, the file is the truth, the process
  holds a cache.

## Visibility, embargo, and the two kinds of viewer

A **public** study is readable by anyone, including somebody with no token at
all: that is what publishing means. A **restricted** study has viewers too, but
a viewer there is *login + explicit grant* — authenticated without a grant is a
refusal (4403), no token at all is a refusal (4401). The distinction is the
whole point: "viewer" is not one thing.

An **embargo** is a temporal gate over exactly that: while the date is in the
future the study behaves as restricted whatever its declared visibility, and
when it passes the study goes back to what it says it is. One rule, so the
Catalog's listing and the room's door cannot drift apart.

## Groups: built, ON TOP of the per-ORCID ACL

A named **group** of ORCIDs can hold a role in a room's ACL, and the registry of
who is in which group lives here too (`Groups`). It takes nothing away from the
foundation: a grant to a person is still a grant to a person, and the two are
read together. The resolution is one word — **the maximum** — which is a
comparison rather than a table of cases because the roles are ordered, and which
is the only safe direction: if the individual grant always won, adding somebody
to the excavation team would silently demote them.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import threading
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Optional, Protocol


class Role(str, Enum):
    """The four roles, in order of privilege.

    A `str` enum so the value can travel in JSON and on the wire without a
    conversion at every boundary — the role a client is told in `host_info` is
    the same word the ACL file holds.
    """

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_least(self, other: "Role") -> bool:
        return self.rank >= other.rank

    @property
    def can_write(self) -> bool:
        """May this role change the graph? The one question the client asks."""
        return self.at_least(Role.EDITOR)

    @property
    def can_manage(self) -> bool:
        """May this role hand out roles at all? (Which ones is a further rule —
        see :func:`may_assign`, because admin and owner do not manage the same
        set and collapsing them is how an admin ends up demoting an owner.)"""
        return self.at_least(Role.ADMIN)


_RANK = {Role.VIEWER: 0, Role.EDITOR: 1, Role.ADMIN: 2, Role.OWNER: 3}


def parse_role(raw: Any) -> Optional[Role]:
    """A role from whatever a caller sent, or None. Never a guess."""
    if isinstance(raw, Role):
        return raw
    try:
        return Role(str(raw).strip().lower())
    except ValueError:
        return None


# ── the ACL document ─────────────────────────────────────────────────────────

class Acl:
    """One room's access list: an owner, members by ORCID, and (later) groups.

    Plain data with two accessors, because it is a document that gets written to
    a store and read back — anything cleverer would be logic that belongs in the
    resolver below.
    """

    def __init__(self, owner: Optional[str] = None,
                 members: Optional[Dict[str, Any]] = None,
                 groups: Optional[Dict[str, Any]] = None) -> None:
        self.owner = _norm(owner)
        self.members: Dict[str, Role] = {}
        for orcid, role in (members or {}).items():
            parsed = parse_role(role)
            key = _norm(orcid)
            if parsed and key:
                self.members[key] = parsed
        #: name → role. The MEMBERSHIP lives in the registry (`Groups`), and
        #: the resolver reaches it through the `groups_of` seam — so a room's
        #: ACL says "the excavation team may edit" without listing six people
        #: who would then have to be edited in every room they work in.
        self.groups: Dict[str, Role] = {}
        for name, role in (groups or {}).items():
            parsed = parse_role(role)
            if parsed and name:
                self.groups[str(name)] = parsed

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Acl":
        raw = raw or {}
        return cls(owner=raw.get("owner"), members=raw.get("members"),
                   groups=raw.get("groups"))

    def as_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner,
                "members": {k: v.value for k, v in sorted(self.members.items())},
                "groups": {k: v.value for k, v in sorted(self.groups.items())}}

    def role_for(self, orcid: Optional[str],
                 groups_of: Optional[Callable[[str], Iterable[str]]] = None
                 ) -> Optional[Role]:
        """The role this ACL grants, ignoring visibility. None = no grant."""
        who = _norm(orcid)
        if not who:
            return None
        if self.owner and who == self.owner:
            return Role.OWNER
        best = self.members.get(who)
        if self.groups and groups_of is not None:
            for name in groups_of(who) or []:
                granted = self.groups.get(str(name))
                # the STRONGEST grant wins: a person in two groups is not
                # penalised for the weaker one
                if granted and (best is None or granted.rank > best.rank):
                    best = granted
        return best


def _norm(orcid: Any) -> Optional[str]:
    """One spelling for an identity. ORCIDs travel as bare iDs and as URLs, and
    an ACL that held both would grant to one and refuse the other."""
    if orcid in (None, ""):
        return None
    text = str(orcid).strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip("/") or None


# ── where the ACL is kept ────────────────────────────────────────────────────

class AclStore(Protocol):
    """Get and put one room's ACL. The same two methods as the snapshot store,
    for the same reason: anything richer would be policy in the wrong place."""

    def get(self, room_id: str) -> Optional[Dict[str, Any]]: ...

    def put(self, room_id: str, acl: Dict[str, Any]) -> None: ...


class InMemoryAclStore:
    """Tests and a single-process laptop. Dies with the process, and says so."""

    def __init__(self) -> None:
        self._data: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, room_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            raw = self._data.get(room_id)
        return json.loads(raw) if raw is not None else None

    def put(self, room_id: str, acl: Dict[str, Any]) -> None:
        blob = json.dumps(acl, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self._data[room_id] = blob


class DirectoryAclStore:
    """A directory of `<room>.acl.json` files, written atomically.

    Beside the snapshots on purpose: the two are the same room's state and an
    operator who can back one up should not discover the other lives elsewhere.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, room_id: str) -> pathlib.Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in room_id)
        return self.root / f"{safe}.acl.json"

    def get(self, room_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(room_id)
        if not path.is_file():
            return None
        # A read error is allowed to RAISE. Swallowing it and answering None
        # would read as "this room has no access list" — which is the one
        # sentence a corrupted ACL must never be allowed to say.
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, room_id: str, acl: Dict[str, Any]) -> None:
        path = self._path(room_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(acl, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)      # atomic: a reader never sees half an ACL


def acl_store_from_env(environ: Optional[Dict[str, str]] = None) -> AclStore:
    """The ACL store this process should use.

    `EM_ACL_DIR` names it outright; failing that it goes beside the snapshots
    (`EM_SNAPSHOT_DIR`), which is where an operator would look for it. Nothing
    configured means in-memory — correct for a laptop and loudly wrong for a
    deployment, which is why `/v1/health` reports which one is answering.
    """
    env = environ if environ is not None else os.environ
    directory = env.get("EM_ACL_DIR") or env.get("EM_SNAPSHOT_DIR")
    if directory:
        return DirectoryAclStore(directory)
    return InMemoryAclStore()


def describe(store: AclStore) -> str:
    return {
        "InMemoryAclStore": "memory (not durable — dies with the process)",
        "DirectoryAclStore": "directory (beside the snapshots)",
    }.get(type(store).__name__, type(store).__name__)


# ── the study's own half: the owner, the visibility, the embargo ─────────────

def owner_from_document(document: Optional[Dict[str, Any]]) -> Optional[str]:
    """The owner the STUDY declares, or None.

    `header.owner` first, because it is the field that exists to say this. Then
    the first authoring ORCID the container carries — a study whose author is
    known already names a person, and asking them to declare themselves twice is
    ceremony. A name without an ORCID is not enough: the ACL is keyed by ORCID,
    and "Anna Rossi" is not an identity a server can check a token against.
    """
    if not isinstance(document, dict):
        return None
    header = document.get("header") or {}
    declared = _norm(header.get("owner"))
    if declared:
        return declared
    for key in ("owner_orcid", "author_orcid", "orcid"):
        found = _norm(header.get(key))
        if found:
            return found
    try:
        from s3dgraphy.study import study_metadata
        card = study_metadata(document)
    except Exception:      # a document the library cannot read declares nothing
        return None
    for author in card.get("authors") or []:
        found = _norm(author.get("orcid"))
        if found:
            return found
    return None


def claim_owner(document: Dict[str, Any], orcid: str) -> bool:
    """Write the bootstrap owner into the header. True when it actually wrote.

    A room whose study names nobody is ownerless, and an ownerless room is one
    nobody can grant access to — so the first authenticated person through the
    door becomes the owner, **once**, and it is recorded in the study rather
    than in a side table, because that is where an owner belongs.
    """
    who = _norm(orcid)
    if not who or not isinstance(document, dict):
        return False
    if owner_from_document(document):
        return False
    header = document.setdefault("header", {})
    if not isinstance(header, dict):
        return False
    header["owner"] = who
    return True


def embargo_active(value: Any, today: Optional[datetime.date] = None) -> bool:
    """Is this embargo still running? One reading, shared with the Catalog.

    A date in the future means yes. Anything unparseable means **no**: an
    embargo nobody can read is not a gate anybody could lift, and treating a
    typo as a permanent lock would hide a study forever with no way to notice.
    The library's `s3dgraphy.study.embargo_active` is the definition; this
    delegates so the room's door and the catalogue's listing cannot drift.
    """
    try:
        from s3dgraphy.study import embargo_active as _library
    except ImportError:      # pragma: no cover — the library is a hard dep
        return False
    return _library(value, today=today)


def effective_visibility(visibility: str, embargo: Any = None,
                         today: Optional[datetime.date] = None) -> str:
    """`public` only when the study says so AND no embargo is running."""
    if embargo_active(embargo, today=today):
        return "restricted"
    return "public" if str(visibility).strip().lower() == "public" else "restricted"


# ── the resolution ───────────────────────────────────────────────────────────

def role_of(acl: Acl, orcid: Optional[str], visibility: str, *,
            embargo: Any = None, today: Optional[datetime.date] = None,
            groups_of: Optional[Callable[[str], Iterable[str]]] = None
            ) -> Optional[Role]:
    """What this identity may do in this room. `None` means: not a member.

    The order is the policy, and it is short on purpose:

    1. an explicit grant wins — owner, member, or (later) a group;
    2. a **public** study with no grant is `viewer`, *even anonymously*;
    3. anything else is None, and the caller refuses.

    Note what (2) does not do: it never *upgrades*. A public study grants
    reading, and writing to it still takes a role somebody gave you.
    """
    granted = acl.role_for(orcid, groups_of=groups_of)
    if granted is not None:
        return granted
    if effective_visibility(visibility, embargo, today) == "public":
        return Role.VIEWER
    return None


def refusal_code(orcid: Optional[str]) -> int:
    """Which refusal a non-member gets: 4401 without an identity, 4403 with one.

    They are different facts and the client acts differently on each: "log in"
    versus "ask for access". Answering 4401 to somebody already logged in sends
    them back to a login they have already done.
    """
    return 4403 if _norm(orcid) else 4401


# ── who may hand out which role ──────────────────────────────────────────────

def may_assign(actor: Optional[Role], target_current: Optional[Role],
               target_new: Optional[Role]) -> Optional[str]:
    """None when the assignment is allowed, otherwise the reason it is not.

    A sentence rather than a boolean because every refusal here is a 403 that
    somebody has to understand — "forbidden" alone is what makes people file
    bugs against a working access-control system.

    The rules, in the words of the decision: an **owner** may do anything,
    including handing the room to somebody else; an **admin** manages editors
    and viewers only, and cannot touch an owner or another admin (which is what
    stops an admin from quietly promoting themselves); anybody else manages
    nobody.
    """
    if actor is None or not actor.can_manage:
        return "managing members needs admin or owner"
    if actor is Role.OWNER:
        return None
    # admin from here on
    if target_new in (Role.OWNER, Role.ADMIN):
        return "only the owner assigns owner or admin"
    if target_current in (Role.OWNER, Role.ADMIN):
        return "an admin cannot change an owner or another admin"
    return None


# ── groups: a named set of ORCIDs, above the per-ORCID ACL ───────────────────
#
# The foundation stays what it was: an ACL keyed by ORCID. Groups sit ON TOP of
# it and take nothing away — a grant to a person is still a grant to a person,
# and the two kinds are read together rather than one replacing the other.
#
# Why a registry at all: "the excavation team" is a sentence people say, and
# writing it out as six ORCIDs in every room's ACL means six edits every time
# somebody joins or leaves, in every room, forever. The group is the name; the
# rooms grant to the name.
#
# The resolution rule is one word: **the maximum**. Somebody with an individual
# `viewer` grant who is also in a group granted `editor` is an editor — because
# the roles are ordered, "maximum" is a comparison rather than a table of
# special cases, and because the alternative (individual always wins) would make
# a group grant a trap: adding somebody to the team would silently demote them.

class Group:
    """`{id, name, members: [orcid]}` — data, with the ORCIDs normalised.

    The `owner` is whoever created it: groups are managed by the person who made
    them, the same arrangement rooms use, and for the same reason — somebody has
    to be able to fix it, and "everybody" is not an answer.
    """

    def __init__(self, group_id: str, name: str = "",
                 members: Optional[Iterable[str]] = None,
                 owner: Optional[str] = None) -> None:
        self.id = str(group_id).strip()
        self.name = str(name or group_id).strip()
        self.owner = _norm(owner)
        self.members: list = []
        for orcid in members or []:
            key = _norm(orcid)
            if key and key not in self.members:
                self.members.append(key)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Group":
        return cls(raw.get("id") or "", raw.get("name") or "",
                   raw.get("members") or [], raw.get("owner"))

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "owner": self.owner,
                "members": list(self.members)}

    def add(self, orcid: str) -> bool:
        key = _norm(orcid)
        if not key or key in self.members:
            return False
        self.members.append(key)
        return True

    def remove(self, orcid: str) -> bool:
        key = _norm(orcid)
        if not key or key not in self.members:
            return False
        self.members.remove(key)
        return True

    def has(self, orcid: Optional[str]) -> bool:
        key = _norm(orcid)
        return bool(key) and key in self.members


class GroupStore(Protocol):
    """Read and write the whole registry. One document, because a group list is
    small and read on every door: a file per group would be N reads per join."""

    def load(self) -> Dict[str, Any]: ...

    def save(self, data: Dict[str, Any]) -> None: ...


class InMemoryGroupStore:
    def __init__(self) -> None:
        self._raw = "{}"
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(self._raw)

    def save(self, data: Dict[str, Any]) -> None:
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self._raw = blob


class DirectoryGroupStore:
    """`groups.json` beside the snapshots, written atomically."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "groups.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        # An unreadable registry RAISES rather than reading as "no groups":
        # answering "no groups" would silently drop every grant made to one.
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)


def group_store_from_env(environ: Optional[Dict[str, str]] = None) -> GroupStore:
    env = environ if environ is not None else os.environ
    directory = env.get("EM_ACL_DIR") or env.get("EM_SNAPSHOT_DIR")
    if directory:
        return DirectoryGroupStore(directory)
    return InMemoryGroupStore()


class Groups:
    """The registry, as the rest of the code wants to ask it.

    Reads through to the store every time. Same reasoning as the ACL: a
    membership change must take effect at the next door, and a cache would need
    an invalidation channel for a document read once per join.
    """

    def __init__(self, store: GroupStore) -> None:
        self.store = store

    def all(self) -> Dict[str, Group]:
        raw = self.store.load() or {}
        out: Dict[str, Group] = {}
        for gid, item in (raw.get("groups") or {}).items():
            group = Group.from_dict({**item, "id": item.get("id") or gid})
            if group.id:
                out[group.id] = group
        return out

    def get(self, group_id: str) -> Optional[Group]:
        return self.all().get(str(group_id).strip())

    def put(self, group: Group) -> None:
        raw = self.store.load() or {}
        groups = dict(raw.get("groups") or {})
        groups[group.id] = group.as_dict()
        raw["groups"] = groups
        self.store.save(raw)

    def drop(self, group_id: str) -> bool:
        raw = self.store.load() or {}
        groups = dict(raw.get("groups") or {})
        if str(group_id) not in groups:
            return False
        del groups[str(group_id)]
        raw["groups"] = groups
        self.store.save(raw)
        return True

    def of(self, orcid: Optional[str]) -> list:
        """Which groups this ORCID belongs to — the expander `role_of` takes."""
        return [gid for gid, group in self.all().items() if group.has(orcid)]

    def expander(self):
        """A `groups_of` callable for :func:`role_of`. Bound to this registry so
        the resolver stays a pure function of what it is handed."""
        return lambda orcid: self.of(orcid)


def describe_groups(store: GroupStore) -> str:
    return {
        "InMemoryGroupStore": "memory (not durable — dies with the process)",
        "DirectoryGroupStore": "directory (beside the snapshots)",
    }.get(type(store).__name__, type(store).__name__)
