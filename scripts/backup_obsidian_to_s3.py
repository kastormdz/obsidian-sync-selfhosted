#!/usr/bin/env python3
"""
Backup del vault Obsidian → S3 (hermes-configs) via MCP server.
Usa obsidian-mcp-client para leer notas con soporte E2E.
"""
import json, os, sys, gzip, subprocess, re
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import unquote

BUCKET = "hermes-configs"
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
MCP_CLIENT = os.path.expanduser("~/.hermes/scripts/obsidian-mcp-client.py")
PREFIX = "obsidian-vault"

SECRETS_FILE = os.path.expanduser("~/.hermes/.secrets/hermes-backups.env")


def _load_secrets(path: str = SECRETS_FILE) -> None:
    """Carga credenciales desde un archivo 0600 fuera de todo repo.

    Antes estaban hardcodeadas en este script (y en otros dos). El archivo gana
    solo si la variable NO viene ya por entorno.
    """
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


_load_secrets()

S3_CONFIG = {
    "endpoint_url": os.environ.get("S3_ENDPOINT", ""),
    "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", ""),
    "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
    "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
}

if not all((S3_CONFIG["endpoint_url"], S3_CONFIG["aws_access_key_id"], S3_CONFIG["aws_secret_access_key"])):
    sys.exit(
        f"❌ Faltan credenciales S3. Completá {SECRETS_FILE} (0600) "
        "o exportá S3_ENDPOINT / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
    )

def mcp_list():
    """Lista todas las notas via MCP."""
    r = subprocess.run(
        [sys.executable, MCP_CLIENT, "list", "", "200"],
        capture_output=True, text=True, timeout=60
    )
    notes = []
    for line in r.stdout.split('\n'):
        m = re.search(r'📝 (.+)', line)
        if m:
            notes.append(m.group(1).strip())
    return notes

def mcp_read(path):
    """Lee contenido de una nota via MCP."""
    r = subprocess.run(
        [sys.executable, MCP_CLIENT, "read", path],
        capture_output=True, text=True, timeout=30
    )
    output = r.stdout
    # Remove the Obsidian deep link line if present
    if output.startswith('[Open in Obsidian]'):
        parts = output.split('\n---\n', 1)
        if len(parts) > 1:
            return parts[1].strip()
    return output

def backup():
    import boto3
    
    print(f"📥 Listando notas via MCP...")
    note_paths = mcp_list()
    print(f"   📝 {len(note_paths)} notas encontradas")
    
    s3 = boto3.client("s3", **S3_CONFIG)
    uploaded = []
    errors = []
    notes_data = []
    md_prefix = f"{PREFIX}/notes/{TIMESTAMP}"
    
    for i, npath in enumerate(note_paths, 1):
        try:
            content = mcp_read(npath)
            if not content or content.startswith('Exception'):
                # Fallback: metadata-only
                content = f"# {npath}\n\n> Contenido no disponible.\n"
            
            # Build markdown with frontmatter
            full_content = f"""---
path: {npath}
backup_date: {TIMESTAMP}
---

{content}
"""
            # Subir a S3
            s3_key = f"{md_prefix}/{npath}"
            s3.put_object(
                Bucket=BUCKET,
                Key=s3_key,
                Body=full_content.encode('utf-8'),
                ContentType="text/markdown",
            )
            uploaded.append(npath)
            notes_data.append({"path": npath, "size": len(content)})
            
            if i % 10 == 0:
                print(f"   📄 {i}/{len(note_paths)} notas...")
                
        except Exception as e:
            errors.append(f"{npath}: {e}")
            print(f"   ❌ {npath}: {e}")
    
    # Dump comprimido de metadatos
    dump = {
        "version": "2.0",
        "exported_at": datetime.now().isoformat(),
        "source": "mcp://obsidian-sync-mcp",
        "note_count": len(note_paths),
        "notes": notes_data,
        "errors": errors,
    }
    
    try:
        compressed = BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode='w') as f:
            f.write(json.dumps(dump, ensure_ascii=False).encode('utf-8'))
        dump_bytes = compressed.getvalue()
        
        s3.upload_fileobj(BytesIO(dump_bytes), BUCKET, f"{PREFIX}/dumps/{TIMESTAMP}_manifest.json.gz")
        s3.upload_fileobj(BytesIO(dump_bytes), BUCKET, f"{PREFIX}/dumps/latest_manifest.json.gz")
        print(f"   📦 Dump de metadatos ({len(dump_bytes)//1024} KB)")
    except Exception as e:
        errors.append(f"Dump: {e}")
    
    # Manifest
    manifest = {
        "timestamp": TIMESTAMP,
        "date": datetime.now().isoformat(),
        "notes_exported": len(note_paths),
        "md_files": len(uploaded),
        "errors": errors,
    }
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/manifests/{TIMESTAMP}.json",
        Body=json.dumps(manifest, indent=2, ensure_ascii=False),
        ContentType="application/json",
    )
    
    # Rotación 7 días
    try:
        cutoff = datetime.now() - timedelta(days=7)
        cutoff_str = cutoff.strftime("%Y%m%d")
        rotated = 0
        
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                fname = key.split("/")[-1]
                if fname[:8].isdigit() and "_" in fname and fname[:8] < cutoff_str:
                    s3.delete_object(Bucket=BUCKET, Key=key)
                    rotated += 1
        
        # Rotar manifests (max 30)
        manifests = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/manifests/")
        if manifests.get("KeyCount", 0) > 30:
            keys = sorted([o["Key"] for o in manifests.get("Contents", [])], reverse=True)
            for old_key in keys[30:]:
                s3.delete_object(Bucket=BUCKET, Key=old_key)
        
        if rotated > 0:
            print(f"   🗑️ Rotados {rotated} backups viejos")
    except Exception as e:
        print(f"   ⚠️ Error rotación: {e}")
    
    print(f"\n📦 Backup MCP→S3: {TIMESTAMP}")
    print(f"   📝 {len(uploaded)}/{len(note_paths)} notas exportadas")
    if errors:
        print(f"   ❌ {len(errors)} errores")
        for e in errors[:3]:
            print(f"      {e}")
    
    return len(errors) == 0

if __name__ == "__main__":
    success = backup()
    sys.exit(0 if success else 1)
