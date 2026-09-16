#!/usr/bin/env bash
# obsidian-mcp-recreate.sh — recrea el MCP de Obsidian y VERIFICA de punta a punta
#
# Uso:
#   obsidian-mcp-recreate.sh apply     → pull + recreate + verificación end-to-end
#   obsidian-mcp-recreate.sh verify    → solo verifica (no toca nada)
#   obsidian-mcp-recreate.sh rollback  → vuelve al compose anterior
#
# Verifica: digest corriendo, versión interna, health, y una lectura real del vault
# a través del cliente MCP (o sea: auth + CouchDB + descifrado E2E funcionando).

set -euo pipefail

WORK="${OBSIDIAN_WORKDIR:-${HOME}/tmp}"
CLIENT="${OBSIDIAN_MCP_CLIENT:-${HOME}/.hermes/scripts/obsidian-mcp-client.py}"
BACKUP="${OBSIDIAN_MCP_BACKUP:-${WORK}/obsidian-hardening/obsidian-mcp-compose.before.yml}"

# Directorio del compose: se lo preguntamos a Docker para no hardcodear rutas
# (y para que el script siga funcionando si el deploy se muda de lugar).
DIR="${OBSIDIAN_MCP_DIR:-$(docker inspect obsidian-mcp \
      --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null)}"
if [ -z "$DIR" ] || [ "$DIR" = "<no value>" ] || [ ! -d "$DIR" ]; then
  DIR="${OBSIDIAN_MCP_DIR:-${HOME}/docker/obsidian-mcp}"
fi

esperado_digest() { grep -oE 'sha256:[0-9a-f]{64}' "$DIR/docker-compose.yml" | head -1; }

verify() {
  local ok=0
  echo "── Verificación ────────────────────────────────"

  local ref; ref="$(docker inspect obsidian-mcp --format '{{.Config.Image}}' 2>/dev/null || echo 'no-existe')"
  local rdig; rdig="$(docker image inspect "$(docker inspect obsidian-mcp --format '{{.Image}}')" --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2)"
  local want; want="$(esperado_digest)"
  if [ "$rdig" = "$want" ]; then
    echo "  digest   ✔ $ref"
  else
    echo "  digest   ✘ RepoDigest $rdig / esperado $want"; ok=1
  fi

  local ver; ver="$(docker exec obsidian-mcp node -e 'console.log(require("/app/package.json").version)' 2>/dev/null || echo '?')"
  echo "  versión  → v$ver"

  local usr; usr="$(docker inspect obsidian-mcp --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^COUCHDB_USER=//p')"
  if [ "$usr" = "admin" ]; then echo "  usuario  ⚠ ADMIN (debería ser no-admin)"; else echo "  usuario  ✔ $usr (no-admin)"; fi

  local h; h="$(curl -s -m 8 http://localhost:8787/health || echo 'sin respuesta')"
  if [ "$h" = "✓ Ok" ]; then echo "  health   ✔ $h"; else echo "  health   ✘ $h"; ok=1; fi

  echo "  lectura real del vault vía MCP:"
  local primera=""
  if timeout 90 python3 "$CLIENT" list "" 3 >/tmp/_mcp_test 2>&1 && grep -q "📝" /tmp/_mcp_test; then
    echo "    ✔ $(grep -c '📝' /tmp/_mcp_test) notas listadas"
    primera="$(grep '📝' /tmp/_mcp_test | head -1 | sed 's/^.*📝 *//')"
  else
    echo "    ✘ sin notas: $(head -2 /tmp/_mcp_test)"; ok=1
  fi

  if [ -n "$primera" ]; then
    # Sin pipe a head: con pipefail, el SIGPIPE de python marcaba falsos negativos
    local salida=""
    salida="$(timeout 120 python3 "$CLIENT" read "$primera" 2>/dev/null || true)"
    if [ -n "$salida" ]; then
      echo "    ✔ lectura + descifrado E2E OK ($primera · ${#salida} bytes)"
    else
      echo "    ✘ '$primera' devolvió vacío"; ok=1
    fi
  fi
  rm -f /tmp/_mcp_test
  echo "────────────────────────────────────────────────"
  return $ok
}

case "${1:-apply}" in
  verify) verify ;;
  apply)
    cd "$DIR"
    echo "▶ Pull de la imagen pineada"
    docker compose pull 2>&1 | tail -2
    echo "▶ Recreando contenedor"
    docker compose up -d --wait 2>&1 | tail -3
    sleep 5
    verify
    ;;
  rollback)
    [ -f "$BACKUP" ] || { echo "ERROR: no encuentro el backup $BACKUP" >&2; exit 1; }
    cp "$BACKUP" "$DIR/docker-compose.yml"
    cd "$DIR" && docker compose up -d --wait 2>&1 | tail -3
    sleep 5
    verify
    ;;
  *) echo "Uso: $0 {apply|verify|rollback}" >&2; exit 1 ;;
esac
