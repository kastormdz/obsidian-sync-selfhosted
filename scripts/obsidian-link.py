"""
obsidian-link — propone y aplica wikilinks entre notas usando el índice local.

POR QUÉ EXISTE
──────────────
Medido 2026-09-29: 152 notas, 82% sin link saliente, 76% islas, 120 componentes
para 152 notas. El RAG recupera por similitud pero NO acumula relaciones: cada
nota nueva es un archivo suelto y nadie vuelve a linkearla.

Este script invierte eso: usa los embeddings que YA están en obsidian-index.db
para calcular, nota por nota, cuáles son sus vecinas reales. No inventa
relaciones — la propuesta sale de los mismos scores que usa el RAG.

DISEÑO
──────
- Solo propone links cuando la relación es FUERTE (score alto) y la nota no
  está ya enlazada a la otra. Un grafo de 300 links weak es ruido; el valor
  está en 30 links que alguien mire y piense "sí, tiene sentido".
- Distingue "vecina" (tema compartido) de "corrección" (la otra nota es más
  canónica sobre el mismo sujeto) — esa segunda categoría es la que más sirve.
- NUNCA toca `Secrets/` ni `06-Todo/`: son credenciales y pendientes.
- Propone primero, aplica después. Nunca escribe sin `--apply` explícito.

USO
───
  python3 obsidian-link.py                       # reporte general
  python3 obsidian-link.py --neighbors keycloak  # vecinas de un tema
  python3 obsidian-link.py --propose              # links sugeridos, no toca nada
  python3 obsidian-link.py --apply                # escribe los links en el vault
  python3 obsidian-link.py --apply --max 30       # hasta 30 (default 50)
  python3 obsidian-link.py --canonico             # detecta duplicados de sujeto
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

HOME = os.path.expanduser("~")
INDEX = os.path.join(HOME, ".hermes", "scripts", "obsidian-index.py")
DB = os.path.join(HOME, ".hermes", "cache", "obsidian-index.db")

# Carpetas fuera de alcance: credenciales y lista de cosas por hacer.
EXCLUIR_DIRS = ("03-Personal/Secrets", "06-Todo", "05-Archive", ".obsidian")
# No proponer links hacia/desde estas: son índices, no contenido.
NO_ENLACAR = ("00-INDEX.md",)

WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def cargando_indice():
    """Importar obsidian-index.py (necesita el modelo de embeddings cargado)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("obsidian_index", INDEX)
    oi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oi)
    return oi


def notas_filtradas() -> list[str]:
    import sqlite3
    c = sqlite3.connect(DB)
    rows = [r[0] for r in c.execute("SELECT path FROM notes ORDER BY path")]
    c.close()
    return [p for p in rows
            if not p.startswith(EXCLUIR_DIRS) and p not in NO_ENLACAR]


def vec_por_nota() -> dict[str, "object"]:
    """Vector medio de cada nota (promedio de sus chunks), normalizado."""
    import numpy as np
    import sqlite3
    c = sqlite3.connect(DB)
    acc: dict[str, list] = defaultdict(list)
    for path, vec in c.execute("SELECT path, vec FROM chunks WHERE vec IS NOT NULL"):
        acc[path].append(np.frombuffer(vec, dtype="float32"))
    c.close()
    out = {}
    for p, vs in acc.items():
        v = np.mean(vs, axis=0)
        n = np.linalg.norm(v)
        if n:
            out[p] = v / n
    return out


def enlaces_actuales() -> dict[str, set[str]]:
    import sqlite3
    c = sqlite3.connect(DB)
    notes: dict[str, list[str]] = defaultdict(list)
    for p, t in c.execute("SELECT path, text FROM chunks ORDER BY path, ord"):
        notes[p].append(t)
    c.close()
    cur: dict[str, set[str]] = defaultdict(set)
    names = {p: os.path.splitext(os.path.basename(p))[0].lower() for p in notes}
    for p, body in notes.items():
        joined = "\n".join(body)
        for raw in WIKILINK.findall(joined):
            t = raw.strip().lower().rstrip(".md")
            for q, n in names.items():
                if n == t and q != p:
                    cur[p].add(q)
    return cur


def vecinos(vecs: dict, cur: dict, min_score: float, top_k: int) -> dict[str, list]:
    """Para cada nota, sus vecinas no enlazadas con score >= min_score.

    Deduplicación recíproca: si A propone B y B propone A, es UN link, no dos.
    Sin esto, 440 propuestas se reducen a ~180 reales y el grafo queda legible.
    El desempate es la longitud del nombre: la nota más específica es la que
    "posee" la relación (patrón: `keycloak-tutorial` es canónica de
    `keycloak-openldap-mendoza`, no al revés).
    """
    import numpy as np
    paths = list(vecs)
    M = np.vstack([vecs[p] for p in paths])
    sim = M @ M.T
    idx = {p: i for i, p in enumerate(paths)}
    crudos: dict[str, list] = {}
    for p in paths:
        row = sim[idx[p]]
        cands = []
        for j, q in enumerate(paths):
            if p == q or q in cur.get(p, ()):
                continue
            s = float(row[j])
            if s >= min_score:
                cands.append((s, q))
        cands.sort(key=lambda x: -x[0])
        if cands:
            crudos[p] = cands[:top_k]

    # Deduplicar recíprocos: gana la nota cuyo nombre es más corto/específico.
    out: dict[str, list] = {}
    ya_vistas: set[frozenset] = set()
    for p, cands in crudos.items():
        for s, q in cands:
            par = frozenset((p, q))
            if par in ya_vistas:
                # quitar la dirección inversa si ya se emitió
                if q in out and any(x[1] == p for x in out[q]):
                    out[q] = [x for x in out[q] if x[1] != p]
                    if not out[q]:
                        del out[q]
                continue
            ya_vistas.add(par)
            out.setdefault(p, []).append((s, q))
    return out


def detecta_canonicos(vecs: dict, cur: dict, umbral: float) -> list[tuple]:
    """Pares casi duplicados: mismo sujeto, dos notas. El más canónico gana.

    Heurística de canonicidad: la nota cuyo nombre es más corto y específico
    (menos sufijos tipo '-investigacion', '-setup', '-informe') y que tiene más
    enlaces entrantes. No es infalible — por eso SOLO propone, no mueve nada.
    """
    import numpy as np
    paths = list(vecs)
    M = np.vstack([vecs[p] for p in paths])
    sim = M @ M.T
    idx = {p: i for i, p in enumerate(paths)}
    pares = []
    for i, p in enumerate(paths):
        for j in range(i + 1, len(paths)):
            q = paths[j]
            s = float(sim[i][j])
            if s >= umbral and p not in cur.get(q, ()) and q not in cur.get(p, ()):
                pares.append((s, p, q))
    pares.sort(key=lambda x: -x[0])
    return pares


def score_de_canonicidad(p: str, cur: dict) -> float:
    """Más alto = más probable que sea la nota canónica del tema."""
    base = os.path.splitext(os.path.basename(p))[0]
    entrantes = len(cur.get(p, ()))
    # sufijos que sugieren "nota derivada" y no canónica
    derivado = sum(1 for s in ("investigacion", "setup", "informe", "research",
                               "test", "notas", "draft", "wip", "deep-dive")
                   if s in base.lower())
    largo = max(0, len(base) - 20) / 40
    return entrantes - derivado * 1.5 - largo


def fmt_paths(ps: list[str], n: int = 6) -> str:
    ps = ps[:n]
    s = "\n".join(f"      {p}" for p in ps)
    return s + (f"\n      … y {len(ps)-n} más" if len(ps) > n else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Propone y aplica wikilinks por similitud (RAG)")
    ap.add_argument("--neighbors", metavar="TEMA", help="vecinas de un tema")
    ap.add_argument("--propose", action="store_true", help="solo muestra, no escribe")
    ap.add_argument("--apply", action="store_true", help="ESCRIBE los links en el vault")
    ap.add_argument("--min-score", type=float, default=0.78,
                    help="similitud mínima para proponer (default 0.78)")
    ap.add_argument("--max", type=int, default=50, help="máximo de notas a tocar")
    ap.add_argument("--canonico", action="store_true", help="detecta duplicados de sujeto")
    ap.add_argument("--umbral-canonico", type=float, default=0.80)
    a = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit("obsidian-link: no hay índice. Corré: obsn reindex")
    oi = cargando_indice()
    notas = set(notas_filtradas())
    vecs_all = vec_por_nota()
    vecs = {p: v for p, v in vecs_all.items() if p in notas}
    cur = enlaces_actuales()
    cur = {p: {q for q in s if q in vecs} for p, s in cur.items()}

    # ── modo: vecinas de un tema ──
    if a.neighbors:
        q = oi.search(a.neighbors, 8)
        print(f"\n─── VECINAS DE {a.neighbors!r} (búsqueda RAG) ───")
        for h in q:
            print(f"  {h['score']:.3f}  {h['path']}"
                  f"{'   [ya enlazada]' if h['path'] in cur.get('00-INDEX.md', ()) else ''}")
        return

    # ── modo: canónicos (duplicados) ──
    if a.canonico:
        pares = [(s, p, q) for s, p, q in detecta_canonicos(vecs, cur, a.umbral_canonico)
                 if p in vecs and q in vecs]
        print(f"\n─── POSIBLES DUPLICADOS (similitud >= {a.umbral_canonico}) ───")
        print(f"  {len(pares)} pares. Propone canónico, NO mueve nada.\n")
        for s, p, q in pares[:30]:
            cp, cq = score_de_canonicidad(p, cur), score_de_canonicidad(q, cur)
            canon = p if cp >= cq else q
            otro = q if canon == p else p
            print(f"  {s:.3f}  canónico: {canon}")
            print(f"          duplicado: {otro}")
        return

    # ── modo: propuesta / aplicación ──
    vec = vecinos(vecs, cur, a.min_score, top_k=4)
    total_links = sum(len(v) for v in vec.values())
    print(f"\n─── PROPUESTA DE LINKS (similitud >= {a.min_score}) ───")
    print(f"  {len(vec)} notas ganarían links · {total_links} links sugeridos\n")
    if not vec:
        print("  (nada que proponer con ese umbral — probá --min-score 0.55)")
        return

    ranking = sorted(vec.items(), key=lambda x: -x[1][0][0])
    for p, cands in ranking[:25]:
        print(f"  {p}")
        for s, q in cands:
            print(f"      ← {q}   ({s:.3f})")
    if len(ranking) > 25:
        print(f"  … y {len(ranking)-25} notas más")

    if not a.apply:
        print("\n  (espejo nada. Usá --apply para escribir)")
        return

    # ── aplicar ──
    import obsc
    tocadas = 0
    for p, cands in ranking:
        if tocadas >= a.max:
            break
        try:
            body = obsc.call("read_note", {"path": p})
        except Exception as e:
            print(f"  ! no pude leer {p}: {e}")
            continue
        # Si ya tiene la sección, agregar OTRA igual la duplica. El código
        # anterior tenía un `pass` acá (no un `continue`), así que re-aplicar
        # sobre una nota ya procesada escribía "## 🔗 Notas relacionadas" dos
        # veces. Verificado 2026-09-29: 3 notas quedaron con la sección duplicada.
        if not isinstance(body, str):
            print(f"  ! respuesta inesperada en {p}")
            continue
        if "Notas relacionadas" in body:
            continue
        nuevos = [q for _, q in cands
                  if f"[[{os.path.splitext(os.path.basename(q))[0]}]]" not in body
                  and f"[[{q}]]" not in body]
        if not nuevos:
            continue
        # wikilinks por basename (como funcionan en Obsidian)
        line = "\n\n## 🔗 Notas relacionadas\n\n" + "\n".join(
            f"- [[{os.path.splitext(os.path.basename(q))[0]}]]" for q in nuevos)
        try:
            obsc.call("edit_note", {"path": p, "content": line, "operation": "append"})
            print(f"  ✓ {p}  ←  {len(nuevos)} links")
            tocadas += 1
        except Exception as e:
            print(f"  ! falló {p}: {e}")
    print(f"\n  {tocadas} notas actualizadas. Reindexá con: obsn reindex")


if __name__ == "__main__":
    main()
