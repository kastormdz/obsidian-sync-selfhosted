# 04 — Servidor MCP (obsidian-sync-mcp)

## Qué hace

Expone el vault como tools MCP para cualquier agente de IA. Se conecta a CouchDB, **descifra el E2E
en memoria** y sirve las notas. El RAG no vive acá: corre en el cliente.

| Tool | Función |
|---|---|
| `read_note` | Leer una nota |
| `write_note` | Crear o sobrescribir |
| `list_notes` | Listar notas (⚠️ capa a **100** por defecto) |
| `list_folders` | Carpetas |
| `list_tags` | Tags |
| `edit_note` | Editar existente |
| `move_note` | Mover/renombrar |
| `delete_note` | Eliminar |
| `get_note_metadata` | Frontmatter, tags, backlinks |

Endpoints: `GET /health`, `GET /sse`, `POST /mcp`, `POST /oauth/register`.

## Despliegue

```yaml
services:
  obsidian-mcp:
    image: ghcr.io/es617/obsidian-sync-mcp@sha256:eefc083ffee77a72d5f2002dd92a8b310a3c58703ac040a39691a2778570e124  # v0.6.5
    container_name: obsidian-mcp
    restart: unless-stopped
    ports:
      - "127.0.0.1:8787:8787"
    env_file:
      - .env
    volumes:
      - /srv/docker/obsidian-mcp/data:/data
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

```bash
cp .env.example .env && $EDITOR .env
docker compose pull && docker compose up -d --wait
./scripts/obsidian-mcp-recreate.sh verify
```

El volumen `/data` guarda los tokens OAuth emitidos: si lo borrás, hay que volver a registrar el cliente.

## Variables de entorno

| Variable | Para qué |
|---|---|
| `COUCHDB_URL` | `http://host.docker.internal:5984` |
| `COUCHDB_USER` / `COUCHDB_PASSWORD` | credenciales del usuario **no-admin** (`livesync`) |
| `COUCHDB_DATABASE` | `obsidian` |
| `COUCHDB_PASSPHRASE` | passphrase E2E — **obligatoria** si el vault está cifrado |
| `MCP_AUTH_TOKEN` | bearer token que exige el servidor |
| `VAULT_NAME` | nombre del vault (se usa en los deep links `obsidian://`) |
| `DATA_DIR` | `/data` |

## Reglas

1. **Pinear por digest, nunca `:latest`.** Un `:latest` puede quedar dos releases atrás sin que nadie
   lo note — y sin los parches de seguridad de dependencias.
2. **Bind a `127.0.0.1`.** El cliente corre en el mismo host; no hay motivo para exponerlo.
3. **Usuario no-admin.** El MCP es un consumidor del vault, no un administrador.
4. **La passphrase E2E va en `.env` (0600).** Sin ella el servidor no descifra nada.

## Verificar la versión contra upstream

```bash
# versión que corre el contenedor
docker exec obsidian-mcp node -e 'console.log(require("/app/package.json").version)'

# último release publicado
curl -s https://api.github.com/repos/es617/obsidian-sync-mcp/releases/latest | grep tag_name

# digest de un tag (para pinear)
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:es617/obsidian-sync-mcp:pull&service=ghcr.io" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -sI -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://ghcr.io/v2/es617/obsidian-sync-mcp/manifests/v0.6.5 | grep -i docker-content-digest
```

## Actualizar

```bash
# 1. Nuevo digest
docker pull ghcr.io/es617/obsidian-sync-mcp:v0.6.6
docker image inspect ghcr.io/es617/obsidian-sync-mcp:v0.6.6 --format '{{index .RepoDigests 0}}'

# 2. Editar la línea image: en docker-compose.yml con el digest nuevo
# 3. Recrear y verificar de punta a punta
./scripts/obsidian-mcp-recreate.sh apply
```

Rollback: `./scripts/obsidian-mcp-recreate.sh rollback` (restaura el compose anterior desde el backup).
