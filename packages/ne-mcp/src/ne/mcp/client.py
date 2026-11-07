# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ExternalMcpToolProvider — surface an in-VPC external MCP server as governed tools.

An external MCP server is an UNTRUSTED tool provider, NOT a governance bypass. This
provider connects to ONE C2-validated catalog server and exposes its remote tools
ONLY as :class:`~ne.runtime.tools.ToolBinding` objects the composition root registers
into the ONE :class:`~ne.runtime.middleware.GovernedToolExecutor`. It exposes NO
public call/execute method — its only output is bindings — so a remote tool can be
run EXCLUSIVELY through the governed pipeline (guardrails → PDP → HITL → audit).

Namespacing + tier floor (C6). Every discovered tool is named
``write.external.mcp.<server>.<tool>``. The ``write.external.`` prefix pins the class
tier to 2 in ``ne.runtime.actions.tier_for_action``; a ``.pii.`` substring in the
remote tool name can only ESCALATE the class, never lower it. Remote tool names are
additionally validated (``^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$``) so a hostile name with
dots/exotic chars cannot game classification — and the full name is prefix-locked
regardless.

Unforgeable in-VPC validation (C2 / Codex P2 + NE-CLEAN1 hardening). The provider
accepts ONLY a :class:`~ne.mcp.catalog.ValidatedCatalogEntry` — the proof-carrying type
whose own before-validator has already run ``ne.net.validate_in_vpc_endpoint``. A raw
:class:`McpServerCatalogEntry` (or an element of raw ``McpSettings.catalog``) is
REFUSED at ``__init__`` with a ``TypeError``, and the validator is RE-RUN as
defense-in-depth (closing the pydantic ``model_copy(update=...)`` forgery hole). That
re-run consults ONLY the ``trusted_allowed_hosts`` the composition root passes in
explicitly (default ``()``  — the built-in in-VPC rules only) — it NEVER reads the
entry's own ``allowed_hosts`` field, because that field is bundled on the (frozen but
still ``model_copy``-forgeable) entry and cannot be trusted for a re-validation whose
whole point is to catch a forged entry. A forgery that bundles
``model_copy(update={"endpoint": ..., "allowed_hosts": (...)})`` therefore cannot
smuggle an external host past the re-check: only the caller-supplied trusted list (or
the original settings-provided entry matched by server name) is consulted. So NO
construction path can hand this provider an unvalidated endpoint — the C2 in-VPC check
is unbypassable by construction, not by caller discipline.

Egress visibility (C2/C12). Each binding carries ``extract_egress_urls`` returning the
server endpoint, bound via a closure DEFAULT (``_ep=entry.endpoint``) so nothing
model-controlled can shadow it (the NE-033 Codex lesson). The governed executor feeds
that endpoint to the C12 egress-host allowlist — an external tool whose server host is
not allowlisted is blocked before the remote is ever contacted.

Redaction (untrusted remote). A remote ``isError`` result raises
:class:`McpExternalToolError` naming ONLY the server + tool name — never the remote
content. The executor wraps it as ``ToolExecutionError`` and audits
``error_class=McpExternalToolError``; no remote payload reaches an exception message or
the audit spine.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from ne.contracts.runtime import ToolSpec
from ne.mcp import _sdk
from ne.mcp.catalog import ValidatedCatalogEntry
from ne.mcp.errors import McpCatalogError, McpExternalToolError
from ne.net import validate_in_vpc_endpoint
from ne.runtime.tools import ToolBinding

#: Opens a live client session to a catalog server (production or in-memory test).
SessionFactory = Callable[
    [ValidatedCatalogEntry], AbstractAsyncContextManager["_sdk.ClientSession"]
]

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@asynccontextmanager
async def _streamable_http_session(
    entry: ValidatedCatalogEntry,
) -> AsyncIterator[_sdk.ClientSession]:
    """Default production factory: a connection-per-call streamable-HTTP session.

    Per the 2026 MCP revision the streamable-HTTP transport is stateless, so a fresh
    connection is opened for each discovery/call and closed on exit. The endpoint has
    already been C2-validated by :func:`ne.mcp.catalog.validated_catalog`.
    """
    async with (
        _sdk.streamablehttp_client(entry.endpoint) as (read, write, _),
        _sdk.ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _render_content(content: Sequence[Any]) -> str:
    """Render remote content blocks to text: TextContent joined by newlines.

    A non-text block renders as ``[non-text content: <type>]`` — the block's own data
    is never inlined (an external server is untrusted).
    """
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(f"[non-text content: {getattr(block, 'type', 'unknown')}]")
    return "\n".join(parts)


class ExternalMcpToolProvider:
    """Discovers a C2-validated external MCP server's tools as governed bindings."""

    def __init__(
        self,
        entry: ValidatedCatalogEntry,
        *,
        trusted_allowed_hosts: Sequence[str] = (),
        session_factory: SessionFactory | None = None,
    ) -> None:
        # C2 (Codex P2): the ONLY accepted entry type is the proof-carrying
        # ValidatedCatalogEntry from ne.mcp.validated_catalog() — its construction already
        # ran ne.net.validate_in_vpc_endpoint. A raw McpServerCatalogEntry / raw
        # McpSettings.catalog element / bare endpoint is rejected before any session.
        if not isinstance(entry, ValidatedCatalogEntry):
            raise TypeError(
                "ExternalMcpToolProvider requires a ValidatedCatalogEntry produced by "
                "ne.mcp.validated_catalog() (C2 in-VPC validation is unbypassable); a raw "
                "McpServerCatalogEntry/McpSettings.catalog entry or endpoint is rejected"
            )
        # Defense-in-depth (NE-CLEAN1): re-run the validator to defeat a pydantic
        # model_copy(update=...) forgery of a validated entry (model_copy skips validators
        # by design). Pure stdlib, no network; the normalized return value is deliberately
        # unused — the call is for its raising behavior (the endpoint is already
        # normalized). Critically, this allowlist comes ONLY from the caller-supplied
        # `trusted_allowed_hosts` (default () = built-in in-VPC rules only) — NEVER from
        # `entry.allowed_hosts`, which is bundled on the entry itself and therefore
        # forgeable by the exact model_copy(update={"endpoint": ..., "allowed_hosts": ...})
        # attack this re-check exists to catch. A composition root that needs per-entry
        # operator-declared hosts must pass its OWN trusted McpSettings entry's
        # allowed_hosts explicitly (matched by server name), not trust the passed object.
        validate_in_vpc_endpoint(
            entry.endpoint, allowed_hosts=tuple(trusted_allowed_hosts), error_cls=McpCatalogError
        )
        self._entry = entry
        self._session_factory: SessionFactory = session_factory or _streamable_http_session

    async def discover_bindings(self) -> tuple[ToolBinding, ...]:
        """Connect, list remote tools, and return one governed binding per tool.

        Raises :class:`McpExternalToolError` if a remote advertises an invalid tool
        name (fail loud — the catalog is trusted, its tools must be well-formed).
        """
        async with self._session_factory(self._entry) as session:
            listed = await session.list_tools()
        bindings: list[ToolBinding] = []
        for remote in listed.tools:
            if not _REMOTE_NAME_RE.match(remote.name):
                raise McpExternalToolError(
                    f"external MCP server {self._entry.name!r} advertised an invalid tool "
                    f"name {remote.name!r} (must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,127}}$)"
                )
            spec = ToolSpec(
                name=f"write.external.mcp.{self._entry.name}.{remote.name}",
                description=remote.description or "",
                tier=self._entry.tier,
                input_schema=remote.inputSchema,
            )
            bindings.append(
                ToolBinding(
                    spec=spec,
                    handler=self._make_handler(remote.name),
                    extract_egress_urls=lambda a, _ep=self._entry.endpoint: (_ep,),
                )
            )
        return tuple(bindings)

    def _make_handler(self, remote_name: str) -> Callable[[Mapping[str, object]], Awaitable[str]]:
        entry = self._entry
        factory = self._session_factory

        async def _handler(args: Mapping[str, object]) -> str:
            async with factory(entry) as session:
                result = await session.call_tool(remote_name, dict(args))
            if result.isError:
                raise McpExternalToolError(
                    f"external MCP tool {remote_name!r} on server {entry.name!r} returned an error"
                )
            return _render_content(result.content)

        return _handler
