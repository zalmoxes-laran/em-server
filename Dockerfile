# em-server — the s3Dgraphy access API over HTTP.
#
# Stateless by construction: no volume, no writable path the app depends on, no
# state in the container. Scale it by adding replicas.
#
#   docker build -t em-server .
#   docker run --rm -p 8000:8000 em-server
#
# Point it at a s3Dgraphy CHECKOUT instead of the published wheel while the
# language and the service move together:
#   docker build --build-arg S3DGRAPHY_SPEC="" -t em-server .   # then mount + pip -e
#
FROM python:3.12-slim AS base

# PYTHONDONTWRITEBYTECODE: nothing in the image should be modified at runtime.
# PYTHONUNBUFFERED: logs reach the orchestrator as they happen, not on flush.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# EXACT pin with the extras — see pyproject.toml for the three reasons. dev12 is
# the first release carrying `s3dgraphy/api.py`, and the extras are what make
# /v1/reproject and /v1/export-ttl work rather than 501.
#
# Build with --build-arg S3DGRAPHY_SPEC='s3dgraphy==1.6.0.dev12' for a slimmer,
# less capable image: it starts and serves, and /v1/health reports which ops it
# cannot do.
ARG S3DGRAPHY_SPEC="s3dgraphy[geo,rdf]==1.6.0.dev12"

WORKDIR /srv/em-server

# Dependencies first, in their own layer: application edits then rebuild in
# seconds instead of re-resolving the world.
COPY pyproject.toml README.md ./
# The explicit rdflib/pyproj lines are GONE, and that is the point of dev12: the
# previous release predated the `[geo]` extra, so `s3dgraphy[rdf,geo]` silently
# skipped pyproj (pip warns about an unknown extra, it does not fail) and the
# image answered `reproject: false`. dev12 declares both extras, so the pin alone
# is enough — verified in the container, not assumed.
# PyJWT[crypto] is here and NOT behind a build arg on purpose: an image that
# cannot verify a token is an image that would come up in the open dev mode on
# the shared infrastructure. The auth dependency is not optional (P1).
# `minio` is here and not behind a build arg for the same reason PyJWT is: an
# image that cannot reach the object store would come up serving assets from a
# container filesystem that disappears with the container. 400 KB.
RUN pip install --upgrade pip && \
    pip install "${S3DGRAPHY_SPEC}" "fastapi>=0.110" "uvicorn[standard]>=0.27" \
                "PyJWT[crypto]>=2.8" "minio>=7.2"

COPY app ./app

# Not root. The application writes nothing inside the image, so there is no
# reason to be able to.
#
# /srv/em-data is created here even though it is empty: a named volume mounted
# on a path the image does NOT have is created root-owned, and a non-root
# process then cannot write its first snapshot. Creating it with the right owner
# is what makes `volumes: [em_data:/srv/em-data]` work — in the dev stack and in
# the Ansible compose, which mounts exactly the same path.
RUN useradd --create-home --shell /usr/sbin/nologin emserver && \
    mkdir -p /srv/em-data && \
    chown -R emserver:emserver /srv/em-server /srv/em-data
USER emserver

EXPOSE 8000

# The orchestrator's own probe target — the same endpoint a human curls.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# One worker per container: replicas are the orchestrator's business, and a
# process count baked into an image is a decision taken in the wrong place.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
