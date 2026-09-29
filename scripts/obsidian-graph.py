"""
obs-graph — auditoría del grafo de wikilinks del vault.

POR QUÉ EXISTE
──────────────
Medido 2026-09-29 sobre 152 notas: 82% no linkean a nada, 76% son islas
(ningún link entra ni sale), 120 componentes disconnected para 152 notas.
El único grafo real era el 00-INDEX.md. El RAG recupera por similitud pero
NO acumula relaciones: cada nota nueva es un archivo suelto.

Este script mide eso. Y `obsidian-link.py` propone los links que faltan usando
los embeddings que ya están en el índice local.

USO
───
  python3 obsidian-graph.py                 # reporte completo
  python3 obsidian-graph.py --stats         # solo el resumen
  python3 obsidian-graph.py --orphans       # lista de huérfanas
  python3 obsidian-graph.py --clusters      # componentes conexas
  python3 obsidian-graph.py --broken        # wikilinks que no resuelven
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

DB = os.path.expanduser("~/.hermes/cache/obsidian-index.db")

WIKILINK = re.compile(r"\[\[([^\]|#]+)")
# Cosas que PARECEN wikilinks pero no lo son (sintaxis de Obsidian o código).
# Sin este filtro, [[name: '*/main']] de un bloque YAML cuenta como link roto
# y el reporte miente sobre la salud del grafo.
# `[[:space:]]` es una clase de caracteres POSIX: aparece en comandos sed/grep
# embebidos y NO es un wikilink. Detectado 2026-09-29 al auditar un vault real.
FALSE_POSITIVE = re.compile(
    r"^(https?|ftp)://|^name:\s|^url:\s|^note$|^link$|^\*|^//|^\["
    r"|^\[:?\w+:\]|^\d+$|^-$"
)


def load_notes(db: str = DB) -> dict[str, str]:
    """Reconstruir cada nota desde sus chunks (el índice tiene el texto completo)."""
    import sqlite3
    if not os.path.exists(db):
        sys.exit(f"obs-graph: no existe el índice {db}. Corré: obsn reindex")
    c = sqlite3.connect(db)
    notes: dict[str, list[str]] = defaultdict(list)
    for path, text in c.execute("SELECT path, text FROM chunks ORDER BY path, ord"):
        notes[path].append(text)
    c.close()
    return {p: "\n".join(parts) for p, parts in notes.items()}


def basename(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].lower()


def build_graph(notes: dict[str, str]) -> dict:
    """Aristas out/in, huérfanas, islas, componentes y links rotos."""
    names = {p: basename(p) for p in notes}
    # índice inverso basename → path (si hay colisiones, gana la primera;
    # se reportan aparte porque un basename duplicado es un problema real)
    by_name: dict[str, list[str]] = defaultdict(list)
    for p, n in names.items():
        by_name[n].append(p)

    out: dict[str, set[str]] = defaultdict(set)
    inb: dict[str, set[str]] = defaultdict(set)
    broken: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []

    for path, body in notes.items():
        for raw in WIKILINK.findall(body):
            target = raw.strip()
            if not target or FALSE_POSITIVE.match(target):
                continue
            low = target.lower().rstrip(".md")
            cands = [p for p in notes if names[p] == low or p.lower().rstrip(".md") == low]
            if not cands:
                broken.append((path, target))
                continue
            if len(cands) > 1:
                ambiguous.append((path, f"{target} → {len(cands)} notas"))
            hit = cands[0]
            if hit != path:
                out[path].add(hit)
                inb[hit].add(path)

    orphans = [p for p in notes if not out[p]]
    islands = [p for p in notes if not inb[p] and not out[p]]
    leaves = [p for p in notes if not inb[p]]

    # Componentes conexas (BFS sobre el grafo no dirigido)
    seen: set[str] = set()
    clusters: list[list[str]] = []
    for p in notes:
        if p in seen:
            continue
        stack, comp = [p], []
        seen.add(p)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in out[n] | inb[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        clusters.append(comp)
    clusters.sort(key=len, reverse=True)

    return {
        "notes": notes, "out": out, "inb": inb,
        "orphans": orphans, "islands": islands, "leaves": leaves,
        "clusters": clusters, "broken": broken, "ambiguous": ambiguous,
        "edges": sum(len(v) for v in out.values()),
    }


def by_folder(notes: dict, out: dict) -> list[tuple[str, int, int]]:
    data: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p in notes:
        d = os.path.dirname(p) or "(raíz)"
        data[d][0] += 1
        if out[p]:
            data[d][1] += 1
    return sorted(data.items(), key=lambda x: -x[1][0])


def report(g: dict, show: str) -> None:
    notes, out, inb = g["notes"], g["out"], g["inb"]
    tot = len(notes)
    if tot == 0:
        print("obs-graph: el índice está vacío")
        return
    linked_out = tot - len(g["orphans"])
    linked_in = len([p for p in notes if inb[p]])

    if show == "stats":
        print(f"notas={tot}  linkean={linked_out} ({100*linked_out//tot}%)  "
              f"reciben={linked_in} ({100*linked_in//tot}%)  "
              f"huérfanas={len(g['orphans'])}  islas={len(g['islands'])}  "
              f"componentes={len(g['clusters'])}  aristas={g['edges']}  "
              f"rotos={len(g['broken'])}")
        return

    print("═" * 66)
    print(f"GRAFO DE WIKILINKS — {tot} notas")
    print("═" * 66)
    print(f"  con link saliente   : {linked_out:4d}  ({100*linked_out//tot:3d}%)")
    print(f"  con link entrante   : {linked_in:4d}  ({100*linked_in//tot:3d}%)")
    print(f"  huérfanas           : {len(g['orphans']):4d}  ({100*len(g['orphans'])//tot:3d}%)")
    print(f"  islas               : {len(g['islands']):4d}  ({100*len(g['islands'])//tot:3d}%)")
    print(f"  componentes         : {len(g['clusters']):4d}")
    print(f"  aristas             : {g['edges']:4d}")
    print(f"  links rotos         : {len(g['broken']):4d}")

    print(f"\n─── DENSIDAD POR CARPETA {'─'*44}")
    for d, (n, l) in by_folder(notes, out):
        pct = 100 * l // n
        bar = "█" * (pct // 5)
        flag = "  ←" if pct < 20 and n >= 2 else ""
        print(f"  {d:42s} {n:3d}  {pct:3d}%  {bar}{flag}")

    if show in ("orphans", "all"):
        print(f"\n─── HUÉRFANAS (no linkean a nada) {'─'*34}")
        for p in sorted(g["orphans"]):
            print(f"  {p}")

    if show in ("clusters", "all"):
        print(f"\n─── COMPONENTES CONEXAS {'─'*42}")
        multi = [c for c in g["clusters"] if len(c) > 1]
        print(f"  {len(g['clusters'])} componentes · {len(multi)} con >1 nota")
        for comp in g["clusters"][:15]:
            if len(comp) == 1:
                print(f"      suelta  {comp[0]}")
            else:
                print(f"  CLUSTER({len(comp):3d})  {comp[0]}" + (f"  +{len(comp)-1} más" if len(comp) > 1 else ""))
        if len(multi) > 15:
            print(f"  … y {len(g['clusters'])-15} componentes más")

    if show in ("broken", "all") and g["broken"]:
        print(f"\n─── LINKS ROTOS {'─'*50}")
        for p, t in g["broken"]:
            print(f"  {p}\n      → [[{t}]]")
    if g["ambiguous"]:
        print(f"\n─── BASENAMES AMBIGUOS {'─'*45}")
        for p, t in g["ambiguous"]:
            print(f"  {p}  →  {t}")

    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Auditoría del grafo de wikilinks del vault")
    ap.add_argument("--stats", action="store_true", help="solo el resumen en una línea")
    ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--clusters", action="store_true")
    ap.add_argument("--broken", action="store_true")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    show = ("orphans" if a.orphans else "clusters" if a.clusters
            else "broken" if a.broken else "stats" if a.stats else "all")
    report(build_graph(load_notes(a.db)), show)


if __name__ == "__main__":
    main()
