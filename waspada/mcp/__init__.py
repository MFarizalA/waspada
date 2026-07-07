"""WASPADA Model Context Protocol layer (WA-015).

A real MCP server (:mod:`waspada.mcp.server`) serving the two tools the
Skeptic (:class:`~waspada.agents.risk_auditor.RiskAuditorAgent`) calls during
its function-calling audit loop, plus two client implementations
(:mod:`waspada.mcp.client`) the agent uses to reach it — an in-process client
(default, zero overhead) and a stdio client (the rubric's protocol path). The
data is the analytics layer's own aggregates — the single source of truth (no
duplicate computation).

See HACKATHON.md § Technical Depth / MCP integration for the rubric context.
"""
from __future__ import annotations

from .client import InProcessClient, StdioClient
from .server import (
    TOOL_LOOKUP_ACCOUNT,
    TOOL_PORTFOLIO_STATS,
    build_server,
    lookup_account,
    portfolio_stats,
    run_stdio,
)
from .store import AnalyticsStore

__all__ = [
    "AnalyticsStore",
    "InProcessClient",
    "StdioClient",
    "build_server",
    "run_stdio",
    "portfolio_stats",
    "lookup_account",
    "TOOL_PORTFOLIO_STATS",
    "TOOL_LOOKUP_ACCOUNT",
]
