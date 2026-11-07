# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-mcp — a THIN, non-bypassable governed MCP adapter over the NE-033 executor.

Two surfaces, both governed by construction (C6):

  * :class:`GovernedMcpServer` exposes the platform's governed tools to MCP clients.
    It holds ZERO handlers and routes EVERY ``call_tool`` through the ONE
    :class:`~ne.runtime.middleware.GovernedToolExecutor` (guardrails → PDP → HITL →
    audit), with the SDK's pre-governance input validation DISABLED so even malformed
    or unknown calls reach the audited governance path.
  * :class:`ExternalMcpToolProvider` consumes a private in-VPC server catalog
    (C2-validated by :func:`validated_catalog`) and surfaces each remote server's
    tools ONLY as governed ``ToolBinding`` objects namespaced
    ``write.external.mcp.<server>.<tool>`` (Tier-2 floor) — an external MCP server is
    an untrusted tool provider, never a governance bypass (accepts only the unforgeable
    ``ValidatedCatalogEntry`` produced by ``validated_catalog`` — C2 validation cannot
    be skipped).

All ``mcp`` SDK imports are isolated behind :mod:`ne.mcp._sdk` (grep guardian + the
``c3-mcp-sdk-chokepoint`` import-linter contract) so the ~2026-08 v2 migration is
one-module work. The Tasks extension is DEFERRED to v1.1 (experimental-only in the
pinned ``mcp`` 1.x line — see the package README).
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.mcp.catalog import ValidatedCatalogEntry, validated_catalog
from ne.mcp.client import ExternalMcpToolProvider
from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.mcp.errors import McpCatalogError, McpExternalToolError, NeMcpError
from ne.mcp.server import GovernedMcpServer

__all__ = [
    "ExternalMcpToolProvider",
    "GovernedMcpServer",
    "McpCatalogError",
    "McpExternalToolError",
    "McpServerCatalogEntry",
    "McpSettings",
    "NeMcpError",
    "ValidatedCatalogEntry",
    "__version__",
    "validated_catalog",
]
