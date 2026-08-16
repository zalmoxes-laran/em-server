#!/usr/bin/env bash
# fcn-down — spegni il Field Computing Node.
#
#   ./fcn-down.sh            # spegnimento pulito: rimuove i container, TIENE i dati
#                            #   (studi, stanze, bucket, realm, CA di Caddy) → riparte con ./fcn-up.sh
#   ./fcn-down.sh --stop     # solo pausa: container fermati ma non rimossi (ripartenza in secondi)
#   ./fcn-down.sh --wipe     # ⚠ AZZERA TUTTO: cancella anche i volumi (dati persi)
#   ./fcn-down.sh --colima   # dopo lo spegnimento, ferma anche la VM Colima (libera RAM)
#
# I flag si combinano: es.  ./fcn-down.sh --wipe --colima
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} /^[[:space:]]*$/{next} {exit}' "$0"; exit 0
fi
set -euo pipefail
cd "$(dirname "$0")"                     # em-server/dev-stack

COMPOSE=(docker-compose -f docker-compose.dev.yml --profile https)
MODE="down"; STOP_COLIMA="no"
for a in "$@"; do
  case "$a" in
    --stop)   MODE="stop" ;;
    --wipe|-v) MODE="wipe" ;;
    --colima) STOP_COLIMA="yes" ;;
    *) echo "flag sconosciuto: $a (usa --stop | --wipe | --colima)"; exit 2 ;;
  esac
done

case "$MODE" in
  stop)
    echo "⏸  fermo i container (li tengo, ripartenza in secondi)…"
    "${COMPOSE[@]}" stop
    ;;
  down)
    echo "▶ spegnimento pulito: rimuovo i container, TENGO i dati…"
    "${COMPOSE[@]}" down
    echo "   i volumi (studi/stanze/bucket/realm/CA) restano; ./fcn-up.sh riparte da lì."
    ;;
  wipe)
    echo "⚠  --wipe: sto per CANCELLARE tutti i dati dell'FCN (studi, foto, utenti, e la CA di Caddy)."
    echo "   Dopo un wipe, l'altro computer dovrà RI-FIDARSI della nuova CA."
    read -r -p "   Scrivi 'wipe' per confermare: " ok
    [ "$ok" = "wipe" ] || { echo "annullato."; exit 1; }
    "${COMPOSE[@]}" down -v
    echo "   tutto azzerato; il prossimo ./fcn-up.sh ri-semina bucket e realm da capo."
    ;;
esac

if [ "$STOP_COLIMA" = "yes" ]; then
  echo "▶ fermo Colima…"
  colima stop || true
fi

echo "✔ FCN spento (${MODE})."
