#!/usr/bin/env bash
# bump-s3dgraphy.sh — allinea em-server a una nuova s3dgraphy pubblicata su PyPI.
# Aggiorna il pin ESATTO in pyproject.toml e nel Dockerfile (tiene gli extra
# [geo,rdf], cambia solo il numero), mostra il diff, e — con --build — ricostruisce
# e riavvia em-server nel dev-stack (il bump della versione fa da cache-bust del layer).
#
#   ./bump-s3dgraphy.sh 1.6.0.dev15            # imposta questa versione
#   ./bump-s3dgraphy.sh --latest              # prende l'ultima da PyPI (pre comprese)
#   ./bump-s3dgraphy.sh 1.6.0.dev15 --build   # bumpa E ricostruisce+riavvia em-server
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} /^[[:space:]]*$/{next} {exit}' "$0"; exit 0
fi
set -euo pipefail
cd "$(dirname "$0")"                          # em-server (dove stanno pyproject.toml + Dockerfile)

BUILD="no"; VER=""
for a in "$@"; do
  case "$a" in
    --build)  BUILD="yes" ;;
    --latest) VER="__latest__" ;;
    -* )      echo "flag sconosciuto: $a"; exit 2 ;;
    * )       VER="$a" ;;
  esac
done
[ -n "$VER" ] || { echo "uso: ./bump-s3dgraphy.sh <versione|--latest> [--build]"; exit 2; }

# --latest: chiede a PyPI l'ultima, pre-release comprese (best-effort; se fallisce, passala a mano)
if [ "$VER" = "__latest__" ]; then
  echo "▶ cerco l'ultima s3dgraphy su PyPI (pre comprese)…"
  VER=$(pip index versions --pre s3dgraphy 2>/dev/null \
        | sed -n 's/.*[Aa]vailable versions: *//p' | tr ',' '\n' | head -1 | tr -d ' ')
  [ -n "$VER" ] || { echo "✗ non riesco a leggere la versione da PyPI; passala a mano."; exit 1; }
  echo "  ultima = $VER"
fi

# forma plausibile (es. 1.6.0 o 1.6.0.dev15)
echo "$VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$' \
  || { echo "✗ versione sospetta: '$VER' (attesa tipo 1.6.0.dev15)"; exit 1; }

# sostituisci OVUNQUE compaia il pin: tiene s3dgraphy[...]== , cambia solo la versione.
# Le righe di commento che nominano una vecchia dev NON hanno '==' e restano intatte.
changed=0
for f in pyproject.toml Dockerfile; do
  [ -f "$f" ] || { echo "⚠ $f non trovato, salto"; continue; }
  sed -E -i.bak "s/(s3dgraphy(\[[a-z,]*\])?==)[0-9][A-Za-z0-9.]*/\1${VER}/g" "$f"
  rm -f "$f.bak"; changed=1
done
[ "$changed" = "1" ] || { echo "✗ né pyproject.toml né Dockerfile trovati — sei in em-server?"; exit 1; }

echo "▶ pin aggiornato a s3dgraphy[geo,rdf]==$VER. Diff:"
git diff -- pyproject.toml Dockerfile 2>/dev/null || echo "  (git non disponibile: controlla i file a mano)"

if [ "$BUILD" = "yes" ]; then
  echo "▶ ricostruisco em-server nel dev-stack…"
  ( cd dev-stack && docker-compose -f docker-compose.dev.yml build em-server \
      && docker-compose -f docker-compose.dev.yml up -d em-server )
  echo "✔ em-server ricostruito e riavviato con s3dgraphy $VER."
else
  echo "  Per applicarlo:  ./bump-s3dgraphy.sh $VER --build"
  echo "  (o:  cd dev-stack && docker-compose -f docker-compose.dev.yml build em-server && up -d em-server)"
fi
