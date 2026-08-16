#!/usr/bin/env bash
# fcn-trust-ca — estrai la CA interna di Caddy dell'FCN e falla fidare a macOS.
# Serve UNA volta (e di nuovo dopo ./fcn-down.sh --wipe, che rigenera la CA).
# Chiede la password (sudo, per scrivere nel portachiavi di sistema).
#
#   ./fcn-trust-ca.sh                 # fidati della CA su QUESTO Mac
#   ./fcn-trust-ca.sh --export-only   # solo estrai il .crt (per copiarlo sull'altro Mac)
#
# Sull'ALTRO computer: copia il file caddy-em-root.crt e lancia lì lo stesso comando
# 'security add-trusted-cert' (o importalo da Accesso Portachiavi → fidati sempre).
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} /^[[:space:]]*$/{next} {exit}' "$0"; exit 0
fi
set -euo pipefail

CONT="em-dev-caddy"
CRT="$HOME/caddy-em-root.crt"
HTTPS_PORT="${HTTPS_PORT:-8443}"

echo "▶ estraggo il root CA dal container ${CONT}…"
docker cp "$CONT:/data/caddy/pki/authorities/local/root.crt" "$CRT"
echo "  salvato in $CRT"

if [ "${1:-}" = "--export-only" ]; then
  echo "✔ solo export. Copialo sull'altro Mac e fidati lì (Accesso Portachiavi, o security add-trusted-cert)."
  exit 0
fi

echo "▶ lo aggiungo al portachiavi di sistema (serve la password)…"
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$CRT"

echo "✔ CA fidata. RIAVVIA Safari (esci del tutto e riapri) e ricarica:"
echo "    https://em.localhost:${HTTPS_PORT}/em/v1/health"
echo "  (dopo un ./fcn-down.sh --wipe la CA cambia → rilancia questo script.)"
