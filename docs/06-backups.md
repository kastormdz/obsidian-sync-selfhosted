# 06 — Backups

## Qué se respalda y dónde

| Cron | Hora | Qué hace | Destino |
|---|---|---|---|
| Obsidian CouchDB Backup to S3 | `0 5 * * *` | Exporta el vault completo vía MCP | `s3://hermes-configs/obsidian-vault/` |
| Backup Configs → S3 | `0 6 * * *` | Configs del server | `s3://hermes-configs/` |

Estructura que genera el backup del vault:

```
obsidian-vault/
├── notes/<YYYYMMDD_HHMMSS>/<path de cada nota>.md   ← nota a nota, con frontmatter de backup
├── dumps/<YYYYMMDD_HHMMSS>_manifest.json.gz         ← metadata comprimida
├── dumps/latest_manifest.json.gz
└── manifests/<YYYYMMDD_HHMMSS>.json                 ← resumen + errores
```

Retención: **7 días** para notas y dumps (rotación por fecha en el nombre), **30 manifests**.

## Cómo se ejecuta

```bash
# Manual (usa el MCP, con el usuario no-admin y descifrado E2E)
python3 scripts/backup_obsidian_to_s3.py
```

El script:
1. Lista las notas vía MCP (pide el vault completo).
2. Lee cada nota (descifrando E2E) y la sube como `.md`.
3. Genera un manifest `.json.gz` con `note_count`, tamaños y errores.
4. Rota lo viejo y devuelve **exit code ≠ 0 si hubo algún error**.

Las credenciales S3 salen de `~/.hermes/.secrets/hermes-backups.env` (0600) o de las variables
`S3_ENDPOINT` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. **No están en el script.**

## Verificar que el backup sirve

Un backup que no se prueba no es un backup. Chequeo rápido:

```bash
python3 - <<'EOF'
import boto3, os, gzip, io, json
cfg = dict(l.split("=", 1) for l in open(os.path.expanduser("~/.hermes/.secrets/hermes-backups.env"))
           if l.strip() and not l.startswith("#"))
s3 = boto3.client("s3", endpoint_url=cfg["S3_ENDPOINT"].strip(),
                  aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"].strip(),
                  aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"].strip())
r = s3.list_objects_v2(Bucket="hermes-configs", Prefix="obsidian-vault/dumps/")
ult = sorted(r["Contents"], key=lambda o: o["LastModified"])[-1]
print("último dump:", ult["Key"], ult["LastModified"])
print("tamaño:", ult["Size"], "bytes")
EOF
```

Criterio de salud: el último snapshot debe tener **todas** las notas del vault (137 al 2026-09-16) y
ser de las últimas 24 h. `scripts/verify-vault.sh` hace esta comprobación junto con el resto.

## Restore

**El backup guarda las notas en texto plano** (el MCP descifra para exportar). Para reconstruir un
vault desde S3:

```bash
# 1. Elegir snapshot
aws --endpoint-url "$S3_ENDPOINT" s3 ls s3://hermes-configs/obsidian-vault/notes/

# 2. Bajarlo entero
aws --endpoint-url "$S3_ENDPOINT" s3 sync \
  s3://hermes-configs/obsidian-vault/notes/<TIMESTAMP>/ /ruta/de/restore/

# 3. Verificar cantidad de notas contra el manifest
python3 -c "import json,gzip;d=json.load(gzip.open('<TIMESTAMP>_manifest.json.gz'));print(d['note_count'],'notas esperadas')"
```

Casos:

| Escenario | Procedimiento |
|---|---|
| Se perdieron notas puntuales | Bajar solo esos `.md` del snapshot más reciente que las tenga |
| Se perdió todo el vault | Restore completo del último snapshot + reconfigurar LiveSync en los dispositivos |
| Se perdió la passphrase E2E | **El backup de S3 sirve** (está en claro); la copia de CouchDB no |

## Historia / limpieza

Había un **tercer cron** (`0 3 * * *`, "Backup Obsidian S3") que bajaba el bucket `obsidian` completo.
Ese bucket era un remanente de una migración anterior (90 objetos, ~330 kB, sin cambios) y el cron
llevaba **102 ejecuciones respaldando algo muerto**. Se retiró el 2026-09-16 junto con el wrapper
`backup_obsidian_wrapper.py`.

Antes de retirar cualquier cron de backup: comparar **qué respalda** contra **qué sigue vivo**. Dos
jobs con nombres parecidos pueden estar cubriendo cosas distintas — o la misma, dos veces.
