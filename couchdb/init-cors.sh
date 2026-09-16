#!/usr/bin/env bash
# init-cors.sh — CORS de CouchDB para Obsidian LiveSync
#
# Uso:  ./init-cors.sh {status|apply|restore}
#
# Usa el alias `_node/_local/_config`, que resuelve siempre. La versión vieja de
# este repo usaba `nonode@nohup` (nombre de nodo inválido): los curl fallaban y
# el CORS quedaba sin aplicar en silencio.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/.env"
SNAPSHOT="$DIR/.cors-before.json"
CONTAINER="${COUCHDB_CONTAINER:-couchdb}"

# Origins que el plugin Self-hosted LiveSync necesita realmente:
#   app://obsidian.md      Obsidian desktop (Electron)
#   capacitor://localhost  Obsidian iOS
#   http://localhost       Obsidian Android
SAFE_ORIGINS='app://obsidian.md,capacitor://localhost,http://localhost'

# Credenciales: primero el contenedor, si no el .env
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  ENVV="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}')"
  USER_NAME="$(sed -n 's/^COUCHDB_USER=//p' <<<"$ENVV")"
  USER_PASS="$(sed -n 's/^COUCHDB_PASSWORD=//p' <<<"$ENVV")"
fi
if [ -z "${USER_NAME:-}" ] && [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  USER_NAME="$(sed -n 's/^COUCHDB_USER=//p' "$ENV_FILE")"
  USER_PASS="$(sed -n 's/^COUCHDB_PASSWORD=//p' "$ENV_FILE")"
fi
[ -n "${USER_NAME:-}" ] || { echo "ERROR: no pude resolver COUCHDB_USER (¿contenedor $CONTAINER? ¿$ENV_FILE?)" >&2; exit 1; }

API="http://127.0.0.1:5984/_node/_local/_config"
AUTH=(-u "$USER_NAME:$USER_PASS")
get() { curl -s "${AUTH[@]}" "$API/$1"; }
put() { curl -s -X PUT "${AUTH[@]}" -H 'Content-Type: application/json' "$API/$1" -d "$2" >/dev/null; }

case "${1:-status}" in
  status)
    echo "── CORS actual ─────────────────────────────"
    printf '  %-12s %s\n' "enable_cors" "$(curl -s "${AUTH[@]}" "http://127.0.0.1:5984/_node/_local/_config/chttpd/enable_cors")"
    for k in origins credentials headers methods; do
      printf '  %-12s %s\n' "$k" "$(get "cors/$k")"
    done
    echo "────────────────────────────────────────────"
    ;;

  apply)
    if [ ! -f "$SNAPSHOT" ]; then
      {
        printf '{\n  "chttpd/enable_cors": %s,\n' "$(curl -s "${AUTH[@]}" "http://127.0.0.1:5984/_node/_local/_config/chttpd/enable_cors")"
        for k in origins credentials headers methods; do
          printf '  "cors/%s": %s,\n' "$k" "$(get "cors/$k")"
        done
        printf '  "_note": "snapshot previo a init-cors.sh apply"\n}\n'
      } > "$SNAPSHOT"
      echo "▶ Snapshot previo guardado en $SNAPSHOT"
    fi
    put "chttpd/enable_cors" '"true"'
    put "cors/credentials" '"true"'
    put "cors/headers" '"accept,authorization,content-type,origin,referer,x-requested-with"'
    put "cors/methods" '"GET,PUT,POST,DELETE,OPTIONS"'
    put "cors/origins" "\"$SAFE_ORIGINS\""
    ACTUAL="$(get "cors/origins" | tr -d '"')"
    if [ "$ACTUAL" = "$SAFE_ORIGINS" ]; then
      echo "✔ origins = $ACTUAL"
    else
      echo "✘ quedó '$ACTUAL' — revisá el nombre del nodo" >&2; exit 1
    fi
    ;;

  restore)
    [ -f "$SNAPSHOT" ] || { echo "ERROR: no hay snapshot en $SNAPSHOT" >&2; exit 1; }
    ORIG="$(python3 -c "import json;print(json.load(open('$SNAPSHOT'))['cors/origins'])")"
    put "cors/origins" "\"$ORIG\""
    echo "✔ Restaurado origins = $ORIG"
    ;;

  *) echo "Uso: $0 {status|apply|restore}" >&2; exit 1 ;;
esac
