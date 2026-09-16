#!/usr/bin/env bash
# obsidian-couchdb-cors.sh — gestiona el CORS de CouchDB para Obsidian LiveSync
#
# Por qué existe: el README del repo usaba `nonode@nohup` (mal). CouchDB expone el
# alias `_node/_local/_config` que SIEMPRE funciona, sin hardcodear el nombre del nodo.
#
# Uso:
#   obsidian-couchdb-cors.sh status    → muestra la config actual
#   obsidian-couchdb-cors.sh apply     → restringe a los origins que LiveSync necesita
#   obsidian-couchdb-cors.sh restore   → vuelve al estado previo (desde el snapshot)
#
# Re-ejecutable e idempotente. Las credenciales se leen del contenedor, nunca del script.

set -euo pipefail

CONTAINER="${COUCHDB_CONTAINER:-couchdb}"
SNAPSHOT_DIR="${OBSIDIAN_CORS_SNAPSHOT_DIR:-${OBSIDIAN_WORKDIR:-${HOME}/tmp}/obsidian-couchdb-config}"

# Origins que el plugin Self-hosted LiveSync necesita de verdad:
#   app://obsidian.md      → Obsidian desktop (Electron)
#   capacitor://localhost  → Obsidian iOS (Capacitor)
#   http://localhost       → Obsidian Android (Capacitor)
# El cliente MCP NO usa CORS (habla HTTP server-side), así que restringir no afecta el RAG.
SAFE_ORIGINS='app://obsidian.md,capacitor://localhost,http://localhost'

USER_NAME="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^COUCHDB_USER=//p')"
USER_PASS="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^COUCHDB_PASSWORD=//p')"
[ -n "$USER_NAME" ] || { echo "ERROR: no pude leer COUCHDB_USER del contenedor $CONTAINER" >&2; exit 1; }

API="http://127.0.0.1:5984/_node/_local/_config"
AUTH=(-u "$USER_NAME:$USER_PASS")

cfg_get() { curl -s "${AUTH[@]}" "$API/$1"; }
cfg_put() { curl -s -X PUT "${AUTH[@]}" -H 'Content-Type: application/json' "$API/$1" -d "$2"; echo; }

case "${1:-status}" in
  status)
    echo "── CORS actual ──────────────────────────────────"
    for k in enable_cors origins credentials headers methods; do
      case "$k" in
        enable_cors) printf '  %-12s %s\n' "$k" "$(curl -s "${AUTH[@]}" "http://127.0.0.1:5984/_node/_local/_config/chttpd/enable_cors")" ;;
        *)           printf '  %-12s %s\n' "$k" "$(cfg_get "cors/$k")" ;;
      esac
    done
    echo "─────────────────────────────────────────────────"
    ;;

  apply)
    mkdir -p "$SNAPSHOT_DIR"
    if [ ! -f "$SNAPSHOT_DIR/cors-before.json" ]; then
      echo "▶ Snapshot del estado previo → $SNAPSHOT_DIR/cors-before.json"
      {
        echo '{'
        echo "  \"chttpd/enable_cors\": $(curl -s "${AUTH[@]}" "http://127.0.0.1:5984/_node/_local/_config/chttpd/enable_cors"),"
        for k in origins credentials headers methods; do
          printf '  "cors/%s": %s,\n' "$k" "$(cfg_get "cors/$k")"
        done
        echo "  \"_node\": \"_local\""
        echo '}'
      } > "$SNAPSHOT_DIR/cors-before.json"
    else
      echo "▶ Snapshot ya existente, no lo piso: $SNAPSHOT_DIR/cors-before.json"
    fi

    echo "▶ Aplicando CORS restringido"
    cfg_put "chttpd/enable_cors" '"true"' >/dev/null
    cfg_put "cors/credentials"   '"true"' >/dev/null
    cfg_put "cors/headers"       '"accept,authorization,content-type,origin,referer,x-requested-with"' >/dev/null
    cfg_put "cors/methods"       '"GET,PUT,POST,DELETE,OPTIONS"' >/dev/null
    cfg_put "cors/origins"       "\"$SAFE_ORIGINS\""

    echo "▶ Verificando"
    ACTUAL="$(cfg_get "cors/origins" | tr -d '"')"
    if [ "$ACTUAL" = "$SAFE_ORIGINS" ]; then
      echo "  ✔ origins = $ACTUAL"
    else
      echo "  ✘ quedó distinto: $ACTUAL" >&2; exit 1
    fi
    if curl -s "${AUTH[@]}" "http://127.0.0.1:5984/_node/_local/_config/chttpd/enable_cors" | grep -q true; then
      echo "  ✔ enable_cors = true"
    else
      echo "  ✘ enable_cors no está en true" >&2; exit 1
    fi
    echo "✔ CORS restringido. Rollback: $0 restore"
    ;;

  restore)
    [ -f "$SNAPSHOT_DIR/cors-before.json" ] || { echo "ERROR: no hay snapshot en $SNAPSHOT_DIR" >&2; exit 1; }
    ORIG="$(python3 -c "import json;print(json.load(open('$SNAPSHOT_DIR/cors-before.json'))['cors/origins'])")"
    cfg_put "cors/origins" "\"$ORIG\""
    echo "✔ Restaurado origins = $ORIG"
    ;;

  *) echo "Uso: $0 {status|apply|restore}" >&2; exit 1 ;;
esac
