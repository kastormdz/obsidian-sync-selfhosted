# Obsidian Sync — Self-Hosted

Sincronización en tiempo real de un vault de Obsidian, auto-gestionada: **CouchDB + Self-hosted LiveSync**, más un **servidor MCP** para que un agente de IA (Hermes) lea y escriba notas con RAG, y **respaldos automáticos** a S3.

> **Estado verificado:** 2026-09-16 sobre el despliegue real (CouchDB 3.5.2, 140 archivos vivos).
> Este README describe **lo que está corriendo**, no lo que nos gustaría que corra. Lo que no está
> verificado se dice explícitamente.

## Arquitectura

```
┌────────────────────┐        ┌──────────────┐        ┌─────────────────────┐
│ Obsidian desktop   │        │ Obsidian     │        │ Hermes (agente IA)  │
│ escritorio · laptop       │        │ Android      │        │ + cliente MCP/RAG   │
└─────────┬──────────┘        └──────┬───────┘        └──────────┬──────────┘
          │ LiveSync (CRDT, E2E)     │ LiveSync                  │ MCP (HTTP local)
          │ LAN ─────────────┐       │ vía HTTPS                 │
          ▼                  ▼       ▼                           ▼
    ┌─────────────────────────────────────────┐        ┌───────────────────┐
    │  NPM Plus  ·  cdb.tudominio.com:443     │        │ obsidian-sync-mcp │
    │  (TLS + WebSockets)                     │        │ 127.0.0.1:8787    │
    └───────────────────┬─────────────────────┘        └─────────┬─────────┘
                        │                                        │
                        ▼                                        │
                ┌────────────────────────────────────────────────▼─────┐
                │  CouchDB 3.5.2  :5984   db "obsidian"                │
                │  usuario `livesync` (no-admin) · E2E activo           │
                └───────────────────────┬──────────────────────────────┘
                                        │ backup diario 05:00
                                        ▼
                            ┌───────────────────────┐
                            │  Garage S3            │
                            │  bucket hermes-configs│
                            └───────────────────────┘
```

**Flujo de datos**

1. Cada dispositivo corre el plugin **Self-hosted LiveSync** y escribe contra CouchDB por CRDT.
2. El vault viaja **cifrado E2E**: CouchDB solo almacena bloques cifrados.
3. El **servidor MCP** se conecta con un usuario **no-admin**, descifra en memoria y expone las notas como tools MCP.
4. El **agente IA** consume esas tools; el pipeline RAG (TF-IDF + embeddings + boost por nombre) corre del lado del cliente.
5. Un **cron diario** exporta el vault completo a S3 (notas + manifest).

## Componentes

| Componente | Imagen / pieza | Función |
|---|---|---|
| Base de datos | `couchdb@sha256:9ea24cbd…` (3.5.2) | Replicación multimaster + CRDT |
| Servidor MCP | `ghcr.io/es617/obsidian-sync-mcp@sha256:eefc083f…` (v0.6.5) | Tools MCP sobre el vault |
| Cliente MCP + RAG | `scripts/obsidian-mcp-client.py` | CRUD + RAG híbrido local |
| Proxy reverso | NPM Plus | TLS para dispositivos remotos |
| Almacenamiento | Garage S3 | Respaldos fuera del server |
| Backups | `scripts/backup_obsidian_to_s3.py` | Export diario del vault |
| Verificación | `scripts/verify-vault.sh` | Healthcheck end-to-end |

## Documentación

| Doc | Contenido |
|---|---|
| [docs/01-arquitectura.md](docs/01-arquitectura.md) | Topología, flujos, **por qué CouchDB** y qué se descartó |
| [docs/02-deploy-couchdb.md](docs/02-deploy-couchdb.md) | Despliegue de CouchDB + CORS correcto |
| [docs/03-seguridad.md](docs/03-seguridad.md) | Usuario no-admin, CORS, firewall, E2E, rotación |
| [docs/04-mcp-server.md](docs/04-mcp-server.md) | Servidor MCP, pin por digest, auth |
| [docs/05-cliente-rag.md](docs/05-cliente-rag.md) | Cliente MCP, pipeline RAG, límites y bugs conocidos |
| [docs/06-backups.md](docs/06-backups.md) | Qué se respalda, dónde, retención y **restore** |
| [docs/07-runbook.md](docs/07-runbook.md) | Verificación rápida y troubleshooting |
| [docs/08-implementacion-para-agentes.md](docs/08-implementacion-para-agentes.md) | **Para agentes de IA:** orden de implementación, invariantes y trampas conocidas |

## Inicio rápido

```bash
git clone git@github.com:kastormdz/obsidian-sync-selfhosted.git
cd obsidian-sync-selfhosted

# 1. CouchDB
cd couchdb && cp .env.example .env && $EDITOR .env
mkdir -p data etc && docker compose up -d
./init-cors.sh apply                                    # CORS para LiveSync

# 2. Usuario no-admin (el plugin NO debe usar la cuenta admin)
../scripts/obsidian-create-livesync-user.sh apply

# 3. Servidor MCP
cd ../mcp-server && cp .env.example .env && $EDITOR .env
docker compose up -d

# 4. Verificar todo de punta a punta
../scripts/verify-vault.sh
```

Después, en cada dispositivo: instalar **Self-hosted LiveSync**, apuntarlo a la URL de CouchDB, usar
el usuario **no-admin** y la misma passphrase E2E.

## Licencia

MIT — ver [LICENSE](LICENSE).
