# 07 — Runbook

## Verificación rápida (30 segundos)

```bash
scripts/verify-vault.sh
```

Chequea, en orden:

1. CouchDB responde (`/_up`) y la base existe.
2. La versión de CouchDB y el conteo de documentos.
3. El usuario **no-admin** puede loguearse y leer.
4. CORS tiene los orígenes correctos (y **no** el wildcard).
5. El contenedor del MCP corre el digest pineado y está `healthy`.
6. El cliente MCP lee una nota real (auth + descifrado E2E funcionando).
7. El último backup de S3 es de las últimas 24 h y tiene todas las notas.

## Troubleshooting

### LiveSync no sincroniza

| Síntoma | Causa probable | Chequeo |
|---|---|---|
| Error de CORS en la consola | Los orígenes no incluyen el cliente | `curl -s -u u:p http://127.0.0.1:5984/_node/_local/_config/cors/origins` |
| Conecta pero no ve cambios | WebSocket apagado en el proxy | Revisar que NPM tenga WebSockets ON en ese host |
| `401 Unauthorized` | Credenciales o rol | El usuario debe ser *member* de la base (no hace falta admin) |
| Notas vacías / basura | Passphrase E2E distinta | La passphrase debe ser **idéntica** en todos los dispositivos |
| Un dispositivo quedó desincronizado | Milestone roto | En el plugin: *Fetch* completo → *Rebuild* |

### El RAG no encuentra algo

1. ¿Usaste `search` en vez de `rag`? `search` mira **solo el nombre**.
2. ¿La nota se escribió hace poco? Correr `cache-clear` y reintentar.
3. ¿El vault tiene más de 100 notas? El servidor capa a 100; el cliente pide el vault completo en
   `rag-improved`, pero si algo usa `list_notes()` directo, va a ver solo las primeras 100.
4. ¿La nota está dentro de una carpeta ignorada por el plugin? (patrón `_` o `.`).

### El MCP devuelve vacío o error de descifrado

```bash
docker logs --tail 50 obsidian-mcp
docker exec obsidian-mcp node -e 'console.log(require("/app/package.json").version)'
```

- `COUCHDB_PASSPHRASE` ausente o distinta → no puede descifrar.
- Usuario sin rol `member` → no puede leer.
- `/data` borrado → hay que re-registrar el cliente OAuth.

### El contenedor arranca y se cae

```bash
docker logs obsidian-mcp
docker compose -f /srv/docker/obsidian-mcp/docker-compose.yml config   # validar env_file presente
```

El error más común: `.env` ausente o sin `COUCHDB_URL`, porque el compose usa `env_file`.

## Operaciones de mantenimiento

| Tarea | Comando |
|---|---|
| Compactar CouchDB | `curl -X POST -u u:p http://127.0.0.1:5984/obsidian/_compact` |
| Ver compactación en curso | `curl -s -u u:p http://127.0.0.1:5984/obsidian/_active_tasks` |
| Estado del usuario no-admin | `scripts/obsidian-create-livesync-user.sh status` |
| Rotar password del no-admin | `scripts/obsidian-create-livesync-user.sh rotate` |
| Recrear/verificar el MCP | `scripts/obsidian-mcp-recreate.sh apply \| verify \| rollback` |
| Ver/restaurar CORS | `couchdb/init-cors.sh status \| apply \| restore` |
| Backup manual | `python3 scripts/backup_obsidian_to_s3.py` |

## Procedimiento de restore (vault perdido)

1. **No toques CouchDB todavía.** Primero verificá que el backup de S3 esté sano (paso 7 de la
   verificación).
2. Bajá el último snapshot: `aws s3 sync s3://<bucket>/obsidian-vault/notes/<TS>/ ./restore/`
3. Contá las notas y compará con el manifest (deben coincidir).
4. Restaurá el vault local desde esos `.md`.
5. Solo entonces reconectá LiveSync (con el usuario no-admin y la passphrase E2E).

## Principios aprendidos a golpes

- **Un backup que no se restaura no es un backup**: por eso el manifest guarda el `note_count` y el
  script devuelve exit code ≠ 0 ante cualquier error.
- **Un cron con nombre lindo puede estar respaldando algo muerto**: comparar siempre qué respalda
  contra qué sigue vivo.
- **El firewall que no está cargado no protege**: verificá el servicio, no el archivo de config.
- **Verificá desde afuera**: desde el propio host todo se ve abierto.
