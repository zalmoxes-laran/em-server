#!/usr/bin/env bash
# A dev access token, on stdout and nothing else.
#
# EMStudio asks for a token when you join a room (Mode ▸ Hub), and the answer is
# a 700-character JWT nobody is going to type. So: print it, and let the shell
# put it on the clipboard.
#
#   ./dev-stack/token.sh                # the token, one line
#   ./dev-stack/token.sh | pbcopy       # …straight to the clipboard (macOS)
#   ./dev-stack/token.sh --claims       # what is inside it, for when it is refused
#
# Nothing but the token goes to stdout — every message here is on stderr — so it
# pipes into anything without a `grep` to clean up after it. The values come from
# `.env.dev`, and they are the SAME grant `smoke.py` uses: if the smokes can get
# a token and this cannot, the difference is the realm, not the script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat >&2 <<'TXT'
token.sh — a dev access token from the local Keycloak realm.

    ./dev-stack/token.sh              print the access token (user: dev)
    ./dev-stack/token.sh | pbcopy     …and put it on the clipboard
    ./dev-stack/token.sh --user viewer
                                      a token for another seeded user. `viewer`
                                      is an ordinary authenticated identity with
                                      no room membership — which is what an
                                      embargo gate refuses (403). The password
                                      is the username unless DEV_PASSWORD says
                                      otherwise
    ./dev-stack/token.sh --claims     print the token's claims instead (aud, iss,
                                      preferred_username, expiry) — the three
                                      things worth looking at when a room says 4401

Reads KEYCLOAK_PORT / DEV_REALM / DEV_CLIENT_ID / DEV_CLIENT_SECRET / DEV_USER /
DEV_PASSWORD from dev-stack/.env.dev; the environment wins over the file.

The token is short-lived by design (the dev realm issues minutes, not days). When
a room stops accepting it, run this again — that is the expiry, not a fault.
TXT
}

WANTED_USER=""
CLAIMS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --claims)  CLAIMS=1; shift ;;
        # A SECOND seeded user, by name. Added for the embargo end-to-end: the
        # gate refuses anybody below editor, and with one user in the realm
        # there was nobody to be refused — the 403 could only be measured
        # against a stand-in. The password defaults to the username, which is
        # what the realm seeds.
        --user)    WANTED_USER="${2:-}"; shift 2 ;;
        --user=*)  WANTED_USER="${1#*=}"; shift ;;
        *)         echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

# `.env.dev` first, environment on top: the same order every other script here
# uses, so one exported variable overrides one line of the file and nothing else.
env_of() {
    local name="$1" fallback="$2" value
    value="${!name:-}"
    if [[ -z "$value" && -f "$HERE/.env.dev" ]]; then
        value="$(grep -E "^${name}=" "$HERE/.env.dev" | tail -1 | cut -d= -f2-)"
    fi
    printf '%s' "${value:-$fallback}"
}

KEYCLOAK_PORT="$(env_of KEYCLOAK_PORT 8085)"
REALM="$(env_of DEV_REALM em-dev)"
CLIENT_ID="$(env_of DEV_CLIENT_ID em-server)"
CLIENT_SECRET="$(env_of DEV_CLIENT_SECRET em-dev-secret)"
USERNAME="$(env_of DEV_USER dev)"
PASSWORD="$(env_of DEV_PASSWORD dev)"
if [[ -n "$WANTED_USER" ]]; then
    USERNAME="$WANTED_USER"
    # the seeded users have their username as password; an exported DEV_PASSWORD
    # still wins, so a realm with real passwords keeps working
    PASSWORD="${DEV_PASSWORD:-$WANTED_USER}"
fi

ENDPOINT="http://localhost:${KEYCLOAK_PORT}/realms/${REALM}/protocol/openid-connect/token"

# `--data-urlencode`, not a hand-built body: a password with an `&` in it would
# otherwise become two form fields and the failure would look like a wrong realm.
answer="$(curl -sS -X POST "$ENDPOINT" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "client_id=${CLIENT_ID}" \
    --data-urlencode "client_secret=${CLIENT_SECRET}" \
    --data-urlencode "username=${USERNAME}" \
    --data-urlencode "password=${PASSWORD}" || true)"

token="$(printf '%s' "$answer" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("access_token",""))' \
    2>/dev/null || true)"

if [[ -z "$token" ]]; then
    echo "no token from ${ENDPOINT}" >&2
    # Keycloak says WHY in the body (`invalid_client`, `invalid_grant`, …), and
    # that word is the whole diagnosis. Passing it on beats "it failed".
    printf '%s\n' "${answer:-<no answer: is the stack up? ./fcn-up.sh>}" >&2
    exit 1
fi

if [[ -n "$CLAIMS" ]]; then
    # Decoded, NOT verified — this prints what the token says about itself, which
    # is what you want when a room refuses it. em-server is the one that checks
    # the signature, and it is right to be the only one that does.
    printf '%s' "$token" | python3 -c '
import base64, json, sys, datetime
payload = sys.stdin.read().split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
for key in ("iss", "aud", "azp", "preferred_username", "email"):
    if key in claims:
        print(f"{key:20} {claims[key]}")
exp = claims.get("exp")
if exp:
    left = exp - int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    print("expires in".ljust(20) + f" {left}s")
'
    exit 0
fi

printf '%s\n' "$token"
