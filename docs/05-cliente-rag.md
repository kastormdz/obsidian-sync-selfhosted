# 05 — Cliente MCP + RAG

`scripts/obsidian-mcp-client.py` es un cliente MCP completo (OAuth 2.1 + PKCE) con **pipeline RAG
híbrido corriendo del lado del cliente**. El servidor MCP solo entrega notas; todo el ranking se
calcula local.

## Instalación

```bash
pip install mcp httpx requests
```

El token se resuelve en este orden: `MCP_AUTH_TOKEN` (entorno) → `<hermes>/.secrets/obsidian-mcp-token`
(0600) → `~/.hermes/.secrets/obsidian-mcp-token` → placeholder `<your-mcp-auth-token>`.
**Nunca hardcodeado en el archivo** (así no se filtra al copiar el script a un repo).

## Comandos

```bash
P=scripts/obsidian-mcp-client.py

python3 $P list [carpeta] [N]          # listar notas
python3 $P read "<path>"               # leer una nota
python3 $P write "<path>" "<texto>"    # crear/sobrescribir
python3 $P edit "<path>" append|prepend|replace "<texto>"
python3 $P move "<de>" "<a>"           # mover/renombrar
python3 $P delete "<path>"
python3 $P meta "<path>"               # frontmatter, tags, backlinks
python3 $P folders | tags              # estructura
python3 $P search "<texto>" [N]        # SOLO por NOMBRE de la nota
python3 $P rag "<consulta>" [N]        # RAG (wrapper compatible)
python3 $P rag-improved "<consulta>" [N]   # RAG completo, todos los stages
python3 $P cache-clear                 # limpiar caché LRU
```

### `search` vs `rag` — la diferencia que importa

| Comando | Busca en | Uso |
|---|---|---|
| `search` | **solo el nombre** de la nota (como el `Ctrl+F` del explorador) | Encontrar una nota por título |
| `rag` / `rag-improved` | **todo el contenido** del vault, con ranking | Preguntas ("¿cuál era la config del firewall?") |

Para preguntas de un agente, **siempre `rag` o `rag-improved`**. `search` no encuentra contenido.

## Pipeline de `rag-improved`

1. **Listado completo** del vault (el servidor capa a 100 por defecto; el cliente pide un límite alto —
   sin eso, las notas fuera de las primeras 100 son invisibles).
2. **Chunking semántico v2**: cortes por headings H1-H6, bloques de código propios (preservando el
   lenguaje), tablas y listas; cada chunk conserva `heading` y `heading_level`.
3. **Expansión de query**: tokens originales + sinónimos + stems.
4. **TF-IDF** sobre los chunks + backend de embeddings opcional.
5. **Ranking híbrido** y boost cuando el nombre de la nota coincide con la consulta.
6. **Caché LRU** de resultados (32 entradas) para consultas repetidas.

Devuelve `{path, name, heading, heading_level, text, chunk_type, score}`.

## Límites y bugs conocidos

| Tema | Detalle |
|---|---|
| Cap de 100 notas del servidor | Documentado en el código. Si algo "no aparece", sospechá de esto: pedí el vault completo |
| `search` con límite | `search "foo" 20` buscaba literalmente `"foo 20"` y devolvía 0 resultados. **Corregido**: ahora acepta el `N` opcional |
| Notas escritas y RAG | Una nota recién escrita puede no aparecer hasta que expire la caché. Correr `cache-clear` después de escribir |
| Dependencias | `mcp`, `httpx`, `requests`. El stemmer español es opcional (si no está, el pipeline sigue sin él) |

## Uso desde otro script o agente

```python
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "omc", os.path.expanduser("~/.hermes/scripts/obsidian-mcp-client.py"))
omc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(omc)

chunks = omc.rag_improved("configuración del firewall", max_results=5)
texto = omc.read_note("02-hermes-docs/hermes-core/obsidian-sync-architecture.md")
```

Vía CLI desde un agente:

```python
import subprocess, json
r = subprocess.run(["python3", "scripts/obsidian-mcp-client.py", "rag-improved", "consulta"],
                   capture_output=True, text=True)
print(r.stdout)
```

## Pitfall de verificación

**Pipelines con `pipefail` + `head`:** `python3 client.py read "<nota>" | head -c 200` hace que python
reciba SIGPIPE y termine con código ≠ 0 — con `set -o pipefail` eso se lee como fallo del comando
aunque la lectura haya sido perfecta. Capturá la salida a una variable en vez de cortar el pipe.

**Ojo con la capitalización de los paths:** el ID del documento en CouchDB está en minúsculas
(`00-index.md`) pero el `path` de la nota conserva el caso original (`00-INDEX.md`). Usá los paths que
devuelve `list`, no los que inventes.
