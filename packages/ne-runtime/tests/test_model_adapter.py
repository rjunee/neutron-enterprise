# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GatewayModel — the D3 prompted tool-calling protocol + C3-clean routing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedSourceInvocationRef,
    ProtectedSourceOutputRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundModelSendResolution,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel, _brace_completion_variants
from ne.runtime.source_results import (
    PROTECTED_REFERENCE_PREFIX,
    SOURCE_UNAVAILABLE_TEXT,
    SOURCE_WITHHELD_TEXT,
    AuthorizationExpiredError,
    MalformedProtectedReferenceError,
    MissingAuthenticatedContextError,
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    current_authenticated_context,
    decode_protected_reference,
    encode_protected_reference,
    model_call_scope,
    require_authenticated_context,
)

from .conftest import RecordingAuditSink, ScriptedGateway, make_budget, text_completion


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read.docs.search",
            description="Search docs",
            parameters_json_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
        ToolDefinition(
            name="write.external.email.send",
            description="Send email",
            parameters_json_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        ),
    ]


class RecordingHook:
    """An ``UnknownToolHook`` that records every (name, args) it is invoked with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
        self.calls.append((name, dict(args)))


def _run(
    gateway: ScriptedGateway,
    messages: list,
    *,
    with_tools: bool = True,
    hook: Any = None,
) -> ModelResponse:
    hook = hook if hook is not None else RecordingHook()
    model = GatewayModel(gateway, RuntimeSettings(), on_unknown_tool=hook)
    params = ModelRequestParameters(function_tools=_tools() if with_tools else [])
    return asyncio.run(model.request(messages, None, params))


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


# --- valid tool_call ------------------------------------------------------


def test_valid_tool_call_becomes_tool_call_part() -> None:
    line = json.dumps({"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}})
    gw = ScriptedGateway([text_completion(line)])
    resp = _run(gw, [_user("go")])
    assert len(resp.parts) == 1
    part = resp.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_name == "read.docs.search"
    assert part.args == {"q": "x"}


def test_unknown_tool_name_still_becomes_tool_call_part() -> None:
    # The governed executor is the authority; an unknown tool still parses as a call,
    # AND the on_unknown_tool hook is invoked exactly once before the response returns.
    line = json.dumps({"tool_call": {"name": "totally.unknown", "arguments": {}}})
    gw = ScriptedGateway([text_completion(line)])
    hook = RecordingHook()
    resp = _run(gw, [_user("go")], hook=hook)
    assert isinstance(resp.parts[0], ToolCallPart)
    assert resp.parts[0].tool_name == "totally.unknown"
    assert hook.calls == [("totally.unknown", {})]


def test_known_tool_call_does_not_invoke_unknown_hook() -> None:
    # A registered tool name is resolved by pydantic-ai normally — the unknown-tool
    # hook is never touched (the known path is unweakened).
    line = json.dumps({"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}})
    gw = ScriptedGateway([text_completion(line)])
    hook = RecordingHook()
    resp = _run(gw, [_user("go")], hook=hook)
    assert isinstance(resp.parts[0], ToolCallPart)
    assert hook.calls == []


def test_gateway_model_requires_unknown_tool_hook() -> None:
    # on_unknown_tool is a required keyword-only arg (no hook-less GatewayModel).
    with pytest.raises(TypeError):
        GatewayModel(ScriptedGateway([]), RuntimeSettings())  # type: ignore[call-arg]


def test_unknown_hook_failure_propagates() -> None:
    # An un-recordable unknown call kills the request (fail closed): a hook that
    # raises propagates straight out of GatewayModel.request.
    class RaisingHook:
        async def __call__(self, name: str, args: Mapping[str, Any]) -> None:
            raise RuntimeError("audit down")

    line = json.dumps({"tool_call": {"name": "totally.unknown", "arguments": {}}})
    gw = ScriptedGateway([text_completion(line)])
    with pytest.raises(RuntimeError):
        _run(gw, [_user("go")], hook=RaisingHook())


# --- malformed vectors all become final-answer TextParts (never raise) ----


@pytest.mark.parametrize(
    "text",
    [
        "just a plain prose answer",
        'here is json {"tool_call": {"name": "read.docs.search", "arguments": {}}}',
        json.dumps([{"tool_call": {"name": "x", "arguments": {}}}]),  # JSON array
        json.dumps({"tool_call": "x"}),  # tool_call not a dict
        json.dumps({"tool_call": {"arguments": {}}}),  # missing name
        json.dumps({"tool_call": {"name": "read.docs.search", "arguments": "notadict"}}),
        json.dumps(
            {"tool_call": {"name": "read.docs.search", "arguments": {}}, "extra": 1}
        ),  # extra top-level key
    ],
)
def test_malformed_becomes_final_answer_text(text: str) -> None:
    gw = ScriptedGateway([text_completion(text)])
    resp = _run(gw, [_user("go")])
    assert len(resp.parts) == 1
    part = resp.parts[0]
    assert isinstance(part, TextPart)
    assert part.content == text


# --- markdown-fenced tool_call transport (observed live 2026-08-04) --------
# Qwen3.6-35B follows the in-band protocol but fences the JSON (```json ... ```);
# a bare json.loads demoted the call to prose and a grounded turn silently lost
# its fetch. The fence is TRANSPORT: unwrapping it must yield the same strict
# shape checks, and anything that is not exactly one all-spanning fence must
# keep behaving as a final text answer.


@pytest.mark.parametrize("tag", ["json", ""])
def test_fenced_tool_call_becomes_tool_call_part(tag: str) -> None:
    line = json.dumps({"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}})
    fenced = f"```{tag}\n{line}\n```"
    gw = ScriptedGateway([text_completion(fenced)])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_name == "read.docs.search"
    assert part.args == {"q": "x"}


def test_fenced_tool_call_with_leading_blank_lines_parses() -> None:
    # The exact live shape: '\n\n```json\n{...}\n```' (reasoning stripped upstream
    # leaves leading newlines; .strip() happens before the fence match).
    line = json.dumps({"tool_call": {"name": "read.docs.search", "arguments": {}}})
    gw = ScriptedGateway([text_completion(f"\n\n```json\n{line}\n```")])
    resp = _run(gw, [_user("go")])
    assert isinstance(resp.parts[0], ToolCallPart)


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"not": "a tool call"}\n```',  # fenced, but wrong shape
        "```json\nnot json at all\n```",  # fenced, not JSON
        'see below:\n```json\n{"tool_call": {"name": "x", "arguments": {}}}\n```',  # prose before
        # prose after the fence
        '```json\n{"tool_call": {"name": "x", "arguments": {}}}\n```\ntrailing prose',
    ],
)
def test_fence_variants_that_are_not_a_lone_tool_call_stay_text(text: str) -> None:
    gw = ScriptedGateway([text_completion(text)])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, TextPart)
    assert part.content == text


# --- first-line fallback + bounded brace completion (observed live 2026-08-04)
# Qwen3.6-35B reproducibly (a) truncates the tool_call line's FINAL closing
# brace (EOS on a `}}` token where `}}}` was intended), and (b) keeps
# generating after the call line — duplicating it and FABRICATING the tool's
# response JSON in the same completion. Under the whole-text-only parse both
# demoted the call to prose: the governed executor never ran and the fabricated
# "result" was emitted to the user as a final answer.


def test_missing_final_brace_completes_to_tool_call() -> None:
    # The exact live shape: inner arguments object complete, outer wrapper(s) unclosed.
    line = '{"tool_call": {"name": "read.docs.search", "arguments": {"q": "sla"}}'
    gw = ScriptedGateway([text_completion(f"\n\n{line}")])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_name == "read.docs.search"
    assert part.args == {"q": "sla"}


def test_two_missing_braces_complete_to_tool_call() -> None:
    line = '{"tool_call": {"name": "read.docs.search", "arguments": {"q": "sla"}'
    gw = ScriptedGateway([text_completion(line)])
    resp = _run(gw, [_user("go")])
    assert isinstance(resp.parts[0], ToolCallPart)


def test_fabricated_response_tail_is_discarded_for_the_first_line_call() -> None:
    # Call line (even brace-truncated) followed by a duplicated call and an
    # invented tool "response": the FIRST line's call is honored — the real
    # governed tool runs — and the fabricated tail never becomes an answer.
    line = '{"tool_call": {"name": "read.docs.search", "arguments": {"q": "sla"}}'
    fabricated = (
        f"{line}\n{line}\n"
        '{\n  "tool_name": "read.docs.search",\n  "response": {"results": [{"id": "DOC-1024"}]}\n}'
    )
    gw = ScriptedGateway([text_completion(fabricated)])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_name == "read.docs.search"
    assert part.args == {"q": "sla"}


def test_completion_stops_at_the_wrapper_depth_so_a_truncated_nested_argument_stays_text() -> None:
    """A candidate needing a FOURTH closing brace is left as prose.

    Pins the BOUND itself, which no other test discriminates. Three completable
    braces are exactly the protocol's container depth (root, the ``tool_call``
    value, the ``arguments`` object) — the third closes the arguments PAYLOAD,
    not a wrapper. This text leaves a further NESTED argument object open on top
    of those three, so honoring it would mean executing a governed tool against
    an argument subtree the model never finished emitting — an unbounded
    omission, not the one-trailing-pair residual the ceiling accepts. Raising
    the ceiling to four makes this parse into a ToolCallPart — that is the
    mutation this test exists to catch.
    """
    text = '{"tool_call": {"name": "read.docs.search", "arguments": {"filter": {"acl": 1'
    gw = ScriptedGateway([text_completion(text)])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, TextPart), f"4-brace-deficit candidate was completed into {part!r}"
    assert part.content == text
    # The control that keeps the assertion from passing vacuously: the SAME
    # shape one brace shallower (a 3-deficit, arguments tree closed) IS honored.
    ok = '{"tool_call": {"name": "read.docs.search", "arguments": {"filter": {"acl": 1}}'
    resp_ok = _run(ScriptedGateway([text_completion(ok)]), [_user("go")])
    assert isinstance(resp_ok.parts[0], ToolCallPart)


def test_brace_completion_appends_only_closing_braces_never_a_name_or_value() -> None:
    """The variants the transport considers can only CLOSE, never invent.

    The governed-action safety property stated directly against the single
    source of the rule: every completion variant is the candidate plus ``}``
    characters, so no tool name, argument key, or argument value can enter a
    call that the model did not itself emit.
    """
    candidate = '{"tool_call": {"name": "x", "arguments": {"q": "sla"}'
    variants = _brace_completion_variants(candidate)
    assert variants[0] == candidate
    assert len(variants) == 4, variants  # the candidate + 3 bounded completions
    for variant in variants[1:]:
        suffix = variant[len(candidate) :]
        assert set(suffix) == {"}"}, f"completion appended non-brace text: {suffix!r}"
    assert len(variants[-1]) - len(candidate) == 3


@pytest.mark.parametrize(
    "text",
    [
        # Prose first line: the fallback never rescues JSON buried mid-text.
        'Here is what I will do:\n{"tool_call": {"name": "x", "arguments": {}}}',
        # Brace completion cannot manufacture the strict shape from a wrong one.
        '{"tool_call": {"name": "x", "arguments": {}}, "extra": 1',
        # Truncated CONTENT (arguments object never closed its string) stays prose.
        '{"tool_call": {"name": "x", "arguments": {"q": "unterminated',
    ],
)
def test_brace_completion_never_widens_the_accepted_shape(text: str) -> None:
    gw = ScriptedGateway([text_completion(text)])
    resp = _run(gw, [_user("go")])
    part = resp.parts[0]
    assert isinstance(part, TextPart)
    assert part.content == text


# --- catalog system message + no tools/tool_choice in params --------------


def test_catalog_and_instruction_reach_the_gateway() -> None:
    gw = ScriptedGateway([text_completion("final")])
    _run(gw, [_user("go")])
    request = gw.requests[-1]
    system_msgs = [m for m in request.messages if m.role == "system"]
    assert system_msgs, "expected a system catalog message"
    catalog = system_msgs[-1].content
    assert "read.docs.search" in catalog
    assert "write.external.email.send" in catalog
    assert "tool_call" in catalog  # the strict instruction
    # D3: params never carry tools / tool_choice.
    assert "tools" not in request.params
    assert "tool_choice" not in request.params
    assert request.task == "agent"


def test_no_catalog_when_no_tools() -> None:
    gw = ScriptedGateway([text_completion("final")])
    _run(gw, [_user("go")], with_tools=False)
    request = gw.requests[-1]
    assert all(m.role != "system" for m in request.messages)


# --- history mapping ------------------------------------------------------


def test_history_mapping_roundtrips_roles() -> None:
    # Match the adapter's canonical rendering: NAME FIRST (the 2026-08-04
    # live lesson — history exemplars must show _TOOL_INSTRUCTION's exact
    # shape; the old sorted render put arguments first and nudged the model
    # toward name-dropped emissions), tight separators.
    prior_call = json.dumps(
        {"tool_call": {"name": "read.docs.search", "arguments": {"q": "x"}}},
        separators=(",", ":"),
    )
    messages = [
        ModelRequest(parts=[SystemPromptPart(content="you are helpful")]),
        ModelRequest(parts=[UserPromptPart(content="find docs")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read.docs.search", args={"q": "x"})],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="prev",
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="read.docs.search", content="3 hits")]),
        ModelResponse(
            parts=[TextPart(content="here you go")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="prev",
        ),
        ModelRequest(parts=[RetryPromptPart(content="please retry")]),
    ]
    gw = ScriptedGateway([text_completion("final")])
    _run(gw, messages)
    chat = gw.requests[-1].messages
    roles = [m.role for m in chat]
    # hist: system, user, assistant(re-rendered call), tool, assistant, user(retry);
    # the trailing catalog system message is merged into the ONE leading system
    # message (C3 message-shape contract), not appended at the end.
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    # The merged head carries BOTH the original system prompt AND the catalog,
    # system prompt first (order-stable, lossless merge).
    assert "you are helpful" in chat[0].content
    assert "Available tools (JSON):" in chat[0].content
    assert chat[0].content.index("you are helpful") < chat[0].content.index(
        "Available tools (JSON):"
    )
    # The prior tool call was re-rendered as its canonical JSON line.
    assert chat[1].content == "find docs"
    assert chat[2].content == prior_call
    # The tool return was rendered with the tool name.
    assert chat[3].content == "read.docs.search returned: 3 hits"


def test_usage_is_mapped() -> None:
    gw = ScriptedGateway([text_completion("final")])
    resp = _run(gw, [_user("go")])
    assert resp.usage.input_tokens == 3
    assert resp.usage.output_tokens == 5
    assert resp.model_name == "ne-gateway:agent"


# --- PRE-015A0-ENFORCEMENT: protected source delivery at the model-send seam ---
#
# The guard's own unit behavior (context propagation, fail-closed absence, the
# reference codec, the typed decisions) lives here with the GatewayModel seam it
# exists to protect, so a change to either side breaks the same file.


def _stamp(*, item_id: str = "item-a", generation: int = 3) -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id=item_id
        ),
        generation=generation,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id=f"receipt-{item_id}",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


_EXPIRES_AT = 1_800_000_000
_BEFORE_EXPIRY = datetime.fromtimestamp(_EXPIRES_AT - 1, tz=UTC)


def _context(principal_id: str = "user:ada") -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=principal_id,
        authorization_groups=("group:finance", "group:staff"),
        department="finance",
        issuer="https://oidc.test/realms/ne",
        expires_at=_EXPIRES_AT,
    )


_DEFAULT_CONTEXT = _context()


def _tool_ref(stamp: SourceAuthorityStamp) -> SourceBoundToolResultRef:
    return SourceBoundToolResultRef(reference_id="tool-1", stamps=(stamp,), result_hash="e" * 64)


@dataclass
class ResolverCall:
    """One recorded resolver invocation — every argument the guard supplied."""

    reference: Any
    principal_id: str
    access_context_hash: str
    run_id: str
    model_call_attempt_id: str


class FakeSourceResolver:
    """A ``SourceBoundResultResolver`` scripting one decision per model-send call."""

    def __init__(
        self,
        script: Sequence[tuple[SourceAuthorityDecision, str | None]],
        *,
        stamp: SourceAuthorityStamp | None = None,
        run_id_override: str | None = None,
    ) -> None:
        self._script = list(script)
        self._stamp = stamp if stamp is not None else _stamp()
        self._run_id_override = run_id_override
        self.calls: list[ResolverCall] = []

    async def for_model_send(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution:
        self.calls.append(
            ResolverCall(
                reference, principal_id, access_context_hash, run_id, model_call_attempt_id
            )
        )
        decision, text = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        authorized = decision is SourceAuthorityDecision.AUTHORIZED
        return SourceBoundModelSendResolution(
            decision=decision,
            materialized_text=text if authorized else None,
            stamps=(self._stamp,) if authorized else (),
            safe_reason=None if authorized else "safe",
            run_id=self._run_id_override if self._run_id_override is not None else run_id,
            model_call_attempt_id=model_call_attempt_id,
        )

    async def for_direct_result(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_approval(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _guard(
    resolver: FakeSourceResolver, *, now: datetime = _BEFORE_EXPIRY
) -> ProtectedSourceResultGuard:
    return ProtectedSourceResultGuard(resolver, clock=lambda: now)


def _send(
    gateway: ScriptedGateway,
    messages: list,
    *,
    guard: ProtectedSourceResultGuard | None,
    context: AuthenticatedRequestContext | None = _DEFAULT_CONTEXT,
    budget: Any = None,
    sink: Any = None,
) -> ModelResponse:
    model = GatewayModel(
        gateway,
        RuntimeSettings(),
        on_unknown_tool=RecordingHook(),
        source_guard=guard,
        budget=budget,
        sink=sink,
    )
    params = ModelRequestParameters(function_tools=[])

    async def drive() -> ModelResponse:
        with model_call_scope(run_id="run-1", model_call_attempt_id="attempt-1"):
            if context is None:
                return await model.request(messages, None, params)
            with authenticated_request_scope(context):
                return await model.request(messages, None, params)

    return asyncio.run(drive())


def _tool_return(reference_text: str) -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(tool_name="read.docs.search", content=reference_text, tool_call_id="c")
        ]
    )


_SECRET = "Q3 revenue was 41.2M, per the board deck."


def test_protected_reference_round_trips_by_equality_for_every_target_kind() -> None:
    stamp = _stamp()
    references = (
        SourceBoundToolResultRef(reference_id="t", stamps=(stamp,), result_hash="e" * 64),
        SourceBoundToolResultSetRef(
            reference_id="ts",
            stamps=(stamp,),
            result_hash="d" * 64,
            evidence_ids=("evidence-1",),
        ),
        ProtectedSourceOutputRef(reference_id="o", stamps=(stamp,), output_hash="f" * 64),
        ProtectedSourceInvocationRef(reference_id="i", stamps=(stamp,), invocation_hash="0" * 64),
    )
    for reference in references:
        assert decode_protected_reference(encode_protected_reference(reference)) == reference
    # Positive control for the absence claim: ordinary text is NOT a reference, so
    # source-free content is never touched by this codec.
    assert decode_protected_reference("read.docs.search returned: 3 hits") is None


def test_tampered_reference_in_the_server_namespace_is_refused_not_decoded() -> None:
    with pytest.raises(MalformedProtectedReferenceError):
        decode_protected_reference(PROTECTED_REFERENCE_PREFIX + "!!!not-base64!!!")
    # Positive control: the same namespace with intact bytes still decodes.
    reference = _tool_ref(_stamp())
    assert decode_protected_reference(encode_protected_reference(reference)) == reference


def test_bound_snapshot_is_exactly_what_reaches_the_resolver() -> None:
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    context = _context("user:grace")
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw,
        [_tool_return(encode_protected_reference(_tool_ref(_stamp())))],
        guard=_guard(resolver),
        context=context,
    )
    call = resolver.calls[0]
    assert call.principal_id == context.principal_id
    assert call.access_context_hash == context.context_hash
    assert (call.run_id, call.model_call_attempt_id) == ("run-1", "attempt-1")


def test_absent_authenticated_context_refuses_the_send_and_calls_no_resolver() -> None:
    """THE fail-closed control: no snapshot means refusal, never a default identity."""
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    messages = [_tool_return(encode_protected_reference(_tool_ref(_stamp())))]
    gw = ScriptedGateway([text_completion("final")])
    with pytest.raises(MissingAuthenticatedContextError):
        _send(gw, messages, guard=_guard(resolver), context=None)
    assert resolver.calls == []
    assert gw.requests == []
    # Positive control: the SAME model, guard and history succeed once a snapshot is
    # bound — so the refusal above is the missing context, not a broken seam.
    gw2 = ScriptedGateway([text_completion("final")])
    _send(gw2, messages, guard=_guard(resolver))
    assert (
        gw2.requests[-1].messages[-1].content
        == f"read.docs.search returned: [source-id: tool-1]\n{_SECRET}"
    )


def test_snapshot_is_refused_at_its_verified_expiry_second_and_honored_one_second_before() -> None:
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    messages = [_tool_return(encode_protected_reference(_tool_ref(_stamp())))]
    at_expiry = datetime.fromtimestamp(_EXPIRES_AT, tz=UTC)
    with pytest.raises(AuthorizationExpiredError):
        _send(ScriptedGateway(), messages, guard=_guard(resolver, now=at_expiry))
    assert resolver.calls == []
    gw = ScriptedGateway([text_completion("final")])
    _send(gw, messages, guard=_guard(resolver, now=_BEFORE_EXPIRY))
    assert (
        gw.requests[-1].messages[-1].content
        == f"read.docs.search returned: [source-id: tool-1]\n{_SECRET}"
    )


def test_a_bound_snapshot_does_not_survive_its_scope_or_leak_into_the_next_one() -> None:
    first, second = _context("user:ada"), _context("user:grace")
    with authenticated_request_scope(first):
        assert require_authenticated_context() is first
        with authenticated_request_scope(second):
            assert require_authenticated_context() is second
        assert require_authenticated_context() is first
    with pytest.raises(MissingAuthenticatedContextError):
        require_authenticated_context()
    assert current_authenticated_context() is None


def test_authorized_delivery_substitutes_only_into_the_outbound_request() -> None:
    encoded = encode_protected_reference(_tool_ref(_stamp()))
    messages = [_tool_return(encoded)]
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    gw = ScriptedGateway([text_completion("final")])
    _send(gw, messages, guard=_guard(resolver))
    assert (
        gw.requests[-1].messages[-1].content
        == f"read.docs.search returned: [source-id: tool-1]\n{_SECRET}"
    )
    # Durable history keeps the OPAQUE reference, so every later turn reauthorizes.
    part = messages[0].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.content == encoded


def test_drift_between_the_two_passes_delivers_the_withheld_string_not_the_first_text() -> None:
    resolver = FakeSourceResolver(
        [
            (SourceAuthorityDecision.AUTHORIZED, _SECRET),
            (SourceAuthorityDecision.PERMANENT_WITHHELD, None),
        ]
    )
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw, [_tool_return(encode_protected_reference(_tool_ref(_stamp())))], guard=_guard(resolver)
    )
    sent = gw.requests[-1].messages[-1].content
    assert sent == f"read.docs.search returned: {SOURCE_WITHHELD_TEXT}"
    assert _SECRET not in sent
    assert len(resolver.calls) == 2


def test_retryable_unknown_delivers_the_unavailable_string_never_the_permanent_one() -> None:
    resolver = FakeSourceResolver([(SourceAuthorityDecision.RETRYABLE_UNKNOWN, None)])
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw, [_tool_return(encode_protected_reference(_tool_ref(_stamp())))], guard=_guard(resolver)
    )
    sent = gw.requests[-1].messages[-1].content
    assert sent == f"read.docs.search returned: {SOURCE_UNAVAILABLE_TEXT}"
    assert SOURCE_WITHHELD_TEXT != SOURCE_UNAVAILABLE_TEXT


def test_a_resolution_bound_to_another_run_never_materializes_its_text() -> None:
    resolver = FakeSourceResolver(
        [(SourceAuthorityDecision.AUTHORIZED, _SECRET)], run_id_override="run-other"
    )
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw, [_tool_return(encode_protected_reference(_tool_ref(_stamp())))], guard=_guard(resolver)
    )
    sent = gw.requests[-1].messages[-1].content
    assert sent == f"read.docs.search returned: {SOURCE_UNAVAILABLE_TEXT}"
    assert _SECRET not in sent


def test_a_reference_of_the_wrong_target_kind_is_withheld_at_the_model_send_seam() -> None:
    invocation = ProtectedSourceInvocationRef(
        reference_id="i", stamps=(_stamp(),), invocation_hash="0" * 64
    )
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    gw = ScriptedGateway([text_completion("final")])
    _send(gw, [_tool_return(encode_protected_reference(invocation))], guard=_guard(resolver))
    assert gw.requests[-1].messages[-1].content == (
        f"read.docs.search returned: {SOURCE_UNAVAILABLE_TEXT}"
    )
    assert resolver.calls == []


def test_a_protected_assistant_part_is_reauthorized_and_withheld_on_drift() -> None:
    encoded = encode_protected_reference(
        ProtectedSourceOutputRef(reference_id="o", stamps=(_stamp(),), output_hash="f" * 64)
    )
    messages = [_user("what did we say"), ModelResponse(parts=[TextPart(content=encoded)])]
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw,
        messages,
        guard=_guard(FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])),
    )
    assert gw.requests[-1].messages[-1].content == _SECRET
    gw2 = ScriptedGateway([text_completion("final")])
    _send(
        gw2,
        messages,
        guard=_guard(FakeSourceResolver([(SourceAuthorityDecision.PERMANENT_WITHHELD, None)])),
    )
    assert gw2.requests[-1].messages[-1].content == SOURCE_WITHHELD_TEXT


def test_a_source_free_history_assembles_identically_with_and_without_the_guard() -> None:
    messages = [_user("go"), ModelResponse(parts=[TextPart(content="prior answer")])]
    plain = ScriptedGateway([text_completion("final")])
    _send(plain, messages, guard=None)
    guarded = ScriptedGateway([text_completion("final")])
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, _SECRET)])
    _send(guarded, messages, guard=_guard(resolver))
    assert guarded.requests[-1] == plain.requests[-1]
    assert resolver.calls == []


def test_only_the_references_that_survive_the_trim_are_reauthorized_before_the_send() -> None:
    """The second pass covers exactly the delivered history — no more, no less."""
    ref_a = SourceBoundToolResultRef(reference_id="a", stamps=(_stamp(),), result_hash="1" * 64)
    ref_b = SourceBoundToolResultRef(reference_id="b", stamps=(_stamp(),), result_hash="2" * 64)
    messages = [
        _user("old"),
        ModelResponse(parts=[TextPart(content="a1")]),
        _tool_return(encode_protected_reference(ref_a)),
        ModelResponse(parts=[TextPart(content="a2")]),
        _user("new"),
        _tool_return(encode_protected_reference(ref_b)),
    ]
    long_text = "x" * 400
    resolver = FakeSourceResolver([(SourceAuthorityDecision.AUTHORIZED, long_text)])
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final")])
    _send(
        gw,
        messages,
        guard=_guard(resolver),
        budget=make_budget(context_window=424, max_output_tokens=16, overhead_margin=8),
        sink=sink,
    )
    assert [call.reference for call in resolver.calls] == [ref_a, ref_b, ref_b]
    sent = [m.content for m in gw.requests[-1].messages]
    assert sent == ["new", f"read.docs.search returned: [source-id: b]\n{long_text}"]


# --- NE-RETRY-LEGIBLE: a retry is a correction, not an error dump -----------


def test_a_validation_error_retry_is_rendered_as_a_correction_not_a_dump() -> None:
    """The live S1 defect, pinned at the adapter seam.

    A schema rejection's ``RetryPromptPart`` carries pydantic's raw error-details
    list, ``input`` included — the model's whole rejected reply. The old
    ``_as_text`` mapping sent that list verbatim as a bare user message with no
    instruction; live (train-49) the model answered it with prose four times in
    a row. The mapping now sends pydantic-ai's own rendering: the errors, minus
    the top-level ``input`` echo, ending with the imperative sentence.
    """
    rejected_reply = "Based on the gathered evidence, the eligible loans are three."
    retry = RetryPromptPart(
        content=[
            {
                "type": "json_invalid",
                "loc": (),
                "msg": "Invalid JSON: expected value at line 1 column 1",
                "input": rejected_reply,
            }
        ]
    )
    gw = ScriptedGateway([text_completion("final")])
    _run(gw, [_user("q"), ModelRequest(parts=[retry])])
    (retry_message,) = [m for m in gw.requests[-1].messages if m.role == "user"][-1:]
    assert "Fix the errors and try again." in retry_message.content
    assert "json_invalid" in retry_message.content
    # The input echo is excluded: the reply is already in the history once.
    assert rejected_reply not in retry_message.content
    # The base adapter is an output_type=str loop: no transport restatement.
    assert "FORMAT, not its content" not in retry_message.content


def test_a_server_authored_string_retry_is_delivered_verbatim() -> None:
    """This loop's own ``ModelRetry`` sentences (residue, claims) were written to
    be sent and budgeted at their measured length — no wrapper is added."""
    gw = ScriptedGateway([text_completion("final")])
    _run(gw, [_user("q"), ModelRequest(parts=[RetryPromptPart(content="please retry")])])
    contents = [m.content for m in gw.requests[-1].messages]
    assert "please retry" in contents
