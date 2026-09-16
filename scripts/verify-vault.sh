#!/usr/bin/env bash
# verify-vault.sh — healthcheck end-to-end del stack de Obsidian
#
# Uso:  scripts/verify-vault.sh
# Exit: 0 = todo bien · 1 = algo falló (imprime qué)
#
# Verifica la cadena completa: CouchDB → usuario no-admin → CORS → MCP → RAG → backup.
# Pensado para correr a mano o desde un cron/watchdog.

set -uo pipefail

CLIENT="${OBSIDIAN_MCP_CLIENT:-$HOME/.hermes/scripts/obsidian-mcp-client.py}"
MCP_URL="${OBSIDIAN_MCP_URL:-http://127.0.0.1:8787}"
CONTAINER="${COUCHDB_CONTAINER:-couchdb}"
S3_BUCKET="${OBSIDIAN_S3_BUCKET:-hermes-configs}"
S3_PREFIX="obsidian-vault"
SECRETS="${HERMES_S3_SECRETS:-$HOME/.hermes/.secrets/hermes-backups.env}"
MAX_AGE_H=30          # el backup no debe tener más de N horas
MIN_NOTES=1           # ajustar al tamaño real del vault

fails=0
ok()   { printf '  ✔ %s\n' "$1"; }
bad()  { printf '  ✘ %s\n' "$1"; fails=$((fails+1)); }
warn() { printf '  ⚠ %s\n' "$1"; }

PW="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^COUCHDB_PASSWORD=//p')"

echo "── 1. CouchDB ─────────────────────────────────"
if [ -z "$PW" ]; then bad "no pude leer COUCHDB_PASSWORD del contenedor $CONTAINER"
else
  if curl -s -m 8 -u "admin:$PW" http://127.0.0.1:5984/_up | grep -q '"status":"ok"'; then ok "responde /_up"; else bad "no responde /_up"; fi
  INFO="$(curl -s -m 8 -u "admin:$PW" http://127.0.0.1:5984/obsidian)"
  if grep -q '"db_name"' <<<"$INFO"; then
    DOCS="$(python3 -c "import sys,json;print(json.load(sys.stdin)['doc_count'])" <<<"$INFO" 2>/dev/null)"
    ok "base 'obsidian' · ${DOCS:-?} documentos"
  else bad "la base 'obsidian' no responde"; fi
fi

echo "── 2. Usuario no-admin ────────────────────────"
SEC="$(curl -s -m 8 -u "admin:$PW" http://127.0.0.1:5984/obsidian/_security)"
if grep -q '"livesync"' <<<"$SEC"; then ok "rol 'livesync' es member de la base"; else bad "el rol no-admin NO es member de la base"; fi

echo "── 3. CORS ────────────────────────────────────"
ORIG="$(curl -s -m 8 -u "admin:$PW" http://127.0.0.1:5984/_node/_local/_config/cors/origins)"
if grep -q '\*' <<<"$ORIG"; then bad "CORS tiene wildcard: $ORIG"
elif grep -q 'app://obsidian.md' <<<"$ORIG"; then ok "orígenes restringidos y con app://obsidian.md"
else bad "CORS sin app://obsidian.md: $ORIG"; fi

echo "── 4. Servidor MCP ────────────────────────────"
if [ "$(curl -s -m 8 "$MCP_URL/health")" = "✓ Ok" ]; then ok "health OK en $MCP_URL"; else bad "health falló en $MCP_URL"; fi
IMG="$(docker inspect obsidian-mcp --format '{{.Config.Image}}' 2>/dev/null || echo '')"
if grep -q '@sha256:' <<<"$IMG"; then ok "imagen pineada por digest"; else warn "imagen NO pineada: ${IMG:-no encontrada}"; fi
USR="$(docker inspect obsidian-mcp --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^COUCHDB_USER=//p')"
if [ "$USR" = "admin" ]; then warn "el MCP usa la cuenta ADMIN (debería ser no-admin)"; else ok "MCP usa usuario no-admin ($USR)"; fi

echo "── 5. Lectura real del vault ──────────────────"
if [ ! -x "$CLIENT" ] && [ ! -f "$CLIENT" ]; then warn "cliente MCP no encontrado en $CLIENT (salteo lectura)"
else
  primera="$(timeout 90 python3 "$CLIENT" list "" 3 2>/dev/null | grep '📝' | head -1 | sed 's/^.*📝 *//')"
  if [ -n "$primera" ]; then
    ok "listado OK (primera: $primera)"
    salida="$(timeout 120 python3 "$CLIENT" read "$primera" 2>/dev/null || true)"
    if [ -n "$salida" ]; then ok "lectura + descifrado E2E OK (${#salida} bytes)"; else bad "no pude leer '$primera'"; fi
  else bad "el listado del vault no devolvió notas"; fi
fi

echo "── 6. Backup en S3 ────────────────────────────"
NOTAS="$(timeout 60 python3 - "$SECRETS" "$S3_BUCKET" "$S3_PREFIX" <<'PY' 2>/dev/null
import sys, os, boto3
from datetime import datetime, timezone
secrets, bucket, prefix = sys.argv[1:4]
cfg = {}
for line in open(os.path.expanduser(secrets)):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
s3 = boto3.client("s3", endpoint_url=cfg["S3_ENDPOINT"],
                  aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
                  aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"])
r = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/dumps/")
ult = sorted(r.get("Contents", []), key=lambda o: o["LastModified"])[-1]
age_h = (datetime.now(timezone.utc) - ult["LastModified"]).total_seconds() / 3600
snap = sorted(p["Prefix"] for p in s3.list_objects_v2(
    Bucket=bucket, Prefix=f"{prefix}/notes/", Delimiter="/").get("CommonPrefixes", []))[-1]
n = sum(p.get("KeyCount", 0) for p in s3.get_paginator("list_objects_v2").paginate(
    Bucket=bucket, Prefix=snap))
print(f"{n} {age_h:.1f}")
PY
)"
if [ -n "$NOTAS" ]; then
  set -- $NOTAS
  N="$1"; AGE="$2"
  if [ "${N:-0}" -lt "$MIN_NOTES" ]; then bad "el último snapshot tiene $N notas"; else ok "último snapshot: $N notas"; fi
  if python3 -c "import sys;sys.exit(0 if float('${AGE:-999}') <= $MAX_AGE_H else 1)"; then ok "backup de hace ${AGE} h"; else bad "backup viejo: ${AGE} h"; fi
else warn "no pude consultar S3 (¿boto3? ¿$SECRETS?)"; fi

echo "───────────────────────────────────────────────"
if [ "$fails" -eq 0 ]; then echo "TODO OK"; else echo "$fails chequeo(s) fallaron"; fi
exit $(( fails > 0 ))
