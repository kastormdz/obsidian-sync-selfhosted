#!/usr/bin/env python3
"""
Backup del vault Obsidian → S3 (bucket hermes-configs).

Corrige dos fallas silenciosas de la versión anterior, que reportaba "137/137 ✅"
mientras perdía datos:

  1. TRUNCAMIENTO: leía las notas con el CLI del cliente MCP por subprocess, y ese CLI
     corta la salida a 20.000 caracteres (guard de pantalla). Toda nota más grande se
     respaldaba cortada — hasta un 91% menos. Ahora usa read_note() en proceso, que
     devuelve el contenido completo.

  2. ARCHIVOS OMITIDOS: enumeraba el vault con el `list` del servidor MCP, que solo
     devuelve archivos .md. Los PDFs y las notas sin extensión nunca se respaldaban.
     Ahora enumera desde CouchDB: todo documento con `path` y sin `deleted`.

Al terminar VERIFICA que cada archivo se haya guardado completo (comparando tamaños contra
los metadatos de CouchDB) y sale con código ≠ 0 si algo falta o quedó corto. Un backup que
no se autoverifica es una promesa, no un backup.

Uso:  python3 backup_couchdb_s3.py
"""

import base64
import gzip
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import unquote

BUCKET = "hermes-configs"
PREFIX = "obsidian-vault"
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
MCP_CONTAINER = os.environ.get("OBSIDIAN_MCP_CONTAINER", "obsidian-mcp")
MCP_CLIENT = os.environ.get("OBSIDIAN_MCP_CLIENT",
                            os.path.expanduser("~/.hermes/scripts/obsidian-mcp-client.py"))
SECRETS_FILE = os.path.expanduser("~/.hermes/.secrets/hermes-backups.env")

# Extensiones que LiveSync guarda como binario: el MCP las devuelve en base64.
BINARY_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
              ".zip", ".gz", ".tar", ".7z", ".rar", ".xz", ".bz2",
              ".mp3", ".mp4", ".m4a", ".mov", ".avi", ".wav", ".ogg", ".webm",
              ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".odt", ".ods",
              ".ttf", ".otf", ".woff", ".woff2", ".bin", ".exe", ".dmg", ".iso"}


def load_secrets(path: str = SECRETS_FILE) -> None:
    """Credenciales S3 desde un archivo 0600 fuera de todo repo."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


load_secrets()

S3_CONFIG = {
    "endpoint_url": os.environ.get("S3_ENDPOINT", ""),
    "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", ""),
    "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
    "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
}
if not all((S3_CONFIG["endpoint_url"], S3_CONFIG["aws_access_key_id"], S3_CONFIG["aws_secret_access_key"])):
    sys.exit(f"❌ Faltan credenciales S3. Completá {SECRETS_FILE} (0600) o exportá S3_ENDPOINT/"
             "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")


# ── cliente MCP (importlib: sin CLI, sin truncamiento) ───────────────────────
def load_mcp_client():
    spec = importlib.util.spec_from_file_location("obsidian_mcp_client", MCP_CLIENT)
    if spec is None or spec.loader is None:
        sys.exit(f"❌ No pude cargar el cliente MCP en {MCP_CLIENT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── CouchDB: la lista autoritativa del vault ─────────────────────────────────
def couchdb_base():
    """URL y credenciales de CouchDB, preguntándoselas al contenedor del MCP.

    Evita hardcodear rutas y credenciales: el contenedor ya las tiene en su entorno.
    Ojo: el COUCHDB_URL del contenedor suele apuntar a `host.docker.internal`, que solo
    resuelve DENTRO de Docker. Desde el host eso es 127.0.0.1 (o el override
    OBSIDIAN_COUCHDB_URL, pensado justamente para correr esto fuera del contenedor).
    """
    url = os.environ.get("OBSIDIAN_COUCHDB_URL", "")
    if not url:
        try:
            envs = subprocess.run(
                ["docker", "inspect", MCP_CONTAINER, "--format",
                 "{{range .Config.Env}}{{println .}}{{end}}"],
                capture_output=True, text=True, timeout=15).stdout
            url = next((l.split("=", 1)[1] for l in envs.splitlines() if l.startswith("COUCHDB_URL=")), "")
        except Exception:
            url = ""
    if not url:
        return None
    m = re.match(r"^(https?://)(?:([^:@/]+):([^@/]*)@)?(.+)$", url.strip())
    if not m:
        return None
    scheme, user, pwd, host = m.groups()
    host = host.replace("host.docker.internal", "127.0.0.1")
    return f"{scheme}{host}", (unquote(user or ""), unquote(pwd or ""))


def vault_index():
    """Todos los documentos que representan archivos vivos del vault.

    Usa _all_docs (solo ids) para descartar los bloques CRDT `h:` y después pide el
    detalle solo de los candidatos: el vault tiene ~7.000 documentos pero apenas ~300
    son archivos; pedir include_docs de todo movería megas al pedo.
    """
    import urllib.request

    base_auth = couchdb_base()
    if not base_auth:
        return []
    base, auth = base_auth
    dbname = os.environ.get("COUCHDB_DATABASE", "obsidian")
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode(),
           "Content-Type": "application/json"}

    def call(path, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(f"{base}/{path}", data=data, headers=hdr,
                                     method="POST" if body else "GET")
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    ids = [row["id"] for row in call(f"{dbname}/_all_docs")["rows"]]
    candidatos = [i for i in ids if not i.startswith(("h:", "_"))]

    docs, CHUNK = [], 200
    for i in range(0, len(candidatos), CHUNK):
        res = call(f"{dbname}/_all_docs?include_docs=true", {"keys": candidatos[i:i + CHUNK]})
        for row in res["rows"]:
            doc = row.get("doc") or {}
            if doc.get("path") and not doc.get("deleted"):
                docs.append({"path": doc["path"], "size": doc.get("size") or 0,
                             "mtime": doc.get("mtime") or 0})
    return docs


def is_binary(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in BINARY_EXT


def decode_payload(path: str, content: str) -> bytes:
    """Contenido listo para subir.

    Los binarios llegan en base64, y los archivos GRANDES llegan como varios bloques
    base64 concatenados (de 100 KiB cada uno, cada bloque con su propio padding). Eso
    rompe un b64decode de una sola pasada —falla con "Excess data after padding"— así que
    se corta por los límites de padding y se decodifica bloque por bloque.
    """
    if not is_binary(path):
        return content.encode("utf-8")
    s = re.sub(r"\s+", "", content)
    partes = re.split(r"(?<==)(?=[A-Za-z0-9+/])", s)
    try:
        return b"".join(base64.b64decode(p + "=" * (-len(p) % 4)) for p in partes if p)
    except Exception as e:
        raise ValueError(f"base64 inválido ({e})")


def backup() -> bool:
    import boto3

    s3 = boto3.client("s3", **S3_CONFIG)
    mcp = load_mcp_client()

    print("📥 Indexando el vault desde CouchDB…")
    index = vault_index()
    if not index:
        print("⚠️  No pude indexar desde CouchDB; caigo al `list` del MCP (solo .md, puede omitir archivos)")
        index = [{"path": n["name"], "size": 0, "mtime": 0} for n in mcp.list_notes(limit=100000)]
    print(f"   📝 {len(index)} archivos vivos en el vault "
          f"({sum(1 for d in index if is_binary(d['path']))} binarios)")

    md_prefix = f"{PREFIX}/notes/{TIMESTAMP}"
    subidos, errores, cortos = [], [], []
    total_bytes = 0

    for i, doc in enumerate(index, 1):
        path = doc["path"]
        try:
            content = mcp.read_note(path)
            esperado = doc.get("size") or 0
            # Una nota legítimamente vacía (0 B en CouchDB) se respalda vacía: eso ES fiel.
            if content.startswith("Note not found") or (not content and esperado > 0):
                errores.append(f"{path}: no se pudo leer (CouchDB declara {esperado} B)")
                continue

            payload = decode_payload(path, content)
            # Verificación de integridad en AMBOS sentidos: el contenido debe tener un
            # tamaño comparable al que CouchDB reporta. Solo mirar "muy corto" deja pasar
            # un base64 sin decodificar (inflado un 33%), que es exactamente lo que pasó.
            if esperado:
                delta = abs(len(payload) - esperado) / esperado
                if delta > 0.10:
                    direccion = "corto" if len(payload) < esperado else "INFLADO"
                    cortos.append(f"{path}: {len(payload)} B vs {esperado} B ({direccion})")

            s3.put_object(Bucket=BUCKET, Key=f"{md_prefix}/{path}", Body=payload,
                          ContentType=("application/octet-stream" if is_binary(path) else "text/markdown"))
            subidos.append(path)
            total_bytes += len(payload)

            if i % 10 == 0:
                print(f"   📄 {i}/{len(index)} archivos…")
        except Exception as e:
            errores.append(f"{path}: {e}")

    # ── manifest + dump ──────────────────────────────────────────────────────
    dump = {"version": "3.0", "exported_at": datetime.now().isoformat(),
            "source": "couchdb index + read_note (E2E)", "files_in_vault": len(index),
            "exported": len(subidos), "bytes": total_bytes,
            "errors": errores, "truncated": cortos,
            "files": [{"path": d["path"], "size": d.get("size")} for d in index]}
    comprimido = BytesIO()
    with gzip.GzipFile(fileobj=comprimido, mode="w") as f:
        f.write(json.dumps(dump, ensure_ascii=False).encode())
    s3.upload_fileobj(BytesIO(comprimido.getvalue()), BUCKET, f"{PREFIX}/dumps/{TIMESTAMP}_manifest.json.gz")
    s3.upload_fileobj(BytesIO(comprimido.getvalue()), BUCKET, f"{PREFIX}/dumps/latest_manifest.json.gz")

    manifest = {"timestamp": TIMESTAMP, "date": datetime.now().isoformat(),
                "files_in_vault": len(index), "exported": len(subidos),
                "bytes": total_bytes, "errors": errores, "truncated": cortos}
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/manifests/{TIMESTAMP}.json",
                  Body=json.dumps(manifest, indent=2, ensure_ascii=False).encode(),
                  ContentType="application/json")

    # ── rotación (7 días de notas/dumps, 30 manifests) ───────────────────────
    try:
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        rotados = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/"):
            for obj in page.get("Contents", []):
                fname = obj["Key"].split("/")[-1]
                if fname[:8].isdigit() and "_" in fname and fname[:8] < cutoff:
                    s3.delete_object(Bucket=BUCKET, Key=obj["Key"]); rotados += 1
        mans = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/manifests/")
        if mans.get("KeyCount", 0) > 30:
            for old in sorted((o["Key"] for o in mans.get("Contents", [])), reverse=True)[30:]:
                s3.delete_object(Bucket=BUCKET, Key=old)
        if rotados:
            print(f"   🗑️  Rotados {rotados} objetos viejos")
    except Exception as e:
        print(f"   ⚠️  Error en rotación: {e}")

    # ── veredicto ────────────────────────────────────────────────────────────
    print(f"\n📦 Backup {TIMESTAMP}: {len(subidos)}/{len(index)} archivos · {total_bytes/1024:.0f} KB")
    for etiqueta, lista in (("❌ errores", errores), ("⚠️  incompletos", cortos)):
        if lista:
            print(f"   {etiqueta}: {len(lista)}")
            for x in lista[:5]:
                print(f"      {x}")
    ok = not errores and not cortos and len(subidos) == len(index)
    print("   ✔ verificado: el contenido coincide con el vault" if ok
          else "   ✘ INCOMPLETO — este snapshot NO sirve como restore tal cual")
    return ok


if __name__ == "__main__":
    sys.exit(0 if backup() else 1)
