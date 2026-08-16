#!/usr/bin/env bash
# fcn-up — accendi il Field Computing Node: Caddy+https su hostname (mai IP nudo),
# e tira su l'intera stack StratiGraph.
#
#   ./fcn-up.sh                 # locale: https://em.localhost:8443 (+ serve anche il nome Bonjour .local)
#   ./fcn-up.sh mac.local       # host PRIMARIO = un hostname risolvibile (per l'altro computer)
#   ./fcn-up.sh --local-s3d     # s3Dgraphy dal CHECKOUT LOCALE (editi e testi live)
#   ./fcn-up.sh mac.local --local-s3d
#
# NB: la CA interna di Caddy NON fa certificati per un IP nudo → per l'altro computer
# serve un HOSTNAME (il nome Bonjour `<mac>.local`, o /etc/hosts, o un dominio vero),
# non 172.x.x.x. E la rete deve vedersi (hotspot che isola i client → travel-router/Tailscale).
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} /^[[:space:]]*$/{next} {exit}' "$0"; exit 0
fi
set -euo pipefail
cd "$(dirname "$0")"                     # em-server/dev-stack

HTTPS_PORT="${HTTPS_PORT:-8443}"
DEV_REALM="${DEV_REALM:-em-dev}"

# ── argomenti: hostname PRIMARIO opzionale + --local-s3d ─────────────────────
LOCAL_S3D="no"; ARG_HOST=""
for a in "$@"; do
  case "$a" in
    --local-s3d) LOCAL_S3D="yes" ;;
    -*) echo "flag sconosciuto: $a (usa --local-s3d)"; exit 2 ;;
    *) ARG_HOST="$a" ;;
  esac
done

# ── 1 · Colima su ────────────────────────────────────────────────────────────
if ! colima status >/dev/null 2>&1; then
  echo "▶ avvio Colima…"
  colima start --cpu 4 --memory 8 --network-address
fi
docker context use colima >/dev/null 2>&1 || true

# ── 2 · host primario (browser) + gli indirizzi che Caddy serve ──────────────
PRIMARY="${ARG_HOST:-em.localhost}"                 # dove punta il browser (URL pubblici)
BONJOUR="$(scutil --get LocalHostName 2>/dev/null || true)"; [ -n "$BONJOUR" ] && BONJOUR="${BONJOUR}.local"
# Caddy serve em.localhost SEMPRE, + il primario e il nome Bonjour se diversi (hostname, mai IP)
addrs="https://em.localhost"
[ "$PRIMARY" != "em.localhost" ] && addrs="$addrs, https://$PRIMARY"
[ -n "$BONJOUR" ] && [ "$BONJOUR" != "em.localhost" ] && [ "$BONJOUR" != "$PRIMARY" ] && addrs="$addrs, https://$BONJOUR"
export EM_SITE="$addrs"
export EM_DEV_DOMAIN="$PRIMARY"

# ── 3 · gli URL PUBBLICI puntano all'host primario ───────────────────────────
export EM_IIIF_PUBLIC="https://${PRIMARY}:${HTTPS_PORT}/iiif"
export EM_CATALOG_EMSTUDIO_URL="https://${PRIMARY}:${HTTPS_PORT}"
# ⚠ VERIFICA: l'issuer OIDC dipende da come Caddy espone Keycloak nel Caddyfile.dev
export OIDC_ISSUER="https://${PRIMARY}:${HTTPS_PORT}/auth/realms/${DEV_REALM}"

# ── 4 · su (con l'override s3Dgraphy-locale se richiesto) ─────────────────────
COMPOSE=(docker-compose --env-file .env.dev -f docker-compose.dev.yml)
if [ "$LOCAL_S3D" = "yes" ]; then
  COMPOSE+=(-f docker-compose.local-s3d.yml)
  echo "▶ modo s3Dgraphy LOCALE: em-server/em-catalog useranno ../../s3Dgraphy/src (edita e riavvia per testare)."
fi
"${COMPOSE[@]}" --profile https up -d --build

# ── 5 · indirizzi + promemoria ───────────────────────────────────────────────
cat <<EOF

✔ FCN acceso${LOCAL_S3D:+ (s3Dgraphy locale)}. Caddy serve: ${EM_SITE}
  Su questo computer:   https://${PRIMARY}:${HTTPS_PORT}/em/v1/health
$( [ -n "$BONJOUR" ] && [ "$BONJOUR" != "$PRIMARY" ] && echo "  Per l'ALTRO computer: https://${BONJOUR}:${HTTPS_PORT}/em/v1/health   (via Bonjour/mDNS)" )

Note:
  · la ROOT (/) è vuota: apri un percorso vero — /em/v1/health , /catalog/ , /iiif/…
  · certificato: se il browser lo rifiuta → ./fcn-trust-ca.sh (una volta; e dopo ogni --wipe).
  · altro computer: serve un HOSTNAME (mai IP nudo, rompe il TLS della CA interna) e che le
    due macchine si vedano in rete (hotspot che isola → travel-router · Internet-Sharing · Tailscale).
    Per usarlo come primario:  ./fcn-up.sh ${BONJOUR:-<mac>.local}
$( [ "$LOCAL_S3D" = "yes" ] && echo "  · dopo aver editato s3Dgraphy:  docker-compose -f docker-compose.dev.yml -f docker-compose.local-s3d.yml restart em-server em-catalog" )
Giù:  ./fcn-down.sh   (o --stop / --wipe / --colima)
EOF
