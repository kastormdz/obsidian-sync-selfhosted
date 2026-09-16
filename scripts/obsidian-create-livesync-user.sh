#!/usr/bin/env bash
# obsidian-create-livesync-user.sh — crea el usuario NO-ADMIN para LiveSync + MCP
#
# Por qué existe: en producción todo corría con la cuenta admin de CouchDB. Un
# dispositivo perdido = acceso total al servidor. Este script crea un usuario con
# rol propio y lo habilita como *member* (no admin) de la base del vault.
#
# Idempotente: re-ejecutarlo no rompe nada ni rota la password existente.
#
# Uso:
#   obsidian-create-livesync-user.sh status   → ¿existe? ¿qué roles tiene la db?
#   obsidian-create-livesync-user.sh apply    → crea el usuario y ajusta _security
#   obsidian-create-livesync-user.sh rotate   → rota la password del usuario
#
# La password se genera acá y se guarda SOLO en /srv/docker/obsidian-mcp/.env (0600).

set -euo pipefail

CONTAINER="${COUCHDB_CONTAINER:-couchdb}"
DB="${COUCHDB_DATABASE:-obsidian}"
USER_NAME="${LIVESYNC_USER:-livesync}"
ROLE="livesync"
# El .env del MCP se ubica preguntándole a Docker dónde vive el compose (sin rutas hardcodeadas).
_mcp_dir() {
  local d
  d="$(docker inspect "${MCP_CONTAINER:-obsidian-mcp}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null)"
  if [ -n "$d" ] && [ "$d" != "<no value>" ] && [ -d "$d" ]; then echo "$d"; return; fi
  echo "${OBSIDIAN_MCP_DIR:-${HOME}/docker/obsidian-mcp}"
}
ENV_FILE="${OBSIDIAN_MCP_ENV:-$(_mcp_dir)/.env}"

ADMIN_USER="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^COUCHDB_USER=//p')"
ADMIN_PASS="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^COUCHDB_PASSWORD=//p')"

api() { curl -s -u "$ADMIN_USER:$ADMIN_PASS" "$@"; }
write_env() {
  umask 077
  touch "$ENV_FILE"
  grep -v -E '^(COUCHDB_USER|LIVESYNC_USER|LIVESYNC_PASSWORD|LIVESYNC_PASSWORD_RAW)=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}
gen_pass() {
  # alfanumérica a propósito: evita romper URLs/DSN con caracteres especiales
  python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(28)))"
}

case "${1:-status}" in
  status)
    echo "── Usuario $USER_NAME ──────────────────────────"
    if api "http://127.0.0.1:5984/_users/org.couchdb.user:$USER_NAME" | grep -q '"_id"'; then
      api "http://127.0.0.1:5984/_users/org.couchdb.user:$USER_NAME" \
        | python3 -c "import sys,json;d=json.load(sys.stdin);print('  estado: EXISTE');print('  roles :',d.get('roles'));print('  hash  :',(d.get('password') or d.get('derived_key') or '?')[:24]+'…')"
    else
      echo "  estado: NO EXISTE"
    fi
    echo "── _security de $DB ────────────────────────────"
    api "http://127.0.0.1:5984/$DB/_security" | python3 -m json.tool
    ;;

  apply)
    if api "http://127.0.0.1:5984/_users/org.couchdb.user:$USER_NAME" | grep -q '"_id"'; then
      echo "▶ El usuario ya existe — no roto su password"
      PASS="$(sed -n 's/^LIVESYNC_PASSWORD_RAW=//p' "$ENV_FILE" 2>/dev/null || true)"
      if [ -z "$PASS" ]; then
        echo "  ⚠ No tengo su password guardada → roto una nueva"
        "$0" rotate >/dev/null
        PASS="$(sed -n 's/^LIVESYNC_PASSWORD_RAW=//p' "$ENV_FILE")"
      fi
    else
      PASS="$(gen_pass)"
      echo "▶ Creando usuario $USER_NAME (rol: $ROLE)"
      python3 - "$ADMIN_USER" "$ADMIN_PASS" "$USER_NAME" "$PASS" "$ROLE" <<'PY'
import json, sys, urllib.request, base64
admin, apw, name, pw, role = sys.argv[1:6]
body = json.dumps({"name": name, "password": pw, "roles": [role], "type": "user"}).encode()
req = urllib.request.Request(f"http://127.0.0.1:5984/_users/org.couchdb.user:{name}", data=body, method="PUT")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", "Basic " + base64.b64encode(f"{admin}:{apw}".encode()).decode())
with urllib.request.urlopen(req) as r:
    print("  →", r.status, json.load(r).get("ok"))
PY
      echo "LIVESYNC_USER=$USER_NAME" >> "$ENV_FILE"
      echo "LIVESYNC_PASSWORD=$PASS" >> "$ENV_FILE"
      echo "LIVESYNC_PASSWORD_RAW=$PASS" >> "$ENV_FILE"
      chmod 600 "$ENV_FILE"
      echo "  ✔ password guardada en $ENV_FILE (0600)"
    fi

    echo "▶ Habilitando rol '$ROLE' como member de $DB (sin tocar admins)"
    api -X PUT -H 'Content-Type: application/json' "http://127.0.0.1:5984/$DB/_security" \
      -d "{\"members\":{\"roles\":[\"_admin\",\"$ROLE\"]},\"admins\":{\"roles\":[\"_admin\"]}}"
    echo

    echo "▶ Prueba de permisos reales (login + read + write + delete)"
    python3 - "$USER_NAME" "$PASS" "$DB" <<'PY'
import json, sys, urllib.request, urllib.error, base64
name, pw, db = sys.argv[1:4]
auth = "Basic " + base64.b64encode(f"{name}:{pw}".encode()).decode()
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:5984{path}",
        data=json.dumps(body).encode() if body else None, method=method)
    req.add_header("Authorization", auth)
    if body: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)

s, _ = call("POST", "/_session", {"name": name, "password": pw})
print(f"  login      → {s} {'✔' if s==200 else '✘'}")
s, d = call("GET", f"/{db}/00-index.md")
print(f"  read nota  → {s} {'✔' if s==200 else '✘'}")
s, d = call("PUT", f"/{db}/perm-check-tmp", {"x": 1})
rev = d.get("rev") if s in (201, 202) else None
print(f"  write test → {s} {'✔' if rev else '✘'}")
if rev:
    s, _ = call("DELETE", f"/{db}/perm-check-tmp?rev={rev}")
    print(f"  delete     → {s} {'✔' if s in (200,202) else '✘'}")
s, _ = call("PUT", f"/{db}/_design/perm-check")
print(f"  _design    → {s} {'(esperado 401/403: solo admin)' if s in (401,403) else '⚠ inesperado'}")
PY
    ;;

  rotate)
    PASS="$(gen_pass)"
    python3 - "$ADMIN_USER" "$ADMIN_PASS" "$USER_NAME" "$PASS" "$ROLE" <<'PY'
import json, sys, urllib.request, base64
admin, apw, name, pw, role = sys.argv[1:6]
auth = "Basic " + base64.b64encode(f"{admin}:{apw}".encode()).decode()
req = urllib.request.Request(f"http://127.0.0.1:5984/_users/org.couchdb.user:{name}")
req.add_header("Authorization", auth)
with urllib.request.urlopen(req) as r: doc = json.load(r)
doc["password"] = pw; doc["roles"] = [role]
req = urllib.request.Request(f"http://127.0.0.1:5984/_users/org.couchdb.user:{name}",
    data=json.dumps(doc).encode(), method="PUT")
req.add_header("Content-Type", "application/json"); req.add_header("Authorization", auth)
with urllib.request.urlopen(req) as r: print("  →", r.status, json.load(r).get("ok"))
PY
    write_env
    { echo "LIVESYNC_USER=$USER_NAME"; echo "LIVESYNC_PASSWORD=$PASS"; echo "LIVESYNC_PASSWORD_RAW=$PASS"; } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "✔ Password rotada y guardada en $ENV_FILE (hay que actualizarla en cada dispositivo)"
    ;;

  *) echo "Uso: $0 {status|apply|rotate}" >&2; exit 1 ;;
esac
