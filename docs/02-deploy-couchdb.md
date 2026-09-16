# 02 — Despliegue de CouchDB

## docker-compose.yml

```yaml
services:
  couchdb:
    image: couchdb@sha256:9ea24cbd76522fe845d1c32c7fd1dcfc8a3ba73dcc4817d62f8a7f7f1dfaffe3  # 3.5.2
    container_name: couchdb
    restart: unless-stopped
    ports:
      - "5984:5984"
    env_file:
      - .env
    volumes:
      - ./couchdb/data:/opt/couchdb/data
      - ./couchdb/etc:/opt/couchdb/etc/local.d
```

> El volumen usa `./couchdb/data` **relativo al directorio del compose**. Si el compose vive en
> `/srv/docker/couchdb/`, los datos quedan en `/srv/docker/couchdb/couchdb/data`. Es contraintuitivo
> pero es lo que hace el despliegue real — si lo cambiás, movés los datos de lugar.

```bash
cp .env.example .env && $EDITOR .env    # COUCHDB_USER / COUCHDB_PASSWORD
mkdir -p couchdb/data couchdb/etc
docker compose up -d
```

## Bases de sistema

CouchDB necesita tres bases internas para funcionar con autenticación:

```bash
for db in _users _replicator _global_changes; do
  curl -X PUT "http://$COUCHDB_USER:$COUCHDB_PASSWORD@127.0.0.1:5984/$db"
done
```

Comprobalo: `curl -s -u user:pass http://127.0.0.1:5984/_all_dbs` debe listar `["_global_changes","_replicator","_users","obsidian"]`.

## CORS (la parte que se rompe en silencio)

El plugin LiveSync necesita CORS. Usá el script del repo, que además guarda un snapshot previo:

```bash
./init-cors.sh apply      # aplica los origins mínimos necesarios
./init-cors.sh status     # muestra la config actual
./init-cors.sh restore    # rollback al estado previo
```

### ⚠️ El pitfall del nombre de nodo

Para cambiar configuración en CouchDB se usa `/_node/<nodo>/_config/...`. El nombre del nodo **no**
es adivinable, y equivocarse no da un error rojo: da un error que pasa desapercibido y **deja CORS sin
aplicar**.

| Forma | Resultado |
|---|---|
| `/_node/nonode@nohup/_config/...` | ❌ nombre inválido (era el que usaba este README) |
| `/_node/nonode@nohost/_config/...` | ✅ funciona, pero hardcodea el nombre |
| `/_node/_local/_config/...` | ✅ **alias que siempre resuelve** — usá este |

Para ver cuál es tu nodo real:

```bash
curl -s -u "$USER:$PASS" http://127.0.0.1:5984/_membership
```

Verificación después de aplicar (esto es lo que realmente importa):

```bash
curl -s -u "$USER:$PASS" http://127.0.0.1:5984/_node/_local/_config/cors/origins
# → "app://obsidian.md,capacitor://localhost,http://localhost"
```

## Datos en disco

| Ruta | Contenido |
|---|---|
| `couchdb/data/` | La base (shards, índices) |
| `couchdb/data/.delete/` | Marcadores de borrado pendientes de purga |
| `couchdb/etc/*.ini` | Config local persistente |

## Dimensionamiento

En el vault de referencia (137 notas):

| Métrica | Valor |
|---|---|
| Documentos | ~6.950 (≈50 por nota, bloques CRDT incluidos) |
| Tamaño | ~10 MB en disco, 7,5 MB activos |

**Compactar** (el historial CRDT solo crece):

```bash
curl -X POST -H 'Content-Type: application/json' -u "$USER:$PASS" \
  http://127.0.0.1:5984/obsidian/_compact
curl -s -u "$USER:$PASS" http://127.0.0.1:5984/obsidian/_active_tasks    # seguimiento
```

Después de compactar, reiniciar también el *view* si se usan vistas:

```bash
curl -X POST -u "$USER:$PASS" -H 'Content-Type: application/json' \
  http://127.0.0.1:5984/obsidian/_view_cleanup
```

## Configurar el plugin LiveSync (en cada dispositivo)

1. Obsidian → Settings → Community Plugins → instalar **Self-hosted LiveSync**.
2. **URI**: `http://<usuario-no-admin>:<password>@<ip>:5984` (LAN) o
   `https://<usuario>:<password>@cdb.tudominio.com` (remoto, por el proxy con TLS).
3. **Database name**: `obsidian`.
4. **E2E encryption**: activar y definir la passphrase (**la misma** en todos los dispositivos y en el
   `.env` del servidor MCP).
5. Primer dispositivo: *Batch sync → Send*. Los demás: *Receive*.
6. Activar WebSocket para el sync en tiempo real.

> Usá el usuario **no-admin**. El plugin no necesita la cuenta admin, y repartir credenciales de admin
> entre celulares y laptops es exactamente el problema que este repo intenta evitar.
