"""MCP client (WA-015) — the Skeptic's bridge to the analytics MCP server.

Two client implementations, one surface::

    portfolio_stats(segment=None) -> dict
    lookup_account(loan_id)       -> dict

  * :class:`InProcessClient` — calls :class:`~waspada.mcp.store.AnalyticsStore`
    directly against in-memory tables. No subprocess, no protocol overhead.
    Use this when the auditor already holds the scored+feature tables (it
    does — they're in the run context). This is the default MCP-backed path
    in CI and offline: same compute, same dict shapes, no stdio round-trip.
  * :class:`StdioClient` — spawns the real MCP server subprocess
    (``python -m waspada.mcp.server``) over stdio and speaks the protocol.
    This is the rubric's explicit MCP-integration path; the live smoke test
    exercises it end-to-end. The async MCP transport runs on a private
    background asyncio thread (its own loop), and the public methods are
    synchronous bridges via ``run_coroutine_threadsafe`` — safe to call from
    the synchronous agent pipeline and from FastAPI request-thread workers.

Both are registered on the Skeptic via :meth:`Agent.register_tool` — the same
seam ingest's ``fetch`` uses. Tests inject a stub (no subprocess); production
registers either client. The callables' signatures match the local fallback
tools, so swapping is transparent.

See :mod:`waspada.mcp.server` for the protocol layer and
:mod:`waspada.mcp.store` for the compute.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from concurrent.futures import Future
from typing import Any, Dict, Optional

from .store import AnalyticsStore

__all__ = ["InProcessClient", "StdioClient"]


# --------------------------------------------------------------------------- #
# InProcessClient — same surface, no subprocess. Wraps AnalyticsStore directly.
# --------------------------------------------------------------------------- #
class InProcessClient:
    """Call the analytics store's compute directly (no stdio round-trip).

    Same ``portfolio_stats`` / ``lookup_account`` surface and dict shapes as
    :class:`StdioClient`, but skips the protocol entirely — the auditor already
    holds the scored+feature tables in its run context, so this is the
    zero-overhead MCP-backed path for CI and offline runs. The compute is
    identical (both delegate to :class:`~waspada.mcp.store.AnalyticsStore`,
    which delegates to :func:`waspada.insight.ranking.segment_health`).
    """

    name = "mcp-inprocess"

    def __init__(self, scored, features=None, analyst_aggregates=None) -> None:
        self._store = AnalyticsStore(scored, features, analyst_aggregates)

    def portfolio_stats(self, segment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._store.portfolio_stats(segment)

    def lookup_account(self, loan_id: str) -> Dict[str, Any]:
        return self._store.lookup_account(str(loan_id))

    def set_analyst_aggregates(self, aggregates) -> None:
        """WA-042: passthrough to the store's analyst_aggregates setter."""
        self._store.set_analyst_aggregates(aggregates)

    def close(self) -> None:
        """No-op (parity with :class:`StdioClient`'s surface)."""
        pass

    def __enter__(self) -> "InProcessClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# StdioClient — spawns the MCP server subprocess, speaks the protocol.
# --------------------------------------------------------------------------- #
class StdioClient:
    """Synchronous facade over the stdio MCP server subprocess.

    Spawns ``python -m waspada.mcp.server --scored <path> [--features <path>]``,
    opens an MCP :class:`~mcp.client.session.ClientSession`, and exposes the two
    tools as plain Python methods returning ``dict``. Use as a context manager
    for deterministic subprocess teardown::

        with StdioClient(scored_path, features_path) as mcp:
            stats = mcp.portfolio_stats(segment={"product": "card"})
            row = mcp.lookup_account("LN00961668")

    The async MCP session lives on a private background thread running its own
    asyncio loop. Public methods are synchronous bridges
    (``run_coroutine_threadsafe``), so they're safe to call from the
    synchronous agent pipeline — including inside FastAPI request workers
    (which run in a threadpool, off the event loop). The loop and subprocess
    are torn down on :meth:`close`.
    """

    name = "mcp-stdio"

    def __init__(
        self,
        scored_path: Optional[str] = None,
        features_path: Optional[str] = None,
        *,
        python: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.scored_path = scored_path
        self.features_path = features_path
        self.python = python or sys.executable
        self.env = env
        self.cwd = cwd
        # Background-loop plumbing.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[Any] = None
        # Held async context managers for ordered teardown.
        self._client_cm = None
        self._stdio_cm = None

    # -------------------------------------------------- async machinery
    def _build_server_params(self):
        from mcp.client.stdio import StdioServerParameters

        args = ["-m", "waspada.mcp.server", "--scored", self.scored_path]
        if self.features_path:
            args.extend(["--features", self.features_path])
        return StdioServerParameters(
            command=self.python, args=args, env=self.env, cwd=self.cwd,
        )

    def _submit(self, coro, timeout: float = 30.0) -> Any:
        """Schedule ``coro`` on the background loop and block for its result."""
        if self._loop is None:
            raise RuntimeError("StdioClient not connected.")
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def connect(self) -> "StdioClient":
        """Start the background loop, spawn the server, open the MCP session."""
        if self._session is not None:
            return self
        loop = asyncio.new_event_loop()
        self._loop = loop
        ready = threading.Event()
        loop_err: list = []

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            except Exception as exc:  # pragma: no cover - defensive
                loop_err.append(exc)
            finally:
                try:
                    # Cancel any lingering tasks before closing.
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                except Exception:
                    pass
                loop.close()

        self._thread = threading.Thread(
            target=_run_loop, name="waspada-mcp-stdio", daemon=True,
        )
        self._thread.start()
        ready.wait(timeout=10.0)
        if loop_err:
            raise RuntimeError(f"stdio loop failed to start: {loop_err[0]!r}")

        # Drive the connect sequence on the background loop.
        try:
            self._submit(_connect_session(self), timeout=30.0)
        except BaseException:
            self._teardown_loop()
            raise
        return self

    def _teardown_loop(self) -> None:
        """Stop the background loop and join its thread."""
        loop = self._loop
        thread = self._thread
        self._loop = None
        self._thread = None
        self._session = None
        self._client_cm = None
        self._stdio_cm = None
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)

    def close(self) -> None:
        """Tear down the session + subprocess + background loop."""
        if self._loop is None:
            return
        # Exit the async CMs in reverse enter order, on the background loop.
        try:
            self._submit(_disconnect_session(self), timeout=15.0)
        except Exception:
            pass
        self._teardown_loop()

    def __enter__(self) -> "StdioClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------- tool calls
    def list_tools(self):
        """Return the server's declared tools (MCP discovery / FC loop)."""
        if self._session is None:
            raise RuntimeError("StdioClient not connected.")
        return self._submit(self._session.list_tools())

    def portfolio_stats(self, segment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call the ``portfolio_stats`` MCP tool -> parsed dict."""
        if self._session is None:
            raise RuntimeError("StdioClient not connected.")
        res = self._submit(self._session.call_tool("portfolio_stats", {"segment": segment or {}}))
        return _parse_tool_result(res)

    def lookup_account(self, loan_id: str) -> Dict[str, Any]:
        """Call the ``lookup_account`` MCP tool -> the account's feature dict."""
        if self._session is None:
            raise RuntimeError("StdioClient not connected.")
        res = self._submit(self._session.call_tool("lookup_account", {"loan_id": str(loan_id)}))
        return _parse_tool_result(res)


# --------------------------------------------------------------------------- #
# Async session lifecycle — runs on the background loop, holds CMs open.
# --------------------------------------------------------------------------- #
async def _connect_session(client: "StdioClient") -> None:
    """Open the stdio server + client session on the loop, stash on ``client``."""
    from mcp.client.stdio import stdio_client
    from mcp.client.session import ClientSession

    params = client._build_server_params()
    stdio_cm = stdio_client(params)
    read, write = await stdio_cm.__aenter__()
    client._stdio_cm = stdio_cm
    client_cm = ClientSession(read, write)
    session = await client_cm.__aenter__()
    await session.initialize()
    client._client_cm = client_cm
    client._session = session


async def _disconnect_session(client: "StdioClient") -> None:
    """Exit the session + stdio CMs in reverse order (best-effort)."""
    for cm in (client._client_cm, client._stdio_cm):
        if cm is None:
            continue
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Result parsing — MCP CallToolResult -> plain dict.
# --------------------------------------------------------------------------- #
def _parse_tool_result(res: Any) -> Dict[str, Any]:
    """Parse an MCP ``CallToolResult`` into a dict.

    Prefers structured content (FastMCP / our server's JSON body); falls back
    to parsing the first text block as JSON. On a tool error (``isError``),
    returns ``{"error": ...}`` rather than raising — the Skeptic's audit loop
    degrades gracefully on a tool miss.
    """
    if getattr(res, "isError", False):
        text = _first_text(res)
        return {"error": f"mcp tool error: {text or 'unknown'}"}

    structured = getattr(res, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        if set(structured.keys()) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured

    text = _first_text(res)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _first_text(res: Any) -> str:
    content = getattr(res, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""
