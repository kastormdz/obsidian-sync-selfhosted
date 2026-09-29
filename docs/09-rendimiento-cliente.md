# 09 — Rendimiento del cliente: daemon, índice local y las trampas

**Medido el 2026-09-29** sobre el despliegue real (155 notas, ~10.000 documentos
en CouchDB contando bloques CRDT). Todos los números son de ejecuciones reales,
no estimaciones.

## TL;DR

El cuello de botella **nunca fue el vault**. Escribir una nota contra el servidor
toma **0.03s**; lo que costaba casi un segundo era *arrancar el proceso de Python*
cada vez. Y el RAG tardaba minutos por un bug de concurrencia en SSE, no por
trabajo real: el servidor pasaba el 97% del tiempo en **0% de CPU**.

| operación | antes | después |
|---|---|---|
| escribir nota | 0.85s | **0.069s** |
| escribir 50 KB | — | **0.066s** |
| editar (append) | 0.85s | **0.064s** |
| leer | 0.85s | **0.041s** |
| metadata | 0.87s | **0.049s** |
| buscar por nombre | 0.84s | **0.036s** |
| borrar | — | **0.051s** |
| **RAG semántico** | **>420s** (abortado) | **0.2s** |
| reindex incremental | — | 5.2s |
| reindex completo | — | ~4 min |

## Dónde se iba el tiempo

| fase | costo | avoidable |
|---|---|---|
| import del SDK `mcp` (pydantic) | ~0.7s | con un daemon: no |
| carga del modelo de embeddings (ONNX) | ~2.0s | con un daemon: no |
| **operación real contra el server** | **0.03s** | — |

Del import, 383ms están en `mcp_types._types` (pydantic generando los modelos).
Es costo fijo del SDK: no se puede optimizar por proceso. **La única forma de
eliminarlo es no crear un proceso por operación.**

## Los cuatro bugs (en orden de impacto)

### 1. Concurrencia sobre una sola sesión SSE

`obsidian-mcp-client.py` leía N notas "en paralelo" con un semáforo de 10,
pero **todas las corrutinas usaban la misma sesión SSE**.

**MCP sobre SSE es un request por stream: no multiplexa.** Varias llamadas
concurrentes sobre la misma sesión cruzan sus respuestas y la conexión se rompe
(`anyio.BrokenResourceError`).

Medido:

| lote (conc=10) | tiempo |
|---|---|
| 60 notas | 0.35s ✅ |
| 100 notas | 0.62s ✅ |
| **130 notas** | **colgado** (>65s de reloj, **1s de CPU**) |

La firma del cuelgue es ese ratio: **1 segundo de CPU en 65 de reloj**.
Eso es espera de I/O, no cómputo. Y el servidor marcaba 0% CPU: no estaba
trabajando, estaba **re-handshakeando OAuth en loop**, porque cada rotura de
conexión dispara un flujo PKCE completo (~0.85s).

**Fix:** `concurrency: int = 1` por defecto. Las 155 notas salen en 1.7s.
Si alguna vez hace falta paralelismo real, tiene que ser con **N sesiones SSE
independientes**, nunca N llamadas sobre la misma.

### 2. El token OAuth no se persistía

`_auth()` guardaba `client_id` y `client_secret` en disco, pero el
**access_token vivía solo en memoria**. Cada proceso —y cada reconexión SSE—
pagaba el flujo PKCE completo.

Los ~0.85s "de operación" que se medían eran casi todos el handshake, no el
trabajo. Se delata en los logs del servidor, que repite:
```
Auth: /oauth/authorize accepted client_id=...
Auth: password accepted, issuing authorization code.
Auth: /oauth/token issuing access token ...
```

**Fix:** persistir `cid`/`csec`/`token` con `chmod 0600` y rename atómico.
`_auth()` pasó de ~0.85s a **0.02s**.

### 3. El RAG leía el vault entero en cada consulta

El pipeline leía el contenido de las 155 notas para buscar candidatos, y
después volvía a leer los candidatos. Doble pasada, sobre el server, por red.

**Fix:** índice local (`obsidian-index.py`) con los chunks y sus embeddings en
SQLite. El RAG consulta local y **no toca la red**.

### 4. Cambios detectados por mtime en vez de por contenido

El reindexado comparaba `mtime` + `size`. Fallaba por partida doble: la cache
de metadata del servidor tiene TTL y puede devolver un `mtime` viejo, y una
edición que mantiene el largo pasa desapercibida.

**Fix:** comparar el **sha1 del contenido**.

## El índice local

```
~/.hermes/cache/obsidian-index.db     chunks + embeddings (SQLite)
```

- Chunking por estructura markdown: headings, code fences, tablas, listas.
- Sub-chunking de fences grandes (un bloque de 16 KB es inbuscable).
- Embeddings con `paraphrase-multilingual-MiniLM-L12-v2` vía **fastembed/ONNX**
  (sin torch), cacheados por hash del texto.
- Reindex **incremental** (5.2s) o completo con `--force` (~4 min).

**El índice tiene que PURGAR lo que ya no está en el vault.** Sin eso solo crece,
y una nota borrada queda disponible para búsqueda: el peor modo de falla, porque
el consumidor después va a leerla y recibe "not found".

### Trampa: migrar el esquema

`CREATE TABLE IF NOT EXISTS` **no altera una tabla existente**. Si agregás una
columna al esquema y no la migrás, el primer `SELECT` revienta con
`no such column: ...`. La migración tiene que ser explícita
(`_ensure_columns()` en el código).

## La trampa del ranking: devolver basura con score

Un modelo de embeddings **siempre devuelve algo**. Consultar por un token que no
existe en el vault devolvía las notas más parecidas semánticamente, con un score
que el consumidor leía como un acierto:

```
rag "zqxjkv-9931"   (no existe)  →  zram-zswap-tutorial.md  0.365
```

Peor que devolver cero, porque un resultado con score se interpreta como
encontrado. El índice tiene un **piso de relevancia**: sin un término con
contenido de la query presente en el chunk, devuelve lista vacía.

Cuatro cosas arruinaban la calidad, todas corregidas y medidas:

1. **Términos genéricos.** `docker` aparece en 60 notas: su similitud se aplana
   contra todo el vault y ahoga lo específico. → ponderar por IDF.
2. **Chunks sin contexto.** Un chunk como "Primer deploy" embebido solo no
   significa nada. → embebir `path :: heading :: texto`.
3. **Secciones cortas descartadas.** Un umbral `min_chars` tiraba los `## Backup`
   de 100 caracteres, donde justement estaba la palabra clave. → el umbral es un
   piso de calidad, **no un filtro**.
4. **Scores fuera de rango.** Sumar los pesos IDF en vez de promediarlos daba
   scores de 1.3 en una métrica que va de 0 a 1.

> Los scores >1 son el boost por nombre (×1.35) y por heading. El ranking es
> relativo; para un umbral absoluto usar el coseno puro, que sí está en 0..1.

## El daemon

`obsd.py` mantiene la sesión SSE abierta y el modelo de embeddings en memoria,
y expone un **socket Unix**:

- **Nunca TCP.** El socket va en `~/.hermes/cache/obsd.sock` con modo `0600`.
- **Lista blanca de tools.** El daemon no es una puerta a "ejecutá lo que sea":
  solo expone CRUD y búsqueda del vault.
- **Autostart.** El primer uso lo levanta si no está corriendo; espera hasta 5s
  a que aparezca el socket.
- **Fallback.** Si no puede arrancar, el cliente llama al MCP directamente:
  lento, pero funcional.
- **Recicla la sesión** cada N llamadas o ante error, como el pool del cliente.

`obsc.py` es el cliente: `obsc.call(tool, args)` y `obsc.rag(query, k)`.

**Cuando el daemon no está corriendo, la primera operación paga el arranque**
(~2s con el modelo). Después, milisegundos.

## Cómo reproducir las mediciones

```bash
# estado del índice
python3 scripts/obsidian-index.py status

# buscar (el número entre corchetes es el tiempo)
python3 scripts/obsidian-index.py search "couchdb replication" 5

# reindexar
python3 scripts/obsidian-index.py build            # incremental
python3 scripts/obsidian-index.py build --force    # completo

# daemon
python3 scripts/obsd.py status
```

Para comparar contra el camino sin daemon:

```bash
time python3 scripts/obsidian-mcp-client.py read "00-INDEX.md"
```

## Advertencias

- **El índice no tiene filtro de lectura.** Replica lo que el servidor MCP ya
  exponía, pero ahora el vault completo está en un archivo local. Si el vault
  tiene una carpeta de credenciales, ese contenido queda indexado: tratá el
  `.db` como el dato sensible que es.
- **El cliente debe vivir en un solo lugar.** Si está duplicado (copia de
  trabajo + repo), las dos copias se desincronizan y termina corriendo la
  versión vieja. Diff después de cada cambio.
- **Nunca escribir al filesystem del vault.** Solo llega a CouchDB si Obsidian
  está corriendo con LiveSync; en un server no está, y el archivo queda como
  fantasma invisible para todos los demás dispositivos.
