# 10 — El grafo de notas y el linkeo automático

**Medido el 2026-09-29** sobre un vault real de 152 notas. Todo lo que sigue son
números de ejecuciones reales.

## El problema

Un RAG recupera por similitud, pero **no acumula relaciones**. Cada nota nueva es
un archivo suelto y nadie vuelve a linkearla: nada obliga a que mencione la
anterior. Y el índice del vault —un índice de navegación— enlaza por existencia,
no por relación, así que tampoco ayuda.

El resultado, medido:

```
notas con link saliente   : 26/152  (17%)
islas (ni entran ni salen): 116     (76%)
componentes conexas       : 120     para 152 notas
```

**120 componentes en 152 notas** no es un grafo con cabos flojos: es 150 carpetas
de una nota y una red de treinta.

El caso que lo delata: tres notas sobre el mismo sistema, escritas por separado
en momentos distintos, que no se conocen entre sí. El RAG devuelve las tres al
buscar el tema — lo que obliga a leerlas enteras para saber si dicen cosas
distintas.

## La solución: usar el índice como motor de grafo

El índice local ya tiene los embeddings de cada chunk. Esos vectores dicen, para
cada nota, cuáles son sus vecinas reales — y lo dicen mejor que un heurístico
sobre nombres. `obsidian-link.py` usa **los mismos números que el RAG**.

```bash
python3 scripts/obsidian-graph.py              # auditoría completa
python3 scripts/obsidian-graph.py --stats      # una línea
python3 scripts/obsidian-graph.py --orphans    # las que no linkean
python3 scripts/obsidian-graph.py --clusters   # componentes conexas
python3 scripts/obsidian-graph.py --broken     # wikilinks que no resuelven

python3 scripts/obsidian-link.py --propose     # links sugeridos (no escribe)
python3 scripts/obsidian-link.py --neighbors "un tema"
python3 scripts/obsidian-link.py --canonico    # pares que parecen duplicados
python3 scripts/obsidian-link.py --apply       # ESCRIBE (flag explícito)
```

## Resultados

| métrica | antes | después | Δ |
|---|---:|---:|---:|
| notas que linkean | 26 (17%) | **70 (46%)** | ×2.7 |
| islas | 116 | **70** | −46 |
| componentes conexas | 120 | **72** | −48 |
| aristas | 46 | **162** | ×3.5 |

Las carpetas con más recorrido: `almacenamiento` 33%→100%, `hermes-core`
33%→81%, `mailadm` 0%→75%, `sistema` 9%→63%.

## Tres decisiones de diseño

**Umbral alto y links no recíprocos.** A 0.62 las propuestas eran 440. Un grafo de
440 links débiles es ruido: se mira una vez y se deja de mirar. Con umbral 0.78 y
deduplicando recíprocos —si A propone B y B propone A, es un link, y lo emite la
de nombre más específico— quedaron **136 sobre 61 notas**, revisables.

**Negocia canónicos.** Cuando dos notas se proponen mutuamente, el link lo emite
la de nombre más específico. El patrón aparece siempre: `keycloak-tutorial` es
canónica de `keycloak-openldap-mendoza`, no al revés.

**Excluye zonas sensibles.** `Secrets/`, `Archive/` y `Todo/` quedan fuera: son
credenciales, archivo muerto y lista de pendientes.

## Lo que el linkeo automático NO arregla

**Las carpetas de listas quedan al 0% y es lo correcto.** Las hojas de referencia
(cheatsheets de regex, listas de palabras) no se relacionan con nada porque no hay
nada con qué relacionarse. Forzarlas sería llenar el grafo de ruido — el mismo
problema que se corrigió al bajar de 440 propuestas a 136.

**Los duplicados de contenido no se resuelven solos.** `--canonico` detecta pares
muy similares y propone cuál es la canónica, pero consolidar dos notas en una
requiere leerlas y decidir qué se pierde. Eso es una decisión humana.

## Trampas encontradas

**Contar links rotos sin filtrar da números falsos.** En el primer conteo
aparecieron 12, pero cinco eran `[[name: '*/main']]` y
`[[url: 'https://...']]` — sintaxis YAML mal interpretada por el regex. Después
salió `[[:space:]]`, que es una clase de caracteres POSIX dentro de un comando
`sed` embebido. `obsidian-graph.py` filtra ambos. Un reporte que cuenta links
rotos que no son links rotos deja de creerse.

**Un `pass` donde iba un `continue`.** `obsidian-link.py --apply` re-aplicaba la
sección `## Notas relacionadas` sobre notas ya procesadas, duplicándola. Doce
notas quedaron así; se restauraron desde backup. El caso general es peor que el
bug: cualquier escritura de vault necesita **backup previo**, porque la escritura
es la parte no reversible.

**La sesión SSE se corta por inactividad.** El servidor MCP cierra la conexión
con `ReadTimeout` si nadie la usa. El pool del cliente reconecta, pero la primera
llamada tras el corte falla. Sin un reintento, la mitad de las operaciones de
escritura fallan con "Connection closed" y parece un problema del vault cuando no
lo es. El daemon reintenta una vez, con flag, para que el corte sea invisible.

## Cómo saber si tu grafo está sano

```bash
python3 scripts/obsidian-graph.py --stats
```

- **componentes < mitad de las notas** → el grafo funciona.
- **notas que linkean > 50%** → hay trabajo relacional real.
- Un vault nuevo arranca en 100% de componentes; eso es normal hasta que las
  notas se acumulen.

## Ver también

- [09-rendimiento-cliente.md](09-rendimiento-cliente.md) — por qué el índice local
  hace esto posible (y por qué el RAG del server tardaba minutos)
