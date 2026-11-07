# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single, repo-wide ``mcp`` import chokepoint (C3 SDK isolation).

This module is the ONLY place in the entire repository permitted to import the
official Model Context Protocol SDK (``mcp``). Every other ``ne.mcp`` module — and
every ``ne.mcp`` test — imports the SDK symbols it needs FROM here
(``from ne.mcp import _sdk`` / ``_sdk.Server`` / ``_sdk.types`` …). Modeled exactly
on the ne-gateway ``_litellm.py`` and ne-connect ``_dlt.py`` chokepoints.

Enforcement is two-fold: the ``c3-mcp-sdk-chokepoint`` import-linter contract at
``/.importlinter`` (sole ignored edge ``ne.mcp._sdk -> mcp``) AND the grep guardian
``test_mcp_isolation_guardian.py`` (which scans BOTH ``packages/*/src`` AND
``packages/*/tests`` — stricter than the litellm guardian, because v2 migration
containment must cover test code too).

v2 migration containment (~2026-08): the SDK is pinned ``mcp>=1.30.0,<2``
(2.x is alpha-only). When the v2/stable
re-pin lands, the migration is ONE-MODULE work — rewrite the imports below; nothing
else in the repo touches ``mcp``.

Tasks extension — DEFERRED to v1.1 (probe-verified 2026-07-06 on ``mcp==1.28.1``):
the task request/result types exist in ``mcp.types``, but the server-side
implementation lives ONLY under ``mcp.shared.experimental.tasks``; ``mcp.server.tasks``
is ABSENT. Building governed long-running semantics on an explicitly *experimental*
API contradicts the ``<2`` stability pin, so Tasks is documented, not built. Re-evaluate
at the v2/stable re-pin (see the package README).

C1 (no phone-home): the ``mcp`` SDK carries NO telemetry — there is no import-time
env force to apply here (unlike litellm/dlt). All CI tests use the in-process memory
transport (:func:`create_connected_server_and_client_session`); nothing in this
package opens a network socket under test.
"""

from __future__ import annotations

import importlib.metadata

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.memory import create_connected_server_and_client_session

MCP_SDK_VERSION = importlib.metadata.version("mcp")

__all__ = [
    "MCP_SDK_VERSION",
    "ClientSession",
    "Server",
    "create_connected_server_and_client_session",
    "stdio_server",
    "streamablehttp_client",
    "types",
]
