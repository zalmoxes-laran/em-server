#!/usr/bin/env python3
"""Seed the two demonstration rooms — the ones the visibility rule is shown on.

    python dev-stack/seed_rooms.py

`mostra` is `visibility: public` and `scavo` is `restricted`. They are the same
study twice, on purpose: the ONLY difference between them is that one word in the
header, so a probe of the two answers the question "what does publishing change?"
without any other variable moving.

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


ROOMS = (
    ("mostra", "Mostra (dissemination)", "public"),
    ("scavo", "Scavo (in corso)", "restricted"),
)


def main() -> int:
    try:
        subprocess.run(["docker", "inspect", CONTAINER], check=True,
                       capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"the container {CONTAINER} is not there. Bring the stack up "
              f"first:\n  docker-compose --env-file .env.dev "
              f"-f docker-compose.dev.yml up -d")
        return 2

    for room_id, name, visibility in ROOMS:
        payload = json.dumps(document(room_id, name, visibility),
                             ensure_ascii=False)
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
        print(f"[  ok  ] {room_id} — visibility: {visibility}")

    print("\nem-server reads a room's document when the room is first opened, so "
          "a room that was already live in this process keeps what it had.\n"
          "Restart it if you have just changed a seed:\n"
          "  docker-compose --env-file .env.dev -f docker-compose.dev.yml "
          "restart em-server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
