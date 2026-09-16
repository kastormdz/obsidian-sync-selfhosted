# 03 — Seguridad

Esta es la parte que en el despliegue original estaba **documentada al revés**. Acá está lo que se
verificó y se corrigió.

## 1. Usuario no-admin para LiveSync y MCP

**Antes:** `_users` solo tenía la cuenta `admin`, y `obsidian/_security` exigía el rol `_admin`. O sea:
cada celular, cada laptop y el servidor MCP se conectaban **como admin de la base**. Un dispositivo
perdido = acceso total.

**Ahora:** existe un usuario `livesync` con rol propio, agregado como *member* (no admin):

```json
{
  "members": { "roles": ["_admin", "livesync"] },
  "admins":  { "roles": ["_admin"] }
}
```

Creación/rotación:

```bash
./scripts/obsidian-create-livesync-user.sh apply    # crea y prueba permisos
./scripts/obsidian-create-livesync-user.sh status   # ¿existe? ¿roles?
./scripts/obsidian-create-livesync-user.sh rotate   # rota la password
```

El script **prueba permisos de verdad** antes de dar OK: login, lectura de una nota, escritura y
borrado de un documento temporal. `_design/*` sigue siendo solo-admin, que es lo correcto.

La password se genera con `secrets` alfanumérica a propósito (evita romper URLs/DSN) y se guarda en
`/srv/docker/obsidian-mcp/.env` con modo `0600`. **Nunca** en el repo.

## 2. CORS

LiveSync necesita CORS habilitado; el despliegue original tenía **wildcard con credenciales**:

```diff
- app://obsidian.md,capacitor://localhost,http://localhost,http://*,https://*
+ app://obsidian.md,capacitor://localhost,http://localhost
```

Orígenes que el plugin realmente usa:

| Cliente | Origin |
|---|---|
| Obsidian desktop (Electron) | `app://obsidian.md` |
| Obsidian iOS (Capacitor) | `capacitor://localhost` |
| Obsidian Android (Capacitor) | `http://localhost` |

El **cliente MCP no usa CORS** (habla HTTP server-side desde el mismo host), así que restringir los
orígenes no afecta al RAG.

Aplicar y verificar:

```bash
./couchdb/init-cors.sh apply      # guarda snapshot previo y restringe
./couchdb/init-cors.sh status
./couchdb/init-cors.sh restore    # rollback
```

Verificación real del preflight (lo que hace el navegador/Electron antes de cada request):

```bash
# Origen legítimo → 204 + Access-Control-Allow-Origin
curl -si -X OPTIONS http://127.0.0.1:5984/obsidian \
  -H 'Origin: app://obsidian.md' -H 'Access-Control-Request-Method: PUT' | head -5

# Origen hostil → NO debe devolver el header ACAO
curl -si -X OPTIONS http://127.0.0.1:5984/obsidian \
  -H 'Origin: https://evil.example' | head -3
```

> **Pitfall histórico:** el README original usaba `nonode@nohup` en las llamadas de configuración.
> El nombre de nodo real es `nonode@nohost`, y con el nombre mal **los `curl` fallaban y el CORS
> quedaba sin aplicar en silencio**. Usá siempre el alias `_node/_local/_config`, que nunca miente.

## 3. Exposición de red

Medido desde **fuera** de la red (no desde el propio host, que siempre se ve abierto):

| Puerto | Resultado |
|---|---|
| `5984` (CouchDB) | ❌ cerrado desde Internet · ✅ abierto en la LAN |
| `443` (NPM) | ✅ abierto (es el acceso de los dispositivos remotos) |
| `8787` (MCP) | ❌ no publicado (bind `127.0.0.1`) |

**Ojo con el firewall del host:** puede existir un `/etc/nftables.conf` con `policy drop` impecable y
aun así no estar aplicado. Verificá el servicio, no el archivo:

```bash
systemctl is-enabled nftables   # si dice 'disabled', el ruleset NO está cargado
```

En el despliegue auditado, `nftables.service` estaba `disabled` y solo quedaban las cadenas de Docker
y fail2ban. La exposición a Internet estaba cerrada únicamente por NAT del router.

## 4. E2E (cifrado extremo a extremo)

Activo. Implicancias prácticas:

- CouchDB **no puede leer el contenido** de las notas; solo el cliente que tiene la passphrase.
- La passphrase debe ser **idéntica** en todos los dispositivos y en el `.env` del servidor MCP
  (`COUCHDB_PASSPHRASE`). Si el MCP no la tiene, devuelve notas vacías o errores de descifrado.
- **El backup a S3 guarda texto plano**: el MCP descifra para exportar. Ese bucket es tan sensible
  como el vault en claro — protegé sus credenciales en consecuencia.
- Si perdés la passphrase, los datos en CouchDB son irrecuperables. Guardala en un gestor de
  contraseñas (Vaultwarden), no solo en el plugin.

## 5. Higiene de credenciales

| Antes | Ahora |
|---|---|
| Password de admin en `docker-compose.yml` | `env_file: .env` (0600), compose sin secretos |
| `MCP_AUTH_TOKEN` hardcodeado en el script cliente | `MCP_AUTH_TOKEN` (entorno) → `~/.hermes/.secrets/obsidian-mcp-token` (0600) → placeholder |
| Claves S3 hardcodeadas en 2 scripts de backup | `~/.hermes/.secrets/hermes-backups.env` (0600) |

El repo **no contiene ningún secreto**, ni en el árbol de trabajo ni en su historia de git.

## 6. Mantenimiento pendiente

- **Compactación.** La base nunca se compactó (`compact_running: false`, `purge_seq: 0`). El historial
  CRDT solo crece. Programar una compactación periódica:

  ```bash
  curl -X POST -H 'Content-Type: application/json' \
    -u "admin:$PW" http://127.0.0.1:5984/obsidian/_compact
  curl -s -u "admin:$PW" http://127.0.0.1:5984/obsidian/_active_tasks   # seguir el progreso
  ```

- **Rotación** de la password de admin, la del usuario `livesync` y el `MCP_AUTH_TOKEN`.
- **Actualización de imágenes**: comparar el digest corriendo contra el último release upstream y
  pinear el nuevo digest explícitamente (no usar `:latest`).
