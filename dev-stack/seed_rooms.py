#!/usr/bin/env python3
"""Seed the demonstration rooms — the visibility pair, and the room to work in.

    python dev-stack/seed_rooms.py
    python dev-stack/seed_rooms.py --force     # …overwriting the work room too

`mostra` is `visibility: public` and `scavo` is `restricted`. They are the same
study twice, on purpose: the ONLY difference between them is that one word in the
header, so a probe of the two answers the question "what does publishing change?"
without any other variable moving.

`basilica-demo` is a third room, and a different KIND of thing: a small
stratigraphy for two EMStudio clients to meet in (walkthrough Tappa 4). Which is
why it is **preserved and not overwritten** on a re-run — the other two are
fixtures nobody edits, this one is where somebody's editing happens, and a seed
that silently replaced a morning's work would be the worst kind of idempotent.
`--force` overwrites it, deliberately and out loud.

    curl -s -o /dev/null -w '%{http_code}\\n' http://localhost:8000/v1/rooms/mostra/iiif/img-1/manifest   # 200
    curl -s -o /dev/null -w '%{http_code}\\n' http://localhost:8000/v1/rooms/scavo/iiif/img-1/manifest    # 401

**Why this file exists.** These two rooms were written into the volume by hand
during the session that built the visibility rule. Hand-seeded state is state
that dies the first time somebody runs `down -v`, and its loss is discovered as a
smoke test failing for a reason nobody can reconstruct. A seed you can re-run is
the difference between a demo and a fixture.

It writes THROUGH the container (`docker exec`) rather than into a host path,
because the snapshots live on a named volume and the volume is the only place
they exist. Nothing here needs em-server to be answering — only the container to
be up — so it also works while the service is restarting.

The image both rooms point at is the one `smoke_iiif.py` uploads; run that first
(or after — the manifest is what needs it, not the seed).
"""

from __future__ import annotations

import json
import subprocess
import sys

#: The image the two rooms annotate — the digest `smoke_iiif.py` produces. It is
#: a CONTENT address, so it is the same on every machine that runs that smoke.
IMAGE_SHA = ("sha256:4239fc67504c2b22c584d4de71b7329e"
             "3c01b2a0b716aaa198ca6c4120b00abe")

CONTAINER = "em-dev-server"
SNAPSHOT_DIR = "/srv/em-data/snapshots"


def document(room_id: str, name: str, visibility: str) -> dict:
    """One room: an image and two annotated regions. Identical but for the word."""
    return {
        "header": {"format": "em.json", "version": "1.0",
                   "visibility": visibility},
        "graphs": {room_id: {
            "graph_id": room_id,
            "name": name,
            "nodes": [
                {"id": "img-1", "node_type": "resource", "name": "Foto di scavo",
                 "data": {"checksum": IMAGE_SHA, "media_type": "image/png",
                          "residency": "reference"}},
                {"id": "reg-a", "node_type": "annotation_region", "name": "muro",
                 "data": {"shape_kind": "rect", "rect": [0.1, 0.1, 0.3, 0.2],
                          "page": 0, "resource_id": "img-1"}},
                {"id": "reg-b", "node_type": "annotation_region", "name": "soglia",
                 "data": {"shape_kind": "rect", "rect": [0.55, 0.6, 0.25, 0.25],
                          "page": 0, "resource_id": "img-1"}},
            ],
            "edges": [
                {"id": "e-a", "source": "reg-a", "target": "img-1",
                 "edge_type": "is_on_resource"},
                {"id": "e-b", "source": "reg-b", "target": "img-1",
                 "edge_type": "is_on_resource"},
            ],
        }},
        "active_graph_id": room_id,
    }


def work_room(room_id: str, name: str) -> dict:
    """A small stratigraphy — the room two clients meet in.

    Not annotated regions this time but **units and a relation**, because what
    Tappa 4 shows is one person editing and another seeing it: that needs a
    graph somebody can rename a node in, and a matrix that redraws when they do.

    Small on purpose. Six units and five relations are enough to see an edit
    arrive; a hundred would only make the screen harder to read.

    Node and edge shapes are literal here — the same idiom as the fixtures above
    — and they are the shapes s3Dgraphy's importer builds (`node_type: "US"`,
    `edge_type: "is_before"`, measured against `StratigraphicUnit`), not a guess.
    """
    units = [
        ("US1", "US 1", "Crollo di tegole sul piano di calpestio."),
        ("US2", "US 2", "Piano in cocciopesto, integro nel settore nord."),
        ("US3", "US 3", "Muro perimetrale est, opera mista."),
        ("US4", "US 4", "Fossa di spoliazione lungo il muro est."),
        ("US5", "US 5", "Riempimento della fossa, terra e frammenti."),
        ("US6", "US 6", "Fondazione del muro perimetrale."),
    ]
    #: `is_before` = the source is EARLIER. Read them as a sequence: the collapse
    #: sits on the floor, the floor abuts the wall, the robbing cuts the wall.
    relations = [("US1", "US2"), ("US2", "US3"), ("US4", "US3"),
                 ("US5", "US4"), ("US3", "US6")]
    return {
        "header": {"format": "em.json", "version": "1.0",
                   "visibility": "restricted",
                   "description": "Dato di dimostrazione, non uno scavo."},
        "graphs": {room_id: {
            "graph_id": room_id,
            "name": name,
            "nodes": [
                {"id": unit, "node_type": "US", "name": label,
                 "description": description}
                for unit, label, description in units
            ],
            "edges": [
                {"id": f"{a}__is_before__{b}", "source": a, "target": b,
                 "edge_type": "is_before"}
                for a, b in relations
            ],
        }},
        "active_graph_id": room_id,
    }


#: `preserve` = written only when the room is not there yet. See the module
#: docstring: a fixture is meant to be rewritten, a room somebody works in is not.
ROOMS = (
    ("mostra", "Mostra (dissemination)", "public", document, False),
    ("scavo", "Scavo (in corso)", "restricted", document, False),
    ("basilica-demo", "Basilica · saggio dimostrativo", None, work_room, True),
)


def exists(room_id: str) -> bool:
    """Is this room already written? Asked of the VOLUME, which is where it is."""
    return subprocess.run(
        ["docker", "exec", CONTAINER, "test", "-f",
         f"{SNAPSHOT_DIR}/{room_id}.em.json"], capture_output=True).returncode == 0


def main() -> int:
    force = "--force" in sys.argv
    try:
        subprocess.run(["docker", "inspect", CONTAINER], check=True,
                       capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"the container {CONTAINER} is not there. Bring the stack up "
              f"first:\n  docker-compose --env-file .env.dev "
              f"-f docker-compose.dev.yml up -d")
        return 2

    for room_id, name, visibility, build, preserve in ROOMS:
        if preserve and not force and exists(room_id):
            print(f"[ kept ] {room_id} — c'era già, non l'ho toccata "
                  f"(--force per riscriverla)")
            continue
        payload = json.dumps(
            build(room_id, name, visibility) if visibility is not None
            else build(room_id, name), ensure_ascii=False)
        # `sh -c` with the JSON on stdin: no quoting of a document into an
        # argv, which is where this kind of script usually breaks.
        #
        # `-u 0` and then chown, rather than writing as the app user, because a
        # snapshot that arrived by an earlier `docker cp` is owned by the HOST
        # user and the app user cannot truncate it — measured: "Permission
        # denied" on a file that plainly exists and is plainly world-readable.
        # The chown hands it back to whoever owns the directory, so em-server
        # keeps being able to rewrite it on its own save.
        target = f"{SNAPSHOT_DIR}/{room_id}.em.json"
        result = subprocess.run(
            ["docker", "exec", "-i", "-u", "0", CONTAINER, "sh", "-c",
             f"cat > {target} && chown --reference={SNAPSHOT_DIR} {target}"],
            input=payload.encode("utf-8"), capture_output=True)
        if result.returncode != 0:
            print(f"[ FAIL ] {room_id}: {result.stderr.decode().strip()}")
            return 1
        print(f"[  ok  ] {room_id} — visibility: {visibility or 'restricted'}")

    print("\nem-server reads a room's document when the room is first opened, so "
          "a room that was already live in this process keeps what it had.\n"
          "Restart it if you have just changed a seed:\n"
          "  docker-compose --env-file .env.dev -f docker-compose.dev.yml "
          "restart em-server")
    print("\nPer Tappa 4 (EMStudio come client), da incollare in "
          "Impostazioni ▸ Live sync:")
    print(f"  URL     https://{env_domain()}:{env_https_port()}/em")
    print(f"  Stanza  basilica-demo")
    print("  Token   ./dev-stack/token.sh   (il campo lo chiede Mode ▸ Hub)")
    return 0


def env_domain() -> str:
    return _env("EM_DEV_DOMAIN", "em.localhost")


def env_https_port() -> str:
    return _env("HTTPS_PORT", "8443")


def _env(name: str, default: str) -> str:
    """Read `.env.dev` without importing anything: this script deliberately has
    no dependencies, so that it works while the stack is half-up."""
    import os
    import pathlib
    if os.environ.get(name):
        return os.environ[name]
    path = pathlib.Path(__file__).resolve().parent / ".env.dev"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return default


if __name__ == "__main__":
    raise SystemExit(main())
