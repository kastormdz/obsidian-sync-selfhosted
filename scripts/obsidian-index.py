"""
obsidian-index.py — Índice local persistente de chunks del vault.

POR QUÉ EXISTE
─────────────
El RAG anterior (rag_improved) hablaba con el server MCP en CADA query:
Stage 1b leía el contenido de las 155 notas, Stage 2 volvía a leer los
candidatos. Con el bug de concurrencia SSE eso eran MINUTOS.

Este índice mantiene TODO el texto chunkeado + embeddings en SQLite local.
El RAG pasa a ser: query → SQLite (milisegundos) → resultados.
El server MCP queda solo para ESCRIBIR y para releer una nota puntual.

SINCRONIZACIÓN
──────────────
- indexar() relee el vault vía MCP (una vez, serializado) y actualiza solo
  las notas cuyo mtime cambió.
- Las escrituras (write/edit/delete/move) llaman refresh_note()/forget_note()
  para mantenerlo al día sin reindexar todo.
- build: `obsidian-index.py build`  (o `rebuild` para forzar)

USO
───
  python3 obsidian-index.py build          # indexa/actualiza
  python3 obsidian-index.py status         # stats
  python3 obsidian-index.py search "query"  # query+rank, igual que el RAG
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Optional

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "cache", "obsidian-index.db")
CLIENT = os.path.join(HOME, ".hermes", "scripts", "obsidian-mcp-client.py")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS notes (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    hash        TEXT,        -- sha1 del contenido: señal fiable de "cambió"
    n_chunks    INTEGER DEFAULT 0,
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    path    TEXT NOT NULL,
    ord     INTEGER,
    heading TEXT,
    hlevel  INTEGER,
    ctype   TEXT,
    text    TEXT,
    vec     BLOB
);

CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS df (
    term TEXT PRIMARY KEY,
    n    INTEGER
);

-- Cache de embeddings por hash. Comparte formato con la DB histórica
-- (~/.hermes/cache/obsidian-embeddings.db) para poder reusarla/migrarla.
CREATE TABLE IF NOT EXISTS emb (
    h   TEXT PRIMARY KEY,
    dim INTEGER,
    v   BLOB
);
"""


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.executescript(SCHEMA)
    _ensure_columns(c)
    _migrate_legacy_embeddings(c)
    return c


def _ensure_columns(c: sqlite3.Connection) -> None:
    """Agregar columnas faltantes a tablas ya creadas.

    `CREATE TABLE IF NOT EXISTS` no altera una tabla existente: si el esquema
    crece, la DB vieja se queda sin la columna nueva y el primer SELECT revienta
    con "no such column". Por eso el esquema NO alcanza — hay que migrar.
    """
    expected = {
        "notes": [("hash", "TEXT"), ("mtime", "REAL"), ("size", "INTEGER"),
                  ("n_chunks", "INTEGER"), ("indexed_at", "REAL")],
    }
    for table, cols in expected.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                print(f"[index] migracion: {table}.{name} agregada", flush=True)
    c.commit()


_LEGACY_DB = os.path.join(HOME, ".hermes", "cache", "obsidian-embeddings.db")


def _migrate_legacy_embeddings(c: sqlite3.Connection) -> None:
    """Copiar la cache histórica de embeddings a la DB del índice (una vez).

    La DB vieja tiene 4.400+ vectores ya calculados; recalcularlos cuesta
    minutos. Solo corre si la tabla emb local está vacía.
    """
    try:
        row = c.execute("SELECT COUNT(*) FROM emb").fetchone()
        if row and row[0] > 0:
            return
        if not os.path.exists(_LEGACY_DB) or os.path.abspath(_LEGACY_DB) == os.path.abspath(DB):
            return
        src = sqlite3.connect(f"file:{_LEGACY_DB}?mode=ro", uri=True, timeout=10)
        n = src.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
        if not n:
            src.close()
            return
        c.execute("ATTACH DATABASE ? AS legacy", (_LEGACY_DB,))
        c.execute("INSERT OR IGNORE INTO emb (h, dim, v) SELECT h, dim, v FROM legacy.emb")
        c.commit()
        c.execute("DETACH DATABASE legacy")
        src.close()
        print(f"[index] migrados {n} embeddings historicos desde obsidian-embeddings.db", flush=True)
    except Exception as exc:
        print(f"[index] no se migraron los embeddings historicos: {exc}", file=sys.stderr)


# ── Cliente MCP (import perezoso) ────────────────────────────────────────────
def client():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcpc", CLIENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Chunking (mismo criterio que el cliente: headings, fences, tablas) ───────
def chunk(content: str, min_chars: int = 120) -> list[dict[str, Any]]:
    """Divide en chunks respetando estructura markdown. Devuelve lista de dicts."""
    if not content or len(content.strip()) < 20:
        return []
    lines = content.split("\n")
    out: list[dict[str, Any]] = []
    buf: list[str] = []
    heading, hlevel, ctype = None, 0, "prose"
    in_fence = False

    def flush() -> None:
        nonlocal buf, heading, hlevel, ctype
        text = "\n".join(buf).strip()
        # min_chars es un piso de calidad, NO un filtro: una sección corta pero
        # densa ("## Backup → /home/...") es exactamente lo que se busca. Si se
        # descarta, el término queda invisible y el RAG devuelve notas ajenas
        # con confianza falsa. Medido 2026-09-29: el token de una nota de prueba
        # vivía en un chunk de 100 chars y el RAG no la encontraba NUNCA.
        if text and (len(text) >= min_chars or not out or ctype in ("code", "table", "list")):
            out.append({"heading": heading, "hlevel": hlevel, "ctype": ctype, "text": text})
        buf = []

    for ln in lines:
        if ln.strip().startswith("```"):
            if in_fence:
                buf.append(ln)
                flush()
                in_fence = False
                ctype = "prose"
            else:
                flush()
                in_fence = True
                ctype = "code"
                buf.append(ln)
            continue
        if in_fence:
            buf.append(ln)
            # Sub-chunking de fences enormes (un solo chunk de 16KB no es buscable)
            if sum(len(b) for b in buf) > 3000:
                buf.append("")
                flush()
                ctype = "code"
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", ln)
        if m:
            flush()
            heading, hlevel = m.group(2).strip(), len(m.group(1))
            ctype = "prose"
            buf.append(ln)
            continue
        if ln.strip().startswith("|") or re.match(r"^\s*[-*]\s|^\s*\d+\.\s", ln):
            flush()
            ctype = "table" if ln.strip().startswith("|") else "list"
            buf.append(ln)
            continue
        if not ln.strip():
            flush()
            continue
        if ctype in ("table", "list"):
            buf.append(ln)
        else:
            buf.append(ln)
    flush()

    # Un chunk de fence puede haber quedado partido sin heading: agregale el del padre
    last_h, last_l = None, 0
    for c in out:
        if c["hlevel"]:
            last_h, last_l = c["heading"], c["hlevel"]
        elif c["heading"] is None:
            c["heading"], c["hlevel"] = last_h, last_l
    return out


# ── Embeddings (fastembed, cacheados en la DB de embeddings existente) ──────
def embedder():
    try:
        from fastembed import TextEmbedding
        return TextEmbedding(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
    except Exception as exc:  # pragma: no cover
        print(f"[warn] fastembed no disponible ({exc}); RAG degradará a TF-IDF", file=sys.stderr)
        return None


def _hash(t: str) -> str:
    return hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()


def embed_all(texts: list[str], c: sqlite3.Connection, model) -> list[Optional[Any]]:
    """Embeddings con cache por hash en la tabla emb (reusa la DB vieja)."""
    import numpy as np
    hashes = [_hash(t) for t in texts]
    got: dict[str, Any] = {}
    unicos = list(dict.fromkeys(hashes))
    for i in range(0, len(unicos), 500):
        lote = unicos[i : i + 500]
        q = "SELECT h, dim, v FROM emb WHERE h IN (%s)" % ",".join("?" * len(lote))
        for h, dim, v in c.execute(q, lote):
            got[h] = np.frombuffer(v, dtype="float32").reshape(dim)
    faltan = [h for h in unicos if h not in got]
    if faltan and model is not None:
        nuevos = list(model.embed(missing_texts := [texts[hashes.index(h)] for h in faltan]))
        c.executemany(
            "INSERT OR REPLACE INTO emb (h, dim, v) VALUES (?,?,?)",
            [(_hash(t), v.shape[0], v.astype("float32").tobytes())
             for t, v in zip(missing_texts, nuevos)],
        )
        c.commit()
        for h, v in zip(faltan, nuevos):
            got[h] = v
    return [got.get(h) for h in hashes]


# ── Indexado ─────────────────────────────────────────────────────────────────
def build(force: bool = False) -> None:
    c = conn()
    m = client()
    notes = m._cached_list_notes()
    known = {r[0]: r[1] for r in c.execute("SELECT path, mtime FROM notes")} if not force else {}
    model = embedder()

    changed, same, failed = [], 0, []
    for n in notes:
        path = n["name"]
        try:
            body = m.read_note(path)
        except Exception:
            failed.append(path); continue
        if isinstance(body, str) and body.startswith("Note not found"):
            failed.append(path); continue
        try:
            meta = m._note_meta_for(path)
            mt = float(meta.get("mtime") or 0) if meta else 0.0
        except Exception:
            mt = 0.0
        # El HASH DEL CONTENIDO es la única señal fiable de "cambió".
        # mtime solo: la cache de metadata del server tiene TTL y puede venir
        # vieja → el write nuevo pasa inadvertido. size solo: una edición que
        # mantiene el largo (un caracter por otro) pasa inadvertida igual.
        # Medido 2026-09-29: una nota recién escrita con el MISMO largo no
        # reindexaba y el RAG seguía sin encontrarla.
        ch = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()
        prev = c.execute("SELECT size, mtime, hash FROM notes WHERE path=?", (path,)).fetchone()
        if prev and not force and len(prev) > 2 and prev[2] == ch:
            same += 1
            continue
        changed.append((path, mt, body))

    print(f"[build] vault={len(notes)}  sin cambios={same}  a reindexar={len(changed)}  fallidas={len(failed)}", flush=True)

    # PURGAR lo que ya no está en el vault. Sin esto el índice solo crece: una
    # nota borrada por MCP queda en el índice para siempre y el RAG puede
    # devolver un path que no existe — el peor modo de falla para un consumidor
    # que después va a read_note() y recibe "Note not found".
    # Medido 2026-09-29: 7 notas de prueba borradas seguían en el índice.
    server_paths = {n["name"] for n in notes}
    stale = [r[0] for r in c.execute("SELECT path FROM notes") if r[0] not in server_paths]
    if stale:
        c.executemany("DELETE FROM chunks WHERE path=?", [(p,) for p in stale])
        c.executemany("DELETE FROM notes WHERE path=?", [(p,) for p in stale])
        print(f"[build] purgadas {len(stale)} notas que ya no estan en el vault", flush=True)

    for i, (path, mt, body) in enumerate(changed, 1):
        c.execute("DELETE FROM chunks WHERE path=?", (path,))
        chunks = chunk(body)
        # El texto embebido incluye path + heading: sin el nombre de la nota,
        # un chunk como "Primer deploy" no tiene ningún contexto semántico y se
        # vuelve indistinguible de miles. Medido 2026-09-29: incluir el path
        # devolvió al top las notas correctas de CouchDB.
        vecs = (embed_all([f"{path} :: {ch['heading'] or ''} :: {ch['text']}" for ch in chunks], c, model)
                if chunks else [])
        c.executemany(
            "INSERT INTO chunks (path, ord, heading, hlevel, ctype, text, vec) VALUES (?,?,?,?,?,?,?)",
            [(path, j, ch["heading"], ch["hlevel"], ch["ctype"], ch["text"],
              (v.astype("float32").tobytes() if v is not None else None))
             for j, (ch, v) in enumerate(zip(chunks, vecs))],
        )
        chash = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()
        c.execute(
            "INSERT OR REPLACE INTO notes (path, mtime, size, hash, n_chunks, indexed_at)"
            " VALUES (?,?,?,?,?,?)",
            (path, mt, len(body), chash, len(chunks), time.time()),
        )
        if i % 10 == 0:
            c.commit(); print(f"  … {i}/{len(changed)}", flush=True)
    c.commit()

    # df (document frequency para TF-IDF)
    c.execute("DELETE FROM df")
    df: dict[str, int] = {}
    for (txt,) in c.execute("SELECT text FROM chunks"):
        for t in set(tok(txt)):
            df[t] = df.get(t, 0) + 1
    c.executemany("INSERT INTO df (term, n) VALUES (?,?)", df.items())
    c.commit()

    total = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    withvec = c.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
    print(f"[build] listo: {len(notes)} notas, {total} chunks ({withvec} con embedding)", flush=True)
    if failed:
        print(f"[build] no leídas: {len(failed)} -> {failed[:5]}", flush=True)


_TOK = re.compile(r"[a-z0-9áéíóúüñ]+")

# Stopwords: sin esto, "zqxjkv-9931-no-existe" toma "no" como su término más
# específico (aparece en la mitad del español escrito) y el piso de relevancia
# deja de filtrar ruido. Son las que nunca son el sujeto de una búsqueda.
_STOP = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o", "u",
    "en", "con", "por", "para", "del", "al", "que", "se", "su", "sus", "es",
    "son", "como", "mas", "más", "pero", "the", "of", "to", "in", "and", "or",
    "a", "an", "at", "on", "for", "no", "si", "sí", "ya", "muy", "sin", "sobre",
    "entre", "desde", "hasta", "este", "esta", "esto", "ese", "esa", "todo",
    "todos", "cada", "mi", "tu", "va", "van", "es", "hay", "the",
}


def tok(s: str) -> list[str]:
    return _TOK.findall(s.lower())


def key_terms(s: str) -> list[str]:
    """Tokens con contenido (sin stopwords). Si todo es stopword, usa todos."""
    t = _TOK.findall(s.lower())
    keep = [w for w in t if w not in _STOP and len(w) > 1]
    return keep or t


def search(query: str, k: int = 5, alpha: float = 0.6) -> list[dict]:
    c = conn()
    import numpy as np

    total_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not total_chunks:
        return []
    terms = set(tok(query)) | {t for t in tok(query) if len(t) > 3}
    if not terms:
        return []
    # Los términos CON contenido (sin stopwords) son los que definen el sujeto
    # de la búsqueda. `key_term` sale de acá, no del set completo: si no, "no"
    # puede ser el más específico y el piso deja de filtrar.
    kt = key_terms(query)
    key_set = set(kt)

    rows = c.execute(
        "SELECT path, ord, heading, hlevel, ctype, text, vec FROM chunks WHERE vec IS NOT NULL"
    ).fetchall()
    if not rows:  # sin embeddings → TF-IDF puro sobre texto
        scored = []
        for path, o, h, hl, ct, txt, _v in c.execute(
                "SELECT path, ord, heading, hlevel, ctype, text FROM chunks"):
            tt = tok(txt)
            s = sum(tt.count(t) for t in terms)
            if s:
                scored.append((s, path, o, h, hl, ct, txt))
        scored.sort(key=lambda x: -x[0])
        return [{"path": r[1], "heading": r[3], "chunk_type": r[5], "score": r[0] / 100, "text": r[6][:6000]}
                for r in scored[:k]]

    # La query se embebe con la MISMA plantilla que los chunks (path::heading::text)
    # para que el coseno compare cosas del mismo tipo.
    qv = embed_all([f":: {query}"], c, _MODEL_HOLDER[0])
    if qv[0] is None:
        return []
    qv = qv[0] / (np.linalg.norm(qv[0]) or 1)
    qset = set(terms)

    # IDF de los términos de la query, en UNA consulta. (Antes era un SELECT
    # por término POR CHUNK: 4.400+ queries para una sola búsqueda.)
    idf: dict[str, float] = {}
    for t in qset:
        row = c.execute("SELECT n FROM df WHERE term=?", (t,)).fetchone()
        idf[t] = float(np.log1p(total_chunks / (1 + (row[0] if row else 0))))

    # Un término que aparece en media vault ("docker", "nota", "server") no
    # discrimina nada: su coseno se aplana contra todo el vault y ahoga los
    # específicos. Pesar por IDF = darle menos peso al genérico.
    # El promedio (no la suma) mantiene lex en 0..1: con la suma, 3 términos
    # llegaban a 3.0 y el score final pasaba de 1.0 — scores de 1.3 sobre
    # notas que no tienen NADA que ver.
    idf_max = max(idf.values()) if idf else 1.0
    w = {t: (idf[t] / idf_max if idf_max else 0.0) for t in qset}
    lex_w = (sum(w.values()) / len(w)) if w else 1.0

    out = []
    for path, o, h, hl, ct, txt, vb in rows:
        vec = np.frombuffer(vb, dtype="float32")
        sem = max(0.0, min(1.0, float(vec @ qv / (np.linalg.norm(vec) or 1))))
        tt = tok(txt)
        if not tt:
            continue
        tf = min(1.0, sum(tt.count(t) for t in qset) / len(tt))
        lex = tf * lex_w
        score = alpha * lex + (1 - alpha) * sem
        # Boost por nombre/heading (paridad con el cliente)
        low = f"{path or ''} {h or ''}".lower()
        if any(t in low for t in qset):
            score *= 1.35
        if hl == 1: score += 0.15
        elif hl == 2: score += 0.10
        out.append({"path": path, "ord": o, "heading": h, "hlevel": hl,
                    "chunk_type": ct, "text": txt, "score": score, "sem": sem})
    out.sort(key=lambda x: -x["score"])

    # PISO DE RELEVANCIA. El modelo SIEMPRE devuelve algo: un coseno bajo
    # contra un término inexistente no es "el mejor resultado", es ruido con
    # score. Medido 2026-09-29: `rag "zqxjkv-9931"` (inexistente) devolvía
    # zram/btrfs con 0.365 y el consumidor lo leía como acierto.
    # Regla: el mejor chunk tiene que contener un término CON CONTENIDO de la
    # query como PALABRA COMPLETA (no un fragmento: "8891" de "vf-8891", o
    # "9931" de "zqxjkv-9931-xyz", aparecen en números de informe ajenos) — o
    # el substring literal de la query. Sin eso y con coseno bajo → [].
    best = out[0] if out else None
    if best is not None:
        hay = f"{best['text']} {best['path']} {best.get('heading') or ''}".lower()
        # Comparación por token completo: evita que "9931" matchee un "99315"
        # o que un número suelto de otra nota cuente como acierto.
        hay_tokens = set(re.findall(r"[a-z0-9áéíóúüñ]+", hay))
        has_word = any(t in hay_tokens for t in key_set)
        needle = query.strip().lower()
        if not has_word and needle not in hay and best["sem"] < _SEM_FLOOR:
            return []
    return out[:k]


_MODEL_HOLDER = [None]

# Piso de relevancia para el camino semántico. Calibrado 2026-09-29 contra el
# vault real con la métrica final (score = 0.6*lex + 0.4*coseno, ambos en 0..1):
#   aciertos reales   0.73 – 0.97   (couchdb/obsidian, backup/seafile,
#                                    keycloak/openldap, obsidian/livesync)
#   falsos positivos  0.34 – 0.58   (tokens inexistentes → zram, btrfs, buzz)
# 0.62 separa ambos grupos: deja pasar todos los aciertos medidos y corta
# todos los falsos medidos. Es un umbral, no una garantía — si el ranking
# vuelve a devolver basura, recalibrar acá con los números, no a ojo.
_SEM_FLOOR = 0.62


def _main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "build":
        _MODEL_HOLDER[0] = embedder()
        build(force="--force" in sys.argv)
    elif cmd == "status":
        c = conn()
        n = c.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        ch = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vec = c.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
        last = c.execute("SELECT MAX(indexed_at) FROM notes").fetchone()[0]
        print(f"notas={n} chunks={ch} con_embedding={vec} "
              f"ultimo_index={time.strftime('%Y-%m-%d %H:%M', time.localtime(last)) if last else 'nunca'}")
    elif cmd == "search":
        _MODEL_HOLDER[0] = embedder()
        q = " ".join(sys.argv[2:-1]) if sys.argv[-1].isdigit() else " ".join(sys.argv[2:])
        k = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 5
        t0 = time.perf_counter()
        res = search(q, k)
        print(f"[{time.perf_counter()-t0:.3f}s] {len(res)} resultados para {q!r}")
        for r in res:
            print(f"  {r['score']:.3f}  {r['path']} :: {r.get('heading')} ({r['chunk_type']})")
    else:
        print("uso: obsidian-index.py {build [--force]|status|search 'query' [N]}")


if __name__ == "__main__":
    _main()
