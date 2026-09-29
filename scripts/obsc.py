"""
obsc — cliente del daemon MCP de Obsidian (y fallback directo).

Medido 2026-09-29: la escritura real cuesta 0.03s, pero llamarlo por CLI
cuesta ~1.6s por el import del SDK `mcp`. Este cliente usa el daemon
obsd.py (sesión SSE abierta) y cae al path directo si el daemon no está.

Uso como librería (preferido — evita el import del SDK por completo):
    from obsc import call
    call("read_note", {"path": "00-INDEX.md"})
    call("write_note", {"path": "x.md", "content": "# hi"})

Ojo: `content` viaja por JSON sobre el socket, así que NO tiene el límite de
argv del shell — ideal para notas grandes.
"""
import json
import os
import socket
import sys
import time
import uuid

HOME = os.path.expanduser("~")
SOCK = os.path.join(HOME, ".hermes", "cache", "obsd.sock")
CLIENT = os.path.join(HOME, ".hermes", "scripts", "obsidian-mcp-client.py")
INDEX = os.path.join(HOME, ".hermes", "scripts", "obsidian-index.py")


def _daemon_alive() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCK)
        s.close()
        return True
    except OSError:
        return False


def _rpc(cmd: str, args: dict, timeout: float = 120.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    try:
        rid = uuid.uuid4().hex[:8]
        s.sendall((json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        resp = json.loads(buf.decode())
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "error desconocido"))
        return resp["result"]
    finally:
        s.close()


def _start_daemon() -> bool:
    import subprocess
    daemon = os.path.join(HOME, ".hermes", "scripts", "obsd.py")
    log = os.path.join(HOME, ".hermes", "cache", "obsd.log")
    subprocess.Popen(
        [sys.executable, daemon, "serve"],
        stdout=open(log, "a"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(50):
        if _daemon_alive():
            return True
        time.sleep(0.1)
    return False


def rag(query: str, k: int = 5, timeout: float = 600.0) -> list:
    """Búsqueda semántica sobre el índice local, servida por el daemon.

    El primer `search` paga la carga del modelo ONNX (~2s); con el daemon
    queda cacheado para siempre. Por eso el RAG también va por el socket y no
    por un proceso nuevo.
    """
    args = {"query": query, "k": k}
    if _daemon_alive():
        return _rpc("rag_search", args, timeout)
    if _start_daemon():
        return _rpc("rag_search", args, timeout)
    import importlib.util
    spec = importlib.util.spec_from_file_location("obsc_idx", INDEX)
    oi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oi)
    oi._MODEL_HOLDER[0] = oi.embedder()
    return oi.search(query, k)


def call(cmd: str, args: dict | None = None, timeout: float = 120.0) -> object:
    """Ejecutar un tool del server MCP vía daemon, con fallback directo."""
    args = args or {}
    if _daemon_alive():
        return _rpc(cmd, args, timeout)
    if _start_daemon():
        return _rpc(cmd, args, timeout)
    # Fallback: proceso separado con el cliente (lento pero funcional)
    import importlib.util
    spec = importlib.util.spec_from_file_location("obsc_mcpc", CLIENT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, cmd)(**args)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "rag":
        # obsc rag <query> [k]
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        k = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 5
        if not q:
            print("uso: obsc rag <query> [k]", file=sys.stderr)
            sys.exit(2)
        t = time.perf_counter()
        res = rag(q, k)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print(f"[{time.perf_counter() - t:.3f}s] {len(res)} resultados",
              file=sys.stderr)
        sys.exit(0)

    if len(sys.argv) < 2:
        print("uso: obsc rag <query> [k] | obsc <tool> '<json-args>'", file=sys.stderr)
        sys.exit(2)
    t = time.perf_counter()
    out = call(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})
    dt = time.perf_counter() - t
    if isinstance(out, str):
        print(out)
    else:
        print(json.dumps(out, ensure_ascii=False))
    print(f"[{dt:.3f}s]", file=sys.stderr)
