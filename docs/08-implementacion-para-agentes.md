# 08 — Guía de implementación para agentes

> **Para agentes de IA.** Si estás leyendo este repo para implementarlo, replicarlo o auditarlo: acá
> está el orden de implementación, los invariantes y —sobre todo— las trampas que ya nos mordieron, para
> que no te muerdan a vos. El `README.md` es la puerta para humanos; `docs/01..07` tiene el detalle de
> cada pieza.

## Qué es esto (en 20 segundos)

Stack self-hosted de sincronización de Obsidian: **CouchDB** guarda el vault como CRDT con cifrado E2E,
el plugin **Self-hosted LiveSync** sincroniza los dispositivos, un **servidor MCP** expone las notas a un
agente de IA (RAG del lado del cliente) y un **cron** respalda todo a S3.

```
Obsidian (desktop/móvil) ──LiveSync/CRDT──► CouchDB ◄──MCP── Agente IA
                                              │
                                              └──cron 05:00──► S3 (bucket privado)
```

## Inventario verificado

```yaml
verificado: 2026-09-16
couchdb:
  version: 3.5.2
  digest: sha256:9ea24cbd76522fe845d1c32c7fd1dcfc8a3ba73dcc4817d62f8a7f7f1dfaffe3
  puerto: 5984              # solo LAN; Internet por el proxy con TLS
  base: obsidian
mcp:
  imagen: ghcr.io/es617/obsidian-sync-mcp
  version: v0.6.5
  digest: sha256:eefc083ffee77a72d5f2002dd92a8b310a3c58703ac040a39691a2778570e124
  puerto: 127.0.0.1:8787    # nunca expuesto
vault:
  archivos_vivos: 140       # 136 notas .md + 2 PDFs + 2 notas sin extensión
  documentos_totales: ~6970 # el resto son bloques CRDT (h:) y tumbas (deleted: true)
  tamano_contenido: ~1757 KB
usuarios:
  admin: definido en la config de CouchDB, NO en _users (es todo o nada, no se puede acotar)
  livesync: usuario con rol propio, solo *member* de la base del vault
cors: "app://obsidian.md,capacitor://localhost,http://localhost"
backup: cron 05:00 → s3://<bucket>/obsidian-vault/ (notas + dumps + manifests), retención 7 días
```

## Orden de implementación

**El orden importa**: cada paso asume el anterior. No adelantes el MCP antes del usuario no-admin.

### 1 · CouchDB

```bash
cd couchdb && cp .env.example .env && chmod 600 .env   # completar COUCHDB_USER/COUCHDB_PASSWORD
mkdir -p couchdb/data couchdb/etc
docker compose up -d
```

**Criterio de éxito:** `curl -s http://127.0.0.1:5984/_up` → `{"status":"ok"}`

Bases de sistema (sin esto la autenticación no funciona):

```bash
for db in _users _replicator _global_changes; do
  curl -X PUT "http://$USER:$PASS@127.0.0.1:5984/$db"
done
```

### 2 · CORS

```bash
cd couchdb && ./init-cors.sh apply && ./init-cors.sh status
```

**Criterio de éxito:** `origins = app://obsidian.md,capacitor://localhost,http://localhost` (sin `*`).

> ⚠️ **Trampa #1:** para cambiar configuración usá el alias `/_node/_local/_config`. El nombre real del
> nodo (`nonode@nohost`) **no es adivinable, y equivocarse falla en silencio**: los `curl` devuelven
> error, CORS queda sin aplicar, y la sincronización sigue andando hasta que un día deja de funcionar.
> Nunca hardcodees el nombre del nodo.

### 3 · Usuario no-admin (NO lo saltees)

El plugin y el MCP **no deben** conectarse con la cuenta admin:

```bash
scripts/obsidian-create-livesync-user.sh apply    # crea, ajusta _security y PRUEBA permisos
scripts/obsidian-create-livesync-user.sh status
```

**Criterio de éxito:** imprime `login 200 · read 200 · write 201 · delete 200`.

Qué hace: crea un usuario con rol propio y lo agrega como **member** de la base del vault (no admin):

```json
{ "members": { "roles": ["_admin", "livesync"] },
  "admins":  { "roles": ["_admin"] } }
```

Por qué: la cuenta `admin` de CouchDB es **todo o nada** — quien la tenga puede crear o borrar bases y
gestionar usuarios. Si esa credencial vive en un celular, lo que perdés es el servidor, no el vault. Con
un usuario *member*, un dispositivo robado expone **solo las notas**.

La documentación oficial del plugin lo respalda: *"If you have separately configured a CouchDB account
with the required access to this database, use that account instead"*. Lo único que se pierde son los
chequeos **opcionales** de servidor.

### 4 · Servidor MCP

```bash
cd mcp-server && cp .env.example .env && chmod 600 .env
# completar: COUCHDB_USER=livesync, su password, COUCHDB_PASSPHRASE (la E2E), MCP_AUTH_TOKEN
docker compose pull && docker compose up -d --wait
```

**Criterio de éxito:** `curl -s http://127.0.0.1:8787/health` → `✓ Ok`, y la versión del contenedor
coincide con el digest pineado.

**Tres reglas que no se negocian:**
1. **Pinear por digest**, nunca `:latest` (un pull puede bajarte releases sin avisar, y sin los parches de seguridad).
2. **Bind a `127.0.0.1`**: el cliente corre en el mismo host.
3. **`COUCHDB_PASSPHRASE` es obligatoria** si el vault está cifrado E2E — sin ella no descifra nada.

### 5 · Cliente MCP + RAG

```bash
pip install mcp httpx requests
python3 scripts/obsidian-mcp-client.py rag-improved "<consulta>" 5
```

El token se resuelve así: `MCP_AUTH_TOKEN` (entorno) → `<hermes>/.secrets/obsidian-mcp-token` (0600) →
`~/.hermes/.secrets/obsidian-mcp-token` → placeholder. **Nunca hardcodeado.**

### 6 · Dispositivos (plugin Self-hosted LiveSync)

En cada uno: instalar el plugin, apuntarlo a `http(s)://<host>:5984`, base `obsidian`, **el usuario
no-admin** y la misma passphrase E2E.

> ⚠️ **Trampa #2 — el "setup URI".** El comando *"Copy settings as a new Setup URI"* serializa **el
> objeto de settings COMPLETO** (`couchDB_URI`, usuario, password, base y la passphrase E2E) y lo cifra
> con una passphrase que elegís en ese momento. Si configuraste los dispositivos pasándote ese URI por
> chat o mail, **la credencial de admin viajó por ese canal**: migrá los dispositivos al usuario
> no-admin y después rotá la password de admin.

> ⚠️ **Trampa #3 — cambiar el usuario no rompe nada.** Es la misma base y la misma passphrase E2E, así
> que el plugin detecta el cambio de configuración y **te pregunta**: elegí **Fetch** (los chunks son
> inmutables → baja solo metadatos y diferencias), **nunca Rebuild** en un dispositivo que ya tiene
> datos. Si queda en "Not ready", los flags de rescate en la raíz del vault son `flag_fetch.md`,
> `flag_rebuild.md` y `redflag.md`.

### 7 · Backup

```bash
python3 scripts/backup_obsidian_to_s3.py     # se autoverifica; exit ≠ 0 si algo falta
scripts/verify-vault.sh                      # verificación integral del stack
```

**Criterio de éxito:** `N/N archivos · ✔ verificado: el contenido coincide con el vault`, exit 0.

Programá el cron (05:00 diario) apuntando a ese script — **al mismo archivo que verificaste**.

### 8 · Verificación final

```bash
scripts/verify-vault.sh
```

Salida esperada (todo ✔, exit 0):

```
✔ responde /_up                        ✔ base 'obsidian' · N documentos
✔ rol 'livesync' es member de la base  ✔ orígenes restringidos y con app://obsidian.md
✔ health OK                            ✔ imagen pineada por digest
✔ MCP usa usuario no-admin (livesync)  ✔ lectura + descifrado E2E OK
✔ backup completo: N archivos = los N del vault
✔ tamaño consistente (X KB vs X KB del vault)
TODO OK
```

## Invariantes (no negociables)

1. **Ningún secreto en el repo**, ni en el árbol ni en la historia. Todo en `.env` (0600) o en
   `~/.hermes/.secrets/`. Verificá con `scripts/sensitive-scan.sh --check --all` **y** con
   `git grep <patrón> $(git rev-list --all)` (lo publicado no se despublica).
2. **Imágenes pineadas por digest.**
3. **Plugin y MCP con el usuario no-admin.** La cuenta admin, solo para administración.
4. **CORS sin wildcard** (tres orígenes exactos).
5. **El MCP escucha solo en localhost.**
6. **Ningún backup sin autoverificación.**
7. **Fallo ruidoso:** si algo no cuadra, exit ≠ 0 y mensaje claro. Nunca "✅" con datos incompletos.

## Trampas conocidas (todas ya nos mordieron)

| Síntoma | Causa | Arreglo |
|---|---|---|
| Los `curl` de configuración no aplican nada | Nombre de nodo inventado (`nonode@nohup`) | Usar el alias `_node/_local/_config` |
| `Name or service not known` desde el host | `host.docker.internal` **solo resuelve dentro de Docker** | Traducirlo a `127.0.0.1` (u override por variable) |
| Un backup dice `N/N ✅` pero pesa la mitad | Leía con el **CLI**, que corta la salida a **20.000 caracteres** | Leer con `read_note()` por importlib |
| Faltan archivos en el backup | El `list` del MCP **solo devuelve `.md`** | Enumerar desde CouchDB: docs con `path` y sin `deleted` |
| Un PDF se guarda 33% más grande y no abre | Los binarios grandes llegan como **varios bloques base64 de 100 KiB concatenados** | Cortar por padding y decodificar bloque por bloque; verificar la firma (`%PDF`) |
| Un chequeo de tamaño deja pasar datos inflados | Solo comparaba "¿quedó corto?" | Comparar en **ambos** sentidos: `abs(real-esperado)/esperado <= 0.10` |
| Un script "falla" aunque el comando salió bien | `set -o pipefail` + `cmd \| head` → SIGPIPE | Capturar a una variable en vez de cortar el pipe |
| El raw de GitHub muestra la versión vieja tras un push correcto | Caché del CDN de `raw.githubusercontent.com` | Verificar por **SHA del commit** o por la API de contenidos |
| "¿El firewall está activo?" | Existe el archivo con `policy drop` pero el servicio no corre | `systemctl is-enabled nftables` (el archivo no prueba nada) |
| El plugin quedó en "Not ready" | DB local corrupta tras cambiar config | Flags `flag_fetch.md` / `flag_rebuild.md` en la raíz del vault |
| "El MCP no encuentra la nota" (y existe) | El ID está en minúsculas, el `path` conserva mayúsculas | Usar los paths que devuelve `list` |
| Una nota no se puede leer y tiene `size: 0` | Es una nota **legítimamente vacía** | Respaldarla vacía es correcto, no es un error |

## Cómo trabaja un agente en este repo

- **Medí antes de afirmar.** Este stack miente en los lugares obvios: el `list` del MCP omite archivos,
  el backup puede reportar éxito con datos truncados, la doc puede estar desactualizada. La verdad está
  en CouchDB y en el bucket.
- **Verificá desde afuera.** Desde el propio host todo se ve abierto y todo funciona.
- **En máquinas vivas, paso a paso.** Un cambio por vez con su verificación, y el OK del humano antes de
  seguir. Antes de tocar una configuración: backup del archivo.
- **Publicá con el escáner.** `scripts/sensitive-scan.sh` antes de cada push.
- **Si rompés algo, decilo.** Un backup incompleto reportado como completo es peor que uno que falla.

## Mapa de archivos

| Archivo | Qué hace | Cuándo usarlo |
|---|---|---|
| `couchdb/docker-compose.yml` | CouchDB pineado + `env_file` | Despliegue |
| `couchdb/init-cors.sh` | CORS de LiveSync (con snapshot y rollback) | Setup y troubleshooting |
| `mcp-server/docker-compose.yml` | Servidor MCP pineado | Despliegue |
| `scripts/obsidian-mcp-client.py` | Cliente MCP: CRUD + RAG | Toda interacción con el vault |
| `scripts/obsidian-create-livesync-user.sh` | Crea/rota el usuario no-admin y prueba permisos | Setup y rotación |
| `scripts/obsidian-couchdb-cors.sh` | Igual que `init-cors.sh`, para un host en vivo | Mantenimiento |
| `scripts/obsidian-mcp-recreate.sh` | Recrea el MCP y verifica (con rollback) | Actualizar la imagen |
| `scripts/backup_obsidian_to_s3.py` | Backup del vault, autoverificado | Cron diario / manual |
| `scripts/verify-vault.sh` | Verificación integral (CouchDB→CORS→MCP→RAG→backup) | Después de CUALQUIER cambio |
| `scripts/sensitive-scan.sh` | Detecta secretos e identificadores antes del commit | Antes de publicar |

## Interfaz del cliente MCP (para operar el vault)

```bash
P=scripts/obsidian-mcp-client.py
python3 $P list [carpeta] [N]        # listar (OJO: solo .md)
python3 $P read "<path>"             # leer — el CLI corta a 20.000 chars
python3 $P write "<path>" "<texto>"  # crear/sobrescribir
python3 $P edit "<path>" append|prepend|replace "<texto>"
python3 $P move "<de>" "<a>"         # mover/renombrar
python3 $P delete "<path>"
python3 $P meta "<path>"             # frontmatter, tags, backlinks
python3 $P search "<texto>" [N]      # SOLO por nombre
python3 $P rag-improved "<consulta>" [N]   # RAG sobre TODO el contenido
python3 $P cache-clear               # tras escribir, para que el RAG lo vea
```

- **Para contenido completo** (notas grandes o binarios) importá el módulo y usá `read_note()`: no hay
  truncamiento. **El CLI es para humanos.**
- **`search` mira solo el nombre; `rag` mira el contenido.** Para preguntas, siempre `rag`.
- Una nota nueva puede no aparecer en el RAG hasta correr `cache-clear`.
