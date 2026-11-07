# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""External MCP servers are governed tool providers, NOT a governance bypass.

A fake EXTERNAL lowlevel MCP server is stood up in-process (via ne.mcp._sdk) and its
tools are discovered by :class:`ExternalMcpToolProvider`. The discovered tools are
namespaced ``write.external.mcp.<server>.<tool>`` (class tier >= 2) and execute ONLY
through the REAL governed executor: PDP deny never reaches the remote; the endpoint is
visible to (and blockable by) the C12 egress guardrail; a remote ``isError`` surfaces
as an audited ``ToolExecutionError`` with no remote content leaked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ne.contracts.types import ApprovalTier
from ne.mcp import _sdk
from ne.mcp.catalog import ValidatedCatalogEntry, validated_catalog
from ne.mcp.client import ExternalMcpToolProvider, _render_content
from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.mcp.errors import McpExternalToolError
from ne.runtime.errors import ToolDeniedError, ToolExecutionError
from ne.runtime.tools import validate_binding

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    UnusedGate,
    make_executor,
    make_guardrails,
    make_principal,
    memory_session_factory,
    run,
)


class ExtServer:
    """A fake external lowlevel MCP server with a remote-invocation counter."""

    def __init__(
        self,
        *,
        tool_name: str = "create_note",
        is_error: bool = False,
        content: list | None = None,
        output_schema: dict | None = None,
        structured_content: dict | None = None,
    ) -> None:
        self.calls = 0
        self._server = _sdk.Server("ext1")
        self._tool_name = tool_name
        self._is_error = is_error
        self._content = content
        self._structured_content = structured_content

        @self._server.list_tools()
        async def _list_tools() -> list[_sdk.types.Tool]:
            return [
                _sdk.types.Tool(
                    name=tool_name,
                    description="Create a note.",
                    inputSchema={"type": "object", "properties": {"note": {"type": "string"}}},
                    outputSchema=output_schema,
                )
            ]

        @self._server.call_tool()
        async def _call_tool(name: str, args: dict):
            self.calls += 1
            if self._is_error:
                return _sdk.types.CallToolResult(
                    isError=True,
                    content=[_sdk.types.TextContent(type="text", text="boom secret")],
                )
            content = self._content or [_sdk.types.TextContent(type="text", text="note created")]
            # A CallToolResult is returned VERBATIM by the lowlevel server (no server-side
            # output validation on that branch), so structuredContent reaches the CLIENT's
            # validator — the seam GHSA-rwrf-2pqf-9j8j lives on.
            return _sdk.types.CallToolResult(
                isError=False, content=content, structuredContent=self._structured_content
            )

    @property
    def server(self):
        return self._server


def _entry() -> ValidatedCatalogEntry:
    return validated_catalog(
        McpSettings(catalog=(McpServerCatalogEntry(name="ext1", endpoint="http://mcp-ext1:8080"),))
    )[0]


def _provider(ext: ExtServer) -> ExternalMcpToolProvider:
    return ExternalMcpToolProvider(_entry(), session_factory=memory_session_factory(ext.server))


def test_discovered_tools_are_namespaced_write_external() -> None:
    ext = ExtServer()
    bindings = run(_provider(ext).discover_bindings())
    assert len(bindings) == 1
    spec = bindings[0].spec
    assert spec.name == "write.external.mcp.ext1.create_note"
    assert spec.tier == ApprovalTier.TIER_2
    validate_binding(bindings[0])  # classifies + tier-floor OK


def test_invalid_remote_tool_name_rejected() -> None:
    ext = ExtServer(tool_name="../evil tool")
    with pytest.raises(McpExternalToolError):
        run(_provider(ext).discover_bindings())


def test_external_call_routes_through_executor() -> None:
    ext = ExtServer()
    binding = run(_provider(ext).discover_bindings())[0]
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(
        [binding],
        sink,
        approval_gate=gate,
        guardrails=make_guardrails(sink, allowed_egress_hosts=("mcp-ext1",)),
    )

    out = run(executor.execute(binding.spec.name, {"note": "hi"}, principal=make_principal()))

    assert out == "note created"
    assert ext.calls == 1
    assert gate.requests and gate.requests[0].tier == ApprovalTier.TIER_2
    assert sink.with_action("pdp.decide")
    assert sink.with_action("tool.approval")
    execs = sink.with_action("tool.execute")
    assert execs and execs[0].outcome == "success"


def test_external_call_denied_by_pdp_never_reaches_remote() -> None:
    ext = ExtServer()
    binding = run(_provider(ext).discover_bindings())[0]
    sink = RecordingAuditSink()
    executor = make_executor(
        [binding],
        sink,
        approval_gate=UnusedGate(),
        guardrails=make_guardrails(sink, allowed_egress_hosts=("mcp-ext1",)),
        pdp_overrides={
            binding.spec.name: {
                "effect": "deny",
                "tier": 2,
                "reason": "denied for test",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    with pytest.raises(ToolDeniedError):
        run(executor.execute(binding.spec.name, {"note": "hi"}, principal=make_principal()))

    assert ext.calls == 0  # PDP deny fired before the handler; remote never contacted


def test_egress_url_visible_to_guardrails() -> None:
    ext = ExtServer()
    binding = run(_provider(ext).discover_bindings())[0]
    sink = RecordingAuditSink()
    # allowed_egress_hosts=() => the endpoint host is not allowlisted => guardrail block.
    executor = make_executor(
        [binding],
        sink,
        approval_gate=UnusedGate(),
        guardrails=make_guardrails(sink, allowed_egress_hosts=()),
    )

    from ne.govern.errors import GuardrailViolation

    with pytest.raises(GuardrailViolation):
        run(executor.execute(binding.spec.name, {"note": "hi"}, principal=make_principal()))

    assert ext.calls == 0
    assert sink.with_action("guardrail.block")


def test_remote_error_result_raises_and_audited() -> None:
    ext = ExtServer(is_error=True)
    binding = run(_provider(ext).discover_bindings())[0]
    sink = RecordingAuditSink()
    executor = make_executor(
        [binding],
        sink,
        approval_gate=NamedApproverGate("alice"),
        guardrails=make_guardrails(sink, allowed_egress_hosts=("mcp-ext1",)),
    )

    with pytest.raises(ToolExecutionError):
        run(executor.execute(binding.spec.name, {"note": "hi"}, principal=make_principal()))

    errs = [e for e in sink.with_action("tool.execute") if getattr(e, "outcome", "") == "error"]
    assert errs
    import json

    assert json.loads(errs[0].detail)["error_class"] == "McpExternalToolError"
    # The untrusted remote content never leaks into any audit event.
    for event in sink.events:
        assert "boom secret" not in (getattr(event, "detail", None) or "")


def test_remote_text_content_rendered() -> None:
    ext = ExtServer(
        content=[
            _sdk.types.TextContent(type="text", text="line one"),
            _sdk.types.TextContent(type="text", text="line two"),
        ]
    )
    binding = run(_provider(ext).discover_bindings())[0]
    sink = RecordingAuditSink()
    executor = make_executor(
        [binding],
        sink,
        approval_gate=NamedApproverGate("alice"),
        guardrails=make_guardrails(sink, allowed_egress_hosts=("mcp-ext1",)),
    )

    out = run(executor.execute(binding.spec.name, {"note": "hi"}, principal=make_principal()))
    assert out == "line one\nline two"

    # A non-text block renders as a redacted placeholder (no inlined block data).
    class _NonText:
        type = "image"

    rendered = _render_content([_sdk.types.TextContent(type="text", text="a"), _NonText()])
    assert rendered == "a\n[non-text content: image]"


_SENTINEL = "must_be_present_proves_file_was_fetched"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten the nested anyio task-group ExceptionGroups the memory transport raises."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaf_exceptions(inner)]
    return [exc]


def test_external_output_schema_ref_is_never_fetched(tmp_path: Path) -> None:
    """GHSA-rwrf-2pqf-9j8j: a server-declared ``outputSchema`` ``$ref`` is NEVER fetched.

    mcp 1.10.0-1.29.1 validated ``call_tool`` results with jsonschema's DEFAULT resolver,
    which retrieves any external ``$ref`` (file://, http://, https://) from the CLIENT's own
    network position, synchronously on the event loop, with no timeout. Discriminating
    fixture: the ref points at a real file whose schema requires a sentinel property. If the
    SDK fetched it, ``{}`` is validated against the FETCHED schema and the error names the
    sentinel (executed on 1.28.1: "Invalid structured content returned by tool t:
    'must_be_present_proves_file_was_fetched' is a required property"). On >= 1.30.0 the
    reference is unresolvable within the schema document the server sent (executed:
    "Invalid schema for tool t: Unresolvable: file:///..."). The memory transport wraps the
    SDK's RuntimeError in nested ExceptionGroups on both versions, so leaves are flattened.
    """
    ext = tmp_path / "ext.json"
    ext.write_text(json.dumps({"type": "object", "required": [_SENTINEL]}), encoding="utf-8")
    server = ExtServer(tool_name="t", output_schema={"$ref": ext.as_uri()}, structured_content={})
    binding = run(_provider(server).discover_bindings())[0]

    async def invoke() -> str:
        return await binding.handler({})

    with pytest.raises((RuntimeError, BaseExceptionGroup)) as excinfo:
        run(invoke())

    leaves = _leaf_exceptions(excinfo.value)
    assert len(leaves) == 1
    (leaf,) = leaves
    assert isinstance(leaf, RuntimeError)
    assert "Unresolvable" in str(leaf)
    for candidate in leaves:
        assert _SENTINEL not in str(candidate)
    # The remote tool DID run; only the SDK's client-side result validation refused it.
    assert server.calls == 1
