# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed, content-minimal wire models for principal-owned Conversations and Runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ne.api.agent_manifest import ExecutionStrategy
from ne.contracts.types import Lane


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationView(_WireModel):
    conversation_id: str
    agent_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    last_sequence: int


class ConversationPage(_WireModel):
    items: tuple[ConversationView, ...]
    next_cursor: str | None


class RunView(_WireModel):
    run_id: str
    conversation_id: str
    agent_id: str
    agent_version: int
    sequence: int
    version: int = Field(ge=1)
    predecessor_run_id: str | None
    root_run_id: str
    retry_ordinal: int = Field(ge=0)
    display_label: str
    strategy: Literal["agentic", "deterministic", "unavailable"]
    status: Literal[
        "submitted",
        "queued",
        "running",
        "waiting_approval",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
        "unavailable",
    ]
    output_verdict: Literal["verified", "grounded", "refused", "unavailable"] | None
    model_policy_id: str | None
    model_request_mode: Literal["auto", "explicit", "unavailable"]
    context_snapshot_hash: str | None
    requested_model: str | None
    requested_model_lane: Lane | None
    served_model: str | None
    #: WHY ``served_model`` is the model it is, which the pair above cannot say.
    #:
    #: ``requested_model`` and ``served_model`` side by side are two bare names
    #: with no stated relationship: a reader who asked for one alias and was
    #: answered by another could not tell "governance moved me" from "the
    #: provider failed and I was routed down the chain" from "these are two
    #: spellings of the same thing". The second is an INVOLUNTARY PROVIDER
    #: FALLBACK -- a real governance event, and a silent one until this field.
    #:
    #: * ``"primary"`` -- nothing was attempted before the model that served.
    #:   A POSITIVE claim, not the absence of a fallback: it is what lets the
    #:   console print "served as requested" from the record instead of reading
    #:   a null as reassurance.
    #: * ``"fallback"`` -- something was attempted first and did not serve.
    #:   ``fallback_from`` names it.
    #: * ``"unavailable"`` -- this Run recorded no route position (a step
    #:   written before the fact was captured). NOT "primary". The console
    #:   renders it as the absence it is.
    #:
    #: NOT a ``model.changed``. That event is a DELIBERATE policy change with an
    #: ``actor``; this is the provider chain moving under an unchanged policy,
    #: and a reader must be able to tell those apart. They are different events
    #: on different fields and neither is derived from the other.
    route_position: Literal["primary", "fallback", "unavailable"]
    #: The candidates passed over, comma-separated, in attempt order -- present
    #: IFF ``route_position`` is ``"fallback"``.
    #:
    #: DECLARED MODEL NAMES ONLY, enforced at the writer
    #: (``run_store._require_route_position``) and again here. Provider error
    #: text is NOT projected and is not what this carries: an error string can
    #: echo prompt substrings back, and whether provider-controlled text may
    #: ever reach an audit row is a separate governance decision that this field
    #: deliberately does not pre-empt.
    fallback_from: str | None
    output_preview: str | None
    config_hash: str
    capability_snapshot_hash: str
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


class RunPage(_WireModel):
    items: tuple[RunView, ...]
    next_cursor: str | None


class RunClaimView(_WireModel):
    """One safe claim shell; ``display`` exists only for exact source-free materialization."""

    claim_id: str
    claim_verdict: Literal["verified", "grounded", "unavailable"]
    content_state: Literal["available", "content_withheld", "content_unavailable"]
    display: str | None
    served_output_hash: str
    claim_content_hash: str
    association_status: Literal["associated", "association_unavailable"]
    association_hash: str | None
    evidence_id: str | None
    producing_event_id: str | None
    locator_status: Literal["located", "evidence_locator_unavailable", "association_unavailable"]
    locator_hash: str | None


class AgenticPlanStepView(_WireModel):
    """One closed, persisted model-selected step intent."""

    ordinal: int = Field(ge=0)
    tool: str = Field(min_length=1, max_length=256)
    step_intent: str = Field(min_length=1, max_length=256)
    #: The exact correlation id the Run's receipt gate minted for THIS step's
    #: governed Tool call -- the same value `run.tool.pre_effect` events carry
    #: in their `tool_call_id` metadata, which is what lets a reader join an
    #: event row to the plan row that names the tool and its intent. Recorded
    #: by the executor frame that minted it (never inferred from counters), so
    #: ``None`` means the binding was not recorded -- an honest unknown, never
    #: a guessed join.
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)


class DeterministicPlanStepView(_WireModel):
    """One exact step from a frozen deterministic execution-profile receipt."""

    ordinal: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: Literal["governed-tool", "deterministic-analysis", "model-judgment"]
    action: str = Field(min_length=1, max_length=255)
    model_role: str | None = Field(default=None, min_length=1, max_length=255)


class RunExecutionProfileReceiptView(_WireModel):
    """Frozen deterministic profile and owner-visible input for one exact Run attempt."""

    profile_receipt_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    workflow_id: str = Field(min_length=1, max_length=128)
    profile_version: int = Field(ge=1)
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_schema: dict[str, Any]
    input_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    typed_input_state: Literal["available", "unavailable"]
    typed_input: dict[str, Any] | None
    ordered_plan: tuple[DeterministicPlanStepView, ...]
    ordered_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class RunSkillPinView(_WireModel):
    """One exact content-addressed Skill pin activated for a Run attempt."""

    skill_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunSkillActivationReceiptView(_WireModel):
    """Content-free Skill activation truth frozen for one exact Run attempt."""

    activation_receipt_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    agent_version: int = Field(ge=1)
    surface_key: str = Field(min_length=1, max_length=768)
    pins: tuple[RunSkillPinView, ...]
    index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class RunDetailView(RunView):
    # WHY THE ANSWER HAS A STATE AND NOT JUST BYTES. `output_preview: str |
    # None` collapses three different facts into one blank: the Run produced
    # nothing, the Run's answer exists but current source authority no longer
    # authorizes this reader, and the console could not resolve it at all. A
    # withheld answer rendered as an empty turn tells the reader the system
    # failed when what actually happened is that it worked.
    #
    # Detail-only ON PURPOSE. Resolving it for a source-derived answer costs one
    # reauthorization per Run, which a 25-row list page must not spend.
    output_state: Literal["available", "withheld", "unavailable"] = "unavailable"
    approval_id: str | None = Field(default=None, min_length=1, max_length=256)
    # WHY A FAILED RUN MAY SAY WHY, AND WHAT IT MAY NOT SAY.
    #
    # A terminal non-completed Run used to carry only `failure_class`, which is
    # an OpaqueId: a Run that died because a lane had no current serving
    # evidence was indistinguishable, from the record, from any other exception
    # class. The reader was then shown the console's own generic timeout line
    # over what was actually a stated governance refusal.
    #
    # This carries the safe reason the RUNTIME published and nothing else.
    # NE-SELECT-REPROBE established `safe_reason` as a static, non-customer
    # string a raiser marks safe at the raise site, checkpointed as
    # `error_reason`; an arbitrary exception message can never become one, and
    # `error_class` is deliberately NOT projected here.
    #
    # A FAILED Run always publishes one. An unmarked failure used to leave this
    # `None`, which reached the reader as an alert containing nothing at all;
    # the runtime now writes a derived floor naming the failure class (see
    # `ne.runtime.run_finalizer.derived_failure_reason`). This layer still
    # invents nothing: `None` remains the answer for a terminal that is not a
    # failure, and for a record it cannot trust.
    failure_reason: str | None = Field(default=None, min_length=1, max_length=256)
    agentic_plan: tuple[AgenticPlanStepView, ...] | None = None
    execution_profile_receipt: RunExecutionProfileReceiptView | None = None
    skill_activation_receipt: RunSkillActivationReceiptView | None = None
    claims: tuple[RunClaimView, ...]
    claim_mapping_state: Literal["mapping_pending", "complete", "partial", "unavailable"]
    claims_truncated: bool


class SafeEventMetadata(_WireModel):
    """A server-enumerated scalar, never an arbitrary persisted detail mapping."""

    key: Literal[
        "claim_count",
        #: POST_DEMO item 4: HOW a completed run reached its terminal —
        #: `execution_complete` for a run that finished its plan,
        #: `approved_effect_complete` for one that resumed and executed the approved
        #: effect without replaying the rest. A closed, server-authored vocabulary.
        "outcome",
        "actor",
        "safe_boundary",
        "decision",
        "delivery_kind",
        "model_policy_id",
        "request_mode",
        "requested_model",
        "served_model",
        "served_lane",
        # THE CAUSE, on the event that reports the effect. `model.served` named
        # the model and never why it was that model; the routing fact existed
        # only on the trace wire. Names and positions -- never the provider's
        # error text.
        "route_position",
        "fallback_from",
        "authorization_receipt_hash",
        "before_requested_model",
        "before_served_model",
        "after_requested_model",
        "after_authorized_model",
        "after_authorized_lane",
        "context_snapshot_hash",
        "context_fit",
        "estimated_input_tokens",
        "target_input_budget",
        "compaction",
        "cache_impact",
        "before_config_hash",
        "config_hash",
        "protected_hash",
        "stamp_set_hash",
        "terminal_intent_hash",
        "turn_id",
        # The Run-to-audit join: the readable action NAME already committed to
        # the governed chain, the server's decision class, and the tool call's
        # correlation id. Names and codes only -- never an argument or a result.
        "audit_action",
        "decision_kind",
        "tool_call_id",
        # The NAME of the governed act, from the chain's own pre-effect record:
        # the tool a `run.tool.pre_effect` receipt named in its resource. A tool
        # name is a registered identifier, never an argument or a result.
        "tool_name",
        # WHAT A RETRIEVAL PRODUCED, as class and count only: the read's kind,
        # its locator status, its assurance class, and how many contributor
        # stamps supported it. Scalar facts ABOUT the retrieval -- the retrieved
        # text itself never has a key here.
        "read_kind",
        "evidence_status",
        "evidence_assurance",
        "contributor_count",
    ]
    value: str | int | bool


class RunEventView(_WireModel):
    event_id: str
    run_id: str
    attempt_id: str
    sequence: int
    event_kind: Literal[
        "claim.verifier",
        "delivery.decided",
        "evidence.produced",
        "evidence.verified_row",
        "model.changed",
        "model.served",
        "run.audit.intent",
        "run.cancel.accepted",
        "run.execution.claimed",
        "run.execution.recovered",
        "run.retry.accepted",
        "run.start.accepted",
        "run.terminal",
        "event.unavailable",
    ]
    event_status: Literal[
        "preterminal",
        "changed",
        "served",
        "withheld",
        "submitted",
        "queued",
        "running",
        "waiting_approval",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
        "receipt_pending",
        "retry",
        "uncertain",
        "verified",
        "unavailable",
    ]
    parent_event_id: str | None
    step_id: str | None
    metadata: tuple[SafeEventMetadata, ...]
    detail_hash: str
    created_at: datetime


class RunEventPage(_WireModel):
    items: tuple[RunEventView, ...]
    next_cursor: str | None


class StartRunRequest(_WireModel):
    """Exact current identities required to accept a new canonical Run."""

    conversation_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    agent_version: int = Field(ge=1)
    strategy: ExecutionStrategy
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: str = Field(min_length=1, max_length=200_000)
    requested_model: str | None = Field(default=None, min_length=1, max_length=256)
    requested_model_lane: Lane | None = None


class ControlRunRequest(_WireModel):
    """Optimistic and frozen identities for cancel/retry admission."""

    expected_version: int = Field(ge=1)
    agent_version: int = Field(ge=1)
    strategy: ExecutionStrategy
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AcceptedRunSubmissionView(_WireModel):
    """Content-minimal projection of one committed request/outbox transaction."""

    action: Literal["start", "cancel", "retry"]
    run_id: str
    attempt_id: str
    version: int = Field(ge=1)
    status: Literal["submitted", "queued", "cancelling"]
    enqueue_id: str
    replayed: bool
