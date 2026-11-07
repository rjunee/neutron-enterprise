# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-6 bounds for the caller-pinned model-planned governed loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.exceptions import ToolRetryError
from pydantic_ai.messages import RetryPromptPart

import ne.runtime.runner as runner_module
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundModelSendResolution,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import (
    ActionValidationError,
    AgenticNoProgressError,
    AgentOutputContractError,
    RuntimeAuditError,
    ToolsetViolationError,
)
from ne.runtime.model import (
    PROTOCOL_RESIDUE_CALL_CORRECTION,
    is_malformed_tool_call_residue,
    is_prose_wrapped_tool_call,
)
from ne.runtime.run_runner import RunCancellationRequested
from ne.runtime.runner import (
    BoundedAgenticAdapter,
    ModelPlannedRun,
    _RecordedExecutor,
    _StructuredAgenticCompletion,
    run_model_planned_agent,
)
from ne.runtime.source_results import (
    SOURCE_UNAVAILABLE_TEXT,
    SOURCE_WITHHELD_TEXT,
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    current_protected_tool_evidence_refs,
    encode_protected_reference,
    model_call_scope,
)

from .conftest import (
    RecordingAuditSink,
    ScriptedGateway,
    req_user,
    resp_text,
    text_completion,
    tool_call_completion,
)

_PRINCIPAL = Principal(id="user:meridian-ops", groups=("ops",))
_REF = "urn:ne:meridian:dispute:mer-dsp-003"
_NO_WALL_CLOCK_LIMIT = float("inf")


def _protected_ref(*, reference_id: str = "protected-output-a", result_hash: str = "e" * 64) -> str:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a",
            site_id="",
            drive_id="drive-a",
            item_id="item-a",
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
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
        receipt_id="receipt-item-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 23, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    stamp = SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )
    return encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id=reference_id,
            stamps=(stamp,),
            result_hash=result_hash,
        )
    )


_PROTECTED_REF = _protected_ref()
_PROTECTED_REF_B = _protected_ref(reference_id="protected-output-b")
#: What the guard now delivers beside the authorized text, and therefore the only
#: vocabulary a claim may cite. These are the `reference_id`s inside the two
#: references above — in production, the `uuid.uuid4()` of `pending_result_id`.
_SOURCE_ID = "protected-output-a"
_SOURCE_ID_B = "protected-output-b"

#: The authorized chunk text tests deliver, and an answer that QUOTES it — the
#: shape the prompt now teaches. The server binds the quoted sentence itself
#: (NE-SPAN-BIND); the model declares nothing.
_CHUNK_SENTENCE = "All eleven late fees were reversed after the billing review."
_CHUNK_SENTENCE_B = "The hardship credit posted to the account on May ninth."
_QUOTING_ANSWER = (
    'The record is unambiguous. The source states: "All eleven late fees were '
    'reversed after the billing review." That places the fault with the billing job.'
)
#: The exact span the server binds out of ``_QUOTING_ANSWER``: the quoted
#: sentence, recovered without its surrounding quotation marks.
_BOUND_SPAN = _CHUNK_SENTENCE
#: An answer in the model's own words: nothing in it is verbatim in any chunk.
_UNQUOTABLE_ANSWER = "Every fee got refunded once the audit wrapped up."


def _authenticated_context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=_PRINCIPAL.id,
        authorization_groups=_PRINCIPAL.groups,
        issuer="https://issuer.example",
        expires_at=4_102_444_800,
    )


class _MutableAuthorityResolver:
    def __init__(
        self,
        *,
        decision: SourceAuthorityDecision = SourceAuthorityDecision.AUTHORIZED,
        materialized_text: str | dict[str, str] = "protected evidence",
    ) -> None:
        self.decision = decision
        self.materialized_text = materialized_text
        self.calls: list[SourceAuthorityDecision] = []

    def _text_for(self, reference: Any) -> str:
        if isinstance(self.materialized_text, str):
            return self.materialized_text
        return self.materialized_text[reference.reference_id]

    async def for_model_send(self, reference: Any, **kwargs: Any) -> SourceBoundModelSendResolution:
        self.calls.append(self.decision)
        return SourceBoundModelSendResolution(
            decision=self.decision,
            materialized_text=(
                self._text_for(reference)
                if self.decision is SourceAuthorityDecision.AUTHORIZED
                else None
            ),
            stamps=(
                reference.stamps if self.decision is SourceAuthorityDecision.AUTHORIZED else ()
            ),
            run_id=kwargs["run_id"],
            model_call_attempt_id=kwargs["model_call_attempt_id"],
        )


def _source_guard(resolver: _MutableAuthorityResolver) -> ProtectedSourceResultGuard:
    return ProtectedSourceResultGuard(resolver)  # type: ignore[arg-type]


def _structured_completion(
    final_text: str,
    *,
    claims: list[dict[str, object]] | None = None,
) -> Any:
    """A final reply as the NEW contract teaches it: final_text and nothing else.

    ``claims`` exists only for the tolerance tests — a model still following an
    older correction may declare claims, and the server accepts and ignores
    them. No test may rely on their CONTENT reaching anything.
    """
    payload: dict[str, object] = {"final_text": final_text}
    if claims is not None:
        payload["claims"] = claims
    return text_completion(json.dumps(payload))


def _claim(
    claim_text: str,
    *,
    claim_key: str,
    source_id: str = _SOURCE_ID,
) -> dict[str, object]:
    """One claim EXACTLY as a model can emit it: a quoted span and a served ref.

    The helper deliberately computes nothing, and it deliberately no longer
    reaches for a module-level reference constant. Its predecessors did both —
    calling ``hashlib.sha256`` for a ``claim_content_hash``, then pasting a
    2,773-character ``_PROTECTED_REF`` the model could not see — which is how two
    contracts no model could satisfy passed this whole suite. Each time, the
    harness was quietly doing the one thing the model could not do.
    """
    return {
        "claim_key": claim_key,
        "claim_text": claim_text,
        "source_id": source_id,
    }


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self._specs = (
            ToolSpec(
                name="read.kg.directory",
                description="Read the evidence directory.",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="write.internal.note",
                description="Park an internal note.",
                tier=ApprovalTier.TIER_2,
                input_schema={
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                },
            ),
        )

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal.id))
        return f"row | status\n{_REF} | served" if tool.startswith("read.") else "submitted"


class _DraftExecutor(_Executor):
    def __init__(self, *, read_output: str = _PROTECTED_REF) -> None:
        super().__init__()
        self.read_output = read_output
        self.evidence_carriers: list[tuple[str, ...]] = []
        self._specs = (
            self._specs[0],
            ToolSpec(
                name="draft.email.create",
                description="Create an unsent email draft.",
                tier=ApprovalTier.TIER_2,
                input_schema={
                    "type": "object",
                    "properties": {
                        "mailbox": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["mailbox", "subject", "body", "to"],
                },
            ),
        )

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.evidence_carriers.append(current_protected_tool_evidence_refs())
        self.calls.append((tool, dict(args), principal.id))
        return self.read_output if tool == "read.kg.directory" else "submitted"


class _CheckpointBoundary:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events

    async def heartbeat(self, stage: str) -> None:
        self.events.append(("heartbeat", stage))

    async def checkpoint(self, *, stage: str, safe_detail: Mapping[str, object]) -> None:
        self.events.append(("checkpoint", (stage, dict(safe_detail))))

    async def await_inflight(self, operation: Any, *, stage: str) -> Any:
        self.events.append(("inflight", stage))
        return await operation


class _OrderingExecutor(_Executor):
    def __init__(self, events: list[tuple[str, object]]) -> None:
        super().__init__()
        self.events = events

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.events.append(("effect", (tool, dict(args))))
        return await super().execute(tool, args, principal=principal)


def _run(
    gateway: Any,
    executor: _Executor,
    sink: RecordingAuditSink,
    *,
    max_tools: int = 4,
    tokens: int = 1000,
    wall: float = 1.0,
    parked: int = 1,
    source_guard: ProtectedSourceResultGuard | None = None,
) -> Any:
    async def scenario() -> Any:
        if source_guard is None:
            return await run_model_planned_agent(
                task="do the bounded work",
                instructions="use governed tools",
                principal=_PRINCIPAL,
                executor=executor,
                gateway=gateway,
                settings=RuntimeSettings(),
                sink=sink,
                max_tool_calls=max_tools,
                total_tokens_limit=tokens,
                wall_clock_seconds=wall,
                max_parked_writes=parked,
            )
        with (
            authenticated_request_scope(_authenticated_context()),
            model_call_scope(run_id="run-a", model_call_attempt_id="attempt-a"),
        ):
            return await run_model_planned_agent(
                task="do the bounded work",
                instructions="use governed tools",
                principal=_PRINCIPAL,
                executor=executor,
                gateway=gateway,
                settings=RuntimeSettings(),
                sink=sink,
                max_tool_calls=max_tools,
                total_tokens_limit=tokens,
                wall_clock_seconds=wall,
                max_parked_writes=parked,
                source_guard=source_guard,
            )

    return asyncio.run(scenario())


def test_zero_pin_skill_index_rebinds_onto_the_real_adapter() -> None:
    """NE-RUN-ZERO-PIN's SECOND crash, at the seam the fake adapters hid.

    The worker rebinds every agentic adapter's instructions to the run's
    activated Skill index — ``replace(adapter, instructions=activation.
    index_text)`` — and ``ne.skills.active_set.render_index`` renders a
    zero-pin activation as the EMPTY STRING by design. ``replace`` re-runs
    ``__post_init__``, so the old non-empty guard turned every zero-pin
    agentic run into a pre-flight ``ValueError`` AFTER the store had accepted
    the empty receipt; the cross-package zero-pin test missed it because its
    adapter was a plain dataclass without the guard. This drives the REAL
    :class:`BoundedAgenticAdapter` through the worker's exact gesture.
    """
    from dataclasses import replace

    adapter = BoundedAgenticAdapter(
        # The platform's sentinel, replaced by the worker before any model call.
        instructions="Run Skill activation is required before execution.",
        max_tool_calls=1,
        total_tokens_limit=1,
        wall_clock_seconds=1.0,
        max_parked_writes=0,
    )
    rebound = replace(adapter, instructions="")
    assert rebound.instructions == ""


def test_steps_are_caller_pinned_and_valid_same_run_evidence_reaches_write() -> None:
    executor = _Executor()
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find evidence."}),
            tool_call_completion(
                "write.internal.note",
                {
                    "body": "review this",
                    "step_intent": "Park the evidenced note.",
                    "evidence_refs": [_REF],
                },
            ),
            _structured_completion("complete"),
        ]
    )

    result = _run(gateway, executor, sink)

    assert result.outcome == "complete"
    assert [step.intent for step in result.steps] == [
        "Find evidence.",
        "Park the evidenced note.",
    ]
    assert result.steps[0].served_row_ids == (_REF,)
    assert result.steps[1].evidence_refs == (_REF,)
    assert result.claims == ()
    assert {step.principal_id for step in result.steps} == {_PRINCIPAL.id}
    assert {principal_id for _tool, _args, principal_id in executor.calls} == {_PRINCIPAL.id}
    # Recorder-only fields never enter the frozen executor's business arguments.
    assert executor.calls == [
        ("read.kg.directory", {}, _PRINCIPAL.id),
        ("write.internal.note", {"body": "review this"}, _PRINCIPAL.id),
    ]


def test_exact_draft_action_is_a_bounded_write_over_served_protected_evidence() -> None:
    executor = _DraftExecutor()
    sink = RecordingAuditSink()
    resolver = _MutableAuthorityResolver(materialized_text=_CHUNK_SENTENCE)
    draft_args = {
        "mailbox": "ops@meridian.test",
        "subject": "Hardship review",
        "body": "Draft only.",
        "to": "customer@meridian.test",
        "step_intent": "Create the evidenced unsent draft.",
    }
    result = _run(
        ScriptedGateway(
            [
                tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
                tool_call_completion("draft.email.create", draft_args),
                # This run served a protected reference, so its answer must
                # QUOTE the delivered text — the server binds the claim itself
                # (NE-SPAN-BIND); the model declares nothing.
                _structured_completion(_QUOTING_ANSWER),
            ]
        ),
        executor,
        sink,
        source_guard=_source_guard(resolver),
    )

    assert result.outcome == "complete"
    assert result.claims[0].claim_key == f"bound:{_SOURCE_ID}"
    assert result.steps[0].served_row_ids == (_PROTECTED_REF,)
    assert result.steps[1].evidence_refs == (_PROTECTED_REF,)
    assert result.steps[1].outcome == "submitted"
    assert executor.calls == [
        ("read.kg.directory", {}, _PRINCIPAL.id),
        (
            "draft.email.create",
            {
                "mailbox": "ops@meridian.test",
                "subject": "Hardship review",
                "body": "Draft only.",
                "to": "customer@meridian.test",
            },
            _PRINCIPAL.id,
        ),
    ]
    assert executor.evidence_carriers == [(), (_PROTECTED_REF,)]
    assert current_protected_tool_evidence_refs() == ()

    recorded = _RecordedExecutor(
        inner=_DraftExecutor(),
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=2,
        max_parked_writes=1,
    )
    specs = {spec.name: spec for spec in recorded.tool_specs}
    assert "evidence_refs" not in specs["read.kg.directory"].input_schema["properties"]
    assert "evidence_refs" not in specs["draft.email.create"].input_schema["properties"]
    assert "evidence_refs" not in specs["draft.email.create"].input_schema["required"]
    assert resolver.calls[-1] is SourceAuthorityDecision.AUTHORIZED


def test_draft_action_refuses_model_authored_identity_without_server_observed_evidence() -> None:
    executor = _DraftExecutor()
    sink = RecordingAuditSink()
    recorded = _RecordedExecutor(
        inner=executor,
        principal=_PRINCIPAL,
        sink=sink,
        max_tool_calls=1,
        max_parked_writes=1,
    )
    args: dict[str, object] = {
        "mailbox": "ops@meridian.test",
        "subject": "Hardship review",
        "body": "Draft only.",
        "to": "customer@meridian.test",
        "step_intent": "Attempt the unsent draft.",
        "evidence_refs": [_PROTECTED_REF],
    }

    with pytest.raises(ActionValidationError, match="earlier successful read"):
        asyncio.run(recorded.execute("draft.email.create", args, principal=_PRINCIPAL))

    assert executor.calls == []
    assert recorded.steps[-1].outcome == "evidence-ref:no-server-observed-evidence"
    assert sink.actions() == ["agentic.evidence_ref"]


@pytest.mark.parametrize(
    ("decision", "safe_text", "error_name"),
    [
        (
            SourceAuthorityDecision.PERMANENT_WITHHELD,
            SOURCE_WITHHELD_TEXT,
            "SourceAuthorityWithheldError",
        ),
        (
            SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            SOURCE_UNAVAILABLE_TEXT,
            "SourceAuthorityRetryableError",
        ),
    ],
)
def test_revoke_after_model_visibility_rechecks_before_draft_with_zero_effect(
    decision: SourceAuthorityDecision,
    safe_text: str,
    error_name: str,
) -> None:
    resolver = _MutableAuthorityResolver(materialized_text=_CHUNK_SENTENCE)

    class _RevokeAfterVisibleGateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> Any:
            self.requests.append(request)
            if len(self.requests) == 1:
                return tool_call_completion(
                    "read.kg.directory", {"step_intent": "Read protected evidence."}
                )
            serialized = request.model_dump_json()
            if len(self.requests) == 2:
                assert "eleven late fees" in serialized
                resolver.decision = decision
                return tool_call_completion(
                    "draft.email.create",
                    {
                        "mailbox": "ops@meridian.test",
                        "subject": "Hardship review",
                        "body": "Draft only.",
                        "to": "customer@meridian.test",
                        "step_intent": "Create the evidenced unsent draft.",
                        # An invented selector is ignored; only the exact observed
                        # protected reference can enter the ambient carrier.
                        "evidence_refs": ["ne-source-ref:v1:not-server-minted"],
                    },
                )
            assert safe_text in serialized
            # The run served a protected reference before the revoke, so the
            # claim contract applies to its answer exactly as it would to any
            # other source-bearing run: it quotes the text it WAS shown while
            # authorized, and the server binds the span from its own retention
            # of that delivery.
            return _structured_completion(
                f'The write stayed blocked. The source had stated: "{_CHUNK_SENTENCE}"'
            )

        def stream(self, _request: Any) -> Any:
            raise NotImplementedError

    gateway = _RevokeAfterVisibleGateway()
    executor = _DraftExecutor()
    sink = RecordingAuditSink()
    result = _run(
        gateway,
        executor,
        sink,
        max_tools=2,
        source_guard=_source_guard(resolver),
    )

    assert result.outcome == "complete"
    assert result.output.startswith("The write stayed blocked.")
    assert result.claims[0].served_ref == _PROTECTED_REF
    assert [tool for tool, _args, _principal in executor.calls] == ["read.kg.directory"]
    assert executor.evidence_carriers == [()]
    assert result.steps[-1].outcome == f"refused:{error_name}"
    assert result.steps[-1].evidence_refs == (_PROTECTED_REF,)
    assert sink.actions() == []
    assert current_protected_tool_evidence_refs() == ()


def test_draft_action_obeys_the_parked_write_cap() -> None:
    result = _run(
        ScriptedGateway(
            [
                tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
                tool_call_completion(
                    "draft.email.create",
                    {
                        "mailbox": "ops@meridian.test",
                        "subject": "Hardship review",
                        "body": "Draft only.",
                        "to": "customer@meridian.test",
                        "step_intent": "Attempt the unsent draft.",
                        "evidence_refs": [_PROTECTED_REF],
                    },
                ),
            ]
        ),
        _DraftExecutor(),
        RecordingAuditSink(),
        parked=0,
    )

    assert (result.outcome, result.exhausted_cap) == ("budget-exhausted", "parked-writes")
    assert result.steps[-1].outcome == "cap:parked-writes"


def test_served_reference_extraction_is_closed_and_keeps_legacy_identity_support() -> None:
    executor = _DraftExecutor(
        read_output=(
            f"protected={_PROTECTED_REF}\n"
            f"legacy={_REF}\n"
            "locator=https://meridian.test/source/item-a\n"
            "prose=source item-a is relevant\n"
            "lookalike=ne-source-ref:v1:not-server-minted"
        )
    )
    recorded = _RecordedExecutor(
        inner=executor,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=1,
        max_parked_writes=0,
    )

    asyncio.run(
        recorded.execute(
            "read.kg.directory",
            {"step_intent": "Read the exact served identities."},
            principal=_PRINCIPAL,
        )
    )

    assert recorded.steps[0].outcome == "served"
    assert recorded.steps[0].served_row_ids == tuple(sorted((_PROTECTED_REF, _REF)))
    assert recorded.served == {_PROTECTED_REF, _REF}


def _retention_over(**texts: str) -> Any:
    """A run-shaped retention buffer: reference_id -> the text its delivery carried."""
    from ne.runtime.source_results import ServedTextRetention

    retention = ServedTextRetention()
    for reference_id, text in texts.items():
        retention.record(reference_id.replace("_", "-"), text)
    return retention


def _bind(final_text: str, served: set[str], **texts: str) -> Any:
    return runner_module._server_bound_claims(
        final_text, served_refs=served, retention=_retention_over(**texts)
    )


# --- NE-SPAN-BIND: the server selects and binds the span itself --------------


def test_the_server_selects_the_span_and_derives_offsets_hash_and_key() -> None:
    """The whole fix in one assertion set: no model-authored text anywhere.

    The server holds the answer and the retained delivered text, computes their
    longest common verbatim word run, maps it back to THE ANSWER'S OWN bytes,
    and derives the offsets, the SHA-256 and the claim_key. Multi-byte text on
    purpose: "Zoë" is three characters and four bytes, so a server that
    measured characters would bind every later claim to the wrong span.
    """
    chunk = "Zoë opened the filing for the eleven reversed fees."
    final_text = "Zoë opened the filing for the eleven reversed fees. Nothing else changed."
    (bound,) = _bind(final_text, {_PROTECTED_REF}, protected_output_a=chunk)

    encoded = final_text.encode("utf-8")
    expected = "Zoë opened the filing for the eleven reversed fees."
    assert bound.claim_key == f"bound:{_SOURCE_ID}"
    assert bound.served_ref == _PROTECTED_REF
    assert encoded[bound.span_start_byte : bound.span_end_byte].decode("utf-8") == expected
    assert bound.claim_content_hash == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    # Byte offsets, not character offsets.
    assert bound.span_end_byte == len(expected) + 1


def test_binding_survives_the_answers_own_re_rendering() -> None:
    """The demo blocker's root: a re-render is not a fabrication.

    The answer re-types the source sentence with a wrapped line and a curly
    apostrophe. Under ``ws+punct`` — the SAME documented fold the old locator
    applied, and the only one — the span is still found, and what binds is the
    ANSWER'S rendering: newline and curly apostrophe intact, hashed from the
    answer's own bytes, never from the chunk's.
    """
    chunk = "Meridian's policy allows one hardship offer per twelve months."
    final_text = (
        "Meridian\u2019s policy allows one hardship offer\nper twelve months. DR-4830 concurs."
    )
    (bound,) = _bind(final_text, {_PROTECTED_REF}, protected_output_a=chunk)

    span = final_text.encode("utf-8")[bound.span_start_byte : bound.span_end_byte]
    assert span.decode("utf-8") == (
        "Meridian\u2019s policy allows one hardship offer\nper twelve months."
    )
    assert bound.claim_content_hash == hashlib.sha256(span).hexdigest()


def test_a_paraphrase_refuses_with_the_no_span_reason_and_a_content_free_shape() -> None:
    """An answer in the model's own words cannot be bound, and says so early.

    This is the same conclusion `check_grounding` reaches after publication as
    `claim is not grounded by exact evidence`; NE-SPAN-BIND reaches it BEFORE.
    The attached shape is counters only — no byte of the answer or the chunk.
    """
    with pytest.raises(ActionValidationError, match="verbatim in any source") as raised:
        _bind(_UNQUOTABLE_ANSWER, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    shape = raised.value.span_shape  # type: ignore[attr-defined]
    assert "maxrun=" in shape and "floor=5" in shape and "sources=1" in shape
    for token in (*_UNQUOTABLE_ANSWER.split(), *_CHUNK_SENTENCE.split()):
        assert token not in shape


def test_an_overlap_below_the_word_floor_refuses_rather_than_binding_boilerplate() -> None:
    """Four common words is a shared phrase, not a statement the source makes."""
    final_text = "It happened after the billing review concluded, we think."
    with pytest.raises(ActionValidationError, match="verbatim in any source") as raised:
        _bind(final_text, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    # The overlap WAS found — "after the billing" — and refused by size.
    assert "maxrun=3" in raised.value.span_shape  # type: ignore[attr-defined]


def test_case_difference_is_never_folded_into_agreement() -> None:
    """No case folding: a span that needs it is a span about a different sentence."""
    final_text = "ALL ELEVEN LATE FEES WERE REVERSED AFTER THE BILLING REVIEW."
    with pytest.raises(ActionValidationError, match="verbatim in any source"):
        _bind(final_text, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)


def test_the_bound_span_never_cuts_an_answer_word() -> None:
    """Char extension recovers punctuation, never a truncated word.

    The chunk ends "billing review."; the answer says "billing reviewer". A
    char-greedy extension would bind "...billing review" out of "reviewer",
    publishing answer bytes that say something the answer does not. The honesty
    rule walks it back to the last whole word.
    """
    chunk = "the audit closed the billing review."
    final_text = "We hold that the audit closed the billing reviewer role entirely."
    (bound,) = _bind(final_text, {_PROTECTED_REF}, protected_output_a=chunk)

    span = final_text.encode("utf-8")[bound.span_start_byte : bound.span_end_byte]
    assert span.decode("utf-8") == "the audit closed the billing"


def test_a_quoted_sentence_recovers_its_punctuated_boundary_words() -> None:
    """Word matching alone would lose `"All` and `review."` to the quote marks.

    Char extension takes them back — the bound span is the full quoted sentence
    without the surrounding quotation marks, because the residue it leaves on
    the cut answer word is punctuation only.
    """
    (bound,) = _bind(_QUOTING_ANSWER, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    span = _QUOTING_ANSWER.encode("utf-8")[bound.span_start_byte : bound.span_end_byte]
    assert span.decode("utf-8") == _BOUND_SPAN


def test_selection_is_deterministic_and_binds_the_earliest_maximal_run() -> None:
    """Same inputs, same span, every time — and ties break earliest-in-answer.

    The model no longer points at an occurrence, so a repeated sentence is not
    an ambiguity refusal any more: the server binds ONE deterministic
    occurrence (the earliest maximal run) and the downstream verifier re-slices
    exactly those bytes.
    """
    final_text = f"{_CHUNK_SENTENCE} Repeated for emphasis: {_CHUNK_SENTENCE}"
    first = _bind(final_text, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)
    second = _bind(final_text, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    assert first == second
    (bound,) = first
    assert bound.span_start_byte == 0


def test_two_sources_bind_two_claims_with_server_minted_keys() -> None:
    """One claim per source that proves a span; keys are the server's surrogates."""
    final_text = (
        'The source states: "All eleven late fees were reversed after the billing '
        'review." And separately: "The hardship credit posted to the account on '
        'May ninth." Both hold.'
    )
    claims = _bind(
        final_text,
        {_PROTECTED_REF, _PROTECTED_REF_B},
        protected_output_a=_CHUNK_SENTENCE,
        protected_output_b=_CHUNK_SENTENCE_B,
    )

    by_key = {claim.claim_key: claim for claim in claims}
    assert set(by_key) == {f"bound:{_SOURCE_ID}", f"bound:{_SOURCE_ID_B}"}
    assert by_key[f"bound:{_SOURCE_ID}"].served_ref == _PROTECTED_REF
    assert by_key[f"bound:{_SOURCE_ID_B}"].served_ref == _PROTECTED_REF_B
    encoded = final_text.encode("utf-8")
    for claim, sentence in (
        (by_key[f"bound:{_SOURCE_ID}"], _CHUNK_SENTENCE),
        (by_key[f"bound:{_SOURCE_ID_B}"], _CHUNK_SENTENCE_B),
    ):
        assert encoded[claim.span_start_byte : claim.span_end_byte].decode("utf-8") == sentence


def test_a_span_common_to_two_sources_binds_once_per_source_without_deduplication() -> None:
    """Both associations are TRUE, so both are recorded — no span dedup."""
    claims = _bind(
        _QUOTING_ANSWER,
        {_PROTECTED_REF, _PROTECTED_REF_B},
        protected_output_a=_CHUNK_SENTENCE,
        protected_output_b=f"Memo prefix. {_CHUNK_SENTENCE} Memo suffix.",
    )

    assert len(claims) == 2
    assert claims[0].span_start_byte == claims[1].span_start_byte
    assert claims[0].claim_key != claims[1].claim_key


def test_a_source_with_no_retained_text_binds_nothing_while_others_still_bind() -> None:
    """Fail closed per source: an undelivered source cannot prove a span."""
    claims = _bind(
        _QUOTING_ANSWER,
        {_PROTECTED_REF, _PROTECTED_REF_B},
        protected_output_a=_CHUNK_SENTENCE,
        # _PROTECTED_REF_B was served but its text was never delivered/retained.
    )

    assert [claim.served_ref for claim in claims] == [_PROTECTED_REF]


def test_a_run_that_served_protected_material_but_delivered_none_refuses() -> None:
    """Served-but-never-authorized still cannot publish an unbound answer."""
    with pytest.raises(ActionValidationError, match="verbatim in any source"):
        runner_module._server_bound_claims(
            _QUOTING_ANSWER, served_refs={_PROTECTED_REF}, retention=None
        )


def test_retention_poisoning_withdraws_a_source_whose_text_drifted() -> None:
    """Two different texts under one id: binding against either would be a guess."""
    from ne.runtime.source_results import ServedTextRetention

    retention = ServedTextRetention()
    retention.record(_SOURCE_ID, _CHUNK_SENTENCE)
    retention.record(_SOURCE_ID, _CHUNK_SENTENCE_B)  # drifted -> poisoned
    retention.record(_SOURCE_ID, _CHUNK_SENTENCE)  # and it stays poisoned

    assert retention.text_for(_SOURCE_ID) is None
    with pytest.raises(ActionValidationError, match="verbatim in any source"):
        runner_module._server_bound_claims(
            _QUOTING_ANSWER, served_refs={_PROTECTED_REF}, retention=retention
        )


def test_retention_refuses_to_grow_past_its_caps_instead_of_truncating() -> None:
    """An over-cap text is NOT retained (fail closed) and never truncated —
    a truncated buffer could miss a span the run really served, which would
    manufacture a false refusal over real bytes."""
    from ne.runtime import source_results as sr

    retention = sr.ServedTextRetention()
    oversized = "word " * (sr._MAX_RETAINED_TEXT_CHARS // 5 + 5)
    retention.record(_SOURCE_ID, oversized)
    assert retention.text_for(_SOURCE_ID) is None

    filler = "x" * sr._MAX_RETAINED_TEXT_CHARS
    for index in range(sr._MAX_RETAINED_TOTAL_CHARS // sr._MAX_RETAINED_TEXT_CHARS):
        retention.record(f"filler-{index}", filler)
    retention.record(_SOURCE_ID_B, _CHUNK_SENTENCE_B)
    assert retention.text_for(_SOURCE_ID_B) is None  # total cap reached: not retained


def test_retention_repr_and_len_carry_no_retained_byte() -> None:
    """The retention object can be interpolated into anything without leaking."""
    from ne.runtime.source_results import ServedTextRetention

    retention = ServedTextRetention()
    retention.record(_SOURCE_ID, _CHUNK_SENTENCE)

    rendered = repr(retention)
    assert len(retention) == 1
    for token in _CHUNK_SENTENCE.split():
        assert token not in rendered
    assert "entries=1" in rendered


def test_the_match_pair_budget_skips_a_pathological_source_deterministically() -> None:
    """Over the pair cap the source is skipped (refusing more, never binding more)."""
    stutter = "same " * 600  # 600 x 600 same-word pairs > _MAX_SPAN_MATCH_PAIRS
    assert runner_module._longest_common_word_run(stutter.split(), stutter.split()) is None


@pytest.mark.parametrize(
    "sample",
    [
        "",
        "   ",
        "plain sentence",
        "  leading and trailing  ",
        "wrapped\nlines\tand\u00a0nbsp",
        "curly \u2018quotes\u2019 and \u2014 dashes",
        "Zo\u00eb  opened\n\n the filing.",
    ],
)
def test_the_locating_fold_is_one_fold_with_positions(sample: str) -> None:
    """`_fold_for_locating` IS `ws+punct`, carrying positions — nothing more.

    The scan is written once with an origin index per kept character; this
    equality keeps it from drifting from the two named fold functions it
    composes, and the origin invariants keep a mapped-back span from ever
    running backwards or reaching past what it covers.
    """
    sample = sample.encode().decode("unicode_escape")
    folded, origin = runner_module._fold_for_locating(sample)

    assert folded == runner_module._fold_whitespace(runner_module._fold_punctuation(sample))
    assert len(origin) == len(folded)
    assert list(origin) == sorted(origin)
    assert all(0 <= index < len(sample) for index in origin)


def test_folded_words_carry_their_exact_folded_starts() -> None:
    folded, _ = runner_module._fold_for_locating("  Alpha   beta\n gamma. ")
    words, starts = runner_module._folded_words(folded)

    assert words == ("Alpha", "beta", "gamma.")
    assert [
        folded[start : start + len(word)] for word, start in zip(words, starts, strict=True)
    ] == list(words)


# --- the model's authored surface is final_text, and only final_text ---------


def test_model_declared_claims_are_parsed_and_ignored_entirely() -> None:
    """The tolerance seam: an old-prompt habit is not shape-rejected, not read.

    A continued run's history still teaches claim declarations. They parse, and
    then the server binds exactly what it would have bound with none — same
    spans, same keys, same sources — and a junk source_id changes nothing.
    """
    declared = runner_module._StructuredAgenticCompletion.model_validate(
        {
            "final_text": _QUOTING_ANSWER,
            "claims": [
                {
                    "claim_key": "model-key",
                    "claim_text": "a sentence the answer never contained",
                    "source_id": "an-id-no-tool-returned",
                }
            ],
        }
    )
    bare = runner_module._StructuredAgenticCompletion.model_validate(
        {"final_text": _QUOTING_ANSWER}
    )

    retention = _retention_over(protected_output_a=_CHUNK_SENTENCE)
    with_claims = runner_module._server_bound_claims(
        declared.final_text, served_refs={_PROTECTED_REF}, retention=retention
    )
    without = runner_module._server_bound_claims(
        bare.final_text, served_refs={_PROTECTED_REF}, retention=retention
    )

    assert with_claims == without
    assert with_claims[0].claim_key == f"bound:{_SOURCE_ID}"


@pytest.mark.parametrize(
    "forbidden",
    [
        "claim_id",
        "evidence_id",
        "locator",
        "verdict",
        "verifier_receipt",
        "event_id",
        # NE-CLAIM-DERIVE: the span's offsets and its content hash are SERVER
        # facts. The vestigial declaration still refuses them, so even the
        # tolerated field cannot smuggle a server-owned assertion.
        "span_start_byte",
        "span_end_byte",
        "claim_content_hash",
    ],
)
def test_structured_claim_model_cannot_author_server_evidence_fields(forbidden: str) -> None:
    claim = _claim("claim", claim_key="claim-a")
    claim[forbidden] = "model-authored"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        runner_module._StructuredAgenticCompletion.model_validate(
            {"final_text": "claim", "claims": [claim]}
        )


def test_a_source_free_answer_binds_nothing_and_is_not_refused() -> None:
    """No protected material served: nothing to bind, nothing to demand."""
    assert _bind("Any answer at all.", set()) == ()
    # A legacy row identity is not protected source material either.
    assert _bind("Any answer at all.", {_REF}) == ()


def test_colliding_reference_ids_are_quarantined_and_the_rest_still_bind() -> None:
    """Two served references sharing one id cannot prove which text is whose.

    The colliding id is excluded from binding (same fail-closed answer the old
    ambiguous-id refusal gave), the un-collided source still binds, and the
    `_claimable_sources` quarantine is asserted directly.
    """
    twin = _protected_ref(reference_id=_SOURCE_ID, result_hash="f" * 64)
    assert twin != _PROTECTED_REF
    table, ambiguous = runner_module._claimable_sources({_PROTECTED_REF, twin, _PROTECTED_REF_B})
    assert ambiguous == frozenset({_SOURCE_ID})
    assert table == {_SOURCE_ID_B: _PROTECTED_REF_B}

    claims = _bind(
        'The source states: "The hardship credit posted to the account on May ninth." Noted.',
        {_PROTECTED_REF, twin, _PROTECTED_REF_B},
        protected_output_a=_CHUNK_SENTENCE,
        protected_output_b=_CHUNK_SENTENCE_B,
    )
    assert [claim.served_ref for claim in claims] == [_PROTECTED_REF_B]


def _everything_the_model_was_sent(gateway: Any) -> str:
    """Every byte this run put in front of the model, across all requests."""
    return "\n".join(
        str(getattr(message, "content", ""))
        for request in gateway.requests
        for message in request.messages
    )


def _source_bearing_gateway(*finals: Any) -> Any:
    return ScriptedGateway(
        [tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}), *finals]
    )


def _two_source_executor() -> Any:
    """An executor whose one read serves TWO citable references.

    NE-CLAIM-BIND: a run that served exactly one reference no longer consults the
    model's `source_id` at all, so a mutation about an INVENTED id has to be posed
    over a run where the id decides something. Two references is the smallest run
    where it does.
    """
    return _DraftExecutor(read_output=f"{_PROTECTED_REF_B}\n{_PROTECTED_REF}")


#: An id no tool result in any of these runs ever carried.
_UNSERVED_SOURCE_ID = "protected-output-never-served"


def _quoting_scenario(
    *finals: Any,
    resolver: _MutableAuthorityResolver | None = None,
    executor: _Executor | None = None,
    sink: RecordingAuditSink | None = None,
) -> tuple[Any, Any]:
    """One protected read whose delivery carries ``_CHUNK_SENTENCE``, then ``finals``."""
    resolver = resolver or _MutableAuthorityResolver(materialized_text=_CHUNK_SENTENCE)
    gateway = _source_bearing_gateway(*finals)
    result = _run(
        gateway,
        executor or _DraftExecutor(),
        sink or RecordingAuditSink(),
        source_guard=_source_guard(resolver),
    )
    return gateway, result


def test_an_answer_quoting_its_source_binds_with_no_model_authored_text() -> None:
    """The mutation this whole change exists for, run end to end.

    The model reads a protected source, quotes its sentence inside final_text,
    and declares NOTHING — no claims, no ids, no offsets, no hashes. The server
    retained the delivered text, selects the quoted span itself, and the answer
    is served bound, first try, no correction spent.
    """
    gateway, result = _quoting_scenario(_structured_completion(_QUOTING_ANSWER))

    assert result.outcome == "complete"
    assert result.output == _QUOTING_ANSWER
    (bound,) = result.claims
    assert bound.claim_key == f"bound:{_SOURCE_ID}"
    assert bound.served_ref == _PROTECTED_REF
    span = _QUOTING_ANSWER.encode("utf-8")[bound.span_start_byte : bound.span_end_byte]
    assert span.decode("utf-8") == _BOUND_SPAN
    assert bound.claim_content_hash == hashlib.sha256(span).hexdigest()
    # One tool call, ONE final answer: the same cost a source-free run pays.
    assert result.requests_used == 2
    assert "could not be bound" not in _everything_the_model_was_sent(gateway)


def test_an_unquotable_answer_is_corrected_and_the_quoting_resend_is_served() -> None:
    """The retry TEACHES the shape: quote the source, the server does the rest."""
    gateway, result = _quoting_scenario(
        _structured_completion(_UNQUOTABLE_ANSWER),
        _structured_completion(_QUOTING_ANSWER),
    )

    assert result.outcome == "complete"
    assert result.output == _QUOTING_ANSWER
    assert result.claims[0].claim_key == f"bound:{_SOURCE_ID}"
    sent = _everything_the_model_was_sent(gateway)
    # The model was actually TOLD the rule it is failed against, with the
    # producible exemplar, and the retry spends exactly one reserved request.
    assert runner_module._CLAIM_NO_COMMON_SPAN in sent
    assert 'The source states: "' in sent
    assert result.requests_used == 3


def test_the_no_span_correction_never_invites_an_invented_quotation() -> None:
    """C12 and the honesty floor: static text, and an honest way out."""
    correction = runner_module._claim_correction(runner_module._CLAIM_NO_COMMON_SPAN)

    # The honest way out when the source says nothing useful is to SAY SO,
    # never to manufacture a quotation that supports the point.
    assert "rather than invent a quotation" in correction
    assert "say the sources do not state it" in correction
    # It teaches the producible shape and forbids the impossible ones.
    assert 'The source states: "' in correction
    assert "no offsets and no hashes" in correction
    assert "sha256" not in correction.lower()
    # And it never carries a reference or a byte of run content.
    assert "ne-source-ref:v1:" not in correction
    assert _PROTECTED_REF not in correction


def test_an_answer_that_cannot_quote_still_fails_and_names_itself() -> None:
    """The paired mutation: enforcement is not traded for a green run.

    A model that keeps answering in its own words does NOT get served. The run
    fails, names the one rule that broke, shows the attempt sequence, and leaks
    no byte of the answer or the retained chunk into the published reason.
    """
    with pytest.raises(AgentOutputContractError) as raised:
        _quoting_scenario(
            _structured_completion(_UNQUOTABLE_ANSWER),
            _structured_completion("Still my own words, still not the source's."),
        )

    reason = raised.value.safe_reason
    assert "could not be bound" in reason
    assert runner_module._CLAIM_NO_COMMON_SPAN in reason
    assert "Attempts: no-span,no-span." in reason
    assert "maxrun=" in reason  # the content-free shape of the final miss
    assert len(reason) <= runner_module._MAX_SAFE_REASON_CHARS
    assert _UNQUOTABLE_ANSWER not in reason
    for token in ("eleven", "fees", "reversed", "billing"):
        assert token not in reason


def test_no_failure_surface_carries_a_retained_byte() -> None:
    """RETENTION IS SECURITY-SENSITIVE: authorized chunk text lives in memory
    for the run and may reach exactly one place — the model, inside its own
    authorized delivery. Audit rows, step records, event details and the
    published failure reason are asserted clean here, over a run that FAILED
    while holding the text (the failure path is where sloppy interpolation
    would leak it)."""
    sink = RecordingAuditSink()
    with pytest.raises(AgentOutputContractError) as raised:
        _quoting_scenario(
            _structured_completion(_UNQUOTABLE_ANSWER),
            _structured_completion(_UNQUOTABLE_ANSWER),
            sink=sink,
        )

    surfaces = [raised.value.safe_reason, str(raised.value)]
    for event in sink.events:
        surfaces.extend(
            str(getattr(event, name, "")) for name in ("detail", "resource", "action", "outcome")
        )
    marker = "eleven late fees"  # a distinctive mid-chunk phrase
    for surface in surfaces:
        assert marker not in surface
        assert _CHUNK_SENTENCE not in surface


def test_retention_is_created_per_run_and_cleared_when_the_run_returns(monkeypatch) -> None:
    """The buffer's lifetime is ONE `run_model_planned_agent` call, provably."""
    from ne.runtime.source_results import ServedTextRetention

    created: list[Any] = []

    class _Watched(ServedTextRetention):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(runner_module, "ServedTextRetention", _Watched)
    _gateway, result = _quoting_scenario(_structured_completion(_QUOTING_ANSWER))

    assert result.outcome == "complete"
    assert len(created) == 1  # one buffer per run, not per delivery
    assert len(created[0]) == 0  # and nothing survives the run's finally


def test_a_multi_source_run_binds_each_quoted_source_with_no_id_to_mistype() -> None:
    """NE-CLAIM-BIND's last open case closes: several sources, still no ids.

    The old contract made the model transcribe a uuid to disambiguate multiple
    sources. The server now derives the association per source from the texts
    themselves, so a two-source run binds both quoted sentences with the model
    naming nothing."""
    answer = (
        'One source states: "All eleven late fees were reversed after the billing '
        'review." The other adds: "The hardship credit posted to the account on '
        'May ninth."'
    )
    resolver = _MutableAuthorityResolver(
        materialized_text={_SOURCE_ID: _CHUNK_SENTENCE, _SOURCE_ID_B: _CHUNK_SENTENCE_B}
    )

    class _TwoReadExecutor(_DraftExecutor):
        """Each read serves ONE reference — a delivery site carries one ref."""

        def __init__(self) -> None:
            super().__init__()
            self._outputs = [_PROTECTED_REF, _PROTECTED_REF_B]

        async def execute(self, tool: str, args: Any, *, principal: Principal) -> str:
            if tool == "read.kg.directory":
                self.read_output = self._outputs.pop(0)
            return await super().execute(tool, args, principal=principal)

    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Read source A."}),
            tool_call_completion("read.kg.directory", {"step_intent": "Read source B."}),
            _structured_completion(answer),
        ]
    )
    result = _run(
        gateway,
        _TwoReadExecutor(),
        RecordingAuditSink(),
        source_guard=_source_guard(resolver),
        max_tools=3,
    )

    assert result.outcome == "complete"
    assert {claim.served_ref for claim in result.claims} == {_PROTECTED_REF, _PROTECTED_REF_B}
    assert {claim.claim_key for claim in result.claims} == {
        f"bound:{_SOURCE_ID}",
        f"bound:{_SOURCE_ID_B}",
    }


def test_a_source_free_run_still_completes_with_no_claims() -> None:
    """Nothing was retrieved, so nothing must be cited — unchanged behaviour."""
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Look."}),
            _structured_completion("No source was needed."),
        ]
    )

    result = _run(gateway, _Executor(), RecordingAuditSink())

    assert result.outcome == "complete"
    assert result.claims == ()
    assert "could not be bound" not in _everything_the_model_was_sent(gateway)


def test_the_request_budget_reserves_the_retry_it_allows() -> None:
    """Without the reserved request the retry would report the WRONG terminal.

    A claim violation that exhausts the budget instead of the retry surfaces as
    `budget-exhausted`/`requests` — a cap the operator never hit, blamed for a
    contract the model broke.
    """
    # The pool is the SUM of the per-class allowances — derived from the
    # enumeration, not chosen — because pydantic-ai charges every class to one
    # counter and cannot reserve a slot for a particular validator.
    # THREE of the four classes are constants; `wrapped-call` is sized to the
    # LADDER (NE-WRAPPED-CALL-CLASS) and therefore reads out of the resolver,
    # not the raw table. Pinned as LITERALS on purpose: this is the one place the
    # measured numbers are stated, so re-measuring one has to come back here.
    #
    # `answer-shape` is FOUR: THREE was the 2026-08-28 three-correction
    # measurement, and the fourth slot is the headroom NE-SHAPE-ALLOWANCE-4
    # added — whose own measurement (`opens=prose ... objects=1 ...
    # sole=tool-call`) has since moved to `wrapped-call`, as the constant's
    # comment says in full.
    allowances = dict(runner_module.output_retry_allowances(4))
    assert allowances == {
        "protocol-residue": 1,
        "wrapped-call": 4,
        "answer-shape": 4,
        "claim-contract": 1,
    }
    assert sum(allowances.values()) == runner_module.output_retries(4) == 10
    assert tuple(allowances) == runner_module._CORRECTABLE_OUTPUT_VIOLATIONS
    # THE LADDER-SIZED CLASS IS THE POINT: it is the ONE entry that moves when
    # the ladder does, and it moves ONE FOR ONE. A constant here would be the
    # non-convergence this class exists to remove — funding it bought exactly one
    # more rung per increment against a ladder that needs many.
    assert dict(runner_module.output_retry_allowances(17))["wrapped-call"] == 17
    assert dict(runner_module.output_retry_allowances(34))["wrapped-call"] == 34
    for ladder in (1, 4, 17, 34):
        constants = {
            name: allowance
            for name, allowance in runner_module.output_retry_allowances(ladder)
            if name != "wrapped-call"
        }
        assert constants == {"protocol-residue": 1, "answer-shape": 4, "claim-contract": 1}
    # The request ceiling FOLLOWS the allowances rather than being re-chosen:
    # asserted both as the derivation and as the arithmetic it now produces, so
    # a raised allowance that failed to move the reserve fails here.
    assert runner_module.model_request_limit(4) == 4 + 1 + runner_module.output_retries(4)
    assert runner_module.model_request_limit(4) == 15
    # `2 * max_tool_calls + 7` — the worst case every `max_input_tokens` in
    # `deploy/agents/registry.toml` is pinned by equality to.
    assert runner_module.model_request_limit(17) == 41
    assert runner_module.model_request_limit(34) == 75


def test_the_claim_correction_survives_an_exhausted_answer_shape_allowance() -> None:
    """The starvation bug, pinned: the reserved slot is still there afterwards.

    pydantic-ai keeps ONE output-retry counter
    (``GraphAgentState.output_retries_used``, ``_agent_graph.py:143``) and debits
    it for schema rejections and output-validator ``ModelRetry``s alike. Here the
    shape allowance is spent to the last slot BEFORE the claim contract is ever
    reached, and the claim correction still goes out and still works.
    """
    gateway, result = _quoting_scenario(
        # Three replies that fail the schema — the full `answer-shape` allowance.
        *[text_completion('{"answer": "wrong shape"}')] * 3,
        # A well-shaped answer that cannot be bound.
        _structured_completion(_UNQUOTABLE_ANSWER),
        # The correction lands, and the quoting answer is served.
        _structured_completion(_QUOTING_ANSWER),
    )

    assert result.outcome == "complete"
    assert result.output == _QUOTING_ANSWER
    # The claim correction was actually DELIVERED — the whole defect this
    # allowance partition fixed was that it never was.
    assert runner_module._CLAIM_NO_COMMON_SPAN in _everything_the_model_was_sent(gateway)
    # 1 tool call + 5 final-answer attempts, all inside the reserved headroom.
    assert result.requests_used == 6


def test_no_violation_class_can_spend_the_claim_contracts_reserved_slot() -> None:
    """A class that outlives its OWN allowance ends the run instead of borrowing.

    Without this the pool is fungible again and the reservation is nominal: a
    fourth schema rejection would simply take the slot the claim contract is
    holding. It ends named — the schema location and Pydantic's error code are
    still published, because the rejected ``ValidationError`` is chained onto the
    exhausted-allowance error rather than dropped.
    """
    gateway = _source_bearing_gateway(*[text_completion('{"answer": "wrong shape"}')] * 6)

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, _DraftExecutor(), RecordingAuditSink())

    reason = raised.value.safe_reason
    assert "did not match this Run's required answer format" in reason
    assert "final_text" in reason
    # Exactly the allowance, and not one slot more: the 4th rejection ended it.
    assert _everything_the_model_was_sent(gateway).count("final_text") >= 1


def test_a_fabricated_quotation_still_fails_hard_with_the_reserve_untouched() -> None:
    """The guarantee the reserve must not buy: a made-up quotation is still fatal.

    The answer QUOTES — quotation marks, `The source states:`, the whole shape —
    but the quoted sentence exists in no served chunk. Budget is deliberately
    plentiful (nothing else has spent a slot), so a green run would mean the
    wider pool had relaxed enforcement. It does not: nothing verbatim, no bind,
    and the run fails naming the rule.
    """
    fabricated = (
        'The source states: "Every fee is refunded automatically each quarter." Clear enough.'
    )
    with pytest.raises(AgentOutputContractError) as raised:
        _quoting_scenario(*[_structured_completion(fabricated)] * 3)

    assert runner_module._CLAIM_NO_COMMON_SPAN in raised.value.safe_reason


@pytest.mark.parametrize("rejection", runner_module._CLAIM_REJECTION_REASONS)
def test_every_claim_rejection_reason_survives_the_projection_cap(rejection: str) -> None:
    """A reason the projection drops is another `failure_reason: null`.

    `RunProjection._failure_reason` discards any `error_reason` over 256
    characters or carrying a non-printable character, so a reason that names the
    cause but overruns the cap explains nothing at all. Every rejection the claim
    contract can raise is measured here, with the widest shape string the span
    selector can attach riding along.
    """
    record = runner_module._ClaimContractRejection()
    record.record(rejection, shape="aw=100000 sources=128 maxrun=4 floor=5")
    reason = runner_module._output_contract_reason(
        UnexpectedModelBehavior("Exceeded maximum output retries (1)"),
        claim_rejection=record,
    )

    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS == 256
    assert all(character.isprintable() for character in reason)
    assert rejection in reason
    assert "Attempts:" in reason
    assert "maxrun=4" in reason


def test_the_attempt_sequence_still_counts_every_correction_the_run_spent() -> None:
    """`residue` rounds ride in front of the claim rejections, as before."""
    record = runner_module._ClaimContractRejection()
    record.record(runner_module._CLAIM_NO_COMMON_SPAN)
    record.record(runner_module._CLAIM_NO_COMMON_SPAN)
    reason = runner_module._output_contract_reason(
        UnexpectedModelBehavior("Exceeded maximum output retries (1)"),
        claim_rejection=record,
        residue_retries=1,
    )

    assert "Attempts: residue,no-span,no-span." in reason


def test_the_guard_delivers_a_citable_id_and_still_never_delivers_the_reference() -> None:
    """NE-REF-VOCAB: the id crosses beside authorized text; the reference never.

    The reference STILL never crosses to the model. What crosses is its
    `reference_id` and the authorized text — the same text the run retains to
    bind the span, so the retention vocabulary and the delivery vocabulary
    cannot diverge.
    """
    gateway, result = _quoting_scenario(_structured_completion(_QUOTING_ANSWER))

    sent = _everything_the_model_was_sent(gateway)
    # The id reached the model, attached to the material it names...
    assert f"[source-id: {_SOURCE_ID}]" in sent
    assert _CHUNK_SENTENCE in sent
    # ...and the reference did not. Run the SERVER'S OWN extractor — the one
    # that built `recorded.served` from the tool result — over every byte the
    # model received: it still finds nothing.
    assert _PROTECTED_REF not in sent
    assert runner_module._served_refs(sent) == ()
    # The server bound the claim back to the full reference, so everything
    # downstream of the runner sees exactly what it always saw.
    assert result.claims[0].served_ref == _PROTECTED_REF


def test_the_system_prompt_still_carries_one_unambiguous_final_answer_protocol() -> None:
    """NE-OUTPUT-CONTRACT, re-pinned over the new tail.

    The in-band catalog's plain-text-final sentence stays REPLACED — a system
    message that says both "answer in plain text (no JSON)" and "answer with
    exactly one JSON object" makes the model guess, and the losing guess burns
    the whole output-retry budget. The prompted schema remains the final system
    instruction and the tail remains its last words.
    """
    gateway, _result = _quoting_scenario(_structured_completion(_QUOTING_ANSWER))

    system = gateway.requests[0].messages[0]
    assert system.role == "system"
    assert "plain text" not in system.content
    assert runner_module._STRUCTURED_AGENTIC_FINAL_ANSWER in system.content
    assert system.content.endswith(runner_module._STRUCTURED_AGENTIC_COMPLETION_TAIL)
    assert system.content.index('"$defs"') < system.content.rindex("FINAL RESPONSE TRANSPORT")
    assert system.content.rindex('"final_text"') > system.content.rindex("FINAL RESPONSE TRANSPORT")


def test_every_prompt_teaches_the_quotation_and_never_a_reference_string() -> None:
    """A prompt that instructs the impossible is worse than no prompt.

    Both model-facing texts — the transport tail and the one claim correction —
    teach the SAME producible shape: quote the source's sentence verbatim inside
    final_text, declare nothing. Neither asks for an id, an offset, a hash or a
    reference string, and neither carries one.
    """
    tail = runner_module._STRUCTURED_AGENTIC_COMPLETION_TAIL
    correction = runner_module._claim_correction(runner_module._CLAIM_NO_COMMON_SPAN)

    for prompt in (tail, correction):
        assert 'The source states: "' in prompt
        assert "character for character" in prompt
        assert "ne-source-ref:v1:" not in prompt
        assert _PROTECTED_REF not in prompt
    # The tail names the [source-id: ...] line only to say what a source-bearing
    # result LOOKS like — never to ask the model to copy the id anywhere.
    assert "[source-id: <id>]" in tail
    assert "you declare no claims, no ids, no offsets and no hashes" in tail


# --- NE-RESIDUE-RETRY: a fumbled tool call is corrected, not served ---------


#: A tool call the strict shape rejects — no ``name`` key. `_parse` demotes it to
#: text by the parse-never-raises design, which is how it ever reached the answer
#: schema in the first place.
_MALFORMED_CALL = '{"tool_call": {"arguments": {"step_intent": "Read evidence."}}}'


def test_a_malformed_tool_call_earns_a_correction_and_the_corrected_call_runs() -> None:
    """The mutation this exists for: the model gets back INSIDE the protocol.

    Live 2026-08-27: the run used 2 of its 10 tool calls, fumbled the syntax of a
    third, and died — the residue became its "final answer" and failed the answer
    schema. Eight tool calls unused because one object was mistyped and there was
    no way back.
    """
    answer = "Meridian ships in Q3."
    executor = _Executor()
    gateway = ScriptedGateway(
        [
            text_completion(_MALFORMED_CALL),
            tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
            _structured_completion(answer),
        ]
    )

    result = _run(gateway, executor, RecordingAuditSink())

    assert result.outcome == "complete"
    assert result.output == answer
    # The corrected call actually EXECUTED — the residue did not cost the run its
    # tool budget, it cost it one model request.
    assert [tool for tool, _args, _principal in executor.calls] == ["read.kg.directory"]
    assert result.claims == ()
    # And the model was told the call shape, not handed a dump of Pydantic errors
    # that never mentions the tool protocol.
    sent = _everything_the_model_was_sent(gateway)
    assert "MALFORMED and has been rejected" in sent
    assert '"name"' in sent


def test_the_residue_correction_points_at_this_loop_s_final_answer_not_prose() -> None:
    """Same predicate as the session path; deliberately NOT the same tail.

    The session loop is ``output_type=str``, so its correction ends "respond with
    your final answer as plain text with NO JSON". This loop REJECTS a plain-text
    final answer. Reusing that sentence here would instruct the model to do the
    one thing this loop refuses — so the shared half is the call shape, and the
    tail is per-loop.
    """
    assert PROTOCOL_RESIDUE_CALL_CORRECTION in runner_module._RESIDUE_RETRY
    assert "plain text with NO JSON" not in runner_module._RESIDUE_RETRY
    assert "FINAL RESPONSE TRANSPORT" in runner_module._RESIDUE_RETRY
    assert "not a valid final answer here" in runner_module._RESIDUE_RETRY


def test_residue_that_survives_the_bounded_retry_still_fails_named() -> None:
    """The refusing half of the pair: correction is bounded, and never invents.

    A model that keeps emitting residue does NOT get it served as an answer and
    does NOT loop forever. The run fails, and the reason carries the same
    ``obj=tool-call`` token the schema seam emits — the same mistake reads the
    same way whichever seam caught it.
    """
    gateway = ScriptedGateway([text_completion(_MALFORMED_CALL)] * 8)

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, _Executor(), RecordingAuditSink())

    reason = raised.value.safe_reason
    assert "malformed tool call" in reason
    assert "[obj=tool-call]" in reason
    assert len(reason) <= runner_module._MAX_SAFE_REASON_CHARS
    # It is a FAILURE, never a laundered answer.
    assert "tool_call" not in reason.replace("[obj=tool-call]", "")


def test_a_well_formed_tool_call_never_reaches_the_residue_capability() -> None:
    """Unaffected: a good call is a ``ToolCallPart`` before output processing runs."""
    executor = _Executor()
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
            _structured_completion("done"),
        ]
    )

    result = _run(gateway, executor, RecordingAuditSink())

    assert result.outcome == "complete"
    assert [tool for tool, _args, _principal in executor.calls] == ["read.kg.directory"]
    assert "MALFORMED" not in _everything_the_model_was_sent(gateway)


def test_a_control_character_answer_is_delivered_without_spending_a_retry() -> None:
    """NE-ANSWER-CTRLCHAR — the S1 live failure's `opens=object` half, pinned.

    Measured on train-49 (worker log): the model sent the CORRECT answer as
    `{"final_text":"..."}` with a literal line break inside the string, strict
    JSON rejected it (`json_invalid`, control character), and the run produced
    no answer at all. The object the model wrote IS the answer; only its
    line-break spelling was wrong, and respelling it is transport, not repair.
    """
    final = '{"final_text":"Eligible: mer-loan-001.\nExcluded: mer-loan-005.","claims":[]}'
    gateway = ScriptedGateway([text_completion(final)])

    result = _run(gateway, _Executor(), RecordingAuditSink())

    assert result.outcome == "complete"
    assert result.output == "Eligible: mer-loan-001.\nExcluded: mer-loan-005."
    # Recovered at the transport seam: no corrective round-trip was needed.
    assert result.requests_used == 1


def test_a_prose_final_answer_is_corrected_with_the_transport_restated() -> None:
    """NE-RETRY-LEGIBLE — the S1 live failure's `opens=prose` half, pinned.

    The retry the model receives must SAY what to do — pydantic-ai's own
    "Fix the errors and try again." rendering plus this loop's transport
    restatement — and must not be the old bare error-details dump that echoed
    the model's whole rejected reply back as `input` with no instruction,
    which the live model answered with prose four consecutive times.
    """
    answer = "Eligible: mer-loan-001, mer-loan-002, mer-loan-003."
    gateway = ScriptedGateway(
        [
            text_completion("Based on the gathered evidence, the eligible loans are three."),
            _structured_completion(answer),
        ]
    )

    result = _run(gateway, _Executor(), RecordingAuditSink())

    assert result.outcome == "complete"
    assert result.output == answer
    sent = _everything_the_model_was_sent(gateway)
    # The corrective retry restates the transport and the escape rule…
    assert runner_module._STRUCTURED_ANSWER_RETRY_CORRECTION in sent
    # …and a reply that really was an ATTEMPTED ANSWER is unaffected by
    # NE-WRAPPED-CALL: it still gets the answer transport, not the call one.
    assert runner_module._WRAPPED_CALL_RETRY not in sent
    assert "Fix the errors and try again." in sent
    # …and no longer carries the raw error-details echo of the reply.
    assert '"input"' not in sent


def test_prose_that_survives_the_bounded_retry_still_fails_named() -> None:
    """The refusing half: the corrective retry is bounded and never invents.

    A model that answers the correction with prose again does NOT get an object
    manufactured for it; the run fails with the same named `json_invalid` shape
    reason as before, and the claim contract's reserved slot stays untouched.
    """
    gateway = ScriptedGateway([text_completion("Prose, and prose only.")] * 8)

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, _Executor(), RecordingAuditSink())

    reason = raised.value.safe_reason
    assert "did not match this Run's required answer format" in reason
    assert "json_invalid" in reason
    assert "opens=prose" in reason
    # 1 first attempt + the full answer-shape allowance, not one more. Derived
    # from the allowance rather than spelled 5, so a re-measured allowance moves
    # this bound with it instead of silently becoming a different assertion.
    shape_allowance = dict(runner_module._OUTPUT_RETRY_ALLOWANCES)["answer-shape"]
    assert shape_allowance == 4
    assert len(gateway.requests) == 1 + shape_allowance == 5


def test_a_prose_wrapped_tool_call_recovers_through_the_corrective_retry() -> None:
    """The measured cost of the NE-CALL-SELECT revert on the S5 shape.

    NE-CALL-SELECT (593ef6d5, reverted 2026-08-29 by arbiter ruling) existed
    because a prose-wrapped call was believed to burn the run's whole
    output-retry budget — a claim that predated NE-RETRY-LEGIBLE. This pins
    what actually happens now: the wrapped write (preamble + literal-newline
    body, the live S5 ``draft.email.create`` shape) stays TEXT, is rejected as
    an answer, and the model receives the corrective restatement — after which
    a compliant resend of the SAME call as one line of strict JSON executes
    through the governed path with the multi-line body intact. Cost: exactly
    ONE extra model request, charged to the answer-shape allowance (3 per
    run), not a dead turn.
    """
    executor = _Executor()
    sink = RecordingAuditSink()
    body = "Dear customer,\nYour loan qualifies."
    wrapped = (
        "Drafting the outreach now.\n"
        '{"tool_call": {"name": "write.internal.note", "arguments": '
        '{"body": "Dear customer,\nYour loan qualifies.", '
        '"step_intent": "Park the note.", "evidence_refs": ["' + _REF + '"]}}}'
    )
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
            # The S5 failure shape, verbatim: a preamble-wrapped call whose
            # string argument carries a LITERAL newline. Never promoted.
            text_completion(wrapped),
            # The compliant resend the correction asks for: the same call as
            # exactly one line of JSON, line break escaped.
            tool_call_completion(
                "write.internal.note",
                {"body": body, "step_intent": "Park the note.", "evidence_refs": [_REF]},
            ),
            _structured_completion("Parked."),
        ]
    )

    result = _run(gateway, executor, sink)

    assert result.outcome == "complete"
    # The governed write ran once, with the model's own line break intact —
    # the wrapped emission itself executed NOTHING.
    assert executor.calls == [
        ("read.kg.directory", {}, _PRINCIPAL.id),
        ("write.internal.note", {"body": body}, _PRINCIPAL.id),
    ]
    # Cost: one corrective round-trip — 4 requests for a run that would have
    # taken 3 had the call been spelled to protocol the first time.
    assert len(gateway.requests) == 4
    # And the round-trip carried the correction for the mistake ACTUALLY made —
    # the call transport (NE-WRAPPED-CALL), not the answer transport's
    # restatement, which would have told the model to abandon the write.
    sent = _everything_the_model_was_sent(gateway)
    assert runner_module._WRAPPED_CALL_RETRY in sent
    assert runner_module._STRUCTURED_ANSWER_RETRY_CORRECTION not in sent


def test_a_wrapped_call_correction_does_not_ask_the_model_to_abandon_the_call() -> None:
    """NE-WRAPPED-CALL — the S5 live failure, reproduced end to end.

    Measured on run ``f9826d5f`` (2026-09-10, both owner-authorised sends): the
    model wrapped its ``draft.email.create`` call in a prose preamble, the call
    was demoted to text, and the rejection earned the ANSWER transport's
    restatement — "Send the same answer again as ONE JSON object with …
    final_text" — advice that instructs a model which tried to CALL A TOOL to
    stop calling it. Four such replies spent the answer-shape allowance and the
    run died before the draft existed, with no cap hit.

    The mutation, both directions: the retry that comes back must name the CALL
    transport and must NOT carry the answer restatement. On the unfixed adapter
    the second assertion fails — the answer restatement is exactly what was sent.
    """
    executor = _Executor()
    wrapped = (
        "I have everything I need, so I will now park the note.\n"
        '{"tool_call": {"name": "write.internal.note", "arguments": '
        '{"body": "Parked.", "step_intent": "Park the note.", '
        '"evidence_refs": ["' + _REF + '"]}}}'
    )
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
            text_completion(wrapped),
            # What the CALL correction asks for: the same call, whole reply.
            tool_call_completion(
                "write.internal.note",
                {
                    "body": "Parked.",
                    "step_intent": "Park the note.",
                    "evidence_refs": [_REF],
                },
            ),
            _structured_completion("Parked."),
        ]
    )

    result = _run(gateway, executor, RecordingAuditSink())

    assert result.outcome == "complete"
    sent = _everything_the_model_was_sent(gateway)
    # The correction names the call transport…
    assert "A tool call must be the ENTIRE reply." in sent
    # …and the misdirected answer restatement is NOT sent for this reply.
    assert runner_module._STRUCTURED_ANSWER_RETRY_CORRECTION not in sent
    # The governed write still ran exactly once, from the COMPLIANT resend.
    assert [tool for tool, _args, _p in executor.calls] == [
        "read.kg.directory",
        "write.internal.note",
    ]


_WRAPPED_CALL = (
    "I have everything I need, so I will now read the directory.\n"
    '{"tool_call": {"name": "read.kg.directory", "arguments": '
    '{"step_intent": "Read evidence."}}}'
)


def _schema_rejection(reply: str) -> ValidationError:
    """A REAL `_StructuredAgenticCompletion` rejection of `reply`.

    The same `ValidationError` pydantic-ai's own parse raises and hands to
    `on_output_validate_error`, rather than a hand-built stand-in that could
    disagree with it.
    """
    try:
        runner_module._StructuredAgenticCompletion.model_validate_json(reply)
    except ValidationError as exc:
        return exc
    raise AssertionError("the reply was supposed to be rejected")


def test_a_wrapped_call_and_a_bad_answer_are_charged_to_DIFFERENT_classes() -> None:
    """NE-WRAPPED-CALL-CLASS, at the one seam that charges — both directions.

    The ledger is the LAST place that called a prose-wrapped tool call an
    answer: every other seam (`sole_answer_object`, `rejected_reply_shape`,
    `_render_retry_prompt`) already treats it as a transport mistake, while
    `on_output_validate_error` charged it to `answer-shape` unconditionally.

    MUTATION, DIRECTION 1: a wrapped call must not touch `answer-shape`.
    MUTATION, DIRECTION 2: a reply that really was an attempted ANSWER must
    still spend it. Before the fix the first assertion reads
    `{"answer-shape": 1}` and the two classes are indistinguishable.
    """
    ledger = runner_module._OutputRetryLedger(
        allowances=dict(runner_module.output_retry_allowances(4))
    )
    capability = runner_module._ProtocolResidueCapability(ledger=ledger)

    async def charge(reply: str) -> None:
        await capability.on_output_validate_error(
            None,
            output_context=None,
            output=reply,
            error=_schema_rejection(reply),
        )

    # DIRECTION 1 — the transport mistake spends the LADDER-sized class.
    with pytest.raises(ValidationError):
        asyncio.run(charge(_WRAPPED_CALL))
    assert ledger.spent == {"wrapped-call": 1}
    assert capability.wrapped_call_retries == 1

    # DIRECTION 2 — an attempted answer still spends the ANSWER class.
    with pytest.raises(ValidationError):
        asyncio.run(charge("The three eligible loans are mer-loan-001 and two others."))
    assert ledger.spent == {"wrapped-call": 1, "answer-shape": 1}
    assert capability.wrapped_call_retries == 1

    # The reply is re-raised UNCHANGED either way: this hook selects a budget
    # class and nothing else — no promotion, no widened transport, no answer
    # manufactured from a call (NE-CALL-SELECT stays reverted).
    assert capability.residue_retries == 0


def test_wrapped_calls_past_the_answer_allowance_no_longer_end_the_run() -> None:
    """The deployed S5 failure, reproduced and then survived.

    FIVE consecutive prose-wrapped calls — one more than the whole `answer-shape`
    allowance — followed by a genuinely mis-shaped answer and then a good one.
    On the unfixed ledger the FIFTH wrapped call raises
    `Exceeded maximum output retries (4)` and the run dies mid-ladder holding an
    untouched tool budget, which is exactly what the live run did. Here the run
    completes, and the answer allowance it never meant to touch is still whole
    enough to correct the one answer that really was mis-shaped.

    `max_tools=6` so the ladder-sized allowance is SIX: the class is sized to the
    ladder, which is the property that makes this converge for a 14-rung ladder
    instead of buying one more rung per increment.
    """
    gateway = ScriptedGateway(
        [text_completion(_WRAPPED_CALL)] * 5
        + [
            text_completion("Prose, which really is an attempted answer."),
            _structured_completion("Done."),
        ]
    )
    executor = _Executor()

    result = _run(gateway, executor, RecordingAuditSink(), max_tools=6, wall=5.0)

    assert result.outcome == "complete"
    assert result.output == "Done."
    # Five wrapped rejections + one answer rejection + the good answer.
    assert len(gateway.requests) == 7
    # And NOT ONE of them executed: a wrapped call is still never promoted.
    assert executor.calls == []
    sent = _everything_the_model_was_sent(gateway)
    assert runner_module._WRAPPED_CALL_RETRY in sent
    # The answer restatement was sent too — for the ONE reply that earned it.
    assert runner_module._STRUCTURED_ANSWER_RETRY_CORRECTION in sent


def test_a_mis_shaped_answer_cannot_borrow_the_wrapped_call_allowance() -> None:
    """DIRECTION 2 end to end: the answer class is still the binding one.

    `max_tools=6` makes `wrapped-call` SIX, larger than `answer-shape`'s four —
    so a run that could borrow it would survive six prose replies. It does not:
    the run still dies after the first attempt plus exactly the four corrections
    `answer-shape` funds, and it still says `json_invalid`/`opens=prose`.
    """
    gateway = ScriptedGateway([text_completion("Prose, and prose only.")] * 10)

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, _Executor(), RecordingAuditSink(), max_tools=6, wall=5.0)

    shape_allowance = dict(runner_module.output_retry_allowances(6))["answer-shape"]
    assert shape_allowance == 4
    assert len(gateway.requests) == 1 + shape_allowance == 5
    assert "json_invalid" in raised.value.safe_reason
    assert "opens=prose" in raised.value.safe_reason


def test_an_exhausted_wrapped_call_allowance_still_fails_named() -> None:
    """The refusing half of the new class: it BOUNDS, it does not launder.

    A model that wraps every rung of its own ladder is a model that cannot use
    the call transport at all, and the run must say so rather than run forever.
    It ends on the same `AgentOutputContractError` shape as every other exhausted
    class, and the reason still carries `sole=tool-call` — so this failure reads
    differently from a run that could not produce a valid answer.
    """
    gateway = ScriptedGateway([text_completion(_WRAPPED_CALL)] * 12)

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, _Executor(), RecordingAuditSink(), max_tools=2, wall=5.0)

    # `max_tools=2` => a ladder-sized allowance of two, spent by the first
    # attempt plus two corrections.
    assert dict(runner_module.output_retry_allowances(2))["wrapped-call"] == 2
    assert len(gateway.requests) == 3
    assert "sole=tool-call" in raised.value.safe_reason


def test_residue_and_a_wrapped_call_are_disjoint_by_construction() -> None:
    """Neither rejection can be charged twice, and neither can take the other's.

    A LONE malformed call is `protocol-residue` (whole reply, or its first line);
    a WRAPPED well-formed call is `wrapped-call` (not the whole reply). The two
    predicates partition the space, so a reply cannot pay both — which is what
    lets `protocol-residue` keep its measured allowance of ONE while the
    neighbouring class is sized to the ladder.
    """
    lone_malformed = '{"tool_call": {"arguments": {"step_intent": "Read evidence."}}}'
    assert is_malformed_tool_call_residue(lone_malformed)
    assert not is_prose_wrapped_tool_call(lone_malformed)
    assert is_prose_wrapped_tool_call(_WRAPPED_CALL)
    assert not is_malformed_tool_call_residue(_WRAPPED_CALL)


def test_the_residue_capability_is_installed_on_the_same_agent(monkeypatch) -> None:
    """It rides the Agent that owns the output type, so a build cannot lose it."""
    captured: dict[str, Any] = {}

    class _Agent:
        def __init__(self, *, model: Any, **kwargs: Any) -> None:
            captured["capabilities"] = kwargs.get("capabilities")
            captured["retries"] = kwargs.get("retries")

        def output_validator(self, func: Any) -> Any:
            return func

        async def run(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                output=runner_module._StructuredAgenticCompletion(final_text="done"),
                usage=SimpleNamespace(requests=1, total_tokens=1),
            )

    monkeypatch.setattr(runner_module, "Agent", _Agent)
    asyncio.run(
        run_model_planned_agent(
            task="bounded",
            instructions="governed",
            principal=_PRINCIPAL,
            executor=_Executor(),
            gateway=ScriptedGateway([text_completion("unused")]),
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=1,
            total_tokens_limit=100,
            wall_clock_seconds=1,
            max_parked_writes=0,
        )
    )

    (capability,) = captured["capabilities"]
    assert isinstance(capability, runner_module._ProtocolResidueCapability)
    assert captured["retries"] == {"output": runner_module.output_retries(1)}


@pytest.mark.parametrize(
    "prompt_name", ["_STRUCTURED_AGENTIC_COMPLETION_TAIL", "_CLAIM_NO_COMMON_SPAN"]
)
def test_every_claim_prompt_teaches_that_the_span_is_the_sources_words(prompt_name: str) -> None:
    """NE-QUOTE-THE-SOURCE: the rule that satisfies both gates, everywhere it speaks.

    The deployed prompt said `claim_text` was "one sentence of final_text" and
    nothing more, so a model answering in its own voice produced a span that
    located fine and grounded never. Measured over 21 claims from 6 runs on that
    prompt: ZERO satisfied both gates. Every model-facing text must state BOTH
    halves — the words are the source's, and they appear in the answer — because
    either half alone is a rule the model can satisfy while still failing.
    """
    named: str = getattr(runner_module, prompt_name)
    prompt = runner_module._CLAIM_CORRECTION_BODIES.get(named) or named

    lowered = prompt.lower()
    assert "chunk text" in lowered
    assert "character for character" in lowered
    assert "final_text" in prompt
    # And none of them tells the model to quote its own wording any more.
    assert "copied from final_text" not in lowered


def test_the_tail_shows_an_exemplar_of_a_quotation_carried_into_the_answer() -> None:
    """One example that teaches both constraints; the old one taught neither.

    Placeholders stay in angle brackets so nothing in the exemplar can be
    mistaken for retrieved data — and under NE-SPAN-BIND the exemplar is the
    QUOTATION inside final_text, because there is no claim object left to show.
    """
    tail = runner_module._STRUCTURED_AGENTIC_COMPLETION_TAIL

    assert 'The source states: "<the source sentence, copied exactly>"' in tail
    assert "quote the source inside your answer" in tail
    assert "An answer written entirely in your own words cannot be published over sources" in tail
    # The honest escape hatch, so the rule cannot be read as "cite something".
    assert "leave that point out rather than quote something that does not say it" in tail
    # And the model authors nothing but the answer.
    assert "you declare no claims, no ids, no offsets and no hashes" in tail


def test_bounded_step_intents_checkpoint_before_each_governed_effect() -> None:
    events: list[tuple[str, object]] = []
    executor = _OrderingExecutor(events)
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find evidence."}),
            tool_call_completion(
                "write.internal.note",
                {
                    "body": "private argument",
                    "step_intent": "Park the evidenced note.",
                    "evidence_refs": [_REF],
                },
            ),
            _structured_completion("complete"),
        ]
    )

    result = asyncio.run(
        run_model_planned_agent(
            task="do the bounded work",
            instructions="use governed tools",
            principal=_PRINCIPAL,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=2,
            total_tokens_limit=1000,
            wall_clock_seconds=1,
            max_parked_writes=1,
            boundary=_CheckpointBoundary(events),
        )
    )

    assert result.outcome == "complete"
    assert events == [
        # POST_DEMO item 12 increment 1c: the adapter's own build boundary, and it
        # belongs FIRST — it fires once, before the agent is run, so every intent
        # checkpoint and every effect is on the far side of it.
        ("heartbeat", "profile:agent-built"),
        (
            "checkpoint",
            (
                "agentic-intent:0",
                {"ordinal": 0, "tool": "read.kg.directory", "step_intent": "Find evidence."},
            ),
        ),
        ("effect", ("read.kg.directory", {})),
        (
            "checkpoint",
            (
                "agentic-intent:1",
                {
                    "ordinal": 1,
                    "tool": "write.internal.note",
                    "step_intent": "Park the evidenced note.",
                },
            ),
        ),
        ("effect", ("write.internal.note", {"body": "private argument"})),
    ]


@pytest.mark.parametrize(
    "intent",
    [None, "", "   ", "line\nbreak", "tab\tbreak", "delete\x7f", "x" * 257],
)
def test_step_intent_refuses_absent_control_or_overbound_text_before_effect(
    intent: object,
) -> None:
    executor = _Executor()
    events: list[tuple[str, object]] = []
    recorded = _RecordedExecutor(
        inner=executor,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=1,
        max_parked_writes=0,
        boundary=_CheckpointBoundary(events),
    )

    with pytest.raises(ActionValidationError, match="step_intent"):
        asyncio.run(
            recorded.execute(
                "read.kg.directory",
                {"step_intent": intent},
                principal=_PRINCIPAL,
            )
        )

    assert events == []
    assert executor.calls == []
    schema = recorded.tool_specs[0].input_schema["properties"]["step_intent"]
    assert schema["minLength"] == 1
    assert schema["maxLength"] == 256


def test_restart_materialized_run_a_sentinel_is_exact_in_run_b_gateway_history() -> None:
    sentinel = "RUN-A-GATEWAY-SENTINEL-4e0813bc"
    gateway = ScriptedGateway([_structured_completion("continued")])
    result = asyncio.run(
        run_model_planned_agent(
            task="continue Run B",
            instructions="use the preserved Conversation",
            principal=_PRINCIPAL,
            executor=_Executor(),
            gateway=gateway,
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=1,
            total_tokens_limit=1000,
            wall_clock_seconds=1,
            max_parked_writes=0,
            message_history=(
                req_user("Run A asked to preserve a sentinel."),
                resp_text(sentinel),
            ),
        )
    )

    assert result.output == "continued"
    assert len(gateway.requests) == 1
    serialized = gateway.requests[0].model_dump_json()
    assert sentinel in serialized
    assert serialized.index(sentinel) < serialized.index("continue Run B")


def test_recording_wrapper_refuses_a_principal_change_before_delegation() -> None:
    executor = _Executor()
    recorded = _RecordedExecutor(
        inner=executor,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=1,
        max_parked_writes=0,
    )

    with pytest.raises(RuntimeAuditError, match="single-principal invariant"):
        asyncio.run(
            recorded.execute(
                "read.kg.directory",
                {"step_intent": "Attempt to switch actor."},
                principal=Principal(id="agent:service-reader"),
            )
        )

    assert executor.calls == []


def test_model_authored_evidence_selector_cannot_replace_server_observed_identity() -> None:
    executor = _Executor()
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find evidence."}),
            tool_call_completion(
                "write.internal.note",
                {
                    "body": "review this",
                    "step_intent": "Try an unsupported write.",
                    "evidence_refs": ["urn:ne:meridian:dispute:invented"],
                },
            ),
            _structured_completion("write was blocked"),
        ]
    )

    result = _run(gateway, executor, sink)

    assert result.outcome == "complete"
    assert [tool for tool, _args, _principal in executor.calls] == [
        "read.kg.directory",
        "write.internal.note",
    ]
    assert result.steps[-1].outcome == "submitted"
    assert result.steps[-1].evidence_refs == (_REF,)
    assert sink.actions().count("agentic.evidence_ref") == 0


def test_model_emitted_out_of_set_name_still_reaches_the_governance_wrapper() -> None:
    class _ScopedExecutor(_Executor):
        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            if tool not in {spec.name for spec in self.tool_specs}:
                raise ToolsetViolationError("audited toolset refusal")
            return await super().execute(tool, args, principal=principal)

    executor = _ScopedExecutor()
    result = _run(
        ScriptedGateway(
            [
                tool_call_completion("write.external.exfiltrate", {}),
                _structured_completion("the out-of-set call was refused"),
            ]
        ),
        executor,
        RecordingAuditSink(),
    )

    assert result.outcome == "complete"
    assert executor.calls == [("write.external.exfiltrate", {}, _PRINCIPAL.id)]
    assert result.steps[0].outcome == "refused:ToolsetViolationError"
    assert result.steps[0].principal_id == _PRINCIPAL.id


def test_each_budget_has_a_distinct_honest_terminal() -> None:
    # Wall-clock exhaustion has its own owning test below. Disable that competing
    # cap here so host/xdist load cannot select it before the exact non-time cap
    # this test is proving; asyncio.timeout(inf) has no real-clock race.
    # Tool-call ceiling: the first call succeeds; pydantic-ai refuses the next.
    executor = _Executor()
    sink = RecordingAuditSink()
    loop = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "First."}),
            tool_call_completion("read.kg.directory", {"step_intent": "Second."}),
        ]
    )
    tool_cap = _run(loop, executor, sink, max_tools=1, wall=_NO_WALL_CLOCK_LIMIT)
    assert (tool_cap.outcome, tool_cap.exhausted_cap, tool_cap.tool_calls_used) == (
        "budget-exhausted",
        "tool-calls",
        1,
    )

    # Token ceiling checks the gateway's real reported usage and withholds output.
    token_cap = _run(
        ScriptedGateway([_structured_completion("must be withheld")]),
        _Executor(),
        RecordingAuditSink(),
        tokens=7,
        wall=_NO_WALL_CLOCK_LIMIT,
    )
    assert (token_cap.outcome, token_cap.exhausted_cap, token_cap.output) == (
        "budget-exhausted",
        "tokens",
        "",
    )

    # Parked-write ceiling is independent of the tool ceiling and fires only after
    # a valid same-run evidence ref exists.
    parked_cap = _run(
        ScriptedGateway(
            [
                tool_call_completion("read.kg.directory", {"step_intent": "Read."}),
                tool_call_completion(
                    "write.internal.note",
                    {
                        "body": "x",
                        "step_intent": "Write.",
                        "evidence_refs": [_REF],
                    },
                ),
            ]
        ),
        _Executor(),
        RecordingAuditSink(),
        parked=0,
        wall=_NO_WALL_CLOCK_LIMIT,
    )
    assert (parked_cap.outcome, parked_cap.exhausted_cap) == (
        "budget-exhausted",
        "parked-writes",
    )
    assert parked_cap.steps[-1].outcome == "cap:parked-writes"


def test_wall_clock_and_context_exhaustion_are_not_reported_as_answers() -> None:
    class _SlowGateway:
        async def complete(self, _request: Any) -> Any:
            await asyncio.sleep(0.05)
            return _structured_completion("late")

        def stream(self, _request: Any) -> Any:
            raise NotImplementedError

    wall = _run(_SlowGateway(), _Executor(), RecordingAuditSink(), wall=0.001)
    assert (wall.outcome, wall.exhausted_cap, wall.output) == (
        "budget-exhausted",
        "wall-clock",
        "",
    )

    class _ContextGateway:
        async def complete(self, _request: Any) -> Any:
            raise ContextBudgetExceededError(
                stage="gateway-preflight",
                estimated_tokens=2,
                input_budget=1,
                context_window=2,
            )

        def stream(self, _request: Any) -> Any:
            raise NotImplementedError

    context = _run(_ContextGateway(), _Executor(), RecordingAuditSink())
    assert (context.outcome, context.exhausted_cap, context.output) == (
        "context-exhausted",
        "context",
        "",
    )


class _HungGateway:
    """A model call parked on a dead socket, cancelled by TWO parties at once.

    This is the rented-rig incident in miniature. The request never returns, so
    the wall clock fires while the run is inside an ``await``; a second party
    then cancels the SAME run task while that first cancellation is still
    unwinding. In the deployment that second party is DBOS — its log line for
    the twelve stranded runs is "Asyncio task cancelled for workflow or step".

    That second ``cancel()`` is the entire mechanism. ``asyncio.timeout.__aexit__``
    converts its own cancellation into ``TimeoutError`` only while
    ``task.uncancel() <= self._cancelling``, so one extra cancel in the window is
    enough for a raw ``CancelledError`` to come out instead — measured here as
    deterministic, 25/25. ``CancelledError`` derives from ``BaseException`` and
    was caught by nothing on that path, so the run published NO terminal at all:
    ``status=running``, ``terminal_at=NULL``, ``updated_at`` frozen at dispatch.

    ``run_task`` must be aimed at the task running ``run_model_planned_agent``
    and cannot be discovered from in here: pydantic-ai runs the model request in
    a CHILD task (asserted below), so cancelling ``current_task()`` would never
    reach the timeout scope at all.
    """

    def __init__(self) -> None:
        self.run_task: asyncio.Task[Any] | None = None
        self.ran_in_a_child_task = False
        self.second_cancel_issued = False

    async def complete(self, request: Any) -> Any:
        del request
        self.ran_in_a_child_task = asyncio.current_task() is not self.run_task
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            if self.run_task is not None:
                self.run_task.cancel()
                self.second_cancel_issued = True
            raise
        raise AssertionError("the hung gateway must never answer")

    def stream(self, request: Any) -> Any:
        del request
        raise NotImplementedError


def _run_hung(gateway: _HungGateway, *, wall: float, cancel_after: float | None) -> Any:
    """Drive ``run_model_planned_agent`` with a handle on its own task.

    ``cancel_after`` is an OUTSIDE cancellation — an operator cancel or a
    shutdown — scheduled independently of the wall clock.
    """

    async def scenario() -> Any:
        run_task = asyncio.ensure_future(
            run_model_planned_agent(
                task="do the bounded work",
                instructions="use governed tools",
                principal=_PRINCIPAL,
                executor=_Executor(),
                gateway=gateway,
                settings=RuntimeSettings(),
                sink=RecordingAuditSink(),
                max_tool_calls=4,
                total_tokens_limit=1000,
                wall_clock_seconds=wall,
                max_parked_writes=1,
            )
        )
        gateway.run_task = run_task
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            run_task.cancel()
        return await run_task

    return asyncio.run(scenario())


def test_a_deadline_and_an_outside_cancel_together_publish_a_CANCELLATION() -> None:
    """A run cancelled AT its deadline ends as cancelled. It neither vanishes nor lies.

    This is case C — the deadline and an outside cancel co-occurring — and it is
    the ONLY case that reaches ``run_model_planned_agent``'s ``CancelledError``
    arm with ``wall_clock.expired()`` true. Measured, 40/40 deterministic on
    CPython 3.12: a deadline alone converts to ``TimeoutError``
    (``expired=True``), an outside cancel alone arrives as ``CancelledError``
    with ``expired=False``, and the two together arrive as ``CancelledError``
    with ``expired=True`` — because the second cancel is precisely what defeats
    ``asyncio.timeout``'s ``task.uncancel() <= self._cancelling`` conversion.

    Two things are therefore true at once here, and the arm must publish one.
    It publishes the CANCELLATION. Before this fix it published
    ``budget-exhausted``/``wall-clock`` and returned normally, which consumed the
    cancellation outright: DBOS awaited a task it had cancelled and got a result,
    and an operator who pressed Cancel was told a ceiling they never hit had
    stopped their run.

    ⚠️ AND THE FIRST FIX'S GUARD REMAINS INTACT — the reason this raises
    ``RunCancellationRequested`` rather than re-raising ``CancelledError``. A raw
    ``CancelledError`` off this path is a ``BaseException`` caught by nothing:
    that is the stranded-run defect itself (``status=running``,
    ``terminal_at=NULL``, twelve settled by hand). ``RunCancellationRequested``
    is the sanctioned in-loop cancellation signal — ``_LeaseExecutionBoundary
    .heartbeat`` already raises this exact type from inside this same call stack
    — so ``run_workflow`` terminalises it as CANCELLED and publishes. The run
    still ends, durably; it now ends under the right name.
    """
    gateway = _HungGateway()
    # 0.25 s against ~7 ms of measured agent setup, so the deadline lands inside
    # the gateway's await with a wide margin even on a loaded box.
    with pytest.raises(RunCancellationRequested) as raised:
        _run_hung(gateway, wall=0.25, cancel_after=None)

    # The cancellation is not thrown away either: it is the published error's
    # cause, so the trail from the terminal back to the raw cancel survives.
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    # Without these the test could silently degrade into re-testing the
    # TimeoutError arm, which was never broken.
    assert gateway.ran_in_a_child_task
    assert gateway.second_cancel_issued


def test_a_deadline_alone_still_publishes_the_wall_clock_terminal() -> None:
    """The other side of the same arm: no outside cancel, no cancellation reported.

    Narrowing the ``CancelledError`` arm must not cost the wall clock its
    terminal. With one cancelling party the deadline converts to ``TimeoutError``
    and the run publishes ``budget-exhausted``/``wall-clock`` from
    ``_wall_clock_terminal`` — the one arm that reaches it now. No partial draft
    is promoted to an answer.
    """

    class _SilentGateway:
        async def complete(self, _request: Any) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("the silent gateway must never answer")

        def stream(self, _request: Any) -> Any:
            raise NotImplementedError

    result = _run(_SilentGateway(), _Executor(), RecordingAuditSink(), wall=0.25)

    assert (result.outcome, result.exhausted_cap, result.output) == (
        "budget-exhausted",
        "wall-clock",
        "",
    )


def test_a_cancellation_this_scope_did_not_cause_still_propagates() -> None:
    """Operator cancel and shutdown MUST still cancel. This is the guard rail.

    Same doubly-cancelled mechanics as the test above — so the ``CancelledError``
    reaches the handler exactly the same way — but with the deadline switched
    off. ``wall_clock.expired()`` is therefore false, and that is the ONLY thing
    permitted to suppress a cancellation here, so this one must arrive at the
    caller untouched. Reporting it as ``budget-exhausted`` would name a ceiling
    nobody hit AND silently break operator cancel and shutdown, which is a worse
    defect than the one being fixed.
    """
    gateway = _HungGateway()

    with pytest.raises(asyncio.CancelledError):
        _run_hung(gateway, wall=_NO_WALL_CLOCK_LIMIT, cancel_after=0.05)

    assert gateway.second_cancel_issued


def test_model_planned_runner_injects_the_exact_protected_source_guard(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Agent:
        def __init__(self, *, model: Any, **_kwargs: Any) -> None:
            captured["model"] = model

        def output_validator(self, func: Any) -> Any:
            captured["output_validator"] = func
            return func

        async def run(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                output=runner_module._StructuredAgenticCompletion(final_text="done"),
                usage=SimpleNamespace(requests=1, total_tokens=1),
            )

    monkeypatch.setattr(runner_module, "Agent", _Agent)
    resolver = _MutableAuthorityResolver()
    guard = _source_guard(resolver)
    result = asyncio.run(
        run_model_planned_agent(
            task="bounded",
            instructions="governed",
            principal=_PRINCIPAL,
            executor=_Executor(),
            gateway=ScriptedGateway([text_completion("unused")]),
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=1,
            total_tokens_limit=100,
            wall_clock_seconds=1,
            max_parked_writes=0,
            source_guard=guard,
        )
    )
    assert result.outcome == "complete"
    # NE-SPAN-BIND: the model receives the run-scoped RETAINING view over the
    # exact injected guard — same resolver, same clock, plus this run's own
    # retention buffer — so every authorized delivery both reaches the model
    # and becomes server-bindable.
    from ne.runtime.source_results import ClaimRetainingSourceGuard

    injected = captured["model"]._source_guard
    assert isinstance(injected, ClaimRetainingSourceGuard)
    assert injected._resolver is resolver
    assert injected._clock is guard._clock
    # The claim contract is installed on the SAME Agent that owns the output
    # type, so a build that swapped the Agent out cannot quietly lose it.
    assert captured["output_validator"].__name__ == "_enforce_claim_contract"


def test_bounded_agentic_adapter_passes_its_exact_immutable_caps(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    async def _run_model_planned_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runner_module, "run_model_planned_agent", _run_model_planned_agent)
    adapter = BoundedAgenticAdapter(
        instructions="Use only the eligible governed tools.",
        max_tool_calls=7,
        total_tokens_limit=4096,
        wall_clock_seconds=12.5,
        max_parked_writes=2,
    )
    guard = object()
    result = asyncio.run(
        adapter.run(
            task="perform bounded work",
            principal=_PRINCIPAL,
            executor=_Executor(),
            gateway=ScriptedGateway([text_completion("unused")]),
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            source_guard=guard,  # type: ignore[arg-type]
        )
    )

    assert result is sentinel
    assert captured["instructions"] == adapter.instructions
    assert captured["max_tool_calls"] == 7
    assert captured["total_tokens_limit"] == 4096
    assert captured["wall_clock_seconds"] == 12.5
    assert captured["max_parked_writes"] == 2
    assert captured["source_guard"] is guard


def test_the_agent_built_boundary_fires_before_the_first_model_request() -> None:
    """POST_DEMO item 12, increment 1c — the marker that divides UNMEASURED work.

    The worker's `profile:adapter-before` was shipped as the split of item 12's
    7.67 s edge and is not: everything between it and the production
    `profile:agentic-before` is a comment block, two attribute reads, a
    do-nothing wrapper constructor and two ContextVar scopes. No I/O. So that
    boundary partitions the edge into ~0 and ~all of it — determinate by reading
    the file, and swamped by the heartbeat's own transaction round trips anyway.

    Everything genuinely unmeasured is inside the adapter: one
    `build_governed_tool` per granted spec and the `Agent` construction over
    them, then — on the production path — the protected-history authorization
    that runs before the first request reaches the wire: `materialize_history`
    (a PDP decision per protected reference), the context-budget trim, and
    `reauthorize_history`, which mints the durable delivery receipts. This
    asserts the new boundary lands BETWEEN those two — after the agent is built,
    before it runs.

    ⚠️ THIS PARAGRAPH SAID "then pydantic-ai's graph startup before its first
    model request" while the body comment below called that exact wording the
    eleventh copy of a retracted claim. Same file, both versions, the wrong one
    first. Twelfth instance, and the same mistake as the round before: fixed at
    the line a reviewer cited rather than everywhere the claim lives.

    Asserted at RUNTIME against what actually fired, not against source order.
    """

    stages: list[str] = []

    class _RecordingBoundary:
        async def heartbeat(self, stage: str) -> None:
            stages.append(stage)

        async def checkpoint(self, *, stage: str, safe_detail: Any) -> None:
            return None

        async def await_inflight(self, operation: Any, *, stage: str) -> Any:
            return await operation

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _StopAfterFirstRequest(Exception):
        """Sentinel: this test pins ORDERING THROUGH THE FIRST REQUEST, nothing after."""

    class _TimelineGateway(ScriptedGateway):
        """Records the FIRST model request onto the same timeline as the stages,
        then STOPS THE RUN.

        `run-model:before:{ordinal}` is emitted by the worker's own run_runner
        composition, not by this function, so the ordering has to be asserted
        against the thing that boundary precedes: the model request itself.

        ⛔ THE SENTINEL IS THE POINT, and Codex raised it to P1 after reporting
        this test times out at its 30 s wall clock in a fresh process. I could not
        reproduce that here -- 0.07 s across six runs alone on a quiet box, and CI
        green on every head -- so I am NOT recording it as reproduced. **The
        remedy is right regardless of whose environment is unusual**: the test
        pins the ORDER of three markers around the first model request and
        nothing else, so waiting for the whole agent loop to terminate makes it
        depend on machinery it does not assert. Raising here removes that
        dependency, which is what a test asserting ordering should have done from
        the start.
        """

        async def complete(self, request: Any) -> Any:
            stages.append("model-request")
            raise _StopAfterFirstRequest

    class _TimelineExecutor(_Executor):
        """Records the moment the governed tools are BUILT.

        ⚠️ WITHOUT THIS THE TEST WAS HALF A TEST, and Codex named the hole: an
        assertion of `agent-built < model-request` alone stays green if the
        heartbeat is hoisted ABOVE `build_governed_tool` and `Agent(...)` —
        which collapses the split the marker exists to make, since that
        construction is the entire left-hand side of it. The list comprehension
        that builds the governed tools reads `tool_specs`, so that read is a
        construction moment on this timeline.

        ⛔ IT IS NOT THE ONLY ONE, and saying "exactly once" here was false --
        caught by an adversarial pass, in the paragraph whose entire job is to
        state precisely what this probe pins. `_RecordedExecutor.execute`
        re-reads `inner.tool_specs` on EVERY tool call to build its `available`
        map, and `_RecordedExecutor.tool_specs` is an uncached property over it.
        So the probe fires once at construction AND once per governed tool call.
        The assertion below still holds -- it uses `stages.index` and the
        construction read is first -- but anyone strengthening it to
        `count("tools-built") == 1` or an exact list would write a test that reds
        on any run with a tool call, having believed this docstring over the
        source.

        ⛔ DECLARED RESIDUAL, because an adversarial pass was right that my first
        wording overstated what this pins. `tool_specs` is read at the START of
        that comprehension, so this marks the ENTRY to construction, not its
        completion. A hoist above that entry reds; a hoist BETWEEN the tools
        comprehension and `Agent(...)` does not — so the `Agent` construction,
        the `PromptedOutput` schema generation and the claim-validator install
        stay unpinned on this side of the split.

        I looked for a cheap probe for the END of construction and there is not
        one: `_RecordedExecutor.tool_specs` builds its tuple eagerly, so the
        inner specs are fully consumed before any `build_governed_tool` body
        runs, and nothing in that region is injectable from out here. Writing a
        fragile probe to claim a stronger property would be worse than saying
        which property this actually has, so it says it.
        """

        @property
        def tool_specs(self) -> Any:
            stages.append("tools-built")
            return self._specs

    executor = _TimelineExecutor()
    gateway = _TimelineGateway([_structured_completion("done")])

    async def scenario() -> Any:
        return await run_model_planned_agent(
            task="do the bounded work",
            instructions="use governed tools",
            principal=_PRINCIPAL,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=4,
            total_tokens_limit=1000,
            # FINITE, and generous. 🔴 I CLAIMED THIS WAS ALREADY BOUNDED ONE
            # ROUND AGO AND IT WAS NOT: the edit was in a script whose LATER
            # assertion failed, so nothing was written, and I reported the fix
            # without re-reading the file. Codex raised it again against the same
            # line. Applied and verified this time.
            #
            # Codex also reported the test stalling past 30s; re-run alone on a
            # quiet box it completes in 0.07s, so that observation still does not
            # reproduce here and is not recorded as one. The bound is right
            # regardless: an infinite wall clock turns any future
            # non-termination into a STALLED suite instead of a failed test, and
            # 30s is ~400x the observed runtime, so it can neither flake under
            # load nor hang.
            wall_clock_seconds=30.0,
            max_parked_writes=1,
            # ⛔ A REAL GUARD, NOT `None`, and that is the point of this line.
            # An adversarial pass observed that every runtime assertion on
            # `profile:agent-built` ran with `source_guard=None` while production
            # always composes a real `ProtectedSourceResultGuard` -- so a future
            # `if source_guard is None:` around the heartbeat would leave the whole
            # tree green while the marker never fired on a live run. The emission
            # is unconditional today (verified: the `if source_guard is not None:`
            # block above it is two lines and closes before the enclosing `try:`),
            # and this keeps it observed in the configuration that ships.
            source_guard=_source_guard(_MutableAuthorityResolver()),
            # ⛔ AND A NON-EMPTY HISTORY, for the same reason round 16 passed a
            # real `source_guard` here: production does. The worker hands
            # `message_history=history` on any continuity run past its first
            # turn, and this row's own production floor IS the continuity floor —
            # so pinning `agent-built` only under an EMPTY history left the
            # marker unasserted on the shape that actually ships. An adversarial
            # pass found the gap one argument over from the one round 16 closed.
            message_history=(
                req_user("A prior turn asked about the hardship review."),
                resp_text("The prior answer stands."),
            ),
            boundary=_RecordingBoundary(),  # type: ignore[arg-type]
        )

    # The sentinel stops the run at the first request, so no outcome is asserted
    # here -- there is deliberately no complete run to have one. `stages` is the
    # product, and every assertion below reds if it is empty or mis-ordered, so
    # suppressing cannot hide a failure.
    with contextlib.suppress(Exception):
        asyncio.run(scenario())

    assert "profile:agent-built" in stages, f"agent-built never fired; got {stages}"
    assert "model-request" in stages, f"the run never reached a model request; got {stages}"
    assert "tools-built" in stages, f"the governed tools were never built; got {stages}"
    # BOTH SIDES, not just the right one. The marker divides "build the governed
    # tools and the Agent" from the protected-history authorization that runs
    # before the first model request reaches the wire -- `materialize_history`, a
    # PDP decision per protected reference, the context-budget trim, and
    # `reauthorize_history`, which mints the durable delivery receipts. (This
    # sentence used to say "everything pydantic-ai does before its first model
    # request". That was the ELEVENTH copy of a claim this PR had already
    # retracted twice, and it was the copy inside the test that PINS the marker.)
    # A test that pins only the second half permits the
    # heartbeat to be hoisted above the construction it is meant to bound —
    # leaving a marker that measures nothing, which is precisely the defect this
    # whole increment keeps repeating.
    assert (
        stages.index("tools-built")
        < stages.index("profile:agent-built")
        < stages.index("model-request")
    ), f"agent-built must fall AFTER tool construction and BEFORE the model request; got {stages}"
    # And it must be a name the span writer will accept — a rejected stage would
    # raise at the boundary instead of measuring.
    from ne.runtime.run_workflow import _validated_stage

    assert _validated_stage("profile:agent-built") == "profile:agent-built"


# --- NE-LOOP-NOPROGRESS ----------------------------------------------------
#
# THE PAIR. One test proves the loop stops when its reads stop finding anything
# and SAYS SO; the other proves a run whose reads keep finding things is not
# touched by that rule at all. Neither is meaningful without the other: a stop
# rule that also stops working runs is worse than no rule, and a rule that never
# fires is not a rule.


class _BarrenExecutor(_Executor):
    """A read tool that succeeds and serves NOTHING — the empty-retrieval case.

    It is deliberately not an error path: the call is allowed, audited and
    successful. It simply has no rows, which is exactly the state a deployment
    with an empty (or fully filtered) index is in.
    """

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal.id))
        return "row | status\n(no rows)" if tool.startswith("read.") else "submitted"


class _DistinctEvidenceExecutor(_Executor):
    """Every read serves a row the run has not seen — a real multi-hop chain."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal.id))
        if not tool.startswith("read."):
            return "submitted"
        self.reads += 1
        return f"row | status\nurn:ne:meridian:dispute:mer-dsp-{self.reads:03d} | served"


def _search(intent: str) -> Any:
    return tool_call_completion("read.kg.directory", {"step_intent": intent})


def test_repeated_empty_reads_stop_the_run_with_a_stated_reason() -> None:
    # The cap is set far above the number of calls this run is allowed to make,
    # so the stop cannot be mistaken for a cap hit and the test cannot pass by
    # accident if the no-progress rule is deleted: without it, this run burns all
    # thirty calls exactly as the reported defect does.
    executor = _BarrenExecutor()
    gateway = ScriptedGateway([_search(f"Search {index}.") for index in range(30)])

    with pytest.raises(AgenticNoProgressError) as raised:
        _run(gateway, executor, RecordingAuditSink(), max_tools=30, wall=_NO_WALL_CLOCK_LIMIT)

    error = raised.value
    assert (error.tool, error.calls) == ("read.kg.directory", 3)
    # FOUR calls, not thirty: the whole point is the round-trips that never
    # happen. Each avoided call is a full model request. Four rather than three
    # is deliberate — the counter measures REPEATS, so the first barren read is
    # the new fact "this query has no rows" and does not open the streak. That
    # one extra round-trip is what buys back every successful ref-free read.
    assert len([call for call in executor.calls if call[0] == "read.kg.directory"]) == 4
    # The published sentence names retrieval, names the tool and names the count.
    # It is what the reader is shown INSTEAD of "the run ended before it produced
    # an answer", so it must be able to carry that weight on its own — and it must
    # not OVERSTATE, which is the defect that made this correction necessary.
    assert "read.kg.directory" in error.safe_reason
    assert "had not already been served" in error.safe_reason
    assert "Nothing was retrieved" not in error.safe_reason
    assert len(error.safe_reason) <= AgenticNoProgressError.SAFE_REASON_MAX
    assert error.safe_reason.isprintable()
    # NEVER AN ANSWER. The run raises; it does not return a ModelPlannedRun with
    # invented text, and there is no branch here that could produce one.
    assert not isinstance(error, ModelPlannedRun)


def test_a_multi_hop_run_whose_searches_keep_finding_evidence_is_untouched() -> None:
    # Eight distinct successful reads — well past the no-progress limit of three
    # — followed by the final answer. Each one serves a row the run has not seen,
    # which is what a multi-hop question looks like, and none of them may count
    # toward a stop.
    executor = _DistinctEvidenceExecutor()
    gateway = ScriptedGateway(
        [*(_search(f"Hop {index}.") for index in range(8)), _structured_completion("grounded")]
    )

    result = _run(gateway, executor, RecordingAuditSink(), max_tools=12, wall=_NO_WALL_CLOCK_LIMIT)

    assert (result.outcome, result.output) == ("complete", "grounded")
    assert result.tool_calls_used == 8
    assert executor.reads == 8


def test_one_empty_read_between_finds_does_not_accumulate_toward_a_stop() -> None:
    # The streak is CONSECUTIVE. A hop that comes back empty in the middle of a
    # productive chain is ordinary — it must not be banked against the run and
    # then completed by two more empties several hops later.
    class _AlternatingExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            if not tool.startswith("read."):
                return "submitted"
            self.reads += 1
            if self.reads % 2 == 0:
                return "row | status\n(no rows)"
            return f"row | status\nurn:ne:meridian:dispute:mer-dsp-{self.reads:03d} | served"

    executor = _AlternatingExecutor()
    gateway = ScriptedGateway(
        [*(_search(f"Hop {index}.") for index in range(9)), _structured_completion("grounded")]
    )

    result = _run(gateway, executor, RecordingAuditSink(), max_tools=12, wall=_NO_WALL_CLOCK_LIMIT)

    assert (result.outcome, result.tool_calls_used) == ("complete", 9)


def test_the_barren_streak_is_per_tool_and_cleared_by_new_evidence() -> None:
    """The rule, driven directly at the unit that owns it.

    Each transition asserted on its own: a FIRST empty read is progress (it is
    the new fact "this query has no rows"), a REPEAT of it opens the streak, a
    further repeat extends it, an empty read of a DIFFERENT tool restarts rather
    than inherits, and a read that serves a row the run has not seen clears it.

    ⚠️ The first-call transition is the one this test used to get wrong, because
    the rule did. It counted the opening barren read as a non-event, which is the
    same mistake that made three SUCCESSFUL CRM lookups look barren: absence of a
    `urn:` ref was read as absence of a result. The counter measures REPEATS now,
    so the streak opens on the second identical result and the run stops on the
    fourth call rather than the third — one extra round-trip, deliberately spent.
    """

    class _SwitchableExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.output = "row | status\n(no rows)"
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.kg.paths",
                    description="Walk the evidence graph.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={"type": "object", "properties": {}},
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            return self.output

    inner = _SwitchableExecutor()
    recorder = _RecordedExecutor(
        inner=inner,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=30,
        max_parked_writes=0,
    )
    assert recorder.max_no_progress_calls == runner_module._NO_PROGRESS_TOOL_CALLS

    async def scenario() -> None:
        args = {"step_intent": "Look."}
        # A result the run has NOT been served before is progress, even with no
        # ref in it. This is the transition the CRM lookups needed.
        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == (None, 0)

        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == ("read.kg.directory", 1)

        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == ("read.kg.directory", 2)

        # A DIFFERENT tool has not served this result before, so its own first
        # call is progress and the previous tool's streak does not carry over.
        await recorder.execute("read.kg.paths", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == (None, 0)
        await recorder.execute("read.kg.paths", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == ("read.kg.paths", 1)

        # Back to the first tool: it HAS served this result, so the repeat counts
        # — and it restarts that tool's streak rather than inheriting the other's.
        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == ("read.kg.directory", 1)

        inner.output = "row | status\n" + _REF + " | served"
        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert (recorder.no_progress_tool, recorder.no_progress_calls) == (None, 0)
        # And the SAME row a second time is not new evidence: it re-opens a
        # streak rather than clearing one.
        await recorder.execute("read.kg.directory", args, principal=_PRINCIPAL)
        assert recorder.no_progress_calls == 1

    asyncio.run(scenario())


def test_successful_ref_free_reads_are_progress_and_never_stop_the_run() -> None:
    """THE REGRESSION, driven off the REAL output contract of a shipped Tool.

    ``_served_refs`` matches only ``urn:ne:…`` and ``ne-source-ref:v1:…``, so a
    successful read whose result format carries neither was indistinguishable
    from one that found nothing. ``read.external.crm.contact`` is exactly that
    shape — ``reference_actions/crm_lookup.py``'s handler returns record ids and
    an email address, no ref anywhere — and it is appended to the SAME executor
    tuple this runner is built over (``ne.api.platform``), as a Tier-0 read, so
    the write exemption did not save it either.

    Three consecutive lookups resolving three DIFFERENT customers stopped the run
    and published *"Nothing was retrieved to ground an answer"*. That is the S5
    outreach shape and the B1 flagship fans out over several customers, so it is
    an ordinary plan. The published sentence was the sharper half of the defect:
    it is shown to the reader INSTEAD of the generic line precisely so it can be
    trusted to tell "no evidence exists" from "the system broke", and it asserted
    the first when neither was true.

    EIGHT calls here, well past the limit of three, each resolving a different
    record. None of them may count toward a stop.
    """

    class _CrmLookupExecutor(_Executor):
        """Verbatim the shipped contract: identifiers and a bool, and no ref."""

        def __init__(self) -> None:
            super().__init__()
            self.reads = 0
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.external.crm.contact",
                    description="Resolve a CRM Contact record by its exact full name.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            self.reads += 1
            return json.dumps(
                {
                    "name": f"Customer {self.reads}",
                    "record_id": f"003AX000{self.reads:04d}",
                    "email": f"person{self.reads}@example.test",
                    "account_id": f"001AX000{self.reads:04d}",
                    "found": True,
                }
            )

    inner = _CrmLookupExecutor()
    recorder = _RecordedExecutor(
        inner=inner,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=30,
        max_parked_writes=0,
    )

    async def scenario() -> None:
        for index in range(8):
            await recorder.execute(
                "read.external.crm.contact",
                {"name": f"Customer {index}", "step_intent": "Resolve the contact."},
                principal=_PRINCIPAL,
            )

    asyncio.run(scenario())

    assert inner.reads == 8
    # Not one of them opened a streak: every call served a record this run had
    # not been served before.
    assert (recorder.no_progress_tool, recorder.no_progress_calls) == (None, 0)
    # And the run served no `urn:` ref at all — which is the whole point. The old
    # predicate would have read this exact state as "retrieved nothing".
    assert recorder.served == set()


def test_the_SAME_ref_free_result_repeated_still_stops_the_run() -> None:
    """The control, and without it the fix above would be a hole.

    Widening "progress" to include an unseen RESULT must not widen it to include
    every result. A Tool that answers the same barren question with the same
    bytes has added nothing after the first time, and the run must still stop and
    still say so — this is the case the rule was built for, and it survives.
    """

    class _StuckLookupExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.external.crm.contact",
                    description="Resolve a CRM Contact record by its exact full name.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={"type": "object", "properties": {}},
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            self.reads += 1
            return json.dumps({"name": "Customer 1", "record_id": None, "found": False})

    inner = _StuckLookupExecutor()
    recorder = _RecordedExecutor(
        inner=inner,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=30,
        max_parked_writes=0,
    )

    async def scenario() -> None:
        for _ in range(8):
            await recorder.execute(
                "read.external.crm.contact",
                {"step_intent": "Resolve the contact."},
                principal=_PRINCIPAL,
            )

    with pytest.raises(AgenticNoProgressError) as raised:
        asyncio.run(scenario())

    error = raised.value
    assert (error.tool, error.calls) == ("read.external.crm.contact", 3)
    # Four calls: the first answer was new, the next three repeated it.
    assert inner.reads == 4
    assert "had not already been served" in error.safe_reason
    # The precondition this control now rests on, pinned rather than assumed: the
    # shape really is ref-free, so the digest fallback is what governs it. A
    # future rendering that grew a ref would make the digest silent here, and the
    # assertion below is what would say so.
    assert runner_module._served_refs(json.dumps({"record_id": None, "found": False})) == ()


def test_a_reworded_search_re_serving_SEEN_chunks_still_stops_the_run() -> None:
    """THE OTHER HALF OF THE CONTROL, and the half the byte digest could not see.

    ``test_the_SAME_ref_free_result_repeated_still_stops_the_run`` returns
    BYTE-IDENTICAL output on every call, so it can only ever exercise the
    digest's HIT path. It is structurally incapable of catching a read whose
    bytes change while its evidence does not — which is exactly what rewording a
    query does to ``read.knowledge.search``. ``ne.api.tools`` renders a counts
    header carrying the PRE-RERANK candidate count and then one
    ``<chunk_id>: <text>`` line per hit in RERANK order, so two wordings of one
    question re-serve the same chunks under different bytes, and an unconditional
    digest disjunct read every one of them as progress.

    Reproduced live: twelve calls, three chunks, the streak never opened, and the
    run died on the tool-call ceiling — so the reader was told
    ``budget-exhausted``, the one terminal ``run_model_planned_agent``'s docstring
    says this stop must never borrow. It also broke the published invariant
    *"rewording the query cannot evade it."* This is the test
    that holds that sentence.
    """

    chunks = (
        ("urn:ne:doc:dispute-sop:0001", "Acknowledge a dispute within five business days."),
        ("urn:ne:doc:dispute-sop:0002", "Mail the acknowledgement notice to the borrower."),
        ("urn:ne:doc:dispute-sop:0003", "Escalate an unresolved dispute to the supervisor."),
    )

    class _RewordedSearchExecutor(_Executor):
        """The shipped ``read.knowledge.search`` rendering, faithful in shape.

        Both moving parts move with the WORDING and neither moves with the
        evidence: the header's ``N`` is the pre-rerank candidate count, and the
        body is ordered by the reranker. The same three chunks come back every
        time, because the corpus holds exactly three matching passages.
        """

        def __init__(self) -> None:
            super().__init__()
            self.outputs: list[str] = []
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.knowledge.search",
                    description="Search governed documents for passages of prose.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            reads = len(self.outputs)
            rotation = reads % len(chunks)
            ordered = chunks[rotation:] + chunks[:rotation]
            lines = [f"[showing 3 of {7 + reads} retrieved chunks]"]
            lines.extend(f"{chunk_id}: {text}" for chunk_id, text in ordered)
            rendered = "\n".join(lines)
            self.outputs.append(rendered)
            return rendered

    executor = _RewordedSearchExecutor()
    # More wordings than the twelve-call ceiling allows, so that reaching the
    # ceiling is a DISTINGUISHABLE outcome rather than the only way this run can
    # end: before the fix this run took all twelve calls and terminalised
    # `budget-exhausted`/`tool-calls`, which is the reported defect verbatim.
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                "read.knowledge.search",
                {
                    "query": f"dispute acknowledgement wording, phrasing {index}",
                    "step_intent": "Find the acknowledgement wording.",
                },
            )
            for index in range(16)
        ]
    )

    with pytest.raises(AgenticNoProgressError) as raised:
        _run(gateway, executor, RecordingAuditSink(), max_tools=12, wall=_NO_WALL_CLOCK_LIMIT)

    error = raised.value
    assert (error.tool, error.calls) == ("read.knowledge.search", 3)
    # FOUR calls, not the ceiling's twelve. The first search was genuinely new
    # evidence; the next three served nothing this run did not already hold,
    # however they were worded. Eight avoided calls are eight avoided model
    # round-trips.
    assert len(executor.outputs) == 4
    # THE PREMISE, asserted rather than assumed: all four outputs are distinct
    # strings, so a digest taken over the output bytes calls all four new.
    assert len(set(executor.outputs)) == 4
    # ...while the evidence never grew past the first call's three chunks.
    # `_LEGACY_SERVED_REF_RE` stops only at whitespace, so each ref carries the
    # `<chunk_id>: <text>` separator's colon; compare on that same footing.
    expected_refs = {f"{chunk_id}:" for chunk_id, _ in chunks}
    assert [set(runner_module._served_refs(output)) for output in executor.outputs] == [
        expected_refs
    ] * 4
    # NOT `budget-exhausted`. The run stopped four calls short of its ceiling, so
    # there is no cap terminal to report and none is reported: the raise IS the
    # outcome, and the sentence the reader is shown says the reads found nothing
    # new rather than that the run ran out of budget with more to find.
    assert not isinstance(error, ModelPlannedRun)
    assert "budget" not in error.safe_reason.lower()
    assert "had not already been served" in error.safe_reason


#: Three chunks in the SHIPPED chunk-id shape: ``{document_id}:{index:04d}``
#: (``ne.brain.ingest.chunker:146``) with ``document_id`` the hex SHA-256 that
#: ``ne.connect.bridge.default_record_mapper`` mints. Deliberately NOT
#: ``urn:ne:...``: that spelling is a row identity from a different read, and
#: writing it here is what let the sibling test above pass through the REF arm
#: while claiming to cover the rendering.
#: FOUR of them, not three: the streak takes four calls to trip, and a
#: three-cycle rotation would hand call four the SAME bytes as call one -- which
#: the old whole-output digest would have caught unaided, costing this test its
#: red. Four chunks make all four renderings byte-distinct.
_SOURCE_FREE_CHUNKS = (
    (f"{'3f' * 32}:0000", "Acknowledge a dispute within five business days."),
    (f"{'7c' * 32}:0000", "Mail the acknowledgement notice to the borrower."),
    (f"{'b1' * 32}:0000", "Escalate an unresolved dispute to the supervisor."),
    (f"{'d4' * 32}:0000", "Record the dispute outcome in the servicing log."),
)

#: The header the demo deployment actually renders. Its ``N`` is
#: ``RerankedRetrievalResult.candidate_count`` = the fused, ACL-trimmed candidate
#: count, and the demo corpus (``demo/acme/renewals/acme-renewals.csv``, 19 rows)
#: is smaller than the pipeline's ``top_k * 4`` candidate window, so N is the
#: corpus and does NOT move with the wording. Pinning it CONSTANT is the point:
#: it removes the header as an explanation and leaves the reranker's ORDER as the
#: only thing a rewording changes.
_SOURCE_FREE_SEARCH_HEADER = "[showing 4 of 19 retrieved chunks]"

#: Every ordering of those four chunks, so a run can take a dozen calls without
#: ever repeating a rendering by accident. A plain rotation would repeat on call
#: five and the old whole-output digest would stop the run there on its own,
#: hiding the ceiling this defect actually reaches.
_SOURCE_FREE_ORDERINGS = tuple(itertools.permutations(_SOURCE_FREE_CHUNKS))


def test_a_reworded_SOURCE_FREE_search_re_serving_seen_chunks_still_stops_the_run() -> None:
    """THE ARM THE ``urn:ne:`` SIBLING ABOVE CANNOT REACH.

    ``test_a_reworded_search_re_serving_SEEN_chunks_still_stops_the_run`` gives
    its chunks ``urn:ne:doc:...`` ids and then asserts ``_served_refs`` returns
    all three on every call, so ``served`` is non-empty, the digest arm is switched
    off by ``not served``, and the run is stopped by the REF arm alone. It cannot
    fail for the ref-free case its name suggests it covers.

    This is that case, and it is the DEPLOYED one. ``ne.api.tools`` replaces the
    rendering with a minted protected ref only for a ``SOURCE_BEARING``
    classification; ``_chunk_source_evidence`` returns ``SOURCE_FREE`` when every
    retained chunk is server-marked source-free
    (``ne.brain.current_source_filter.chunk_is_source_free``), and the demo's only
    configured sync source is ``csv``, whose ``SYNC_SOURCES`` disposition IS
    ``SourceDisposition.SOURCE_FREE``. So the model gets the raw
    ``<chunk_id>: <text>`` rendering, no ref pattern matches a hex-digest chunk
    id, and the digest is the ONLY thing standing between a reworded query and
    the tool-call ceiling.

    Only the ORDER moves here -- same three chunks, same header, every call. That
    is enough: the reranker orders by the QUERY's scores, so two wordings of one
    question re-serve one chunk set under two byte-distinct renderings.
    """

    class _RewordedSourceFreeSearchExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.outputs: list[str] = []
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.knowledge.search",
                    description="Search governed documents for passages of prose.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            ordered = _SOURCE_FREE_ORDERINGS[len(self.outputs)]
            rendered = "\n".join(
                [
                    _SOURCE_FREE_SEARCH_HEADER,
                    *(f"{chunk_id}: {text}" for chunk_id, text in ordered),
                ]
            )
            self.outputs.append(rendered)
            return rendered

    executor = _RewordedSourceFreeSearchExecutor()
    # More wordings than the ceiling allows, so reaching the ceiling stays a
    # DISTINGUISHABLE outcome: before the fix this run took all twelve calls and
    # terminalised `budget-exhausted`/`tool-calls`.
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                "read.knowledge.search",
                {
                    "query": f"dispute acknowledgement wording, phrasing {index}",
                    "step_intent": "Find the acknowledgement wording.",
                },
            )
            for index in range(16)
        ]
    )

    with pytest.raises(AgenticNoProgressError) as raised:
        _run(gateway, executor, RecordingAuditSink(), max_tools=12, wall=_NO_WALL_CLOCK_LIMIT)

    error = raised.value
    assert (error.tool, error.calls) == ("read.knowledge.search", 3)
    # FOUR calls, not the ceiling's twelve.
    assert len(executor.outputs) == 4
    # THE PREMISE, asserted rather than assumed. Both halves of it:
    # every rendering is a DISTINCT string ...
    assert len(set(executor.outputs)) == 4
    # ... and NONE of them carries a ref, so `served` is empty on every call and
    # the ref arm is silent. This is the assertion the `urn:ne:` sibling inverts.
    assert [runner_module._served_refs(output) for output in executor.outputs] == [()] * 4
    # Nor is this the withholding case: the header discloses three chunks.
    assert not any(runner_module._disclosed_nothing(output) for output in executor.outputs)
    # NOT `budget-exhausted`. The run stopped eight calls short of its ceiling.
    assert not isinstance(error, ModelPlannedRun)
    assert "budget" not in error.safe_reason.lower()
    assert "had not already been served" in error.safe_reason


def test_a_SOURCE_FREE_search_that_retrieves_a_NEW_chunk_is_still_progress() -> None:
    """THE OTHER DIRECTION, which the fix above must not break.

    Dropping the header and the ordering from the digest could have been achieved
    by muting the digest for this grammar outright -- and that would have made
    every source-free search look barren, because a source-free result has no ref
    to fall back on and would then have NO way to report progress at all. Three
    productive searches in a row would stop the run.

    So: same tool, same source-free rendering, but every second call retrieves a
    chunk the run had not been served. The streak reopens each time and never
    reaches three, over more calls than the ceiling it would otherwise hit.
    """
    corpus = (
        *_SOURCE_FREE_CHUNKS,
        (f"{'e9' * 32}:0000", "Close the dispute only after the borrower confirms."),
        (f"{'0a' * 32}:0000", "Retain dispute correspondence for seven years."),
        (f"{'56' * 32}:0000", "Log every borrower contact attempt with a timestamp."),
        (f"{'8d' * 32}:0000", "Notify the supervisor when a dispute passes 30 days."),
    )

    class _GrowingSourceFreeSearchExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.outputs: list[str] = []
            self.disclosed = len(_SOURCE_FREE_CHUNKS)
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.knowledge.search",
                    description="Search governed documents for passages of prose.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            if len(self.outputs) % 2 == 1:
                self.disclosed += 1
            served = corpus[: self.disclosed]
            rotation = len(self.outputs) % len(served)
            ordered = served[rotation:] + served[:rotation]
            rendered = "\n".join(
                [
                    f"[showing {len(served)} of 19 retrieved chunks]",
                    *(f"{chunk_id}: {text}" for chunk_id, text in ordered),
                ]
            )
            self.outputs.append(rendered)
            return rendered

    inner = _GrowingSourceFreeSearchExecutor()
    recorder = _RecordedExecutor(
        inner=inner,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=30,
        max_parked_writes=0,
    )

    async def scenario() -> None:
        for _ in range(8):
            await recorder.execute(
                "read.knowledge.search",
                {"step_intent": "Find the acknowledgement wording."},
                principal=_PRINCIPAL,
            )

    # No raise: eight calls, every other one genuinely productive.
    asyncio.run(scenario())
    assert len(inner.outputs) == 8
    assert recorder.no_progress_calls < 3
    # The premise again: still ref-free throughout, so it really is the digest
    # that is voting progress here and not the ref arm.
    assert [runner_module._served_refs(output) for output in inner.outputs] == [()] * 8


def test_a_100_percent_WITHHELD_search_broadened_three_times_still_stops_the_run() -> None:
    """THE THIRD HALF OF THE CONTROL, and the one the other two cannot reach.

    ``test_a_reworded_search_re_serving_SEEN_chunks_still_stops_the_run`` covers
    a search that SERVES refs, so it only ever exercises the ref arm. This is the
    same tool with the refs taken away: a governed connector is degraded, every
    match is withheld, and the result carries no chunk line and no protected ref
    at all. The digest fallback therefore votes — and its vote is worthless here,
    because the header that remains is still QUERY-DEPENDENT.
    ``ne.api.tools._result_header`` renders
    ``[showing 0 of {matched} retrieved chunks; {withheld} withheld — …]`` with
    ``matched = max(total, shown + withheld)``, so a model that broadens sees
    ``0 of 7``, then ``0 of 11``, then ``0 of 4``: three byte-distinct, ref-free
    results, three digest votes for progress, a streak that never opens, and a
    run that burns its whole tool-call ceiling and terminalises
    ``budget-exhausted``/``tool-calls`` — the one terminal
    ``run_model_planned_agent``'s docstring says this stop must never borrow.

    This is not a hypothetical shape. ``ne.api.tools:118-122`` records that a
    model told "0 of 0" DOES broaden, that it happened three times in a real Run,
    and asserts the loop is "stopped by the no-progress rule". This test is what
    makes that sentence true when the withholding clause is present.

    The header literals below are the SHIPPED rendering, byte-for-byte, produced
    by ``ne.api.tools._result_header`` (packages/ne-api/src/ne/api/tools.py:106).
    They are copied rather than imported: ne-runtime does not depend on ne-api,
    and this test would be worth less if it asserted against a grammar it had
    generated itself.
    """

    reason = (
        "their governed source is currently degraded; this is a source-health outage, "
        "not an empty corpus, so rephrasing or broadening the query cannot surface them"
    )
    # `matched` moves with the WORDING, exactly as the pre-rerank candidate count
    # does. Nothing else in the line can move: the withheld count follows it and
    # the reason is fixed text.
    # Sixteen DISTINCT counts for sixteen broadenings, so no two results collide
    # by accident: without the fix every one of them is a fresh digest and the
    # run reaches its ceiling rather than being rescued by a repeat.
    matched = (7, 11, 4, 9, 13, 5, 8, 12, 6, 14, 3, 10, 15, 2, 16, 1)

    class _BlackedOutSearchExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.outputs: list[str] = []
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.knowledge.search",
                    description="Search governed documents for passages of prose.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            withheld = matched[len(self.outputs) % len(matched)]
            rendered = f"[showing 0 of {withheld} retrieved chunks; {withheld} withheld — {reason}]"
            self.outputs.append(rendered)
            return rendered

    executor = _BlackedOutSearchExecutor()
    # More broadenings than the ceiling allows, so reaching the ceiling is a
    # DISTINGUISHABLE outcome rather than the only way this run can end: before
    # the fix this run took all twelve calls and terminalised
    # `budget-exhausted`/`tool-calls`, which is the reported defect verbatim.
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                "read.knowledge.search",
                {
                    "query": f"dispute acknowledgement wording, broadening {index}",
                    "step_intent": "Broaden the search until something comes back.",
                },
            )
            for index in range(16)
        ]
    )

    with pytest.raises(AgenticNoProgressError) as raised:
        _run(gateway, executor, RecordingAuditSink(), max_tools=12, wall=_NO_WALL_CLOCK_LIMIT)

    error = raised.value
    assert (error.tool, error.calls) == ("read.knowledge.search", 3)
    # THREE calls, not the ceiling's twelve — and not four either. A read that
    # states it disclosed nothing is barren from the FIRST one: the extra
    # round-trip the digest half buys a genuinely-new barren result is not owed
    # to a result whose own header says broadening cannot help.
    assert len(executor.outputs) == 3
    # THE PREMISE, asserted rather than assumed. All three outputs are distinct
    # strings, so a digest over the bytes calls all three new...
    assert len(set(executor.outputs)) == 3
    # ...and not one of them carries a ref, so the ref arm is silent and the
    # digest arm is the only thing that could have voted.
    assert [runner_module._served_refs(output) for output in executor.outputs] == [(), (), ()]
    # NOT `budget-exhausted`. The run stopped nine calls short of its ceiling, so
    # there is no cap terminal to report and none is reported.
    assert not isinstance(error, ModelPlannedRun)
    assert "budget" not in error.safe_reason.lower()


def test_disclosed_nothing_reads_only_a_TOTAL_blackout_out_of_the_shipped_header() -> None:
    """The grammar boundary, pinned on all four renderings the search tool emits.

    ``_disclosed_nothing`` mirrors ``ne.api.workflows.RetrievedText
    .source_health_blackout`` (``shown == 0 and withheld > 0``) against
    ``ne.api.tools._result_header``'s one grammar. The literals below are that
    renderer's output byte-for-byte; ne-runtime does not depend on ne-api, so
    they are copied rather than generated, and this test is what says so if the
    two ever drift.
    """

    reason = (
        "their governed source is currently degraded; this is a source-health outage, "
        "not an empty corpus, so rephrasing or broadening the query cannot surface them"
    )

    # A total blackout: matched seven, disclosed none.
    assert runner_module._disclosed_nothing(
        f"[showing 0 of 7 retrieved chunks; 7 withheld — {reason}]"
    )
    # A partial disclosure served two real passages; the bytes keep their vote.
    assert not runner_module._disclosed_nothing(
        f"[showing 2 of 9 retrieved chunks; 7 withheld — {reason}]\nhandbook:0001: text"
    )
    # A genuinely empty corpus withheld nothing — and its header cannot move with
    # the wording, so the digest already stops its repeats without help.
    assert not runner_module._disclosed_nothing("[showing 0 of 0 retrieved chunks]")
    # Anything that is not this grammar is none of the runner's business.
    assert not runner_module._disclosed_nothing('{"found": false}')
    # Only the FIRST line is read, so chunk text quoting a header cannot disarm
    # a search that actually disclosed something.
    assert not runner_module._disclosed_nothing(
        f"[showing 1 of 8 retrieved chunks; 7 withheld — {reason}]\n"
        f"handbook:0001: quoting [showing 0 of 7 retrieved chunks; 7 withheld — {reason}]"
    )


def test_a_PARTIAL_withholding_still_counts_as_progress() -> None:
    """The control on the control: only a TOTAL blackout loses its digest vote.

    ``[showing 2 of 9 … 7 withheld]`` served two real passages. Something was
    disclosed, so the read moved the run forward and the streak must clear —
    narrowing the digest arm must not turn a partly-degraded source into a
    barren one. Same shipped grammar, same tool, one difference: ``shown``.
    """

    reason = (
        "their governed source is currently degraded; this is a source-health outage, "
        "not an empty corpus, so rephrasing or broadening the query cannot surface them"
    )

    class _PartlyWithheldSearchExecutor(_Executor):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0
            self._specs = (
                *self._specs,
                ToolSpec(
                    name="read.knowledge.search",
                    description="Search governed documents for passages of prose.",
                    tier=ApprovalTier.TIER_0,
                    input_schema={"type": "object", "properties": {}},
                ),
            )

        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            self.calls.append((tool, dict(args), principal.id))
            self.reads += 1
            return "\n".join(
                (
                    f"[showing 2 of 9 retrieved chunks; 7 withheld — {reason}]",
                    f"handbook:{self.reads:04d}: escalation step {self.reads}",
                    f"handbook:{self.reads + 100:04d}: notice wording {self.reads}",
                )
            )

    inner = _PartlyWithheldSearchExecutor()
    recorder = _RecordedExecutor(
        inner=inner,
        principal=_PRINCIPAL,
        sink=RecordingAuditSink(),
        max_tool_calls=30,
        max_parked_writes=0,
    )

    async def scenario() -> None:
        for index in range(8):
            await recorder.execute(
                "read.knowledge.search",
                {"query": f"escalation {index}", "step_intent": "Find the wording."},
                principal=_PRINCIPAL,
            )

    asyncio.run(scenario())

    assert inner.reads == 8
    # Not one of them opened a streak: every call disclosed passages this run had
    # not been served before.
    assert (recorder.no_progress_tool, recorder.no_progress_calls) == (None, 0)
    # And it did so with no ref anywhere — a real chunk id is
    # `{document_id}:{index:04d}` (`ne.brain.ingest.chunker:146`), not a `urn:ne:`
    # row identity, which is exactly why muting the digest for this TOOL rather
    # than for this RESULT would have made these productive searches look barren.
    assert recorder.served == set()


# --- NE-OUTPUT-CONTRACT: an unvalidatable final reply -----------------------
#
# Live 2026-08-27, frontier lane (`served_model: frontier-default`): the owner's
# demo question retrieved real evidence in ~2.5s and then died at 27s on
# `pydantic_ai.exceptions.UnexpectedModelBehavior: Exceeded maximum output
# retries (1)` (_agent_graph.py:1315 — the `except ToolRetryError` arm that
# consumes output-retry budget). The run terminalised `failed` with
# `failure_reason: null`, so the reader saw a bare status word for a failure the
# server could describe exactly. These tests pin both halves: a multi-hop turn
# over real evidence still ANSWERS, an unvalidatable one still FAILS (nothing
# malformed is passed through), and the failure now STATES its cause without
# inventing one.


def test_multi_hop_turn_over_real_evidence_still_produces_its_answer() -> None:
    """The mutation guard's other half: the fix must not make answering harder."""
    executor = _Executor()
    sink = RecordingAuditSink()
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find the SOP."}),
            tool_call_completion("read.kg.directory", {"step_intent": "Confirm the counties."}),
            _structured_completion("Columbia, Richmond, Jefferson."),
        ]
    )

    result = _run(gateway, executor, sink)

    assert result.outcome == "complete"
    assert result.output == "Columbia, Richmond, Jefferson."
    assert len(result.steps) == 2


@pytest.mark.parametrize(
    ("reply", "expected_location"),
    [
        # The shape the contradicting catalog sentence invited: a prose answer,
        # which the prompted object processor cannot parse at all.
        (
            "For DR-4830 the affected counties are Columbia, Richmond and Jefferson.",
            "<whole reply>",
        ),
        # A JSON object of the WRONG shape — extra="forbid" plus a missing field.
        ('{"answer": "Columbia, Richmond, Jefferson."}', "final_text"),
    ],
)
def test_unvalidatable_final_reply_fails_the_run_and_names_the_rejection(
    reply: str,
    expected_location: str,
) -> None:
    executor = _Executor()
    sink = RecordingAuditSink()
    # Every attempt after the tool call repeats the same unvalidatable reply, so
    # the bounded output-retry budget is genuinely exhausted.
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find the SOP."}),
            *[text_completion(reply)] * 6,
        ]
    )

    with pytest.raises(AgentOutputContractError) as raised:
        _run(gateway, executor, sink)

    reason = raised.value.safe_reason
    # It FAILS: no partial/unvalidated draft is handed back as an answer.
    assert reply not in reason
    # It STATES a cause the server actually observed — the schema location and
    # Pydantic's own error code, never the rejected value or the model's prose.
    assert "did not match this Run's required answer format" in reason
    assert expected_location in reason
    assert str(raised.value) == reason


@pytest.mark.parametrize(
    "payload",
    [
        "For DR-4830 the affected counties are Columbia, Richmond and Jefferson.",
        '{"answer": "Columbia, Richmond, Jefferson."}',
        # Every declared claim field wrong at once — the widest rejection list the
        # schema can produce, and the one most likely to overrun the cap.
        '{"final_text":"ok","claims":[{"claim_key":"","claim_text":"",'
        '"served_ref":"chunk-7","span_start_byte":-1}]}',
    ],
)
def test_the_published_reason_survives_the_projection_s_own_validation(payload: str) -> None:
    """A reason the projection drops is not an explanation — it is another null.

    `RunProjection._failure_reason` refuses any checkpointed `error_reason` over
    256 characters or carrying a non-printable character. Both bounds are pinned
    here against the builder, so widening one without the other cannot silently
    reintroduce `failure_reason: null`.
    """
    try:
        _StructuredAgenticCompletion.model_validate_json(payload)
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - every payload above is invalid by construction
        pytest.fail("the malformed completion must not validate")

    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS == 256
    assert reason.strip() == reason
    assert all(character.isprintable() for character in reason)
    assert "Schema rejected:" in reason


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # The model called a tool where its answer belonged. Well-formed or not:
        # `_parse` only demotes the MALFORMED one to text, and that is the one
        # that reaches the answer schema.
        ('{"tool_call":{"name":"read.knowledge.search","arguments":{"q":"d"}}}', "obj=tool-call"),
        ('{"tool_call":{"arguments":{"q":"d"}}}', "obj=tool-call"),
        # The call's FIELDS without its wrapper.
        ('{"name":"read.knowledge.search","arguments":{"q":"d"}}', "obj=tool-call-fields"),
        # A claim hoisted to the top level instead of nested under `claims`.
        ('{"claim_key":"a","claim_text":"b","source_id":"c"}', "obj=claim"),
        # The answer, wrapped in a key of the model's own invention.
        ('{"answer":{"final_text":"x","claims":[]}}', "obj=wrapper"),
        # Something else entirely — named as such rather than guessed at.
        ('{"response":"x"}', "obj=other"),
    ],
)
def test_the_reason_says_which_other_object_the_model_sent(payload: str, expected: str) -> None:
    """`extra_forbidden` + `final_text [missing]` is one sentence for six mistakes.

    `_safe_error_location` masks every undeclared key to `<undeclared key>` —
    correctly, it is model-authored text — and the builder then dedupes those
    identical entries. So a lone tool call, a bare name/arguments pair, a
    top-level claim and an `{"answer": ...}` wrapper all rendered BYTE-IDENTICALLY,
    and they want four different fixes. Measured before this existed: all six
    payloads below produced the same reason.
    """
    try:
        _StructuredAgenticCompletion.model_validate_json(payload)
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - every payload above is invalid by construction
        pytest.fail("the payload must not validate")

    assert expected in reason
    assert "undeclared=" in reason
    # Still inside the projection's cap, with the richer sentence intact.
    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS == 256
    assert all(character.isprintable() for character in reason)


def test_the_rejected_object_shape_republishes_no_key_and_no_value() -> None:
    """C12: a closed token plus an integer, exactly like `rejected_reply_shape`."""
    secret = "Meridian-4830-SECRET"
    payload = json.dumps({secret: {"customer": secret}})

    try:
        _StructuredAgenticCompletion.model_validate_json(payload)
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - invalid by construction
        pytest.fail("the payload must not validate")

    assert secret not in reason
    assert "obj=other undeclared=1" in reason


def test_a_malformed_reply_keeps_its_shape_token_and_gains_no_object_token() -> None:
    """The two diagnostics do not collide: a reply that was not JSON has no
    rejected OBJECT to describe, and says so by omission rather than by `other`."""
    try:
        _StructuredAgenticCompletion.model_validate_json("not json at all")
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - invalid by construction
        pytest.fail("the payload must not validate")

    assert "json_invalid" in reason
    assert "obj=" not in reason


def test_failure_reason_invents_nothing_when_no_validation_error_is_reachable() -> None:
    """No ValidationError in the chain ⇒ the generic sentence, no fabricated fields."""
    reason = runner_module._output_contract_reason(
        UnexpectedModelBehavior("Exceeded maximum output retries (1)")
    )

    assert "Schema rejected:" not in reason
    assert reason.endswith("No answer was produced.")


def test_the_published_reason_never_quotes_the_rejected_value() -> None:
    """C12: `loc` and `type` only — never `input`, never the rendered `msg`."""
    secret = "Meridian customer 4830-SECRET"
    try:
        _StructuredAgenticCompletion.model_validate({"final_text": {"nested": secret}})
    except ValidationError as validation:
        retry = ToolRetryError(RetryPromptPart(content=f"rejected: {secret}"))
        retry.__cause__ = validation
        fatal = UnexpectedModelBehavior("Exceeded maximum output retries (1)")
        fatal.__cause__ = retry
    else:  # pragma: no cover - the payload above is invalid by construction
        pytest.fail("the malformed completion must not validate")

    reason = runner_module._output_contract_reason(fatal)

    assert secret not in reason
    assert "final_text" in reason


def test_a_model_invented_key_name_is_never_republished_as_the_reason() -> None:
    """`extra_forbidden`'s last `loc` segment is MODEL-authored, not ours (C12)."""
    secret = "customer 4830 Meridian"
    try:
        _StructuredAgenticCompletion.model_validate({"final_text": "ok", secret: 1})
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - extra="forbid" rejects the payload by construction
        pytest.fail("an undeclared key must not validate")

    assert secret not in reason
    assert "<undeclared key> [extra_forbidden]" in reason


# --- NE-OUTPUT-TRANSPORT: the reason says WHICH not-JSON --------------------


@pytest.mark.parametrize(
    ("reply", "expected_shape"),
    [
        # Prose and only prose: the model answered in the wrong transport.
        ("Columbia, Richmond and Jefferson.", "opens=prose fences=0 objects=0"),
        # Cut off at the output budget mid-object: it STARTED right and stopped.
        ('{"final_text":"Columbia, Rich', "opens=object fences=0 objects=0"),
        # Two candidate objects: normalisation deliberately declined to guess.
        (
            'e.g. {"final_text":"<a>"} then {"final_text":"b","claims":[]}',
            "opens=prose fences=0 objects=2",
        ),
    ],
)
def test_a_json_invalid_reason_distinguishes_prose_truncation_and_ambiguity(
    reply: str, expected_shape: str
) -> None:
    """`<whole reply> [json_invalid]` alone cannot tell these three apart, and they
    want three different fixes. The shape token does, in a closed server-owned
    vocabulary, and still under the projection's 256-character cap."""
    try:
        _StructuredAgenticCompletion.model_validate_json(reply)
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - every payload above is invalid by construction
        pytest.fail("the malformed completion must not validate")

    assert f"<whole reply> [json_invalid; {expected_shape} chars={len(reply)}]" in reason
    assert len(reason) <= runner_module._MAX_SAFE_REASON_CHARS


def test_the_shape_token_republishes_no_byte_of_the_rejected_reply() -> None:
    """C12 holds for the shape exactly as it holds for `loc`/`type`: the offending
    `input` is READ to describe it and never quoted, in whole or in part."""
    secret = "Meridian customer 4830-SECRET renewal at risk"
    try:
        _StructuredAgenticCompletion.model_validate_json(f"Summary: {secret}")
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - prose is invalid by construction
        pytest.fail("prose must not validate")

    for token in ("Meridian", "4830", "SECRET", "renewal", "Summary"):
        assert token not in reason
    assert "json_invalid; opens=prose" in reason


def test_a_non_json_invalid_rejection_keeps_its_bare_rendering() -> None:
    """The shape answers a question only `json_invalid` raises. A field-level
    rejection already names the field, so its entry is untouched."""
    try:
        _StructuredAgenticCompletion.model_validate_json('{"answer":"x"}')
    except ValidationError as validation:
        reason = runner_module._output_contract_reason(validation)
    else:  # pragma: no cover - the payload above is invalid by construction
        pytest.fail("the malformed completion must not validate")

    assert "json_invalid" not in reason
    assert ";" not in reason.split("Schema rejected:")[1]


# ---------------------------------------------------------------------------
# NE feed wire: the plan-ordinal -> tool_call_id binding and the pre-effect
# model name. The binding is stated by the one frame that knows both ids
# (ReceiptGatedToolExecutor), never inferred by counting two counters.
# ---------------------------------------------------------------------------


def test_planner_announces_its_checkpointed_ordinal_to_the_executing_frame() -> None:
    from ne.runtime.run_audit_receipts import current_agentic_plan_step

    observed: list[object] = []

    class _AnnouncementObservingExecutor(_Executor):
        async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
            observed.append(current_agentic_plan_step())
            return await super().execute(tool, args, principal=principal)

    executor = _AnnouncementObservingExecutor()
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.directory", {"step_intent": "Find evidence."}),
            _structured_completion("complete"),
        ]
    )
    result = asyncio.run(
        run_model_planned_agent(
            task="do the bounded work",
            instructions="use governed tools",
            principal=_PRINCIPAL,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
            sink=RecordingAuditSink(),
            max_tool_calls=2,
            total_tokens_limit=1000,
            wall_clock_seconds=1,
            max_parked_writes=1,
        )
    )
    assert result.outcome == "complete"
    # The announcement names EXACTLY the ordinal and tool the planner durably
    # checkpointed for this call, and it does not leak past the call.
    assert [(step.ordinal, step.tool) for step in observed] == [(0, "read.kg.directory")]  # type: ignore[union-attr]
    assert current_agentic_plan_step() is None


class _BindStop(Exception):
    """Stops the receipt-gated call after the bind write, before any effect."""


class _RecordingBoundary:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def heartbeat(self, stage: str) -> None:
        self.events.append(("heartbeat", stage))

    async def checkpoint(self, *, stage: str, safe_detail: Mapping[str, object]) -> None:
        self.events.append(("checkpoint", (stage, dict(safe_detail))))

    async def await_inflight(self, operation: Any, *, stage: str) -> Any:
        return await operation

    def checkpoints(self) -> list[tuple[str, dict[str, object]]]:
        return [detail for kind, detail in self.events if kind == "checkpoint"]  # type: ignore[misc]


class _StoppingReceiptSink:
    async def append_receipt(self, event: Any) -> Any:
        raise _BindStop

    async def append(self, event: Any) -> None:
        raise _BindStop


class _StoppingAudit:
    def __init__(self) -> None:
        self.targets: list[dict[str, Any]] = []

    def for_target(self, **kwargs: Any) -> Any:
        self.targets.append(kwargs)
        return _StoppingReceiptSink()


def _receipt_gated_executor(boundary: _RecordingBoundary) -> Any:
    from ne.runtime.run_runner import ReceiptGatedToolExecutor

    return ReceiptGatedToolExecutor(
        _Executor(),
        audit=_StoppingAudit(),  # type: ignore[arg-type]
        run=SimpleNamespace(run_id="run-1"),  # type: ignore[arg-type]
        target=SimpleNamespace(attempt_id="attempt-1"),  # type: ignore[arg-type]
        owner_principal_id=_PRINCIPAL.id,
        step_id="run-tool",
        boundary=boundary,
    )


def test_receipt_gate_binds_the_announced_plan_ordinal_to_its_minted_call_id() -> None:
    from ne.runtime.run_audit_receipts import (
        AgenticPlanStepAnnouncement,
        agentic_plan_step_scope,
    )

    boundary = _RecordingBoundary()
    executor = _receipt_gated_executor(boundary)

    async def scenario() -> None:
        with (
            agentic_plan_step_scope(
                AgenticPlanStepAnnouncement(ordinal=3, tool="read.kg.directory")
            ),
            pytest.raises(_BindStop),
        ):
            await executor.execute("read.kg.directory", {}, principal=_PRINCIPAL)

    asyncio.run(scenario())
    # The binding is durably checkpointed BEFORE the receipt (the receipt sink
    # raised, and the bind is already recorded), and it carries the planner's
    # ordinal beside the EXACT minted id -- no counter arithmetic anywhere.
    assert boundary.checkpoints() == [
        (
            "agentic-call:3",
            {"ordinal": 3, "tool": "read.kg.directory", "tool_call_id": "run-tool:tool:0"},
        )
    ]


def test_receipt_gate_never_binds_a_mismatched_or_absent_announcement() -> None:
    from ne.runtime.run_audit_receipts import (
        AgenticPlanStepAnnouncement,
        agentic_plan_step_scope,
    )

    boundary = _RecordingBoundary()
    executor = _receipt_gated_executor(boundary)

    async def scenario() -> None:
        # An announcement naming a DIFFERENT tool would produce a wrong join;
        # nothing is written, and the call itself proceeds unchanged.
        with (
            agentic_plan_step_scope(
                AgenticPlanStepAnnouncement(ordinal=0, tool="write.internal.note")
            ),
            pytest.raises(_BindStop),
        ):
            await executor.execute("read.kg.directory", {}, principal=_PRINCIPAL)
        # No announcement (a deterministic Run, or a non-planner caller): the
        # binding checkpoint does not exist for anyone to misread.
        with pytest.raises(_BindStop):
            await executor.execute("read.kg.directory", {}, principal=_PRINCIPAL)

    asyncio.run(scenario())
    assert boundary.checkpoints() == []


def test_model_pre_effect_names_only_a_declared_pinned_model() -> None:
    from ne.contracts.models import CompletionRequest
    from ne.runtime.run_runner import ReceiptGatedGateway

    recorded: list[Any] = []

    class _RecordingReceiptSink:
        async def append(self, event: Any) -> None:
            recorded.append(event)

    class _RecordingAudit:
        def for_target(self, **kwargs: Any) -> Any:
            return _RecordingReceiptSink()

    class _ServingGateway:
        async def complete(self, request: CompletionRequest) -> Any:
            return "served"

    gateway = ReceiptGatedGateway(
        _ServingGateway(),  # type: ignore[arg-type]
        audit=_RecordingAudit(),  # type: ignore[arg-type]
        owner_principal_id=_PRINCIPAL.id,
        step_id="run-model",
    )

    def request(model: str | None) -> CompletionRequest:
        return CompletionRequest(
            messages=(),
            task="frontier-default",
            principal_id=_PRINCIPAL.id,
            requested_model=model,
        )

    async def scenario() -> None:
        assert await gateway.complete(request("frontier-anthropic")) == "served"
        assert await gateway.complete(request(None)) == "served"
        # Provider-shaped text -- spaces, quotes -- is OMITTED, never truncated
        # into something that reads like a declared name.
        assert await gateway.complete(request('error: "boom" happened')) == "served"

    asyncio.run(scenario())
    assert [event.provenance.get("requested_model") for event in recorded] == [
        "frontier-anthropic",
        None,
        None,
    ]
    assert all(event.action == "run.model.pre_effect" for event in recorded)


# --- U3-F4: the ambient evidence carrier is populated for EVERY governed call --


class _TierZeroProbeExecutor(_Executor):
    """Two TIER-0 read tools; every call records the ambient evidence carrier."""

    def __init__(self) -> None:
        super().__init__()
        self.evidence_carriers: list[tuple[str, ...]] = []
        self._specs = (
            self._specs[0],  # read.kg.directory, TIER_0
            ToolSpec(
                name="read.kg.status",
                description="Read one status row.",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {}},
            ),
        )

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.evidence_carriers.append(current_protected_tool_evidence_refs())
        self.calls.append((tool, dict(args), principal.id))
        return _PROTECTED_REF if tool == "read.kg.directory" else "status: open"


def test_every_governed_call_after_a_protected_read_carries_the_served_evidence() -> None:
    """U3-F4 (live run 7f782fe0). The runner used to populate the carrier only
    for calls IT predicted as protected (``is_write or spec.tier >= TIER_2``),
    but the middleware gates on the STRICTER-OF effective tier — the
    ``tiers.rego`` mirror, the PDP-decided tier, and obligation floors can all
    escalate a non-write TIER-0 tool, and none of those arms is predictable
    from the spec the runner reads. Such a call reached
    ``_run_source_references`` with an EMPTY carrier and died permanently
    un-parkable with an evidence-shaped refusal. The carrier is now populated
    UNCONDITIONALLY from the server-observed served set for every governed
    call, so whichever arm escalates, the approval parks over the COMPLETE
    admitted evidence (the middleware's exact-serve and stamp checks still
    bind every member)."""
    executor = _TierZeroProbeExecutor()
    sink = RecordingAuditSink()
    resolver = _MutableAuthorityResolver(materialized_text=_CHUNK_SENTENCE)
    result = _run(
        ScriptedGateway(
            [
                tool_call_completion("read.kg.directory", {"step_intent": "Read evidence."}),
                tool_call_completion("read.kg.status", {"step_intent": "Read the status."}),
                _structured_completion(_QUOTING_ANSWER),
            ]
        ),
        executor,
        sink,
        # The bound under test is the CARRIER, not the clock: the harness's
        # 1-second default wall deadline manufactures budget-exhausted
        # terminals on a loaded box (observed in review at load ~3).
        wall=_NO_WALL_CLOCK_LIMIT,
        source_guard=_source_guard(resolver),
    )
    assert result.outcome == "complete"
    assert [tool for tool, _args, _principal in executor.calls] == [
        "read.kg.directory",
        "read.kg.status",
    ]
    # First call: nothing served yet, no carrier to enter. Second call: a
    # TIER-0 non-write STILL carries the full served protected evidence, so a
    # mirror/PDP/obligation escalation inside the middleware can park over it.
    assert executor.evidence_carriers == [(), (_PROTECTED_REF,)]
    # The scope is call-bounded: nothing leaks past the run.
    assert current_protected_tool_evidence_refs() == ()


def test_every_profile_marker_runner_emits_is_a_name_the_span_writer_accepts() -> None:
    """The round-13 derivation, extended to the SECOND module that emits these.

    Round 13 replaced a hand-typed list in the worker's span test with one
    derived from `run_execution.py`, and the record then claimed any future
    `profile:` marker was covered. An adversarial pass showed that was false in
    the same commit: `profile:agent-built` is emitted from `runner.py`, outside
    that derivation, and survived only on a hand-typed assertion -- the pattern
    the derivation was written to retire. This closes the second module.

    The stakes are the worker test's stakes. `_validated_stage` runs INSIDE the
    lease-renewal transaction and raises `ValueError`, which
    `_LeaseExecutionBoundary.heartbeat` does not translate, so a name the guard
    refuses aborts the renewal and reaches the caller as something nothing
    downstream reads as a lost lease.
    """
    import ast
    from pathlib import Path as _Path

    from ne.runtime.run_workflow import _validated_stage

    source = (
        _Path(__file__).resolve().parents[1] / "src" / "ne" / "runtime" / "runner.py"
    ).read_text()
    # ⚠️ FOUR ROUNDS OF THIS GUARD CHECKED LESS THAN ITS COMMENT CLAIMED, so the
    # history is worth the lines. R21 matched only f-strings whose text STARTS
    # with `profile:` -- run against `run_runner.py`, the file its own comment
    # cited as the precedent, it found ZERO of that file's three interpolated
    # heartbeats. R22 required a double-quoted literal but anchored on
    # `.heartbeat(`, so `hb = boundary.heartbeat` then `await hb(name)` evaded
    # it. R23 anchored on the identifier over comment-stripped text, which closed
    # that hole and opened three more: it read a DIFFERENT text than the
    # derivation (so a call quoted in a COMMENT inflated the name count and could
    # mask a real emission's deletion), it fails closed on a call the formatter
    # wraps across lines, and it forbade the module from ever defining or naming
    # `heartbeat` at all.
    #
    # ⭐ ROUND 24 STOPS PATTERN-MATCHING TEXT AND PARSES THE MODULE. `ast` sees
    # exactly what Python sees: comments and docstrings are not code, wrapping is
    # irrelevant, and an attribute BOUND rather than called is a different node.
    # Three properties, each the failure of one earlier round:
    #   (a) every `.heartbeat(...)` call takes ONE positional argument that is a
    #       string CONSTANT -- no f-string, concatenation, name or conditional;
    #   (b) the `heartbeat` attribute is never accessed except as a call target,
    #       and the bare name is never used, so nothing can route around (a);
    #   (c) EVERY literal so found is run through `_validated_stage`, not just the
    #       `profile:`-prefixed ones. An adversarial pass was right that this was
    #       the live hole: `heartbeat("skills activated")` is a plain literal, so
    #       every earlier guard PASSED it while the derivation never captured it
    #       and so never validated it. At runtime the space makes
    #       `_validated_stage` raise `ValueError` INSIDE the lease-renewal
    #       transaction, where `_LeaseExecutionBoundary.heartbeat` translates only
    #       `RuntimeError` -- the renewal aborts and nothing downstream reads it
    #       as a lost lease. That is verbatim the failure this test exists to
    #       find, and "only an interpolated name can reach it" was false.
    #
    # ⚠️ ONE MUTATION I EXPECTED TO RED DID NOT, AND THE GUARD WAS RIGHT:
    # `heartbeat("profile:" "binding")`. Python folds adjacent string literals at
    # PARSE time, so `ast` sees one `Constant` whose value is exactly
    # `"profile:binding"` -- indistinguishable from the single-literal form and
    # identical in value, hence correctly allowed and correctly validated.
    # `"profile:" + name` is a `BinOp` and reds, as do a conditional expression
    # and passing the bound attribute to another function. Recorded because a
    # future reader will see the implicit-concatenation form pass and wonder.
    tree = ast.parse(source)
    call_targets: set[int] = set()
    stage_literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "heartbeat"):
            continue
        call_targets.add(id(func))
        assert len(node.args) == 1 and not node.keywords, (
            "a heartbeat call here does not take exactly one positional stage name "
            f"(line {node.lineno})"
        )
        arg = node.args[0]
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"the stage name at line {node.lineno} is not a string CONSTANT -- an "
            "f-string, concatenation, name or conditional is invisible to the "
            "derived list below, and a name the writer refuses aborts the lease "
            "renewal as an untranslated ValueError"
        )
        stage_literals.append(arg.value)
    bound = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "heartbeat"
        and id(node) not in call_targets
    ]
    assert not bound, (
        "the `heartbeat` attribute is bound or passed rather than called directly "
        f"(line {bound[0].lineno}); every route around the call form defeats the "
        "derived list"
    )
    assert not [
        node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "heartbeat"
    ], "a bare `heartbeat` name is in play in this module; only `.heartbeat(...)` is allowed"
    # ⚠️ AND NO STRING "heartbeat" EITHER. An adversarial pass on #943 got past the
    # round-24 guard with `getattr(context.boundary, "heartbeat")("skills activated")`:
    # the `Call.func` is a `Call`, not an `Attribute`, so the call check never sees
    # it; `"heartbeat"` is a `Constant`, not a `Name`, so the bare-name check passes;
    # and the stage literal never enters `stage_literals`, so `_validated_stage` never
    # runs on it. The space then raises `ValueError` inside the lease-renewal
    # transaction -- verbatim the failure this test exists to find, suite green.
    # Refusing the METHOD NAME AS DATA closes `getattr`, `operator.attrgetter`,
    # `functools.partial` and every other indirection at once, without the test having
    # to enumerate them.
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "heartbeat"
    ], (
        'the string "heartbeat" appears in this module; the method name used as DATA '
        "reaches the boundary through `getattr` and friends, which no AST call check "
        "can see"
    )
    assert stage_literals, "no heartbeat calls found -- the derivation is reading the wrong source"
    # (c): EVERY name, not only the profile-prefixed ones.
    for stage in stage_literals:
        assert _validated_stage(stage) == stage, stage
    emitted = sorted({name for name in stage_literals if name.startswith("profile:")})
    # ⚠️ The `_validated_stage` loop is ABOVE the membership assertions ON PURPOSE,
    # and round 23 had it the other way round in this file while `runner.py`'s twin
    # stated the discipline correctly. With membership first, renaming
    # `profile:agent-built` to `profile:agent built` reds on
    # "agent-built is missing" instead of on the writer's own refusal --
    # so the mutation proves the wrong thing. This ordering makes it honest.
    assert "profile:agent-built" in emitted, emitted


# --- NE-ROWSENT-SKIPS: the shape field distinguishes THREE non-gradings ------
#
# `rowsent` is the only field that separates the failures inside one
# `AgentOutputContractError`, and read against live terminals it could not tell
# "this source retained no text" from "this source was not a table rendering" —
# both produced NO entry, and they are different defects wanting different
# fixes. One is upstream of the claim binder entirely. These pin that each
# non-grading outcome now says which one it was.


def _shape_of(final_text: str, served: set[str], **texts: str) -> str:
    with pytest.raises(ActionValidationError, match="verbatim in any source") as raised:
        _bind(final_text, served, **texts)
    return str(raised.value.span_shape)  # type: ignore[attr-defined]


def test_a_claimable_source_with_no_retained_text_is_recorded_as_minus_one() -> None:
    """The case that was invisible: claimable, but nothing was ever delivered.

    A source the run counts as claimable that retained no text cannot prove a
    span against anything — and that is a defect UPSTREAM of the claim binder,
    not a fact about the model's writing. It used to leave no trace at all, so a
    reader could not tell it from a source whose text simply was not a table.
    """
    shape = _shape_of(_UNQUOTABLE_ANSWER, {_PROTECTED_REF})

    assert "rowsent=-1" in shape, shape
    # Still counted as a claimable source, which is what makes the -1 meaningful:
    # one source, zero gradings, and now a stated reason.
    assert "sources=1" in shape


def test_retained_text_that_is_not_a_table_rendering_is_recorded_as_minus_two() -> None:
    """Prose retained text: the table path does not apply, and now says so.

    This is the OTHER reading of a missing entry, and the whole point of the
    change is that it is no longer the same value as the one above.
    """
    shape = _shape_of(_UNQUOTABLE_ANSWER, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    assert "rowsent=-2" in shape, shape
    assert "rowsent=-1" not in shape


def test_the_three_skip_reasons_are_distinct_values() -> None:
    """The property that makes the field worth reading, asserted directly.

    Collapsing any two of these back onto one value re-creates exactly the
    ambiguity that made 44 live terminals look like a single defect.
    """
    assert (
        len(
            {
                runner_module._ROWSENT_NO_RETAINED_TEXT,
                runner_module._ROWSENT_NOT_A_RENDERING,
                runner_module._ROWSENT_EMPTY_CORPUS,
            }
        )
        == 3
    )
    # And all negative, so no skip reason can ever be mistaken for a COUNT of
    # graded sentences, which is what non-negative values mean.
    for marker in (
        runner_module._ROWSENT_NO_RETAINED_TEXT,
        runner_module._ROWSENT_NOT_A_RENDERING,
        runner_module._ROWSENT_EMPTY_CORPUS,
    ):
        assert marker < 0, marker


# --- NE-ROWSENT-CODES: WHY each graded candidate refused ---------------------
#
# `rowsent` counts the candidates the table-sentence path graded. It never said
# what any of them died of, so a run that graded four and bound none read the
# same whether the model paraphrased, crossed a row boundary or invented a
# number. `rowsentcodes` names the RULE, in the grounding gate's own closed
# vocabulary, one integer per candidate — and still not one byte of content.

#: A minimal verified-T3 rendering: the exact shape
#: ``rendering_preview_answers`` parses, one row, three columns.
_ROWSENT_COLUMNS = ("dispute_id", "credit_amount_cents", "notice_date")
_ROWSENT_RENDERING = "\n".join(
    (
        "[showing 1 of 1 rows]",
        " | ".join(_ROWSENT_COLUMNS),
        "-+-".join("-" * len(column) for column in _ROWSENT_COLUMNS),
        "mer-dsp-003 | 20000 | 2031-04-24",
    )
)
#: The live reproduction the codes exist for: over the SAME row, the first of
#: these binds and the second refuses, and seven fix attempts died guessing at
#: which rule the second one hit.
_ROWSENT_BINDING_SENTENCE = "mer-dsp-003 has credit_amount_cents 20000."
_ROWSENT_REFUSED_SENTENCE = "mer-dsp-003 has notice_date 2031-04-24."


def _shape_field(shape: str, name: str) -> str:
    """The value of one ``name=value`` field of a ``span_shape`` string."""
    fields = dict(pair.split("=", 1) for pair in shape.split())
    assert name in fields, shape
    return fields[name]


def test_a_refused_row_sentence_records_the_rule_that_refused_it() -> None:
    """The whole instrument: a graded-and-refused candidate now says WHY.

    Before this, the answer below produced `rowsent=1` — one candidate graded,
    nothing bound — and the reader had nine grounding rules to guess between.
    """
    shape = _shape_of(
        _ROWSENT_REFUSED_SENTENCE, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING
    )

    assert _shape_field(shape, "rowsent") == "1", shape
    # The rule really is phrase-recombination, read off the gate, not asserted
    # by a literal here: the code is looked up in the mapping under test.
    assert _shape_field(shape, "rowsentcodes") == str(
        runner_module._ROWSENT_REFUSAL_CODES["phrase-recombination"]
    ), shape


def test_every_graded_candidate_contributes_exactly_one_code() -> None:
    """`rowsent` counts; `rowsentcodes` explains — and the two agree per source.

    A code list shorter than the count means a candidate was graded with its
    refusal silently dropped, which is the defect this field exists to end.
    """
    two_refusals = f"{_ROWSENT_REFUSED_SENTENCE} mer-dsp-009 has notice_date 2044-01-02."
    shape = _shape_of(two_refusals, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING)

    counts = _shape_field(shape, "rowsent").split("/")
    groups = _shape_field(shape, "rowsentcodes").split("/")
    assert len(counts) == len(groups), shape
    for count, group in zip(counts, groups, strict=True):
        assert len(group.split(",")) == int(count), shape


def test_a_source_that_never_graded_a_candidate_contributes_an_empty_group() -> None:
    """The three skip markers keep their positions: a skip grades nothing.

    Read positionally against `rowsent`, an empty group is exactly "this source
    never reached the gate" — the negative marker beside it says which skip.
    """
    shape = _shape_of(_UNQUOTABLE_ANSWER, {_PROTECTED_REF}, protected_output_a=_CHUNK_SENTENCE)

    assert _shape_field(shape, "rowsent") == str(runner_module._ROWSENT_NOT_A_RENDERING), shape
    assert _shape_field(shape, "rowsentcodes") == "none", shape


def test_the_refusal_vocabulary_is_exactly_the_grounding_gates_own_classes() -> None:
    """CLOSED, and closed over the REAL vocabulary — not a parallel dialect.

    `check_grounding` already classifies every refusal into `FindingReason`.
    Inventing a second naming would guarantee drift the first time a class is
    added upstream; deriving the assertion from the Literal makes that addition
    red HERE, at the one place that has to learn a code for it.
    """
    from ne.govern.grounding import FindingReason

    assert set(runner_module._ROWSENT_REFUSAL_CODES) == set(get_args(FindingReason))
    codes = list(runner_module._ROWSENT_REFUSAL_CODES.values())
    assert len(set(codes)) == len(codes), codes
    # The out-of-band codes never collide with a rule's code, or the field would
    # report "bound" and "vocab-ungrounded" with the same integer.
    reserved = {
        runner_module._ROWSENT_CODE_BOUND,
        runner_module._ROWSENT_CODE_PROMPT_ECHO,
        runner_module._ROWSENT_CODE_OTHER,
    }
    assert len(reserved) == 3
    assert reserved.isdisjoint(codes), reserved & set(codes)


def test_an_unrecognised_refusal_reason_maps_to_the_reserved_other_code() -> None:
    """The closure property: nothing unknown ever becomes free text.

    A finding class added upstream without a code here, and a refusal carrying
    no finding at all, both land on ONE reserved integer. The alternative — a
    reason string in the diagnostic — would put gate-authored text into a frame
    that is content-free by contract (C12).
    """
    from ne.govern.grounding import GroundingFinding, GroundingVerdict

    unknown = GroundingVerdict(
        passed=False,
        findings=(GroundingFinding("a-class-invented-after-this-test", 1),),  # type: ignore[arg-type]
        prompt_echo_count=0,
    )
    assert runner_module._rowsent_refusal_code(unknown) == runner_module._ROWSENT_CODE_OTHER
    findingless = GroundingVerdict(passed=False, findings=(), prompt_echo_count=0)
    assert runner_module._rowsent_refusal_code(findingless) == runner_module._ROWSENT_CODE_OTHER
    # A verdict that PASSED the gate can still be refused by the path's own
    # prompt-echo rule; that is not a `FindingReason` and does not borrow one.
    echoed = GroundingVerdict(passed=True, findings=(), prompt_echo_count=2)
    assert runner_module._rowsent_refusal_code(echoed) == runner_module._ROWSENT_CODE_PROMPT_ECHO
    # And a real refusal reports the LOWEST code present, deterministically.
    both = GroundingVerdict(
        passed=False,
        findings=(
            GroundingFinding("phrase-recombination", 1),
            GroundingFinding("vocab-ungrounded", 3),
        ),
        prompt_echo_count=0,
    )
    assert runner_module._rowsent_refusal_code(both) == min(
        runner_module._ROWSENT_REFUSAL_CODES["phrase-recombination"],
        runner_module._ROWSENT_REFUSAL_CODES["vocab-ungrounded"],
    )


def test_the_shape_stays_integers_only_with_the_codes_attached() -> None:
    """C12 holds field by field: every value is digits and separators.

    Asserted over the WHOLE shape, not just the new field — a diagnostic that
    leaks one column name leaks it wherever it is spelled.
    """
    shape = _shape_of(
        _ROWSENT_REFUSED_SENTENCE, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING
    )

    for pair in shape.split():
        name, value = pair.split("=", 1)
        assert re.fullmatch(r"none|[0-9,/-]*", value), (name, value)
    # And no word of the answer or the rendering survives anywhere in it.
    content = (*_ROWSENT_REFUSED_SENTENCE.split(), *_ROWSENT_RENDERING.split())
    for token in content:
        if any(character.isalpha() for character in token):
            assert token not in shape, token


def test_recording_the_codes_changes_no_binding_decision() -> None:
    """Additive: the same sentence binds, over the same row, unchanged.

    The codes are appended AFTER `_passes` has already decided. This pins the
    binding half of the live reproduction — the sibling of the refused sentence
    above, from the same row — so an "improvement" to the diagnostic that moved
    the gate could not pass silently.
    """
    (bound,) = _bind(
        _ROWSENT_BINDING_SENTENCE, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING
    )

    span = _ROWSENT_BINDING_SENTENCE.encode("utf-8")[bound.span_start_byte : bound.span_end_byte]
    # The sentence itself, without its terminator — the boundary the path cuts at.
    assert span.decode("utf-8") == _ROWSENT_BINDING_SENTENCE.rstrip(".")
    assert bound.served_ref == _PROTECTED_REF


def test_the_codes_field_is_bounded_so_it_never_costs_the_whole_shape() -> None:
    """The field cannot grow until the fragment stops fitting the safe reason.

    `_failure_reason` fits `[shape]` as ONE fragment and drops it whole when it
    is too long, so an unbounded per-candidate list would trade `aw`, `sources`,
    `maxrun` and `floor` away for the very codes it adds. `rowsent` still carries
    the exact counts, so nothing is lost but repetition.
    """
    pathological = [[5] * runner_module._MAX_GRADED_SENTENCES for _ in range(3)]
    field = runner_module._rowsent_codes_field(pathological)

    integers = [code for group in field.split("/") for code in group.split(",") if code]
    assert len(integers) == runner_module._ROWSENT_CODES_RENDERED
    # Still one group per entry, so the positional read against `rowsent` holds
    # even where the budget ran out: the later groups render empty, not absent.
    assert len(field.split("/")) == len(pathological)
    assert re.fullmatch(r"[0-9,/]*", field), field


# --- NE-ROWSENT-CODES-FIT: the newest field can never cost the older ones ----
#
# `_output_contract_reason` fits `[shape]` as ONE fragment and keeps it only if
# the whole reason lands under `_MAX_SAFE_REASON_CHARS`, so a shape one
# character too long is not a shorter diagnostic -- it is NO diagnostic and a
# `failure_reason: null` all over again. Appending `rowsentcodes` could push a
# shape over that line and take `aw`, `sources`, `maxrun`, `floor` AND `rowsent`
# down with it: the newest field costing strictly more than it added. The
# 32-integer render cap does not prevent it -- the `/` group separators are one
# per claimable source and are not capped. `_shape_renderings` is what does, by
# shedding codes and only codes until the shape fits.


def _codes_free(shape: str) -> str:
    """``shape`` with its `rowsentcodes` field removed -- the guaranteed floor."""
    head, separator, _ = shape.rpartition(f" {runner_module._ROWSENT_CODES_FIELD}")
    return head if separator else shape


def _published_reason(shape: str) -> str:
    """The safe reason a run carrying ``shape`` actually publishes."""
    record = runner_module._ClaimContractRejection()
    record.record(runner_module._CLAIM_NO_COMMON_SPAN, shape=shape)
    return runner_module._output_contract_reason(
        UnexpectedModelBehavior("Exceeded maximum output retries (1)"),
        claim_rejection=record,
    )


def _room_for_one_shape() -> int:
    """The longest shape the safe reason can carry, MEASURED not assumed.

    Probing with a one-character shape gives the exact overhead of everything
    else in the sentence, so these tests stay pinned to the real budget rather
    than to a constant that would drift the moment a fragment is reworded.
    """
    probe = _published_reason("X")
    assert "[X]" in probe, probe
    return runner_module._MAX_SAFE_REASON_CHARS - (len(probe) - len("X"))


def _shape_filling(budget: int, *, groups: int) -> str:
    """A shape whose codes-free form is EXACTLY ``budget`` characters wide.

    Padded through `aw`, which is a word count: any width of it is a shape the
    span selector could really attach, so the test measures the fitter rather
    than a string that could not occur.
    """
    counts = "/".join(["1"] * groups)
    codes = "/".join(["5"] * groups)
    tail = (
        f" sources={groups} maxrun=3 floor={runner_module._MIN_BOUND_SPAN_WORDS} rowsent={counts}"
    )
    padding = budget - len("aw=") - len(tail)
    assert padding > 0, (budget, tail)
    head = f"aw={'9' * padding}{tail}"
    assert len(head) == budget, head
    return f"{head} {runner_module._ROWSENT_CODES_FIELD}{codes}"


def test_a_shape_whose_codes_cannot_fit_still_publishes_every_prior_field() -> None:
    """(a) The worst case: not one code fits, and NOTHING else is lost.

    The codes-free form here fills the budget exactly, so every rendering that
    carries a code overflows. Before the fitter that meant the whole `[shape]`
    fragment was dropped and the reader got no `aw`, no `sources`, no `maxrun`,
    no `floor` and no `rowsent` either -- the field cost five older ones.
    """
    shape = _shape_filling(_room_for_one_shape(), groups=8)
    reason = _published_reason(shape)

    # The floor is published WHOLE, not partially: `_codes_free(shape)` is one
    # exact substring, so no prior field was shortened to make room either.
    assert f"[{_codes_free(shape)}]" in reason, reason
    for name in ("aw=", "sources=", "maxrun=", "floor=", "rowsent="):
        assert name in reason, (name, reason)
    # And the codes really were the thing that could not fit.
    assert runner_module._ROWSENT_CODES_FIELD not in reason, reason
    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS
    assert all(character.isprintable() for character in reason)


def test_an_oversized_code_list_is_trimmed_instead_of_dropping_the_shape() -> None:
    """Between the two extremes: some codes fit, so some codes are published.

    Shedding is from the BACK at group and code boundaries, so what survives is
    the leading groups -- the ones a reader diagnoses from -- and the prior
    fields are never the thing that gives way.
    """
    shape = _shape_filling(_room_for_one_shape() - 20, groups=8)
    reason = _published_reason(shape)

    # The floor opens the fragment and the codes follow it, so every prior field
    # is published verbatim and only the codes were spent.
    assert f"[{_codes_free(shape)} {runner_module._ROWSENT_CODES_FIELD}" in reason, reason
    published = _shape_field(reason.split("[")[-1].rstrip("]"), "rowsentcodes")
    assert published, reason
    # Trimmed, not whole: the full field could not have fitted in the 20 spare
    # characters, 14 of which the field NAME alone spends.
    assert len(published) < len(_shape_field(shape, "rowsentcodes")), reason
    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS


def test_the_ordinary_shape_still_publishes_its_codes() -> None:
    """(b) The normal case is untouched: a real refusal carries its codes.

    Built through the REAL binder rather than a hand-written string, so this
    also pins that an everyday shape is nowhere near the budget and the fitter
    costs it nothing.
    """
    shape = _shape_of(
        _ROWSENT_REFUSED_SENTENCE, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING
    )
    reason = _published_reason(shape)

    assert f"[{shape}]" in reason, reason
    assert _shape_field(shape, "rowsentcodes") == str(
        runner_module._ROWSENT_REFUSAL_CODES["phrase-recombination"]
    ), shape
    assert 0 < len(reason) <= runner_module._MAX_SAFE_REASON_CHARS


def test_every_rendering_the_fitter_offers_is_integers_and_separators_only() -> None:
    """(c) Shedding CUTS the value; it never rewrites it, and never adds text.

    Checked over every rendering of a real shape and of a pathological one, so a
    future trimmer that inserted an ellipsis or a "truncated" marker -- content
    in a frame that is content-free by contract (C12) -- reds here.
    """
    real = _shape_of(
        _ROWSENT_REFUSED_SENTENCE, {_PROTECTED_REF}, protected_output_a=_ROWSENT_RENDERING
    )
    wide = _shape_filling(_room_for_one_shape(), groups=12)
    for shape in (real, wide, _codes_free(real)):
        renderings = list(runner_module._shape_renderings(shape))

        assert renderings[0] == shape, renderings
        # THE invariant: whatever else happens, the codes-free shape is offered.
        assert renderings[-1] == _codes_free(shape), renderings
        # Strictly descending, so the fitter always terminates on the floor.
        assert [len(r) for r in renderings] == sorted((len(r) for r in renderings), reverse=True), (
            renderings
        )
        assert len(set(renderings)) == len(renderings), renderings
        for rendering in renderings:
            assert rendering.startswith(_codes_free(shape)), rendering
            for pair in rendering.split():
                name, value = pair.split("=", 1)
                assert re.fullmatch(r"none|[0-9,/-]*", value), (name, value)
