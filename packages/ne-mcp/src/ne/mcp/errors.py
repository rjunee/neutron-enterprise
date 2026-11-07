# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-mcp (the thin governed MCP adapter layer).

Redaction discipline mirrors ne-runtime / ne-govern: an ne-mcp error message names
ONLY the catalog server name and the remote tool name — never remote content, never
endpoint userinfo, never argument values. An external MCP server is an UNTRUSTED tool
provider, so nothing it returns is ever echoed into an exception or an audit record.
"""

from __future__ import annotations


class NeMcpError(Exception):
    """Base class for every error raised by ne-mcp."""


class McpCatalogError(NeMcpError):
    """A catalog entry is invalid (fail loud at composition time).

    Raised when a private in-VPC server catalog entry fails C2 in-VPC/SSRF
    validation, duplicates another entry's ``name``, or declares a tier below the
    Tier-2 floor (an external MCP server is an untrusted tool provider; its tier may
    only escalate). The message names at most the catalog ``name`` / parsed host —
    never endpoint userinfo (the ne.net validator omits it by design).
    """


class McpExternalToolError(NeMcpError):
    """A remote MCP server returned an error, or advertised an invalid tool name.

    The message names ONLY the server catalog name and the remote tool name; remote
    content, endpoint, and argument values are never included (the remote is
    untrusted). When raised from a governed tool handler, the executor's own
    ``tool.execute`` error audit records ``error_class=McpExternalToolError`` — the
    remote payload never reaches the audit spine.
    """
