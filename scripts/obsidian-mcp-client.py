#!/usr/bin/env python3
"""
Obsidian MCP Client — CRUD + RAG via obsidian-sync-mcp server.

Features:
  - Full CRUD tools via MCP (read/write/edit/move/delete, list, folders, tags, metadata)
  - Session Pooling (singleton _MCPSessionPool, reconecta cada 200 calls o en error)
    * Usa un loop dedicado persistente (Option C) para evitar conflictos entre llamadas
      secuenciales cuando asyncio.run() destruiría el loop entre llamadas.
  - Improved RAG pipeline:
    * Stage 0: Query expansion (Spanish synonyms + snowballstemmer)
    * Stage 1: Candidate collection (name search → word overlap → content scan)
    * Stage 2: Async batch read (asyncio.gather + Semaphore(10))
    * Stage 3: Semantic chunking v2 (H1-H6, code fences, tables, lists, prose)
    * Stage 4: Hybrid scoring (TF-IDF + sentence-transformers paraphrase-multilingual-MiniLM-L12-v2)
    * Stage 5: Enriched ranking (heading level, freshness 60d, backlinks, chunk type boost)
    * Stage 6: LRU cache (32 entries)
  - OAuth 2.1 PKCE flow with HTMLParser (no regex), credential persistence
  - Backward-compatible rag() wrapper: {path, content, score, heading, chunk_type}
  - Full CLI via arguments
  - Logging + type hints throughout
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import importlib
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import unquote  # noqa: F401 — kept for backward compat

import requests
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

BASE: str = "http://localhost:8787"


def _load_auth_token() -> str:
    """Token del MCP: entorno → archivo 0600 → placeholder.

    Antes estaba hardcodeado acá, y por eso la copia del repo tenía que editarse a
    mano en cada sincronización (con riesgo de publicar el token).
    """
    token = os.environ.get("MCP_AUTH_TOKEN")
    if token:
        return token
    for candidate in (
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".secrets", "obsidian-mcp-token"),
        os.path.expanduser("~/.hermes/.secrets/obsidian-mcp-token"),
    ):
        try:
            with open(candidate) as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    return "<your-mcp-auth-token>"


AUTH_TOKEN: str = _load_auth_token()


def _mcp_compose_dir() -> str:
    """Directorio del compose del servidor MCP, preguntándoselo a Docker.

    Evita hardcodear rutas (que en un repo público filtran el layout del host) y
    además sobrevive a que el deploy se mueva de lugar. Se puede forzar con
    OBSIDIAN_MCP_DIR.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "obsidian-mcp", "--format",
             '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'],
            capture_output=True, text=True, timeout=10,
        )
        discovered = r.stdout.strip()
        if discovered and discovered != "<no value>" and os.path.isdir(discovered):
            return discovered
    except Exception:
        pass
    return os.environ.get("OBSIDIAN_MCP_DIR", os.path.expanduser("~/docker/obsidian-mcp"))

logger: logging.Logger = logging.getLogger("obsidian_mcp")
logging.basicConfig(
    level=logging.WARNING,
    format="%(name)s %(levelname)s %(message)s",
)

_oauth = {"cid": None, "csec": None, "token": None}
_OAUTH_FILE = os.path.expanduser("~/.hermes/.mcp_oauth.json")

# Strip the [Open in Obsidian] prepend the MCP server adds on every read
_PREPEND_RE = re.compile(
    r'^\[Open in Obsidian\]\(obsidian://[^)]+\)(?:\n\n---\n\n)?',
    re.MULTILINE
)

def _strip_prepend(text: str) -> str:
    """Remove the [Open in Obsidian] link the MCP server prepends on every read."""
    return _PREPEND_RE.sub('', text).lstrip('\n')


# ═════════════════════════════════════════════════════════════════════════════
# OAuth 2.1 PKCE — HTMLParser-based, no regex for HTML parsing
# ═════════════════════════════════════════════════════════════════════════════


class _FormParser(HTMLParser):
    """HTMLParser that collects all <input> field name/value pairs."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            attr_dict = dict(attrs)
            name = attr_dict.get("name")
            value = attr_dict.get("value", "")
            if name:
                self.fields[name] = value or ""


def _parse_form_fields(html: str) -> dict[str, str]:
    """Parse an HTML page and return all <input> name→value pairs."""
    parser = _FormParser()
    parser.feed(html)
    return parser.fields


def _auth() -> str:
    """Return a valid OAuth access token, running PKCE flow if needed."""
    if _oauth["token"]:
        return _oauth["token"]  # type: ignore[return-value]

    # Load persisted credentials
    if not _oauth["cid"] and os.path.exists(_OAUTH_FILE):
        try:
            with open(_OAUTH_FILE) as f:
                saved = json.load(f)
                _oauth["cid"] = saved["cid"]
                _oauth["csec"] = saved["csec"]
        except Exception as exc:
            logger.warning("No se pudieron cargar credenciales OAuth: %s", exc)

    # Register client if needed
    if not _oauth["cid"]:
        _register_oauth_client()

    # PKCE flow
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    )
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )

    session = requests.Session()

    # Step 1: authorize
    r = session.get(
        f"{BASE}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": _oauth["cid"],
            "redirect_uri": "http://localhost:8787/oauth/callback",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "h",
        },
        timeout=5,
    )

    fields = _parse_form_fields(r.text)
    csrf = fields.get("csrf")
    code = fields.get("code")
    if not csrf or not code:
        raise ValueError(
            f"OAuth: no se encontraron csrf/code en HTML. Campos: {list(fields.keys())}"
        )

    # Step 2: approve
    r2 = session.post(
        f"{BASE}/oauth/approve",
        data={"code": code, "csrf": csrf, "password": AUTH_TOKEN},
        allow_redirects=False,
        timeout=5,
    )
    location = r2.headers.get("Location", "")
    rc_match = re.search(r"code=([^&]+)", location)
    if not rc_match:
        raise ValueError(f"OAuth: no se encontró code en redirect: {location}")
    redirect_code = rc_match.group(1)

    # Step 3: exchange token
    r3 = session.post(
        f"{BASE}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": redirect_code,
            "redirect_uri": "http://localhost:8787/oauth/callback",
            "client_id": _oauth["cid"],
            "client_secret": _oauth["csec"],
            "code_verifier": code_verifier,
        },
        timeout=5,
    )
    _oauth["token"] = r3.json()["access_token"]
    return _oauth["token"]  # type: ignore[return-value]


def _register_oauth_client() -> None:
    """Register a new OAuth client, with automatic MCP container restart on overflow."""
    payload = {
        "client_name": "hermes",
        "redirect_uris": ["http://localhost:8787/oauth/callback"],
    }
    resp = requests.post(f"{BASE}/oauth/register", json=payload, timeout=5).json()

    if "client_id" in resp:
        _oauth["cid"] = resp["client_id"]
        _oauth["csec"] = resp["client_secret"]
        os.makedirs(os.path.dirname(_OAUTH_FILE), exist_ok=True)
        with open(_OAUTH_FILE, "w") as f:
            json.dump({"cid": _oauth["cid"], "csec": _oauth["csec"]}, f)
        return

    # Too many clients — clean data dir and restart
    compose_dir = _mcp_compose_dir()
    data_dir = os.path.join(compose_dir, "data")
    if os.path.exists(data_dir):
        for fname in os.listdir(data_dir):
            fpath = os.path.join(data_dir, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)
    logger.warning("Demasiados clientes OAuth — limpiando data dir y reiniciando container")
    print("⚠️ Limpiando datos MCP y reintentando...", file=sys.stderr)
    subprocess.run(
        ["docker", "compose", "restart"],
        cwd=compose_dir,
        capture_output=True,
        timeout=30,
    )
    time.sleep(4)

    resp2 = requests.post(f"{BASE}/oauth/register", json=payload, timeout=5).json()
    if "client_id" in resp2:
        _oauth["cid"] = resp2["client_id"]
        _oauth["csec"] = resp2["client_secret"]
        os.makedirs(os.path.dirname(_OAUTH_FILE), exist_ok=True)
        with open(_OAUTH_FILE, "w") as f:
            json.dump({"cid": _oauth["cid"], "csec": _oauth["csec"]}, f)
    else:
        raise RuntimeError(f"Registro OAuth falló tras reinicio: {resp2}")


# ═════════════════════════════════════════════════════════════════════════════
# Session Pool — Singleton con loop dedicado (Option C)
#
# PROBLEMA ORIGINAL: asyncio.run(coro) crea y destruye el event loop en cada
# llamada. El asyncio.Lock y el cliente SSE están atados al loop anterior, por
# lo que la próxima llamada intenta reusar objetos de un loop ya cerrado.
#
# SOLUCIÓN (Option C): Un único event loop creado UNA SOLA VEZ en un hilo
# background. Todas las operaciones async van a ese loop via
# loop.run_until_complete(). threading.Lock protege el acceso concurrente
# desde el hilo principal.
# ═════════════════════════════════════════════════════════════════════════════


class _MCPSessionPool:
    """
    Singleton pool que recicla la sesión MCP SSE.

    Usa un asyncio event loop DEDICADO y persistente para evitar el bug donde
    asyncio.run() destruye el loop entre llamadas secuenciales, invalidando el
    asyncio.Lock y el cliente SSE que están atados al loop anterior.

    Reconecta automáticamente si hay error o cada MAX_CALLS llamadas.
    El acceso concurrente desde hilos externos está protegido con threading.Lock.
    """

    _MAX_CALLS: int = 200

    def __init__(self) -> None:
        self._session: Optional[ClientSession] = None
        self._sse_ctx: Any = None
        self._session_ctx: Any = None
        # threading.Lock (no asyncio.Lock) para proteger acceso desde el hilo principal
        self._lock: threading.Lock = threading.Lock()
        self._calls: int = 0
        # Loop dedicado: creado una vez, nunca destruido hasta cleanup
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

    async def _connect(self) -> None:
        """Open a fresh SSE + ClientSession connection (runs in self._loop)."""
        headers = {"Authorization": f"Bearer {_auth()}"}
        self._sse_ctx = sse_client(f"{BASE}/sse", headers=headers)
        read, write = await self._sse_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        session: ClientSession = await self._session_ctx.__aenter__()
        await session.initialize()
        self._session = session
        self._calls = 0
        logger.debug("SSE pool conectado (loop dedicado id=%d)", id(self._loop))

    async def _disconnect(self) -> None:
        """Tear down the current SSE connection gracefully (runs in self._loop)."""
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._sse_ctx is not None:
                await self._sse_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None
        self._session_ctx = None
        self._sse_ctx = None

    async def _do_call(self, name: str, args: dict[str, Any]) -> Any:
        """Internal async tool call. Must run inside self._loop."""
        if self._session is None or self._calls >= self._MAX_CALLS:
            await self._disconnect()
            await self._connect()
        try:
            result = await self._session.call_tool(name, arguments=args)  # type: ignore[union-attr]
            self._calls += 1
            return result
        except Exception as exc:
            logger.warning("SSE error, reconectando: %s", exc)
            await self._disconnect()
            raise

    def call(self, name: str, args: Optional[dict[str, Any]] = None) -> Any:
        """
        Execute a tool call over the pooled SSE session (synchronous, thread-safe).
        Reconnects if session is stale or call limit reached.
        """
        with self._lock:
            return self._loop.run_until_complete(self._do_call(name, args or {}))

    async def _do_close(self) -> None:
        """Internal async close. Must run inside self._loop."""
        await self._disconnect()

    def close(self) -> None:
        """Explicitly close the pool connection (synchronous, thread-safe)."""
        with self._lock:
            try:
                self._loop.run_until_complete(self._do_close())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    def run_in_loop(self, coro: Any) -> Any:
        """
        Run an arbitrary coroutine in the pool's dedicated loop.
        Used by batch readers that need to share the same session.
        """
        with self._lock:
            return self._loop.run_until_complete(coro)


_pool: _MCPSessionPool = _MCPSessionPool()


async def _call(name: str, args: Optional[dict[str, Any]] = None) -> Any:
    """
    Coroutine tool call via session pool.

    Llama a _pool._do_call() directamente (sin lock de threading) porque esta
    coroutine siempre se ejecuta dentro de _pool.run_in_loop(), que ya tiene
    el threading.Lock adquirido.
    """
    return await _pool._do_call(name, args or {})


def _cleanup_pool() -> None:
    """atexit handler: close the SSE pool and dedicated loop on interpreter exit."""
    try:
        _pool.close()
    except Exception:
        pass


atexit.register(_cleanup_pool)


def _run(coro: Any) -> Any:
    """
    Run a coroutine synchronously using the pool's dedicated persistent loop.

    IMPORTANTE: NO usa asyncio.run() para evitar que el loop se destruya entre
    llamadas secuenciales, lo que invalida el cliente SSE y el Lock del pool.
    El threading.Lock se adquiere aqui via run_in_loop().
    """
    return _pool.run_in_loop(coro)


# ═════════════════════════════════════════════════════════════════════════════
# CRUD Tools
# ═════════════════════════════════════════════════════════════════════════════


def list_notes(**kwargs: Any) -> list[dict[str, str]]:
    """
    List notes from the vault.

    Args:
        folder: Filter by folder path (optional)
        limit: Maximum number of notes to return (optional)
        tag: Filter by tag (optional)

    Returns:
        List of {name, url} dicts.
    """
    params: dict[str, Any] = {k: v for k, v in kwargs.items() if v is not None}
    r = _run(_call("list_notes", params))
    notes: list[dict[str, str]] = []
    for c in r.content:
        if hasattr(c, "text"):
            for line in c.text.strip().split("\n"):
                m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", line)
                if m:
                    notes.append({"name": m.group(1), "url": m.group(2)})
    return notes


def read_note(path: str) -> str:
    """
    Read a note's full content.

    Args:
        path: Vault-relative path to the note.

    Returns:
        Note content as plain text (prepend stripped).
    """
    r = _run(_call("read_note", {"path": path}))
    text = ""
    for c in r.content:
        if hasattr(c, "text"):
            text += c.text
    return _strip_prepend(text)


def write_note(path: str, content: str) -> str:
    """
    Create or overwrite a note.

    Args:
        path: Vault-relative path to the note.
        content: Full content to write.

    Returns:
        Server response string.
    """
    r = _run(_call("write_note", {"path": path, "content": content}))
    _notes_cache_clear()
    _rag_cache_clear()
    return str(r)


def delete_note(path: str) -> str:
    """
    Delete a note from the vault.

    Args:
        path: Vault-relative path to the note.

    Returns:
        Server response string.
    """
    r = _run(_call("delete_note", {"path": path}))
    _notes_cache_clear()
    _rag_cache_clear()
    return str(r)


def edit_note(
    path: str,
    content: str,
    operation: str = "append",
    old_text: Optional[str] = None,
) -> str:
    """
    Edit an existing note.

    Args:
        path: Vault-relative path to the note.
        content: Text to insert or replace with.
        operation: One of 'append', 'prepend', or 'replace'.
        old_text: For 'replace' operation, the text to be replaced.

    Returns:
        Server response string.
    """
    args: dict[str, Any] = {"path": path, "content": content, "operation": operation}
    if old_text:
        args["old_text"] = old_text
    r = _run(_call("edit_note", args))
    _notes_cache_clear()
    _rag_cache_clear()
    return str(r)


def move_note(from_path: str, to_path: str) -> str:
    """
    Move or rename a note.

    Args:
        from_path: Current vault-relative path.
        to_path: Destination vault-relative path.

    Returns:
        Server response string.
    """
    r = _run(_call("move_note", {"from": from_path, "to": to_path}))
    _notes_cache_clear()
    _rag_cache_clear()
    return str(r)


def list_folders() -> str:
    """
    List all folders in the vault.

    Returns:
        Newline-separated folder list.
    """
    r = _run(_call("list_folders"))
    for c in r.content:
        if hasattr(c, "text"):
            return c.text.strip()
    return ""


def list_tags() -> str:
    """
    List all tags used in the vault.

    Returns:
        Newline-separated tag list.
    """
    r = _run(_call("list_tags"))
    for c in r.content:
        if hasattr(c, "text"):
            return c.text.strip()
    return ""


def get_note_metadata(path: str) -> str:
    """
    Retrieve metadata (frontmatter, tags, backlinks) for a note.

    Args:
        path: Vault-relative path to the note.

    Returns:
        Metadata as formatted text.
    """
    r = _run(_call("get_note_metadata", {"path": path}))
    for c in r.content:
        if hasattr(c, "text"):
            return c.text.strip()
    return ""


def search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """
    Search notes by name substring match.

    Args:
        query: Search string.
        limit: Maximum number of results.

    Returns:
        List of {name, url} dicts.
    """
    notes = _cached_list_notes()
    q = query.lower()
    results: list[dict[str, str]] = []
    for n in notes:
        if q in n["name"].lower():
            results.append(n)
    return results[:limit]


# ═════════════════════════════════════════════════════════════════════════════
# LRU Cache for RAG query results (Stage 6)
# ═════════════════════════════════════════════════════════════════════════════

_RAG_CACHE: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_RAG_CACHE_MAX: int = 32


def _rag_cache_get(key: str) -> Optional[list[dict[str, Any]]]:
    """Return cached result for key, or None if not present."""
    if key in _RAG_CACHE:
        _RAG_CACHE.move_to_end(key)
        return _RAG_CACHE[key]
    return None


def _rag_cache_set(key: str, value: list[dict[str, Any]]) -> None:
    """Insert or update cache entry, evicting LRU if at capacity."""
    if key in _RAG_CACHE:
        _RAG_CACHE.move_to_end(key)
    else:
        if len(_RAG_CACHE) >= _RAG_CACHE_MAX:
            _RAG_CACHE.popitem(last=False)
        _RAG_CACHE[key] = value


def _rag_cache_clear() -> None:
    """Clear all cached RAG results."""
    _RAG_CACHE.clear()


# ═════════════════════════════════════════════════════════════════════════════
# Notes-list cache with TTL — avoids repeated MCP round-trips in RAG pipelines.
# Invalidated on every write operation.
# ═════════════════════════════════════════════════════════════════════════════

_NOTES_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_NOTES_CACHE_TTL: float = 15.0  # seconds


def _notes_cache_clear() -> None:
    """Invalidate the notes-list cache (called after any write op)."""
    _NOTES_CACHE.clear()


def _cached_list_notes() -> list[dict[str, str]]:
    """list_notes() with TTL cache. Falls back to a live call on any error.
    Pide TODO el vault (limit alto): el server por defecto capa a 100 notas y
    sin esto busca solo en una fracción (notas fuera de las primeras 100 invisibles)."""
    now = time.time()
    hit = _NOTES_CACHE.get("all")
    if hit and (now - hit[0]) < _NOTES_CACHE_TTL:
        return hit[1]
    notes = list_notes(limit=100000)
    _NOTES_CACHE["all"] = (now, notes)
    return notes


# ═════════════════════════════════════════════════════════════════════════════
# Semantic Chunking v2 (Mejora 2)
# ═════════════════════════════════════════════════════════════════════════════

_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE: re.Pattern[str] = re.compile(r"^```|^~~~")
_TABLE_ROW_RE: re.Pattern[str] = re.compile(r"^\|.+\|")
_LIST_RE: re.Pattern[str] = re.compile(r"^(\s*)([-*+]|\d+\.)\s")


def _chunk_semantic(content: str, min_chars: int = 80) -> list[dict[str, Any]]:
    """
    Semantic chunker v2 that recognises:
      - Headings H1-H6  → chunk boundary + heading_level metadata
      - Code fences (``` / ~~~) → own chunk, preserves language hint
      - Markdown tables  → own chunk
      - Lists (ul/ol)   → own chunk, items grouped together
      - Prose/paragraphs → chunk with forward-merge if < min_chars

    Each chunk dict: {heading, text, score, chunk_type, heading_level}

    Args:
        content: Raw markdown text of a note.
        min_chars: Minimum character length before forward-merging prose chunks.

    Returns:
        List of chunk dicts with metadata.
    """
    if not content or len(content.strip()) < min_chars:
        return [
            {
                "heading": "",
                "text": content.strip(),
                "score": 0.0,
                "chunk_type": "prose",
                "heading_level": 0,
            }
        ]

    lines = content.split("\n")
    chunks: list[dict[str, Any]] = []
    current_heading: str = ""
    current_heading_level: int = 0
    current_type: str = "prose"
    buffer: list[str] = []
    in_fence: bool = False
    in_table: bool = False
    in_list: bool = False

    def _flush(
        buf: list[str], h: str, hlevel: int, ctype: str
    ) -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        if ctype == "code" and len(text) > 4000:
            # Split huge code fences into ~3KB line-aligned segments so the
            # RAG can match content in the middle/end of a long script.
            lines_buf = text.split("\n")
            seg: list[str] = []
            seg_len = 0
            for ln in lines_buf:
                seg.append(ln)
                seg_len += len(ln) + 1
                if seg_len >= 3000:
                    chunks.append(
                        {
                            "heading": h,
                            "text": "\n".join(seg).strip(),
                            "score": 0.0,
                            "chunk_type": ctype,
                            "heading_level": hlevel,
                        }
                    )
                    seg = []
                    seg_len = 0
            if seg:
                chunks.append(
                    {
                        "heading": h,
                        "text": "\n".join(seg).strip(),
                        "score": 0.0,
                        "chunk_type": ctype,
                        "heading_level": hlevel,
                    }
                )
            return
        chunks.append(
            {
                "heading": h,
                "text": text,
                "score": 0.0,
                "chunk_type": ctype,
                "heading_level": hlevel,
            }
        )

    for line in lines:
        stripped = line.strip()

        # ── Code fence toggle ──────────────────────────────────────────────
        if _FENCE_RE.match(stripped):
            if not in_fence:
                _flush(buffer, current_heading, current_heading_level, current_type)
                buffer = [line]
                in_fence = True
                in_table = False
                in_list = False
                current_type = "code"
            else:
                buffer.append(line)
                _flush(buffer, current_heading, current_heading_level, "code")
                buffer = []
                in_fence = False
                current_type = "prose"
            continue

        if in_fence:
            buffer.append(line)
            continue

        # ── Heading ────────────────────────────────────────────────────────
        m = _HEADING_RE.match(stripped)
        if m:
            _flush(buffer, current_heading, current_heading_level, current_type)
            buffer = []
            in_table = False
            in_list = False
            current_heading = m.group(2).strip()
            current_heading_level = len(m.group(1))
            current_type = "prose"
            continue

        # ── Table row ──────────────────────────────────────────────────────
        if _TABLE_ROW_RE.match(stripped):
            if not in_table:
                _flush(buffer, current_heading, current_heading_level, current_type)
                buffer = []
                in_table = True
                in_list = False
                current_type = "table"
            buffer.append(line)
            continue
        else:
            if in_table and stripped:
                _flush(buffer, current_heading, current_heading_level, "table")
                buffer = []
                in_table = False
                current_type = "prose"

        # ── List item ──────────────────────────────────────────────────────
        if _LIST_RE.match(line):
            if not in_list:
                _flush(buffer, current_heading, current_heading_level, current_type)
                buffer = []
                in_list = True
                current_type = "list"
            buffer.append(line)
            continue
        else:
            if in_list and stripped:
                _flush(buffer, current_heading, current_heading_level, "list")
                buffer = []
                in_list = False
                current_type = "prose"

        buffer.append(line)

    _flush(buffer, current_heading, current_heading_level, current_type)

    # Forward-merge small prose chunks to avoid tiny fragments
    merged: list[dict[str, Any]] = []
    pending: Optional[dict[str, Any]] = None
    for ch in chunks:
        if pending is None:
            pending = ch
        elif (
            len(pending["text"]) < min_chars
            and pending["chunk_type"] == "prose"
            and ch["chunk_type"] == "prose"
        ):
            pending["text"] += "\n\n" + ch["text"]
        else:
            merged.append(pending)
            pending = ch
    if pending:
        merged.append(pending)

    return merged or [
        {
            "heading": "",
            "text": content.strip()[:2000],
            "score": 0.0,
            "chunk_type": "prose",
            "heading_level": 0,
        }
    ]


# ═════════════════════════════════════════════════════════════════════════════
# TF-IDF Scorer
# ═════════════════════════════════════════════════════════════════════════════

_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ0-9]+")


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens including Spanish chars."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _compute_tfidf(
    query: str, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Rank chunks by TF-IDF similarity against query. Mutates score in-place.

    Args:
        query: Search query string.
        chunks: List of chunk dicts to score.

    Returns:
        Same chunks list with updated 'score' values.
    """
    q_tokens = _tokenize(query)
    if not q_tokens:
        for c in chunks:
            c["score"] = 0.0
        return chunks

    n_chunks = len(chunks)
    if n_chunks == 0:
        return chunks

    chunk_tokens = [_tokenize(c["text"]) for c in chunks]
    all_chunk_sets = [set(t) for t in chunk_tokens]
    unique_terms = set(q_tokens)

    idf: dict[str, float] = {}
    for term in unique_terms:
        df = sum(1 for cs in all_chunk_sets if term in cs)
        idf[term] = math.log1p(n_chunks / (1 + df))

    for i, c in enumerate(chunks):
        tokens = chunk_tokens[i]
        if not tokens:
            c["score"] = 0.0
            continue
        n_tokens = len(tokens)
        score = 0.0
        for term in unique_terms:
            tf = tokens.count(term) / n_tokens
            score += tf * idf.get(term, 0.0)
        c["score"] = score / len(unique_terms)

    return chunks


# ═════════════════════════════════════════════════════════════════════════════
# Query Expansion — Spanish synonyms + snowballstemmer (Mejora 4)
# ═════════════════════════════════════════════════════════════════════════════

_SYNONYMS: dict[str, list[str]] = {
    "reunion":    ["meeting", "encuentro", "junta", "sesion"],
    "meeting":    ["reunion", "encuentro", "junta"],
    "tarea":      ["task", "pendiente", "todo", "accion"],
    "task":       ["tarea", "pendiente", "todo"],
    "proyecto":   ["project", "iniciativa"],
    "project":    ["proyecto", "iniciativa"],
    "nota":       ["note", "apunte"],
    "note":       ["nota", "apunte"],
    "trabajo":    ["job", "laboral", "empleo"],
    "idea":       ["concepto", "propuesta", "sugerencia"],
    "error":      ["bug", "fallo", "fail", "issue"],
    "bug":        ["error", "fallo", "fail", "issue"],
    "instalar":   ["install", "setup", "deploy", "configurar"],
    "docker":     ["contenedor", "container"],
    "container":  ["docker", "contenedor"],
    "servidor":   ["server", "host", "maquina"],
    "server":     ["servidor", "host"],
    "archivo":    ["file", "fichero"],
    "file":       ["archivo", "fichero"],
    "config":     ["configuracion", "conf", "settings", "ajustes"],
    "usuario":    ["user", "cuenta"],
    "user":       ["usuario", "cuenta"],
    "password":   ["pass", "clave", "secret", "contrasena"],
    "clave":      ["password", "pass", "secret"],
    "base":       ["database", "db", "datos"],
    "database":   ["base", "db", "datos"],
    "script":     ["programa", "codigo", "automatizacion"],
    "red":        ["network", "lan", "vlan"],
    "network":    ["red", "lan"],
    "backup":     ["respaldo", "copia"],
    "respaldo":   ["backup", "copia"],
    "log":        ["registro", "logs", "bitacora"],
    "registro":   ["log", "logs"],
}

_STEMMER: Any = None


def _get_stemmer() -> Any:
    """Lazy-load snowballstemmer for Spanish. Returns None if unavailable."""
    global _STEMMER
    if _STEMMER is None:
        try:
            from snowballstemmer import stemmer  # type: ignore[import]
            _STEMMER = stemmer("spanish")
        except ImportError:
            logger.info("snowballstemmer no disponible — sin stemming")
            _STEMMER = False  # sentinel to avoid repeated import attempts
    return _STEMMER if _STEMMER is not False else None


def _expand_query(query: str) -> list[str]:
    """
    Expand query with Spanish synonyms and optional snowball stemming.

    Args:
        query: Original search query.

    Returns:
        List of expanded terms (original tokens + synonyms + stems).
    """
    st = _get_stemmer()
    original_tokens = _tokenize(query)
    expanded: set[str] = set(original_tokens)

    for tok in original_tokens:
        # Synonym expansion
        if tok in _SYNONYMS:
            expanded.update(_SYNONYMS[tok])
        # Stemming
        if st:
            stem: str = st.stemWord(tok)
            if stem != tok:
                expanded.add(stem)

    return list(expanded)


# ═════════════════════════════════════════════════════════════════════════════
# Hybrid Search — TF-IDF + sentence-transformers (Mejora 3)
# ═════════════════════════════════════════════════════════════════════════════


class _EmbeddingBackend:
    """
    Lazy-load wrapper for sentence-transformers.
    Gracefully degrades to TF-IDF-only if the library is not installed.
    Model: paraphrase-multilingual-MiniLM-L12-v2
    """

    _model: Any = None
    _available: Optional[bool] = None

    @classmethod
    def available(cls) -> bool:
        """Return True if sentence-transformers can be imported."""
        if cls._available is None:
            try:
                importlib.import_module("sentence_transformers")
                cls._available = True
            except ImportError:
                cls._available = False
                logger.info("sentence-transformers no disponible → solo TF-IDF")
        return cls._available  # type: ignore[return-value]

    @classmethod
    def model(cls) -> Any:
        """Return the loaded SentenceTransformer model, loading it if necessary."""
        if cls._model is None and cls.available():
            from sentence_transformers import SentenceTransformer  # type: ignore[import]

            cls._model = SentenceTransformer(
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                device="cpu",
            )
        return cls._model

    @classmethod
    def encode(cls, texts: list[str]) -> Any:
        """
        Encode a list of texts into embedding vectors.

        Args:
            texts: Texts to encode.

        Returns:
            numpy array of shape (len(texts), dim), or None if unavailable.
        """
        m = cls.model()
        if m is None:
            return None
        return m.encode(
            texts,
            batch_size=min(len(texts), 32),
            show_progress_bar=False,
        )


def _score_hybrid(
    query: str,
    chunks: list[dict[str, Any]],
    alpha: float = 0.6,
) -> list[dict[str, Any]]:
    """
    Hybrid scoring: alpha * TF-IDF + (1 - alpha) * cosine(sentence embeddings).
    Falls back to alpha=1.0 (pure TF-IDF) if sentence-transformers is unavailable.

    Args:
        query: Search query.
        chunks: List of chunk dicts to score.
        alpha: Weight for TF-IDF component (0.0–1.0). Default 0.6.

    Returns:
        Same chunks list with updated 'score' values.
    """
    chunks = _compute_tfidf(query, chunks)

    if not _EmbeddingBackend.available():
        return chunks

    try:
        texts = [c["text"] for c in chunks]
        query_emb = _EmbeddingBackend.encode([query])
        chunk_embs = _EmbeddingBackend.encode(texts)

        if query_emb is None or chunk_embs is None:
            return chunks

        import numpy as np  # type: ignore[import]

        q_norm = query_emb / (np.linalg.norm(query_emb) + 1e-10)
        c_norm = chunk_embs / (np.linalg.norm(chunk_embs, axis=1, keepdims=True) + 1e-10)
        similarities = (c_norm @ q_norm.T).flatten()

        for i, c in enumerate(chunks):
            tfidf_score = c["score"]
            cosine_score = float(similarities[i])
            c["score"] = alpha * tfidf_score + (1.0 - alpha) * max(0.0, cosine_score)

    except Exception as exc:
        logger.debug("Hybrid scoring error (fallback a TF-IDF): %s", exc)

    return chunks


# ═════════════════════════════════════════════════════════════════════════════
# Enriched Ranking — heading level, freshness, backlinks, chunk type (Mejora 5)
# ═════════════════════════════════════════════════════════════════════════════


def _boost_score(
    chunks: list[dict[str, Any]],
    note_meta: dict[str, Any],
    query_terms: set[str],
) -> list[dict[str, Any]]:
    """
    Apply signal boosts on top of the base TF-IDF/hybrid score:

    - Heading level:  H1 +0.15, H2 +0.10, H3 +0.05
    - Exact heading match: up to +0.20 (proportional to term overlap)
    - Freshness decay: exponential decay over 60 days, max +0.10
    - Backlinks: log-scaled boost up to +0.08 (normalized to 50 backlinks)
    - Chunk type: code/table +0.02, list +0.01

    Args:
        chunks: List of chunk dicts with base scores.
        note_meta: Metadata dict for the note (mtime, backlink_count, etc.).
        query_terms: Set of query tokens to check heading overlap against.

    Returns:
        Same chunks list with boosted 'score' values.
    """
    for c in chunks:
        base = c.get("score", 0.0)
        boosts = 0.0

        # Heading level boost
        level = c.get("heading_level", 0)
        boosts += {1: 0.15, 2: 0.10, 3: 0.05}.get(level, 0.0)

        # Exact match in heading
        heading_text = c.get("heading", "").lower()
        if heading_text and query_terms:
            heading_words = set(_tokenize(heading_text))
            overlap = len(query_terms & heading_words) / len(query_terms)
            boosts += 0.20 * overlap

        # Freshness — exponential decay with 60-day half-life
        mtime = note_meta.get("mtime") or note_meta.get("updatedAt")
        if mtime:
            try:
                if isinstance(mtime, str):
                    t = time.mktime(time.strptime(mtime[:19], "%Y-%m-%dT%H:%M:%S"))
                else:
                    t = float(mtime)
                days_old = (time.time() - t) / 86400.0
                boosts += 0.10 * math.exp(-days_old / 60.0)
            except (ValueError, OSError):
                pass

        # Backlinks — log-scaled, normalized to 50 backlinks = max
        bl_raw = note_meta.get("backlink_count", 0)
        bl = bl_raw if isinstance(bl_raw, (int, float)) else 0
        if bl > 0:
            boosts += 0.08 * math.log1p(bl) / math.log1p(50)

        # Chunk type bonus
        ctype = c.get("chunk_type", "prose")
        boosts += {"code": 0.02, "table": 0.02, "list": 0.01}.get(ctype, 0.0)

        c["score"] = base + boosts

    return chunks


# ═════════════════════════════════════════════════════════════════════════════
# Async Batch — parallel note reads (Mejora 6)
# ═════════════════════════════════════════════════════════════════════════════


async def _read_notes_batch(
    note_names: list[str],
    concurrency: int = 10,
) -> dict[str, str]:
    """
    Read N notes in parallel, bounded by a semaphore.

    Args:
        note_names: List of vault-relative note paths to read.
        concurrency: Maximum parallel reads (default 10).

    Returns:
        Dict mapping note name → full content string.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _read_one(name: str) -> tuple[str, str]:
        async with sem:
            try:
                r = await _pool._do_call("read_note", {"path": name})
                text = ""
                for c in r.content:
                    if hasattr(c, "text"):
                        text += c.text
                return name, text
            except Exception as exc:
                logger.debug("Error leyendo %s: %s", name, exc)
                return name, ""

    results = await asyncio.gather(*[_read_one(n) for n in note_names])
    return dict(results)


def _run_batch_read(note_names: list[str]) -> dict[str, str]:
    """
    Synchronous wrapper around _read_notes_batch() for use in rag_improved().

    Usa el loop dedicado del pool para evitar conflictos con asyncio.run().

    Args:
        note_names: List of vault-relative note paths.

    Returns:
        Dict mapping note name → content string.
    """
    return _pool.run_in_loop(_read_notes_batch(note_names))


# ═════════════════════════════════════════════════════════════════════════════
# Full RAG Pipeline (Mejoras 0-6 integradas)
# ═════════════════════════════════════════════════════════════════════════════


def rag_improved(
    query: str,
    max_results: int = 5,
    max_candidates: int = 20,
    use_cache: bool = True,
    hybrid: bool = True,
    use_expansion: bool = True,
    use_boost: bool = True,
) -> list[dict[str, Any]]:
    """
    Full 7-stage RAG pipeline with all improvements integrated.

    Stage 0: Query expansion (Spanish synonyms + snowballstemmer)
    Stage 1: Candidate collection (name search → word overlap → content scan)
    Stage 2: Async batch read via asyncio.gather + Semaphore(10)
    Stage 3: Semantic chunking v2 (H1-H6, code, table, list, prose)
    Stage 4: Hybrid scoring (TF-IDF + sentence-transformers, alpha=0.6)
    Stage 5: Enriched ranking (heading level, freshness, backlinks, chunk type)
    Stage 6: LRU cache (32 entries)

    Args:
        query: Search query string.
        max_results: Number of top chunks to return.
        max_candidates: Maximum notes to consider as candidates.
        use_cache: Enable LRU result caching.
        hybrid: Enable hybrid TF-IDF + embedding scoring.
        use_expansion: Enable query expansion.
        use_boost: Enable enriched ranking boosts.

    Returns:
        List of chunk dicts: {path, name, heading, text, score, chunk_type, heading_level}
    """
    cache_key = query.strip().lower()

    # ── Stage 6: LRU cache check ─────────────────────────────────────────────
    if use_cache:
        cached = _rag_cache_get(cache_key)
        if cached is not None:
            logger.debug("RAG cache hit: %s", cache_key)
            return cached[:max_results]

    # ── Stage 0: Query expansion ─────────────────────────────────────────────
    if use_expansion:
        expanded_terms = _expand_query(cache_key)
    else:
        expanded_terms = _tokenize(cache_key)
    expanded_query = " ".join(expanded_terms)
    query_terms_set: set[str] = set(expanded_terms)

    logger.debug("Query expandida: %r → %d términos", query, len(expanded_terms))

    # ── Stage 1: Candidate collection — BUSCA POR TODO (nombre + contenido) ──
    name_candidates: list[dict[str, str]] = search(expanded_query, max_candidates)
    seen_names: set[str] = {n["name"] for n in name_candidates}
    all_notes = _cached_list_notes()

    # 1a) Word overlap on note names (candidatos adicionales por palabras del nombre)
    name_scored: list[tuple[int, dict[str, str]]] = []
    for n in all_notes:
        if n["name"] in seen_names:
            continue
        name_words = set(re.findall(r"[a-z0-9]+", n["name"].lower()))
        overlap = len(query_terms_set & name_words)
        if overlap > 0:
            name_scored.append((overlap, n))
    name_scored.sort(key=lambda x: -x[0])
    for _, n in name_scored:
        if n["name"] not in seen_names:
            name_candidates.append(n)
            seen_names.add(n["name"])

    # 1b) Content scan SIEMPRE sobre TODAS las notas (buscar por TODO, no solo fallback)
    scan_list = [n for n in all_notes if n["name"] not in seen_names]
    content_matches: list[dict[str, str]] = []
    if scan_list:
        try:
            contents = _run_batch_read([n["name"] for n in scan_list])
        except Exception:
            contents = {}
            for n in scan_list:
                try:
                    contents[n["name"]] = read_note(n["name"])
                except Exception:
                    contents[n["name"]] = ""
        for n in scan_list:
            content_lower = (contents.get(n["name"]) or "").lower()
            if any(t in content_lower for t in query_terms_set if len(t) > 2):
                content_matches.append(n)
    candidates = name_candidates + content_matches

    # Deduplicate (cap generoso: el ranking final decide, nada se pierde temprano)
    seen_dedup: set[str] = set()
    candidates_dedup: list[dict[str, str]] = []
    for n in candidates:
        if n["name"] not in seen_dedup:
            seen_dedup.add(n["name"])
            candidates_dedup.append(n)
    candidates = candidates_dedup[:max(60, max_candidates)]

    if not candidates:
        return []

    logger.debug("RAG: %d candidatos seleccionados", len(candidates))

    # ── Stage 2: Async batch read ─────────────────────────────────────────────
    note_names = [n["name"] for n in candidates]
    contents: dict[str, str] = _run_batch_read(note_names)

    # ── Stage 3: Semantic chunking ────────────────────────────────────────────
    all_chunks: list[dict[str, Any]] = []
    note_meta_cache: dict[str, dict[str, Any]] = {}

    for n in candidates:
        note_content = contents.get(n["name"], "")
        if not note_content or len(note_content.strip()) < 20:
            continue

        # Fetch metadata for enriched ranking
        meta: dict[str, Any] = {}
        if use_boost:
            try:
                meta_str = get_note_metadata(n["name"])
                for line in meta_str.split("\n"):
                    if "Size:" in line:
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            meta["size"] = parts[1].strip()
                    if "Modified:" in line:
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            mtime_raw = parts[1].strip()
                            # Strip timezone offset for strptime compat
                            meta["mtime"] = mtime_raw[:19] if len(mtime_raw) > 19 else mtime_raw
            except Exception:
                pass
        note_meta_cache[n["name"]] = meta

        note_chunks = _chunk_semantic(note_content)
        for ch in note_chunks:
            ch["path"] = n["name"]
            ch["name"] = n["name"]
        all_chunks.extend(note_chunks)

    if not all_chunks:
        return []

    logger.debug("RAG: %d chunks generados", len(all_chunks))

    # ── Stage 4: Hybrid scoring ───────────────────────────────────────────────
    if hybrid:
        all_chunks = _score_hybrid(expanded_query, all_chunks)
    else:
        all_chunks = _compute_tfidf(expanded_query, all_chunks)

    # ── Stage 4.5: Boost por coincidencia de NOMBRE ─────────────────────────
    # Una nota cuyo NOMBRE matchea algún término de la query es más relevante
    # que una mención suelta en el contenido de otra → boost ×1.35
    for ch in all_chunks:
        name_l = ch.get("name", "").lower()
        if any(t in name_l for t in query_terms_set if len(t) > 2):
            ch["score"] = ch.get("score", 0) * 1.35

    # ── Stage 5: Enriched ranking ─────────────────────────────────────────────
    if use_boost:
        # Apply per-note metadata boosts
        for ch in all_chunks:
            note_key = ch.get("name", "")
            note_meta = note_meta_cache.get(note_key, {})
            _boost_score([ch], note_meta, query_terms_set)

    # ── Sort and return ───────────────────────────────────────────────────────
    all_chunks.sort(key=lambda x: -x["score"])
    results = all_chunks[:max_results]

    # ── Stage 6: Store in LRU cache ───────────────────────────────────────────
    if use_cache:
        _rag_cache_set(cache_key, results)

    return results


def rag(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """
    Backward-compatible RAG wrapper.

    Delegates to rag_improved() and normalises output to:
    {path, content, score, heading, chunk_type}

    Args:
        query: Search query string.
        max_results: Number of results to return.

    Returns:
        List of dicts with keys: path, content, score, heading, chunk_type.
    """
    results = rag_improved(query, max_results=max_results)
    return [
        {
            "path": r["name"],
            "content": r["text"][:6000],
            "score": r["score"],
            "heading": r["heading"],
            "chunk_type": r.get("chunk_type", "prose"),
        }
        for r in results
    ]


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "list":
        folder = sys.argv[2] if len(sys.argv) > 2 else None
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        notes = list_notes(folder=folder, limit=limit)
        print(f"📚 {len(notes)} notas:")
        for n in notes:
            print(f"  📝 {n['name']}")

    elif cmd == "read":
        if len(sys.argv) < 3:
            print("Uso: read <path>", file=sys.stderr)
            sys.exit(1)
        path = " ".join(sys.argv[2:])
        content = read_note(path)
        if len(content) > 20000:
            print(f"[AVISO] Nota de {len(content)} chars; mostrando primeros 20000. Usa importlib/read_note() para el contenido completo.")
            print(content[:20000])
        else:
            print(content)

    elif cmd == "write":
        if len(sys.argv) < 3:
            print("Uso: write <path> [content | --stdin]", file=sys.stderr)
            sys.exit(1)
        path = sys.argv[2]
        if len(sys.argv) >= 4 and sys.argv[3] == "--stdin":
            # Leer contenido multilínea desde stdin (heredoc). Evita el
            # quoting/escaping roto de argv para notas largas.
            content = sys.stdin.read()
        elif len(sys.argv) >= 4:
            content = sys.argv[3]
        else:
            content = sys.stdin.read()
        print(write_note(path, content))

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Uso: delete <path>", file=sys.stderr)
            sys.exit(1)
        print(delete_note(" ".join(sys.argv[2:])))

    elif cmd == "edit":
        if len(sys.argv) < 4:
            print("Uso: edit <path> <append|prepend|replace> [content | --stdin] [old_text]", file=sys.stderr)
            sys.exit(1)
        op = sys.argv[3]
        # Si falta content explícito o se usa --stdin, leer de stdin (multilínea)
        if len(sys.argv) >= 5 and sys.argv[4] == "--stdin":
            content = sys.stdin.read()
            old = sys.argv[5] if len(sys.argv) > 5 else None
        elif len(sys.argv) >= 5:
            content = sys.argv[4]
            old = sys.argv[5] if len(sys.argv) > 5 else None
        else:
            content = sys.stdin.read()
            old = None
        print(edit_note(sys.argv[2], content, op, old))

    elif cmd == "move":
        if len(sys.argv) < 4:
            print("Uso: move <from> <to>", file=sys.stderr)
            sys.exit(1)
        print(move_note(sys.argv[2], sys.argv[3]))

    elif cmd == "meta":
        if len(sys.argv) < 3:
            print("Uso: meta <path>", file=sys.stderr)
            sys.exit(1)
        print(get_note_metadata(" ".join(sys.argv[2:])))

    elif cmd == "folders":
        print(list_folders())

    elif cmd == "tags":
        print(list_tags())

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Uso: search <query> [N]", file=sys.stderr)
            sys.exit(1)
        args = sys.argv[2:]
        limit = 10
        # Sin esto, `search "foo" 20` buscaba literalmente "foo 20" y daba 0 resultados
        if len(args) > 1 and args[-1].isdigit():
            limit = int(args.pop())
        results = search(" ".join(args), limit)
        print(f"🔍 {len(results)} resultados:")
        for r in results:
            print(f"  {r['name']}")

    elif cmd == "rag":
        if len(sys.argv) < 3:
            print("Uso: rag <query> [max_results]", file=sys.stderr)
            sys.exit(1)
        max_r = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 5
        q = " ".join(sys.argv[2:] if not sys.argv[-1].isdigit() else sys.argv[2:-1])
        print(json.dumps(rag(q, max_r), ensure_ascii=False, indent=2))

    elif cmd == "rag-improved":
        if len(sys.argv) < 3:
            print("Uso: rag-improved <query> [max_results]", file=sys.stderr)
            sys.exit(1)
        max_r = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 5
        q = " ".join(sys.argv[2:] if not sys.argv[-1].isdigit() else sys.argv[2:-1])
        print(json.dumps(rag_improved(q, max_r), ensure_ascii=False, indent=2))

    elif cmd == "cache-clear":
        _rag_cache_clear()
        print("✅ Cache RAG limpiada")

    else:
        print(
            """Uso: obsidian-mcp-client.py <comando> [args]

Comandos CRUD:
  list [folder] [N]              - Listar notas (filtro carpeta + límite)
  read <path>                    - Leer nota
  write <path> <content>         - Crear/sobreescribir nota
  delete <path>                  - Eliminar nota
  edit <path> <op> <content>     - Editar (append|prepend|replace)
  move <from> <to>               - Mover/renombrar nota
  meta <path>                    - Metadatos (frontmatter, tags, backlinks)
  folders                        - Listar carpetas
  tags                           - Listar tags
  search <query>                 - Buscar por nombre

Comandos RAG:
  rag <query> [N]                - RAG básico (backward-compat)
  rag-improved <query> [N]       - RAG full pipeline (todos los stages)
  cache-clear                    - Limpiar cache LRU
"""
        )
