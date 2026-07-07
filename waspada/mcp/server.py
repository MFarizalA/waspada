"""WASPADA MCP server (WA-015) — portfolio_stats + lookup_account over stdio.

A real Model Context Protocol server exposing the two tools the Skeptic (the
:class:`~waspada.agents.risk_auditor.RiskAuditorAgent`) calls during its
function-calling audit loop:

  * ``portfolio_stats(segment?)`` — NPL ratio, vintage default rate, status
    mix, account count for a product×region slice (or the whole book).
  * ``lookup_account(loan_id)`` — the feature row for one account (the numbers
    a dispute cites as evidence).

Both tools serve the analytics aggregates the pipeline already computed — the
single source of truth (see :mod:`waspada.mcp.store`). The server speaks MCP
over stdio (the transport Qwen's tool-calling loop and the local client use;
SSE on Function Compute is the WA-021 stretch).

Design
------
:func:`build_server` returns a configured low-level ``mcp.server.Server`` for a
given :class:`~waspada.mcp.store.AnalyticsStore`. Keeping this a factory (not a
module-global) means tests can wire a real store without spawning a subprocess,
and the CLI / Function Compute entry point (:func:`run_stdio`) is a thin
wrapper that loads the scored+feature parquet and starts the stdio loop.

The two tools are also exposed as plain Python functions
(:func:`portfolio_stats`, :func:`lookup_account`) so the agent can bind them
directly as the local fallback (the ``register_tool`` pattern from ingest's
``fetch`` — same seam, stubbable in tests).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pyarrow.parquet as pq

from .store import AnalyticsStore

__all__ = [
    "AnalyticsStore",
    "build_server",
    "run_stdio",
    "portfolio_stats",
    "lookup_account",
    "TOOL_PORTFOLIO_STATS",
    "TOOL_LOOKUP_ACCOUNT",
]

# --------------------------------------------------------------------------- #
# Tool schemas — the MCP-declared shapes the Skeptic's FC loop discovers.
# --------------------------------------------------------------------------- #
# Qwen / OpenAI-compatible function calling works from a JSON-schema tool
# description. We declare the two tools here once so the server, the client,
# and the agent's tool-registration all cite the exact same shapes.
TOOL_PORTFOLIO_STATS = {
    "name": "portfolio_stats",
    "description": (
        "Portfolio-level risk aggregates from the analytics layer: NPL ratio, "
        "vintage default rate by cohort, status mix, and account count. "
        "Optionally restrict to a product×region segment. Use this to ground "
        "a risk claim in the actual loan book rather than a static corpus."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "segment": {
                "type": "object",
                "description": (
                    "Optional product×region filter. Either key may be omitted "
                    "(treated as a wildcard). Example: {\"product\": "
                    "\"debt_consolidation\", \"region\": \"West\"}."
                ),
                "properties": {
                    "product": {"type": "string"},
                    "region": {"type": "string"},
                },
            },
        },
        "additionalProperties": False,
    },
}

TOOL_LOOKUP_ACCOUNT = {
    "name": "lookup_account",
    "description": (
        "Look up the feature row for one loan_id: payment_ratio, dti, "
        "outstanding_ratio, loan_age, delinquency_status, grade, etc. — the "
        "numbers a risk dispute cites as evidence. Returns an empty object if "
        "the loan_id is not in the book."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "loan_id": {
                "type": "string",
                "description": "The loan identifier to look up.",
            },
        },
        "required": ["loan_id"],
        "additionalProperties": False,
    },
}

_TOOL_SCHEMAS = [TOOL_PORTFOLIO_STATS, TOOL_LOOKUP_ACCOUNT]


# --------------------------------------------------------------------------- #
# Plain-Python tool implementations (reusable without an MCP session).
# --------------------------------------------------------------------------- #
# These take an AnalyticsStore and return a JSON-serializable dict. The MCP
# server wraps them (below); the auditor binds them as its local fallback tools
# so the register_tool seam is identical to ingest's `fetch`.
def portfolio_stats(store: AnalyticsStore, *, segment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute portfolio aggregates for ``segment`` (or the whole book)."""
    return store.portfolio_stats(segment)


def lookup_account(store: AnalyticsStore, *, loan_id: str) -> Dict[str, Any]:
    """Return the feature row for ``loan_id`` (empty dict if absent)."""
    return store.lookup_account(loan_id)


# --------------------------------------------------------------------------- #
# Server factory — a configured low-level mcp.server.Server for a store.
# --------------------------------------------------------------------------- #
def build_server(store: AnalyticsStore):
    """Build an MCP ``Server`` exposing the two tools backed by ``store``.

    Returns the ``Server`` (not yet running). Tests call this directly and
    exercise the registered handlers; :func:`run_stdio` drives the stdio loop.
    """
    from mcp.server import Server
    from mcp.server import NotificationOptions
    from mcp.server.models import InitializationOptions
    import mcp.types as types

    server = Server("waspada-analytics")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # type: ignore[no-untyped-def]
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in _TOOL_SCHEMAS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> list[types.TextContent]:  # type: ignore[no-untyped-def]
        args = dict(arguments or {})
        try:
            if name == "portfolio_stats":
                result = store.portfolio_stats(args.get("segment"))
            elif name == "lookup_account":
                result = store.lookup_account(str(args.get("loan_id", "")))
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # defensive: never crash the MCP loop
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    # Expose the store for direct (non-subprocess) testing.
    server.waspada_store = store  # type: ignore[attr-defined]
    return server


# --------------------------------------------------------------------------- #
# Stdio entry — loads the scored+feature parquet and runs the MCP loop.
# --------------------------------------------------------------------------- #
def _load_tables(scored_path: Path, features_path: Optional[Path]) -> AnalyticsStore:
    """Load the scored (required) + feature (optional) parquet into a store."""
    scored = pq.read_table(str(scored_path))
    features = pq.read_table(str(features_path)) if features_path else None
    return AnalyticsStore(scored, features)


def run_stdio(scored_path: Path, features_path: Optional[Path] = None) -> None:
    """Run the MCP server over stdio, backed by the parquet at ``scored_path``.

    This is the subprocess entry point (``python -m waspada.mcp.server
    --scored <path>``). The agent's MCP client (:mod:`waspada.mcp.client`)
    spawns it via :class:`mcp.StdioServerParameters`.
    """
    import asyncio

    from mcp.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    store = _load_tables(scored_path, features_path)
    server = build_server(store)

    async def _main() -> None:
        init_options = server.create_initialization_options(
            notification_options=NotificationOptions()
        )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)

    asyncio.run(_main())


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: ``python -m waspada.mcp.server --scored scored.parquet``."""
    parser = argparse.ArgumentParser(
        prog="waspada.mcp.server",
        description="WASPADA MCP server (portfolio_stats + lookup_account) over stdio.",
    )
    parser.add_argument(
        "--scored", required=True,
        help="Path to the scored-accounts parquet (superset of ScoredAccounts).",
    )
    parser.add_argument(
        "--features", default=None,
        help="Path to the FeatureFrame parquet (enables lookup_account). Optional.",
    )
    args = parser.parse_args(argv)

    scored_path = Path(args.scored)
    if not scored_path.is_file():
        print(f"[waspada.mcp] scored parquet not found: {scored_path}", file=sys.stderr)
        return 2
    features_path = Path(args.features) if args.features else None
    if features_path is not None and not features_path.is_file():
        print(f"[waspada.mcp] features parquet not found: {features_path}", file=sys.stderr)
        return 2

    run_stdio(scored_path, features_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - module-exec entry
    raise SystemExit(main())
