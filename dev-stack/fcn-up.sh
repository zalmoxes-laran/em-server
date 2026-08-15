#!/usr/bin/env bash
# fcn-up — accendi il Field Computing Node: rileva l'indirizzo di rete, punta gli URL
# pubblici a quell'indirizzo (così un ALTRO computer non viene rimandato a sé stesso),
# e tira su l'intera stack StratiGraph dietro Caddy+https.
#
#   ./fcn-up.sh                 # usa l'IP LAN rilevato
#   ./fcn-up.sh strati.local    # forza un hostname (se ne hai uno risolvibile)
#
# NON è magia di rete: se i due computer non si vedono (hotspot che isola i client),
# nessuna configurazione della stack lo aggira — vedi i promemoria alla fine.
set -euo pipefail
cd "$(dirname "$0")"                     # em-server/dev-stack

HTTPS_PORT="${HTTPS_PORT:-8443}"
KEYCLOAK_PORT="${KEYCLOAK_PORT:-8085}"
DEV_REALM="${DEV_REALM:-em-dev}"

# ── 1 · Colima su ────────────────────────────────────────────────────────────
if ! colima status >/dev/null 2>&1; then
  echo "▶ avvio Colima…"
  # --network-address dà alla VM un IP raggiungibile in LAN: è ciò che serve
  # perché un altro computer arrivi alle porte (senza, Colima lega a 127.0.0.1).
  colima start --cpu 4 --memory 8 --network-address
fi
docker context use colima >/dev/null 2>&1 || true

# ── 2 · l'indirizzo dell'FCN sulla rete attuale ──────────────────────────────
# Preferisci l'IP della VM Colima (raggiungibile in LAN con --network-address);
# se non c'è, ripiega sull'IP LAN del Mac (che con Colima spesso NON basta).
COLIMA_IP="$(colima ls -j 2>/dev/null | sed -n 's/.*"address":"\([0-9.]*\)".*/\1/p' | head -1 || true)"
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
LAN_IP="$(ipconfig getifaddr "${IFACE:-en0}" 2>/dev/null || ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
FCN_HOST="${1:-${COLIMA_IP:-$LAN_IP}}"
if [ -z "$FCN_HOST" ]; then echo "✗ non trovo un indirizzo di rete. Sei online?"; exit 1; fi

# ── 3 · gli URL PUBBLICI puntano all'FCN, non a localhost ────────────────────
# (gli indirizzi INTERNI restano nomi-servizio della rete docker: non si toccano)
export EM_DEV_DOMAIN="$FCN_HOST"
export EM_IIIF_PUBLIC="https://${FCN_HOST}:${HTTPS_PORT}/iiif"
export EM_CATALOG_EMSTUDIO_URL="https://${FCN_HOST}:${HTTPS_PORT}"
# ⚠ VERIFICA: l'issuer OIDC dipende da come Caddy espone Keycloak nel tuo
# Caddyfile.dev. Se Keycloak è dietro Caddy (es. rotta /auth) usa la prima riga;
# se è esposto sulla sua porta diretta, usa la seconda. Controlla Caddyfile.dev.
export OIDC_ISSUER="https://${FCN_HOST}:${HTTPS_PORT}/auth/realms/${DEV_REALM}"
# export OIDC_ISSUER="http://${FCN_HOST}:${KEYCLOAK_PORT}/realms/${DEV_REALM}"

# ── 4 · su, profilo https ────────────────────────────────────────────────────
docker-compose --env-file .env.dev -f docker-compose.dev.yml --profile https up -d --build

# ── 5 · l'indirizzo per l'altro computer + i promemoria onesti ───────────────
cat <<EOF

✔ FCN acceso.
  Su questo computer:   https://em.localhost:${HTTPS_PORT}
  Per l'ALTRO computer: https://${FCN_HOST}:${HTTPS_PORT}

Perché l'altro computer arrivi (tre ostacoli, in ordine):
  1) DEVONO VEDERSI IN RETE. L'hotspot del telefono spesso isola i client.
     Prova dall'altro:   ping ${FCN_HOST}
     se non risponde → travel-router · Condivisione Internet di macOS via cavo · Tailscale.
  2) se il ping arriva ma la pagina no: Colima. Assicurati di aver avviato con
     'colima start --network-address' (questo script lo fa al primo avvio).
  3) fidati della CA di Caddy sull'altro computer, o il browser rifiuta il certificato
     (la CA vive nel volume caddy_data; il comando di trust è nel README-DEV).

Giù:  docker-compose -f docker-compose.dev.yml --profile https down   (aggiungi -v per azzerare i dati)
EOF
