"""
obsd — daemon persistente del cliente MCP de Obsidian.

POR QUÉ
───────
Medido 2026-09-29: el trabajo real de una escritura es **0.03s**, pero cada
invocation por CLI paga **~1.6s** — 0.7s de import del SDK `mcp` (383ms de
ellos son pydantic generando `mcp_types._types`) + arranque de intérprete.
98% de la latencia esFIXTOS: es la forma de hablar con el server, no el server.

Un daemon con la sesión SSE ya abierta lo borra: el cliente se importa una vez
y la conexión se recicla. Las llamadas por socket Unix cuestan milisegundos.

TRANSPORTE
──────────
Unix socket en ~/.hermes/cache/obsd.sock. Protocolo: JSON por línea.
Request : {"cmd": "<tool>", "args": {...}, "id": <n>}
Response: {"id": <n>, "ok": true, "result": <valor>}   |  {"id": <n>, "ok": false, "error": "..."}

Arranque automático: el cliente intenta connect(); si falla, hace spawn del
daemon y reintenta una vez (con timeout). Si el daemon no puede arrancar,
cae al path directo (lento pero funcional).

SEGURIDAD
─────────
Socket en ~/.hermes/cache/ (0700 en el dir) y permiso 0600 en el socket:
solo el usuario owner puede hablarle. Nunca escucha en TCP.
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

HOME = os.path.expanduser("~")
SOCK = os.path.join(HOME, ".hermes", "cache", "obsd.sock")
CLIENT = os.path.join(HOME, ".hermes", "scripts", "obsidian-mcp-client.py")
INDEX = os.path.join(HOME, ".hermes", "scripts", "obsidian-index.py")

# Tools que el daemon puede ejecutar. Lista blanca: el daemon no debe ser una
# puerta a "ejecutá lo que sea" — solo CRUD/RAG del vault.
ALLOWED = {
    "read_note", "write_note", "delete_note", "edit_note", "move_note",
    "list_notes", "list_folders", "list_tags", "get_note_metadata", "search",
    "rag_search",   # índice LOCAL (no toca el server)
}

_mcpc = None
_idx = None
_lock = threading.Lock()


def load_client():
    global _mcpc
    if _mcpc is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("obsd_mcpc", CLIENT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _mcpc = m
    return _mcpc


def load_index():
    """Importar el módulo del índice con el modelo de embeddings YA cargado.

    El modelo ONNX tarda ~2s en cargar. Se carga una vez al arrancar el
    daemon y vive en memoria: por eso el RAG vía socket es ~0.1s y por
    proceso nuevo es ~2.5s.
    """
    global _idx
    if _idx is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("obsd_idx", INDEX)
        oi = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oi)
        oi._MODEL_HOLDER[0] = oi.embedder()
        _idx = oi
    return _idx


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        rid = None
        req: dict = {}
        try:
            req = json.loads(line)
            rid = req.get("id")
            cmd = req.get("cmd")
            args = req.get("args") or {}
            if cmd not in ALLOWED:
                raise ValueError(f"tool no permitida: {cmd!r}")
            with _lock:  # el pool MCP es una sesion; serializar por seguridad
                if cmd == "rag_search":
                    oi = load_index()
                    result = oi.search(args["query"], args.get("k", 5))
                else:
                    m = load_client()
                    result = getattr(m, cmd)(**args)
            resp = {"id": rid, "ok": True, "result": result}
        except Exception as exc:
            # El server MCP cierra la sesión SSE por inactividad
            # (httpx2.ReadTimeout → "SSE error, reconectando: Connection
            # closed"). El pool del cliente reconecta sola, pero la PRIMERA
            # llamada tras el corte falla. Reintentar una vez convierte un
            # error visible en un éxito transparente — sin esto, la mitad de
            # las operaciones de consolidation fallan con "Connection closed".
            err = f"{type(exc).__name__}: {exc}"
            if ("Connection closed" in err or "ReadTimeout" in err
                    or "BrokenResource" in err) and not req.get("_reintento"):
                req["_reintento"] = True
                try:
                    with _lock:
                        m = load_client()
                        result = getattr(m, cmd)(**args) if cmd != "rag_search" \
                            else load_index().search(args["query"], args.get("k", 5))
                    resp = {"id": rid, "ok": True, "result": result}
                except Exception as exc2:
                    resp = {"id": rid, "ok": False,
                            "error": f"{type(exc2).__name__}: {exc2}"}
            else:
                resp = {"id": rid, "ok": False, "error": err}
        try:
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
        except BrokenPipeError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_UNIX


def serve() -> None:
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    # Pre-cargar el cliente ANTES de escuchar: el primer request no debe pagar
    # el import (y si falla, el daemon no arranca).
    load_client()
    srv = Server(SOCK, Handler)
    os.chmod(SOCK, 0o600)
    print(f"[obsd] escuchando en {SOCK} (pid {os.getpid()})", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        try:
            os.unlink(SOCK)
        except OSError:
            pass


# ── Cliente ──────────────────────────────────────────────────────────────────
def request(cmd: str, args: dict, timeout: float = 120.0, autostart: bool = True) -> dict:
    """Enviar una llamada al daemon. Si no está, arranca uno (autostart)."""
    if not os.path.exists(SOCK) and autostart:
        spawn_daemon()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        if autostart:
            spawn_daemon()
            time.sleep(0.3)
            return request(cmd, args, timeout, autostart=False)
        raise

    try:
        rid = uuid.uuid4().hex[:8]
        payload = json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n"
        s.sendall(payload.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        resp = json.loads(buf.decode())
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "error desconocido"))
        return resp["result"]
    finally:
        s.close()


def spawn_daemon() -> None:
    """Lanzar el daemon en segundo plano, desacoplado del proceso padre."""
    py = sys.executable
    log = os.path.join(HOME, ".hermes", "cache", "obsd.log")
    subprocess.Popen(
        [py, os.path.abspath(__file__), "serve"],
        stdout=open(log, "a"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(50):  # esperar hasta 5s a que aparezca el socket
        if os.path.exists(SOCK):
            return
        time.sleep(0.1)


def direct(cmd: str, args: dict) -> dict:
    """Fallback: llamada directa al cliente, sin daemon."""
    m = load_client()
    return getattr(m, cmd)(**args)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        alive = False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2); s.connect(SOCK); s.close(); alive = True
        except OSError:
            pass
        print(f"obsd: {'corriendo' if alive else 'parado'} ({SOCK})")
        return
    # Modo CLI: obsd <tool> [json-args]
    if len(sys.argv) < 2:
        print("uso: obsd status | obsd serve | obsd <tool> '<json-args>'", file=sys.stderr)
        sys.exit(2)
    tool = sys.argv[1]
    if tool not in ALLOWED:
        print(f"obsd: tool no permitida: {tool}", file=sys.stderr)
        sys.exit(2)
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        out = request(tool, args)
    except Exception as exc:
        print(f"obsd: daemon no disponible ({exc}); fallback directo", file=sys.stderr)
        out = direct(tool, args)
    if isinstance(out, str):
        print(out)
    else:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
