# 01 — Arquitectura

## Topología verificada (2026-09-16)

| Capa | Qué corre | Dónde | Puerto |
|---|---|---|---|
| Base de datos | CouchDB 3.5.2 (`db: obsidian`) | servidor, Docker | `5984` (0.0.0.0, LAN) |
| Servidor MCP | obsidian-sync-mcp v0.6.5 | servidor, Docker | `127.0.0.1:8787` |
| Proxy reverso | NPM Plus | servidor, Docker | `443` (público) |
| Almacenamiento | Garage S3 v2.3.0 | servidor, Docker | `3900-3903` |
| Cliente MCP + RAG | `obsidian-mcp-client.py` | Hermes (mismo host) | — |
| Clientes LiveSync | plugin Self-hosted LiveSync | escritorio + laptop + Android | — |

Números reales de la base (los que importan para dimensionar):

| Métrica | Valor |
|---|---|
| Archivos vivos | 140 (136 notas `.md` · 2 PDFs · 2 notas sin extensión) |
| Documentos totales | ~6.950 (≈50 docs CRDT por nota) |
| Tamaño en disco | ~10 MB (7,5 MB activos) |
| Revisión máxima observada | 77 (`00-INDEX.md`) |

> La diferencia entre **notas** y **documentos** no es un error: LiveSync guarda el contenido como
> bloques CRDT (`h: <id>`) más los documentos de las notas. Es normal que haya ~40-60 documentos por
> nota. Lo que sí hay que vigilar es que la base **no se compacte nunca**, porque el historial crece
> sin techo (ver [03-seguridad.md](03-seguridad.md#compactación)).

## Flujo de escritura (tiempo real)

```
Obsidian (plugin LiveSync)
   └─ escribe bloques Yjs cifrados ──► CouchDB (db "obsidian")
                                          │
                                          ├─ replicación multimaster: todos los dispositivos
                                          │  tienen la misma copia sin conflictos de archivo
                                          └─ historial de revisiones por documento
```

El **CRDT** es la razón de ser de esta arquitectura: dos dispositivos editando la misma nota **no
generan conflictos de archivo** ni duplicados `conflict-*.md`; los cambios se fusionan carácter a carácter.

## Flujo de lectura (agente IA)

```
Hermes
  └─ obsidian-mcp-client.py ──HTTP+OAuth──► obsidian-sync-mcp :8787
                                                │  descifra E2E en memoria
                                                └─ REST ──► CouchDB :5984
                                                              (usuario `livesync`, no-admin)
```

El **RAG corre del lado del cliente**, no en el servidor: el servidor solo entrega notas, y el
cliente arma chunks, los puntúa con TF-IDF + embeddings y devuelve los mejores. Ver
[05-cliente-rag.md](05-cliente-rag.md).

## Por qué CouchDB (y qué se descartó)

El requisito real era **merge sin duplicados con sync en tiempo real** entre desktop Linux y Android.

| Alternativa | Por qué NO |
|---|---|
| **Syncthing** sobre la carpeta del vault | Sync a nivel archivo: duplica (`sync-conflict-*`) y ensucia `.obsidian/workspace.json` |
| **Remotely Save** (S3/WebDAV) | Sin merge: gana el `mtime` más nuevo, sin copia de conflicto. Sin realtime (solo sincroniza con Obsidian abierto) |
| **Sync Engine** (S3/WebDAV) | Alternativa moderna y muy válida, pero el merge es diff3 por línea, no CRDT carácter a carácter |
| **Carpeta en la nube** (Drive/Dropbox) | Solo desktop; un dispositivo a la vez; sin merge |
| **Obsidian Sync oficial** | US$4/mes; no self-hosted |

CouchDB + LiveSync es más infraestructura que las alternativas, pero es **la única** de la lista que
da merge real carácter a carácter y sync continuo. Si algún día el realtime deja de ser requisito,
Sync Engine sobre WebDAV/S3 es la ruta de simplificación (una sola pieza: un bucket o un WebDAV).

## Decisiones de diseño

1. **CouchDB nunca expuesto directo a Internet.** El acceso remoto (celular) va por NPM con TLS.
   El `5984` solo se publica en la LAN.
2. **El plugin y el MCP usan un usuario NO-admin** (`livesync`), con rol *member* de la base. La
   cuenta admin queda solo para tareas administrativas.
3. **E2E activo.** La passphrase vive en el plugin de cada dispositivo y en el `.env` del MCP. Sin
   ella, los datos almacenados son ilegibles.
4. **Imágenes pineadas por digest.** Un `docker compose pull` no puede cambiar la versión de
   sorpresa.
5. **El servidor MCP escucha solo en localhost.** No hay razón para exponerlo.
6. **Un solo backup diario** del vault (05:00), más el de configs (06:00). Se eliminó un tercer cron
   que respaldaba un bucket S3 obsoleto de una migración anterior.
