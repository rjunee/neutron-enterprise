# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Dormant canonical Agent Run, evidence, claim, and private-payload store.

PRE-009A owns resources and transactions only.  This module deliberately has no
API projection, worker execution, source-authority evaluation, or legacy transport
activation.  Later composition calls :func:`ensure_run_schema` explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, ClassVar, Final, Literal, cast

import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from ne.audit.engine import call_db, default_offload, ensure_transactional_sqlite
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    EvidenceAssurance,
    ProtectedSourceOutputRef,
    SourceAuthorityStamp,
    canonical_source_authority_stamps,
)
from ne.runtime.model_continuity import (
    CONTINUITY_PROMPT_RESERVE_TOKENS,
    context_component_id,
    context_snapshot_hash,
    ne_agent_model_boundary_receipts,
    ne_agent_run_model_steps,
)
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CONVERSATION_TURN_TARGET_KIND,
    SESSION_MESSAGE_TARGET_KIND,
    TUPLED_TARGET_KINDS,
    CanonicalTargetTuple,
    KeyEnvelopeError,
    PrivatePayloadDomain,
    PrivatePayloadEnvelope,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    conversation_turn_content_binding,
    is_claimable_target_kind,
    protected_payload_aad_binding,
    protected_target_hash,
    session_message_content_binding,
)
from ne.runtime.store import (
    metadata,
    ne_agent_conversation_turns,
    ne_agent_session_messages,
)

_CLAIM_VERIFIER_RECEIPT_NAMESPACE = uuid.UUID("ff1b8d21-3e35-4dd4-a408-86bd3b3bbfd5")

__all__ = [
    "CONVERSATION_TURN_TARGET_KIND",
    "SESSION_MESSAGE_TARGET_KIND",
    "AcceptedRunAdmission",
    "AgentRun",
    "AgentRunStore",
    "ClaimDeclaration",
    "ClaimEvidenceDeclaration",
    "ClaimVerifierDeclaration",
    "ClaimVerifierRecord",
    "EvidenceContributorDeclaration",
    "EvidenceDeclaration",
    "EvidenceProjectionUnavailable",
    "EvidenceRecord",
    "EvidenceRowDeclaration",
    "EvidenceSetDeclaration",
    "EvidenceSetRecord",
    "ProtectedPayloadRef",
    "ProtectedTurnUnavailable",
    "RunAttemptLink",
    "RunAuditDecisionKind",
    "RunAuditReceipt",
    "RunClaimEvidenceObservation",
    "RunEvent",
    "RunEventPage",
    "RunExecutionProfileReceipt",
    "RunOutputVerdict",
    "RunPage",
    "RunSkillActivationReceipt",
    "RunState",
    "RunSubmissionConflictError",
    "RunSubmissionNotFoundError",
    "StoredSourcePayload",
    "conversation_turn_content_binding",
    "ensure_run_schema",
    "ne_agent_run_authenticated_contexts",
    "ne_agent_run_execution_profiles",
    "ne_agent_run_lifecycle_outbox",
    "protected_target_hash",
    "session_message_content_binding",
]

_POSTGRES_RUN_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(56453543500901)"
_SQLITE_RUN_SCHEMA_LOCK = threading.Lock()


class RunState(StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CONTEXT_EXHAUSTED = "context_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"


class RunOutputVerdict(StrEnum):
    VERIFIED = "verified"
    GROUNDED = "grounded"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


class RunAuditDecisionKind(StrEnum):
    SUBMISSION = "submission"
    STATE_TRANSITION = "state_transition"
    EVIDENCE = "evidence"
    DELIVERY = "delivery"
    APPROVAL = "approval"
    TERMINAL = "terminal"
    CANCEL = "cancel"
    RETRY = "retry"


_TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
        RunState.CONTEXT_EXHAUSTED,
        RunState.BUDGET_EXHAUSTED,
    }
)
_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.SUBMITTED: frozenset({RunState.QUEUED, RunState.CANCELLING, RunState.FAILED}),
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED}),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING_APPROVAL,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLING,
            RunState.EXPIRED,
            RunState.CONTEXT_EXHAUSTED,
            RunState.BUDGET_EXHAUSTED,
        }
    ),
    RunState.WAITING_APPROVAL: frozenset(
        {
            RunState.RUNNING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLING,
            RunState.EXPIRED,
            RunState.CONTEXT_EXHAUSTED,
            RunState.BUDGET_EXHAUSTED,
        }
    ),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
    **{state: frozenset() for state in _TERMINAL_STATES},
}


def _id() -> str:
    return str(uuid.uuid4())


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(name: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase sha256")


def _require_nonempty(**values: str) -> None:
    for name, value in values.items():
        if not value or not value.strip():
            raise ValueError(f"{name} must be non-empty")


#: The COMPLETE alphabet a passed-over model name may be spelled with, plus the
#: separator. Deliberately not a regex over "anything printable": this is the
#: enforcement point for "declared model names only", and it has to be readable
#: as such. Model names in this deployment are lane-qualified slugs
#: (``frontier-sonnet``, ``private-qwen``, ``gpt-oss-120b``).
_MODEL_NAME_ALPHABET: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:_-"
)
_MAX_FALLBACK_FROM: Final[int] = 256


def _require_route_position(route_position: str, fallback_from: str | None) -> None:
    """Admit only a well-formed routing pair, and only DECLARED MODEL NAMES.

    C12 -- THIS IS THE BOUNDARY PROVIDER TEXT MUST NOT CROSS. ``fallback_from``
    is projected all the way onto the run wire and rendered by the console, so
    it is the one routing field an attacker-influenced string could ride. A
    provider's error text can echo prompt substrings back (design 10.3 rule 5)
    and a candidate's ``api_base`` can carry a credential. Neither is a model
    name, and neither survives this check: every character must come from
    :data:`_MODEL_NAME_ALPHABET`, so a space, a quote, a slash or a newline --
    the shape of every error message -- is rejected at the writer rather than
    sanitized somewhere downstream.

    The position and the names are ONE fact and are validated as one: names
    exist exactly when the position is ``"fallback"``. An empty
    ``fallback_from`` is not "no names", it is a malformed claim, and a
    ``"primary"`` row carrying names contradicts itself.
    """
    if route_position not in {"primary", "fallback"}:
        raise ValueError("model step route position is invalid")
    if (route_position == "fallback") != (fallback_from is not None):
        raise ValueError("model step route position and fallback names disagree")
    if fallback_from is None:
        return
    if not fallback_from or len(fallback_from) > _MAX_FALLBACK_FROM:
        raise ValueError("model step fallback names are invalid")
    names = fallback_from.split(",")
    if any(
        not name or any(character not in _MODEL_NAME_ALPHABET for character in name)
        for name in names
    ):
        raise ValueError("model step fallback names must be declared model names")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _keyed_hash(schema: str, fields: Mapping[str, object]) -> str:
    """Hash one COMPLETE, self-describing field map.

    Field names travel with their values, so dropping a member from an intent map
    changes the hash for every caller rather than silently colliding two intents
    whose remaining values happen to line up positionally.
    """
    return _hash(_canonical_json({"schema": schema, "fields": dict(fields)}).encode("utf-8"))


class _ClaimRaceLost(Exception):
    """A speculative claim lost its arbitration; its whole transaction must roll back."""


class RunSubmissionNotFoundError(RuntimeError):
    """The addressed Run is absent or not owned by the submitting principal."""


class RunSubmissionConflictError(RuntimeError):
    """An idempotency key or optimistic Run version conflicts."""


@dataclass(frozen=True, slots=True)
class AcceptedRunAdmission:
    action: Literal["start", "cancel", "retry"]
    run_id: str
    attempt_id: str
    version: int
    status: str
    enqueue_id: str
    replayed: bool = False


def _insert_ignore(table: sa.Table, conn: sa.Connection, *, index_elements: Sequence[str]) -> Any:
    """Return a dialect-specific ``INSERT ... ON CONFLICT DO NOTHING`` for ``table``.

    Signature and dialect contract are deliberately identical to
    ``ne.runtime.store._insert_ignore`` — two spellings of one mechanism in one
    package is how a caller ends up passing the wrong shape to the wrong module.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    if dialect == "sqlite":
        return sqlite.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    raise NotImplementedError("ne-runtime supports sqlite and postgresql")


def _envelope_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("cipher_version", sa.Integer, nullable=False),
        sa.Column("plaintext_hash", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String, nullable=False),
        sa.Column("payload_nonce", sa.String, nullable=False),
        sa.Column("payload_ciphertext", sa.Text, nullable=False),
        sa.Column("key_nonce", sa.String, nullable=False),
        sa.Column("encrypted_dek", sa.Text, nullable=False),
    ]


ne_agent_runs = sa.Table(
    "ne_agent_runs",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("conversation_id", sa.String, nullable=False, index=True),
    sa.Column("owner_principal_id", sa.String, nullable=False, index=True),
    sa.Column("department", sa.String, nullable=False),
    sa.Column("conversation_agent_name", sa.String, nullable=False),
    sa.Column("agent_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("strategy", sa.String, nullable=False),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("capability_snapshot_hash", sa.String(64), nullable=False),
    sa.Column("capability_snapshot_receipt_id", sa.String, nullable=False),
    sa.Column("agent_definition_hash", sa.String(64), nullable=True),
    sa.Column("model_policy_id", sa.String, nullable=True),
    sa.Column("authorized_model", sa.String, nullable=True),
    sa.Column("authorized_model_lane", sa.String, nullable=True),
    sa.Column("model_authorization_receipt_hash", sa.String(64), nullable=True),
    sa.Column("context_snapshot_hash", sa.String(64), nullable=True),
    sa.Column("requested_model", sa.String, nullable=True),
    sa.Column("requested_model_lane", sa.String, nullable=True),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("display_label", sa.String, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("output_verdict", sa.String, nullable=True),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("conversation_id", "sequence", name="uq_agent_run_conversation_sequence"),
    sa.UniqueConstraint("run_id", "conversation_id", name="uq_agent_run_conversation_identity"),
    sa.UniqueConstraint(
        "run_id", "owner_principal_id", "conversation_id", name="uq_agent_run_owner_scope"
    ),
    # Referencable owner identity: a receipt row can carry a real composite FK to
    # (run_id, owner_principal_id) instead of relying on a caller convention.
    sa.UniqueConstraint("run_id", "owner_principal_id", name="uq_agent_run_owner_identity"),
    sa.ForeignKeyConstraint(
        ["conversation_id", "owner_principal_id", "department", "conversation_agent_name"],
        [
            "ne_agent_conversations.conversation_id",
            "ne_agent_conversations.owner_principal_id",
            "ne_agent_conversations.department",
            "ne_agent_conversations.agent_name",
        ],
        name="fk_agent_run_conversation_owner_scope",
    ),
    sa.CheckConstraint(
        "status IN ('submitted','queued','running','waiting_approval','cancelling','completed',"
        "'failed','cancelled','expired','context_exhausted','budget_exhausted')",
        name="ck_agent_run_status",
    ),
    sa.CheckConstraint(
        "output_verdict IS NULL OR ("
        "status IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') AND "
        "output_verdict IN ('verified','grounded','refused','unavailable') AND "
        "(output_verdict = 'unavailable' OR status = 'completed'))",
        name="ck_agent_run_output_verdict",
    ),
    sa.CheckConstraint("agent_version >= 1", name="ck_agent_run_agent_version"),
    sa.CheckConstraint("sequence >= 1", name="ck_agent_run_sequence"),
    sa.CheckConstraint("length(config_hash) = 64", name="ck_agent_run_config_hash"),
    sa.CheckConstraint(
        "length(capability_snapshot_hash) = 64", name="ck_agent_run_capability_hash"
    ),
    sa.CheckConstraint(
        "length(trim(capability_snapshot_receipt_id)) > 0",
        name="ck_agent_run_capability_receipt_nonempty",
    ),
    sa.CheckConstraint(
        "(requested_model IS NULL) = (requested_model_lane IS NULL)",
        name="ck_agent_run_requested_model_pair",
    ),
    sa.CheckConstraint(
        "requested_model_lane IS NULL OR requested_model_lane IN ('frontier-api','private-vllm')",
        name="ck_agent_run_requested_model_lane",
    ),
    sa.CheckConstraint(
        "(status IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') "
        "AND terminal_at IS NOT NULL) OR "
        "(status NOT IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') "
        "AND terminal_at IS NULL)",
        name="ck_agent_run_terminal_time",
    ),
)

ne_agent_run_submissions = sa.Table(
    "ne_agent_run_submissions",
    metadata,
    sa.Column("submission_id", sa.String, primary_key=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("idempotency_key", sa.String, nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("run_version", sa.Integer, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("enqueue_id", sa.String, nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "owner_principal_id", "action", "idempotency_key", name="uq_run_submission_key"
    ),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_submission_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_submission_attempt",
    ),
)

ne_agent_run_enqueue_outbox = sa.Table(
    "ne_agent_run_enqueue_outbox",
    metadata,
    sa.Column("enqueue_id", sa.String, primary_key=True),
    sa.Column("action", sa.String, nullable=False),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("run_version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_enqueue_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_enqueue_attempt",
    ),
)

ne_agent_run_inputs = sa.Table(
    "ne_agent_run_inputs",
    metadata,
    sa.Column("input_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    *_envelope_columns(),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_run_input_run"),
    sa.CheckConstraint("length(plaintext_hash) = 64", name="ck_run_input_plaintext_hash"),
)

ne_agent_run_attempt_links = sa.Table(
    "ne_agent_run_attempt_links",
    metadata,
    sa.Column("attempt_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("conversation_id", sa.String, nullable=False),
    sa.Column("predecessor_attempt_id", sa.String, nullable=True),
    sa.Column("attempt_number", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "attempt_number", name="uq_run_attempt_number"),
    sa.UniqueConstraint("attempt_id", "run_id", name="uq_run_attempt_identity"),
    sa.ForeignKeyConstraint(
        ["run_id", "conversation_id"],
        ["ne_agent_runs.run_id", "ne_agent_runs.conversation_id"],
        name="fk_run_attempt_run_conversation",
    ),
    sa.ForeignKeyConstraint(
        ["predecessor_attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_attempt_predecessor",
    ),
    sa.CheckConstraint("attempt_number >= 1", name="ck_run_attempt_number"),
)

ne_agent_run_skill_activations = sa.Table(
    "ne_agent_run_skill_activations",
    metadata,
    sa.Column("activation_receipt_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("agent_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("surface_key", sa.String, nullable=False),
    # Canonical metadata only: Skill id/version/content hash triples. No index,
    # procedure, prompt, model output, Tool result, or customer content is stored.
    sa.Column("pins_json", sa.Text, nullable=False),
    sa.Column("index_hash", sa.String(64), nullable=False),
    sa.Column("surface_hash", sa.String(64), nullable=False),
    sa.Column("receipt_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "attempt_id", name="uq_run_skill_activation_attempt"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_skill_activation_attempt",
    ),
    sa.CheckConstraint("agent_version >= 1", name="ck_run_skill_activation_agent_version"),
    sa.CheckConstraint("length(index_hash) = 64", name="ck_run_skill_activation_index_hash"),
    sa.CheckConstraint("length(surface_hash) = 64", name="ck_run_skill_activation_surface_hash"),
    sa.CheckConstraint("length(receipt_hash) = 64", name="ck_run_skill_activation_receipt_hash"),
)

# The verifier-authored delayed-work identity snapshot.  This is deliberately a
# separate append-only receipt rather than a reconstruction from the Run owner:
# Principal profile claims and bearer material are structurally absent, while
# issuer, verified expiry and the exact authorization groups remain hash-bound.
ne_agent_run_authenticated_contexts = sa.Table(
    "ne_agent_run_authenticated_contexts",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("authorization_groups_json", sa.Text, nullable=False),
    sa.Column("department", sa.String, nullable=True),
    sa.Column("issuer", sa.String, nullable=False),
    sa.Column("expires_at", sa.BigInteger, nullable=False),
    sa.Column("context_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["run_id", "owner_principal_id"],
        ["ne_agent_runs.run_id", "ne_agent_runs.owner_principal_id"],
        name="fk_run_authenticated_context_owner",
    ),
    sa.CheckConstraint("schema_version = 1", name="ck_run_authenticated_context_version"),
    sa.CheckConstraint("expires_at >= 0", name="ck_run_authenticated_context_expiry"),
    sa.CheckConstraint("length(context_hash) = 64", name="ck_run_authenticated_context_hash"),
)

# Exact deterministic profile truth observed by the worker before it validates
# or executes the attempt.  The JSON fields are metadata-only schema/plan data;
# neither the private Run input nor any source-derived value is persisted here.
ne_agent_run_execution_profiles = sa.Table(
    "ne_agent_run_execution_profiles",
    metadata,
    sa.Column("profile_receipt_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("workflow_id", sa.String, nullable=False),
    sa.Column("profile_version", sa.Integer, nullable=False),
    sa.Column("profile_hash", sa.String(64), nullable=False),
    sa.Column("input_schema_json", sa.Text, nullable=False),
    sa.Column("input_schema_hash", sa.String(64), nullable=False),
    sa.Column("ordered_plan_json", sa.Text, nullable=False),
    sa.Column("ordered_plan_hash", sa.String(64), nullable=False),
    sa.Column("receipt_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_execution_profile_attempt",
    ),
    sa.CheckConstraint("profile_version >= 1", name="ck_run_execution_profile_version"),
    sa.CheckConstraint("length(profile_hash) = 64", name="ck_run_execution_profile_hash"),
    sa.CheckConstraint(
        "length(input_schema_hash) = 64", name="ck_run_execution_profile_schema_hash"
    ),
    sa.CheckConstraint("length(ordered_plan_hash) = 64", name="ck_run_execution_profile_plan_hash"),
    sa.CheckConstraint("length(receipt_hash) = 64", name="ck_run_execution_profile_receipt"),
)

# PRE-010G is the sole author of this row when it atomically attaches a user turn
# to an admitted Run.  010F has no insert API for the table: it can only load the
# exact tuple and reject a workflow request that supplies anything else.
ne_agent_run_turn_targets = sa.Table(
    "ne_agent_run_turn_targets",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("conversation_id", sa.String, nullable=False),
    sa.Column("turn_id", sa.String, nullable=False, unique=True),
    sa.Column("turn_sequence", sa.Integer, nullable=False),
    sa.Column("attachment_id", sa.String, nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["run_id", "conversation_id"],
        ["ne_agent_runs.run_id", "ne_agent_runs.conversation_id"],
        name="fk_run_turn_target_run",
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_turn_target_attempt",
    ),
    sa.UniqueConstraint("conversation_id", "turn_sequence", name="uq_run_turn_target_sequence"),
    sa.CheckConstraint("turn_sequence >= 1", name="ck_run_turn_target_sequence"),
)

# One database-owned generation fence.  Production activation changes this row
# in the same database used by both API and worker; legacy attachment INSERTs are
# rejected by database triggers, so a second process cannot race an in-memory pin.
ne_agent_default_ask_activation = sa.Table(
    "ne_agent_default_ask_activation",
    metadata,
    sa.Column("singleton", sa.Integer, primary_key=True),
    sa.Column("wire_generation", sa.String, nullable=False),
    sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("singleton = 1", name="ck_default_ask_activation_singleton"),
)

ne_agent_run_retry_lineage = sa.Table(
    "ne_agent_run_retry_lineage",
    metadata,
    sa.Column("retry_id", sa.String, primary_key=True),
    sa.Column("predecessor_run_id", sa.String, nullable=False, unique=True),
    sa.Column("predecessor_attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("successor_run_id", sa.String, nullable=False, unique=True),
    sa.Column("successor_attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("root_run_id", sa.String, nullable=False),
    sa.Column("retry_ordinal", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("root_run_id", "retry_ordinal", name="uq_run_retry_root_ordinal"),
    sa.ForeignKeyConstraint(
        ["predecessor_attempt_id", "predecessor_run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_retry_predecessor",
    ),
    sa.ForeignKeyConstraint(
        ["successor_attempt_id", "successor_run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_retry_successor",
    ),
    sa.ForeignKeyConstraint(["root_run_id"], ["ne_agent_runs.run_id"], name="fk_run_retry_root"),
    sa.CheckConstraint("retry_ordinal >= 1", name="ck_run_retry_ordinal"),
)

ne_agent_run_events = sa.Table(
    "ne_agent_run_events",
    metadata,
    sa.Column("event_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("event_kind", sa.String, nullable=False),
    sa.Column("event_status", sa.String, nullable=False),
    sa.Column("parent_event_id", sa.String, nullable=True),
    sa.Column("step_id", sa.String, nullable=True),
    sa.Column("safe_detail", sa.Text, nullable=False),
    sa.Column("detail_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    sa.UniqueConstraint("event_id", "run_id", name="uq_run_event_identity"),
    sa.UniqueConstraint("event_id", "run_id", "attempt_id", name="uq_run_event_attempt_identity"),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_run_event_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_event_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["parent_event_id"], ["ne_agent_run_events.event_id"], name="fk_run_event_parent"
    ),
    sa.CheckConstraint("sequence >= 1", name="ck_run_event_sequence"),
    sa.CheckConstraint("length(detail_hash) = 64", name="ck_run_event_detail_hash"),
)

# A canonical terminal change and its audit-publication intent are committed in
# one transaction.  Publication may be retried asynchronously; the unique Run
# identity prevents either a second terminal event or a changed terminal intent.
ne_agent_run_lifecycle_outbox = sa.Table(
    "ne_agent_run_lifecycle_outbox",
    metadata,
    sa.Column("publication_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("event_id", sa.String, nullable=False, unique=True),
    sa.Column("terminal_status", sa.String, nullable=False),
    sa.Column("output_verdict", sa.String, nullable=False),
    sa.Column("outcome", sa.String, nullable=False),
    sa.Column("terminal_intent_hash", sa.String(64), nullable=False),
    # 010F's content-free terminal binding.  These columns remain nullable for
    # pre-010F lifecycle rows, but the finalizer always supplies the complete set.
    sa.Column("target_hash", sa.String(64), nullable=True),
    sa.Column("protected_output_kind", sa.String, nullable=True),
    sa.Column("protected_output_ref", sa.String, nullable=True),
    sa.Column("protected_output_hash", sa.String(64), nullable=True),
    sa.Column("mapping_state", sa.String, nullable=True),
    sa.Column("envelope_hash", sa.String(64), nullable=True),
    sa.Column("delivery_set_id", sa.String, nullable=True),
    sa.Column("delivery_set_hash", sa.String(64), nullable=True),
    sa.Column("audit_receipt_set_hash", sa.String(64), nullable=True),
    sa.Column("approval_set_hash", sa.String(64), nullable=True),
    sa.Column("evidence_set_hash", sa.String(64), nullable=True),
    sa.Column("verifier_set_hash", sa.String(64), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("audit_event_id", sa.String, nullable=True),
    sa.UniqueConstraint("run_id", "attempt_id", name="uq_run_lifecycle_terminal_attempt"),
    sa.ForeignKeyConstraint(
        ["event_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_events.event_id",
            "ne_agent_run_events.run_id",
            "ne_agent_run_events.attempt_id",
        ],
        name="fk_run_lifecycle_terminal_event",
    ),
    sa.ForeignKeyConstraint(
        ["audit_event_id"],
        ["ne_agent_run_audit_receipts.audit_event_id"],
        name="fk_run_lifecycle_terminal_audit_receipt",
    ),
    sa.CheckConstraint(
        "terminal_status IN ('completed','failed','cancelled','expired',"
        "'context_exhausted','budget_exhausted')",
        name="ck_run_lifecycle_terminal_status",
    ),
    sa.CheckConstraint(
        "output_verdict IN ('verified','grounded','refused','unavailable')",
        name="ck_run_lifecycle_output_verdict",
    ),
    sa.CheckConstraint(
        "output_verdict = 'unavailable' OR terminal_status = 'completed'",
        name="ck_run_lifecycle_verdict_shape",
    ),
    sa.CheckConstraint(
        "(terminal_status = 'completed' AND ("
        "(output_verdict = 'refused' AND outcome = 'refused') OR "
        # POST_DEMO item 4: a COMPLETED run that got there by RESUMING an approved
        # effect says so. Both words mean "completed, not refused"; they differ in
        # HOW, which the run record could not previously express at all. The pair
        # stays closed — this admits one more exact value, never a free string.
        "(output_verdict != 'refused' AND outcome IN "
        "('execution_complete','approved_effect_complete')))) OR "
        "(terminal_status = 'failed' AND outcome = 'failed') OR "
        "(terminal_status = 'cancelled' AND outcome = 'cancelled') OR "
        "(terminal_status = 'expired' AND outcome = 'expired') OR "
        "(terminal_status = 'context_exhausted' AND outcome = 'context_exhausted') OR "
        "(terminal_status = 'budget_exhausted' AND outcome = 'budget_exhausted')",
        name="ck_run_lifecycle_outcome_shape",
    ),
    sa.CheckConstraint(
        "(published_at IS NULL AND audit_event_id IS NULL) OR "
        "(published_at IS NOT NULL AND audit_event_id IS NOT NULL)",
        name="ck_run_lifecycle_publication_shape",
    ),
    sa.CheckConstraint(
        "length(terminal_intent_hash) = 64",
        name="ck_run_lifecycle_terminal_intent_hash",
    ),
)

# Per-Run next-event-sequence counter.  Exactly the ``ne_agent_session_seq``
# mechanism NE-070H already proved: the next sequence is CLAIMED with an atomic
# ``UPDATE last_sequence = last_sequence + 1`` under the counter row lock, never
# chosen by reading ``max(sequence)`` and adding one.
ne_agent_run_event_seq = sa.Table(
    "ne_agent_run_event_seq",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("last_sequence", sa.Integer, nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_run_event_seq_run"),
    sa.CheckConstraint("last_sequence >= 0", name="ck_run_event_seq_nonnegative"),
)

ne_agent_evidence_records = sa.Table(
    "ne_agent_evidence_records",
    metadata,
    sa.Column("evidence_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("turn_id", sa.String, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("step_id", sa.String, nullable=False),
    sa.Column("tool_call_id", sa.String, nullable=False),
    sa.Column("read_ordinal", sa.Integer, nullable=False),
    sa.Column("invocation_key", sa.String(64), nullable=False, unique=True),
    # The COMPLETE canonical intent of the claim that won ``invocation_key``.
    # ``invocation_key`` only ADDRESSES the read; this hash binds every declared
    # field, so a replay carrying different content under the same address is
    # discriminated instead of silently answered with the winner's record.
    sa.Column("intent_hash", sa.String(64), nullable=False),
    sa.Column("locator_status", sa.String, nullable=False),
    sa.Column("locator_id", sa.String, nullable=True, unique=True),
    sa.Column("pending_result_id", sa.String, nullable=False, index=True),
    sa.Column("producing_event_id", sa.String, nullable=False, unique=True),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_evidence_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_evidence_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["producing_event_id", "run_id"],
        ["ne_agent_run_events.event_id", "ne_agent_run_events.run_id"],
        name="fk_evidence_event",
    ),
    sa.ForeignKeyConstraint(
        ["locator_id"], ["ne_agent_evidence_locators.locator_id"], name="fk_evidence_locator"
    ),
    sa.ForeignKeyConstraint(
        ["pending_result_id"],
        ["ne_agent_pending_source_tool_results.pending_result_id"],
        name="fk_evidence_pending_result",
    ),
    sa.CheckConstraint(
        "locator_status IN ('located','evidence_locator_unavailable')",
        name="ck_evidence_locator_status",
    ),
    sa.CheckConstraint(
        "(locator_status = 'located' AND locator_id IS NOT NULL) OR "
        "(locator_status = 'evidence_locator_unavailable' AND locator_id IS NULL)",
        name="ck_evidence_locator_conditional",
    ),
    sa.CheckConstraint("read_ordinal >= 0", name="ck_evidence_read_ordinal"),
    sa.CheckConstraint("length(intent_hash) = 64", name="ck_evidence_intent_hash"),
    sa.UniqueConstraint("evidence_id", "run_id", name="uq_evidence_run_identity"),
)

ne_agent_evidence_locators = sa.Table(
    "ne_agent_evidence_locators",
    metadata,
    sa.Column("locator_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("locator_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    *_envelope_columns(),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_locator_run"),
)


#: Content-free canonical target components persisted beside every TUPLED row.
#: ``run_id`` is already a column, so it is not repeated here.
_TARGET_TUPLE_COLUMNS = (
    "conversation_id",
    "turn_id",
    "turn_sequence",
    "attachment_id",
    "attempt_id",
)

_TUPLED_KIND_SQL = ", ".join(f"'{kind}'" for kind in sorted(TUPLED_TARGET_KINDS))


def _target_tuple_discriminator_sql() -> str:
    """SQL for 'a TUPLED row carries the COMPLETE tuple; every other row carries none'.

    Derived from ``TUPLED_TARGET_KINDS`` in the leaf rather than restated, so the
    database and the claim path cannot disagree about which kinds are addressable.
    Exhaustive in both directions: a tupled kind may not omit a component, and a
    non-tupled kind may not smuggle one in.
    """
    present = " AND ".join(f"{c} IS NOT NULL" for c in ("run_id", *_TARGET_TUPLE_COLUMNS))
    absent = " AND ".join(f"{c} IS NULL" for c in _TARGET_TUPLE_COLUMNS)
    return (
        f"(target_kind IN ({_TUPLED_KIND_SQL}) AND {present})"
        f" OR (target_kind NOT IN ({_TUPLED_KIND_SQL}) AND {absent})"
    )


_CONTENT_STATUS_SQL = ", ".join(
    f"'{status.value}'" for status in sorted(ProtectedContentStatus, key=lambda s: s.value)
)
_SOURCE_BEARING_SQL = f"'{ProtectedContentStatus.SOURCE_BOUND_CONTAINER.value}'"


def _content_status_shape_sql(*, required: bool) -> str:
    """SQL for the legal content_status <-> source-stamp relationship.

    Only a source-bound container may carry an authority stamp set; a row calling
    itself source-free or legacy-unavailable may not. Without this a source-bearing
    payload could be persisted under a source-free status and later materialized as
    a source-free slice -- which is the disclosure the status exists to prevent.
    """
    if not required:
        return "content_status IS NULL"
    bearing = f"(content_status = {_SOURCE_BEARING_SQL} AND stamp_set_hash IS NOT NULL)"
    free = f"(content_status <> {_SOURCE_BEARING_SQL} AND stamp_set_hash IS NULL)"
    # `content_status IS NOT NULL` FIRST, and not merely `IN (...)`: a CHECK
    # constraint rejects only FALSE, and `NULL IN (...)` evaluates to NULL, so an
    # IN-only predicate silently ADMITS a null status. Caught by
    # test_the_content_status_to_stamp_relationship_is_enforced_by_postgres.
    return (
        f"content_status IS NOT NULL AND content_status IN ({_CONTENT_STATUS_SQL})"
        f" AND ({bearing} OR {free})"
    )


def _protected_table(
    name: str,
    id_name: str,
    domain: PrivatePayloadDomain,
    *,
    content_status_required: bool = False,
) -> sa.Table:
    short = name.removeprefix("ne_agent_")
    return sa.Table(
        name,
        metadata,
        sa.Column(id_name, sa.String, primary_key=True),
        sa.Column("run_id", sa.String, nullable=True, index=True),
        sa.Column("owner_principal_id", sa.String, nullable=False),
        sa.Column("target_kind", sa.String, nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("target_binding", sa.JSON, nullable=True),
        # The canonical target tuple, stored in the CLEAR beside the ciphertext.
        # Content-free by construction: identifiers and one ordinal, never text.
        sa.Column("conversation_id", sa.String, nullable=True),
        sa.Column("turn_id", sa.String, nullable=True),
        sa.Column("turn_sequence", sa.Integer, nullable=True),
        sa.Column("attachment_id", sa.String, nullable=True),
        sa.Column("attempt_id", sa.String, nullable=True),
        # AUTHENTICATED content discriminator (SPEC): also bound into the AAD, so
        # editing this column alone makes the row undecryptable rather than
        # relabelled.
        sa.Column("content_status", sa.String, nullable=True),
        sa.Column("stamp_set_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *_envelope_columns(),
        sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"]),
        sa.UniqueConstraint("target_kind", "target_hash", name=f"uq_{short}_target"),
        # Referencable identity so a RunClaim carries a real composite foreign key
        # to (ref, owner, tuple) instead of a naming convention.
        sa.UniqueConstraint(
            id_name,
            "owner_principal_id",
            "conversation_id",
            "run_id",
            "turn_id",
            "turn_sequence",
            "attachment_id",
            "attempt_id",
            name=f"uq_{short}_claim_identity",
        ),
        sa.CheckConstraint("cipher_version = 1", name=f"ck_{short}_cipher_version"),
        sa.CheckConstraint(
            "turn_sequence IS NULL OR turn_sequence >= 1", name=f"ck_{short}_turn_sequence"
        ),
        sa.CheckConstraint(_target_tuple_discriminator_sql(), name=f"ck_{short}_target_tuple"),
        sa.CheckConstraint(
            _content_status_shape_sql(required=content_status_required),
            name=f"ck_{short}_content_status",
        ),
        info={
            "private_payload_domain": domain.value,
            "content_status_required": content_status_required,
        },
    )


ne_agent_pending_source_tool_results = _protected_table(
    "ne_agent_pending_source_tool_results",
    "pending_result_id",
    PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
)

ne_agent_evidence_sets = sa.Table(
    "ne_agent_evidence_sets",
    metadata,
    sa.Column("evidence_set_id", sa.String, primary_key=True),
    sa.Column("set_key", sa.String(64), nullable=False, unique=True),
    sa.Column("intent_hash", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("turn_id", sa.String, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("step_id", sa.String, nullable=False),
    sa.Column("tool_call_id", sa.String, nullable=False),
    sa.Column("read_ordinal", sa.Integer, nullable=False),
    sa.Column("pending_result_id", sa.String, nullable=False, unique=True),
    sa.Column("evidence_ids", sa.JSON, nullable=False),
    sa.Column("row_event_ids", sa.JSON, nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_evidence_set_run"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_evidence_set_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["pending_result_id"],
        ["ne_agent_pending_source_tool_results.pending_result_id"],
        name="fk_evidence_set_pending_result",
    ),
    sa.CheckConstraint("read_ordinal >= 0", name="ck_evidence_set_read_ordinal"),
    sa.CheckConstraint("length(intent_hash) = 64", name="ck_evidence_set_intent_hash"),
)
ne_agent_protected_turn_contents = _protected_table(
    "ne_agent_protected_turn_contents",
    "turn_content_id",
    PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
    content_status_required=True,
)
ne_agent_protected_source_outputs = _protected_table(
    "ne_agent_protected_source_outputs",
    "protected_output_id",
    PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
)
ne_agent_protected_source_invocations = _protected_table(
    "ne_agent_protected_source_invocations",
    "protected_invocation_id",
    PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
)
ne_agent_protected_workflow_states = _protected_table(
    "ne_agent_protected_workflow_states",
    "protected_workflow_state_id",
    PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE,
)

ne_agent_delivery_receipts = sa.Table(
    "ne_agent_delivery_receipts",
    metadata,
    sa.Column("delivery_receipt_id", sa.String, primary_key=True),
    # ADDRESSES one delivery decision; ``intent_hash`` carries its complete content.
    sa.Column("delivery_key", sa.String(64), nullable=False, unique=True),
    sa.Column("intent_hash", sa.String(64), nullable=False),
    # Nullable, because a STANDALONE Workflow delivery has no Run, no attempt and
    # no RunEvent. A composite foreign key is unenforced under MATCH SIMPLE the
    # moment any referencing column is NULL, so nullability alone would leave the
    # event binding a convention — ck_delivery_run_bound_shape below is what makes
    # it an integrity constraint: the three columns are all present or all absent,
    # never a partial shape that slips between the two.
    sa.Column("run_id", sa.String, nullable=True, index=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("delivery_kind", sa.String, nullable=False),
    sa.Column("decision", sa.String, nullable=False),
    sa.Column("protected_ref", sa.String, nullable=False),
    sa.Column("protected_hash", sa.String(64), nullable=False),
    sa.Column("stamp_set_hash", sa.String(64), nullable=False),
    sa.Column("receiving_attempt_id", sa.String, nullable=True),
    sa.Column("event_id", sa.String, nullable=True, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "delivery_kind IN ('tool_result_served','history_output_served','direct_result_served',"
        "'withheld')",
        name="ck_delivery_kind",
    ),
    sa.CheckConstraint("decision IN ('served','withheld')", name="ck_delivery_decision"),
    sa.CheckConstraint("length(intent_hash) = 64", name="ck_delivery_intent_hash"),
    # Run-bound => Run, attempt and event all present. Standalone => all three
    # absent. There is no third shape, so no receipt can carry a Run without the
    # event that proves it, or an event without the Run that owns it.
    sa.CheckConstraint(
        "(run_id IS NOT NULL AND receiving_attempt_id IS NOT NULL AND event_id IS NOT NULL)"
        " OR (run_id IS NULL AND receiving_attempt_id IS NULL AND event_id IS NULL)",
        name="ck_delivery_run_bound_shape",
    ),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"], name="fk_delivery_run"),
    # The receipt's event must be a REAL RunEvent OF THE SAME RUN.
    sa.ForeignKeyConstraint(
        ["event_id", "run_id"],
        ["ne_agent_run_events.event_id", "ne_agent_run_events.run_id"],
        name="fk_delivery_run_event",
    ),
    sa.ForeignKeyConstraint(
        ["receiving_attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_delivery_receiving_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["run_id", "owner_principal_id"],
        ["ne_agent_runs.run_id", "ne_agent_runs.owner_principal_id"],
        name="fk_delivery_run_owner",
    ),
)

ne_agent_run_claims = sa.Table(
    "ne_agent_run_claims",
    metadata,
    sa.Column("claim_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("turn_id", sa.String, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    # The SAME canonical tuple the protected row carries. Stored, not re-derived,
    # so the composite foreign key below makes "this claim and that row describe one
    # turn" a database fact rather than the caller's promise.
    sa.Column("conversation_id", sa.String, nullable=False),
    sa.Column("turn_sequence", sa.Integer, nullable=False),
    sa.Column("attachment_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("claim_key", sa.String, nullable=False),
    sa.Column("claim_index", sa.Integer, nullable=False),
    sa.Column("claim_verdict", sa.String, nullable=False),
    sa.Column("display_state", sa.String, nullable=False),
    sa.Column(
        "protected_domain",
        sa.String,
        nullable=False,
        server_default=PrivatePayloadDomain.PROTECTED_TURN_CONTENT.value,
    ),
    sa.Column("protected_ref", sa.String, nullable=False),
    sa.Column("served_output_hash", sa.String(64), nullable=False),
    sa.Column("span_start_byte", sa.Integer, nullable=False),
    sa.Column("span_end_byte", sa.Integer, nullable=False),
    sa.Column("claim_content_hash", sa.String(64), nullable=False),
    sa.Column("association_status", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "turn_id", "claim_key", name="uq_run_claim_local_key"),
    sa.UniqueConstraint("claim_id", "run_id", name="uq_run_claim_run_identity"),
    sa.CheckConstraint(
        "claim_verdict IN ('verified','grounded','unavailable')", name="ck_run_claim_verdict"
    ),
    sa.CheckConstraint(
        "display_state IN ('protected_source_free_slice','protected_source_slice')",
        name="ck_run_claim_display_state",
    ),
    sa.CheckConstraint(
        "protected_domain IN ('protected_turn_content','protected_source_output')",
        name="ck_run_claim_protected_domain",
    ),
    sa.CheckConstraint(
        "association_status IN ('associated','association_unavailable')",
        name="ck_run_claim_association_status",
    ),
    sa.CheckConstraint(
        "span_start_byte >= 0 AND span_end_byte > span_start_byte", name="ck_run_claim_span"
    ),
    sa.CheckConstraint(
        "association_status = 'associated' OR claim_verdict = 'unavailable'",
        name="ck_run_claim_unavailable_verdict",
    ),
    sa.CheckConstraint("turn_sequence >= 1", name="ck_run_claim_turn_sequence"),
)

# One opaque terminal attachment joins the canonical Conversation and Run.  It is
# intentionally separate from the legacy attachment table: that table has required
# plaintext-era columns and a publication protocol owned by 009C/010G, whereas this
# row is the protected-only history seam 010F can commit atomically.
ne_agent_run_history_attachments = sa.Table(
    "ne_agent_run_history_attachments",
    metadata,
    sa.Column("run_id", sa.String, primary_key=True),
    sa.Column("conversation_id", sa.String, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("turn_id", sa.String, nullable=False),
    sa.Column("turn_sequence", sa.Integer, nullable=False),
    sa.Column("attachment_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("protected_domain", sa.String, nullable=False),
    sa.Column("protected_ref", sa.String, nullable=False),
    sa.Column("protected_hash", sa.String(64), nullable=False),
    sa.Column("content_status", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["run_id", "owner_principal_id", "conversation_id"],
        [
            "ne_agent_runs.run_id",
            "ne_agent_runs.owner_principal_id",
            "ne_agent_runs.conversation_id",
        ],
        name="fk_run_history_run_owner",
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_history_attempt",
    ),
    sa.UniqueConstraint(
        "conversation_id", "turn_sequence", name="uq_run_history_conversation_sequence"
    ),
    sa.UniqueConstraint("conversation_id", "turn_id", name="uq_run_history_conversation_turn"),
    sa.CheckConstraint("turn_sequence >= 1", name="ck_run_history_turn_sequence"),
    sa.CheckConstraint("length(protected_hash) = 64", name="ck_run_history_protected_hash"),
    sa.CheckConstraint(
        "(content_status = 'source_free' AND protected_domain = 'protected_turn_content') OR "
        "(content_status = 'source_bound_container' AND "
        "protected_domain = 'protected_source_output')",
        name="ck_run_history_protected_shape",
    ),
)


ne_agent_run_finalizations = sa.Table(
    "ne_agent_run_finalizations",
    metadata,
    sa.Column("finalization_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("attempt_id", sa.String, nullable=False, unique=True),
    sa.Column("turn_id", sa.String, nullable=False),
    sa.Column("target_hash", sa.String(64), nullable=False),
    sa.Column("delivery_set_id", sa.String, nullable=False),
    sa.Column("delivery_set_hash", sa.String(64), nullable=False),
    sa.Column("audit_receipt_set_hash", sa.String(64), nullable=False),
    sa.Column("approval_set_hash", sa.String(64), nullable=False),
    sa.Column("evidence_set_hash", sa.String(64), nullable=False),
    sa.Column("verifier_set_hash", sa.String(64), nullable=False),
    sa.Column("envelope_hash", sa.String(64), nullable=False),
    sa.Column("mapping_state", sa.String, nullable=False),
    sa.Column("output_verdict", sa.String, nullable=False),
    sa.Column("protected_output_kind", sa.String, nullable=False),
    sa.Column("protected_output_ref", sa.String, nullable=True),
    sa.Column("protected_output_hash", sa.String(64), nullable=True),
    sa.Column("terminal_event_id", sa.String, nullable=False, unique=True),
    sa.Column("publication_id", sa.String, nullable=False, unique=True),
    sa.Column("finalizer_intent_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
        name="fk_run_finalization_attempt",
    ),
    sa.ForeignKeyConstraint(
        ["terminal_event_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_events.event_id",
            "ne_agent_run_events.run_id",
            "ne_agent_run_events.attempt_id",
        ],
        name="fk_run_finalization_event",
    ),
    sa.ForeignKeyConstraint(
        ["publication_id"],
        ["ne_agent_run_lifecycle_outbox.publication_id"],
        name="fk_run_finalization_publication",
    ),
    sa.CheckConstraint(
        "mapping_state IN ('complete','partial','unavailable')",
        name="ck_run_finalization_mapping_state",
    ),
    sa.CheckConstraint(
        "output_verdict IN ('verified','grounded','refused','unavailable')",
        name="ck_run_finalization_output_verdict",
    ),
    sa.CheckConstraint(
        "protected_output_kind IN "
        "('protected_turn_content','protected_source_output','unavailable')",
        name="ck_run_finalization_output_kind",
    ),
    sa.CheckConstraint(
        "(protected_output_ref IS NULL) = (protected_output_hash IS NULL)",
        name="ck_run_finalization_output_pair",
    ),
    sa.CheckConstraint("length(delivery_set_hash) = 64", name="ck_run_finalization_delivery_hash"),
    sa.CheckConstraint("length(target_hash) = 64", name="ck_run_finalization_target_hash"),
    sa.CheckConstraint(
        "length(audit_receipt_set_hash) = 64", name="ck_run_finalization_audit_set_hash"
    ),
    sa.CheckConstraint(
        "length(approval_set_hash) = 64", name="ck_run_finalization_approval_set_hash"
    ),
    sa.CheckConstraint(
        "length(evidence_set_hash) = 64", name="ck_run_finalization_evidence_set_hash"
    ),
    sa.CheckConstraint(
        "length(verifier_set_hash) = 64", name="ck_run_finalization_verifier_set_hash"
    ),
    sa.CheckConstraint("length(envelope_hash) = 64", name="ck_run_finalization_envelope_hash"),
    sa.CheckConstraint(
        "length(finalizer_intent_hash) = 64", name="ck_run_finalization_intent_hash"
    ),
)

ne_agent_claim_verifier_receipts = sa.Table(
    "ne_agent_claim_verifier_receipts",
    metadata,
    sa.Column("verifier_receipt_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("claim_key", sa.String, nullable=False),
    sa.Column("claim_content_hash", sa.String(64), nullable=False),
    sa.Column("served_output_hash", sa.String(64), nullable=False),
    sa.Column("span_start_byte", sa.Integer, nullable=False),
    sa.Column("span_end_byte", sa.Integer, nullable=False),
    sa.Column("verifier_method", sa.String, nullable=False),
    sa.Column("verifier_version", sa.String, nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("event_id", sa.String, nullable=False, unique=True),
    sa.Column("receipt_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"]),
    sa.ForeignKeyConstraint(
        ["event_id", "run_id"],
        ["ne_agent_run_events.event_id", "ne_agent_run_events.run_id"],
        name="fk_claim_verifier_run_event",
    ),
)

ne_agent_run_claim_evidence = sa.Table(
    "ne_agent_run_claim_evidence",
    metadata,
    sa.Column("association_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("claim_id", sa.String, nullable=False, unique=True),
    sa.Column("evidence_id", sa.String, nullable=False),
    sa.Column("locator_hash", sa.String(64), nullable=False),
    sa.Column("served_citation_ref_hash", sa.String(64), nullable=False),
    sa.Column("producing_event_id", sa.String, nullable=False),
    sa.Column("delivery_receipt_id", sa.String, nullable=True),
    sa.Column("association_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["claim_id", "run_id"],
        ["ne_agent_run_claims.claim_id", "ne_agent_run_claims.run_id"],
        name="fk_claim_evidence_run_claim",
    ),
    sa.ForeignKeyConstraint(
        ["evidence_id", "run_id"],
        ["ne_agent_evidence_records.evidence_id", "ne_agent_evidence_records.run_id"],
        name="fk_claim_evidence_run_evidence",
    ),
    sa.ForeignKeyConstraint(
        ["producing_event_id", "run_id"],
        ["ne_agent_run_events.event_id", "ne_agent_run_events.run_id"],
        name="fk_claim_evidence_run_event",
    ),
    sa.ForeignKeyConstraint(
        ["delivery_receipt_id"], ["ne_agent_delivery_receipts.delivery_receipt_id"]
    ),
)

ne_agent_run_audit_receipts = sa.Table(
    "ne_agent_run_audit_receipts",
    metadata,
    sa.Column("run_audit_receipt_id", sa.String, primary_key=True),
    # ADDRESSES one audit decision; ``intent_hash`` carries its complete content.
    sa.Column("intent_key", sa.String(64), nullable=False, unique=True),
    sa.Column("intent_hash", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("run_event_id", sa.String, nullable=False),
    sa.Column("step_id", sa.String, nullable=True),
    sa.Column("tool_call_id", sa.String, nullable=True),
    sa.Column("approval_id", sa.String, nullable=True),
    sa.Column("decision_kind", sa.String, nullable=False),
    sa.Column("audit_event_id", sa.String, nullable=False, unique=True),
    sa.Column("audit_sequence", sa.BigInteger, nullable=False),
    sa.Column("audit_record_hash", sa.String(64), nullable=False),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "attempt_id", "run_event_id", name="uq_run_audit_exact_event"),
    sa.CheckConstraint("audit_sequence >= 1", name="ck_run_audit_sequence"),
    sa.CheckConstraint("length(intent_hash) = 64", name="ck_run_audit_intent_hash"),
    sa.CheckConstraint(
        "decision_kind IN ('submission','state_transition','evidence','delivery','approval',"
        "'terminal','cancel','retry')",
        name="ck_run_audit_decision_kind",
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
    ),
    sa.ForeignKeyConstraint(
        ["run_event_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_events.event_id",
            "ne_agent_run_events.run_id",
            "ne_agent_run_events.attempt_id",
        ],
        name="fk_run_audit_exact_attempt_event",
    ),
)


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    conversation_id: str
    owner_principal_id: str
    department: str
    conversation_agent_name: str
    agent_id: str
    agent_version: int
    strategy: str
    config_hash: str
    capability_snapshot_hash: str
    capability_snapshot_receipt_id: str
    sequence: int
    display_label: str
    status: RunState
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None
    agent_definition_hash: str | None = None
    model_policy_id: str | None = None
    authorized_model: str | None = None
    authorized_model_lane: str | None = None
    model_authorization_receipt_hash: str | None = None
    context_snapshot_hash: str | None = None
    requested_model: str | None = None
    requested_model_lane: str | None = None
    version: int = 1
    output_verdict: str | None = None
    predecessor_run_id: str | None = None
    root_run_id: str = ""
    retry_ordinal: int = 0
    served_model: str | None = None
    #: Attached by the API projection alongside ``served_model``, from the SAME
    #: model-step row, so the name that served and the reason it served can
    #: never be read off two different steps. ``None`` means the Run recorded no
    #: route position -- never "primary".
    route_position: str | None = None
    fallback_from: str | None = None
    output_preview: str | None = None


@dataclass(frozen=True)
class RunAttemptLink:
    attempt_id: str
    run_id: str
    conversation_id: str
    predecessor_attempt_id: str | None
    attempt_number: int
    created_at: datetime


@dataclass(frozen=True)
class RunSkillActivationReceipt:
    activation_receipt_id: str
    run_id: str
    attempt_id: str
    agent_id: str
    agent_version: int
    surface_key: str
    pins: tuple[tuple[str, int, str], ...]
    index_hash: str
    surface_hash: str
    receipt_hash: str
    created_at: datetime


@dataclass(frozen=True)
class RunExecutionProfileReceipt:
    profile_receipt_id: str
    run_id: str
    attempt_id: str
    workflow_id: str
    profile_version: int
    profile_hash: str
    input_schema: Mapping[str, object]
    input_schema_hash: str
    ordered_plan: tuple[Mapping[str, object], ...]
    ordered_plan_hash: str
    receipt_hash: str
    created_at: datetime


@dataclass(frozen=True)
class RunAuditReceipt:
    run_audit_receipt_id: str
    run_id: str
    attempt_id: str
    run_event_id: str
    step_id: str | None
    tool_call_id: str | None
    approval_id: str | None
    decision_kind: RunAuditDecisionKind
    audit_event_id: str
    audit_sequence: int
    audit_record_hash: str
    config_hash: str
    created_at: datetime


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    run_id: str
    attempt_id: str
    sequence: int
    event_kind: str
    event_status: str
    parent_event_id: str | None
    step_id: str | None
    safe_detail: Mapping[str, object]
    detail_hash: str
    created_at: datetime


@dataclass(frozen=True)
class RunPage:
    runs: tuple[AgentRun, ...]
    next_cursor: tuple[datetime, str] | None


@dataclass(frozen=True)
class RunEventPage:
    events: tuple[RunEvent, ...]
    next_sequence: int | None


@dataclass(frozen=True)
class EvidenceDeclaration:
    turn_id: str
    step_id: str
    tool_call_id: str
    read_ordinal: int
    content_hash: str
    safe_event_detail: Mapping[str, object]
    locator_plaintext: bytes | None
    pending_result_plaintext: bytes
    source_stamps: tuple[SourceAuthorityStamp, ...]
    assurance: EvidenceAssurance = EvidenceAssurance.DIRECT


@dataclass(frozen=True)
class EvidenceContributorDeclaration:
    """One immutable locator/receipt member of a verified evidence set."""

    locator_plaintext: bytes
    source_stamp: SourceAuthorityStamp
    assurance: EvidenceAssurance
    safe_event_detail: Mapping[str, object]


@dataclass(frozen=True)
class EvidenceRowDeclaration:
    """One verified row and its ordered, complete contributor declarations."""

    row_anchor: str
    receipt_hash: str
    contributors: tuple[EvidenceContributorDeclaration, ...]


@dataclass(frozen=True)
class EvidenceSetDeclaration:
    """One protected result with per-row, per-contributor immutable evidence."""

    turn_id: str
    step_id: str
    tool_call_id: str
    read_ordinal: int
    content_hash: str
    pending_result_plaintext: bytes
    rows: tuple[EvidenceRowDeclaration, ...]


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    invocation_key: str
    locator_status: Literal["located", "evidence_locator_unavailable"]
    locator_id: str | None
    pending_result_id: str
    producing_event_id: str


@dataclass(frozen=True)
class EvidenceSetRecord:
    """The atomic winner for one protected result and its complete evidence set."""

    evidence_set_id: str
    set_key: str
    pending_result_id: str
    evidence: tuple[EvidenceRecord, ...]
    row_event_ids: tuple[str, ...]


#: The longest ``safe_reason`` that actually REACHES a reader — pinned against
#: ``ne.runtime.run_finalizer._MAX_SAFE_REASON_CHARS``, which drops anything longer.
_MAX_CLAIM_EVIDENCE_REASON_CHARS = 256


class RunClaimEvidenceUnavailable(LookupError):
    """A same-Run claim could not be bound to its cited evidence, and says why.

    This used to be a bare ``LookupError("Run claim evidence is unavailable")``.
    Nothing reads a bare ``LookupError``: ``run_workflow`` published it as
    ``Recorded failure class: LookupError`` and the reader got a class name for a
    condition the server could name precisely.

    ``cause`` is the honest half. The SAME load refuses two genuinely different
    things and they must not be reported as one:

    ``claim_contract``
        The answer cited a reference this turn never served under it — a
        model/contract problem, and the answer is the thing at fault.
    ``evidence_store``
        The cited reference WAS served, and the record behind it could not be
        read back or did not agree with itself — a store condition. Reporting
        that as a contract error would blame the answer for a system fault, so
        it keeps its own sentence.

    Every reason is a static server-authored literal: no claim text, no evidence
    text, no identifiers, no values (C12).
    """

    _REASONS: ClassVar[dict[str, str]] = {
        "claim_contract": (
            "The answer cited a source reference this turn did not serve, so the claim "
            "could not be bound to its evidence and the answer was not served."
        ),
        "evidence_store": (
            "The stored evidence for a cited source could not be read back intact, so the "
            "claim could not be verified. This is a store condition, not a bad answer."
        ),
    }

    def __init__(self, cause: Literal["claim_contract", "evidence_store"]) -> None:
        self.cause = cause
        self.safe_reason = self._REASONS[cause][:_MAX_CLAIM_EVIDENCE_REASON_CHARS]
        super().__init__("Run claim evidence is unavailable")


def _claim_contract_unavailable() -> RunClaimEvidenceUnavailable:
    return RunClaimEvidenceUnavailable("claim_contract")


def _evidence_store_unavailable() -> RunClaimEvidenceUnavailable:
    return RunClaimEvidenceUnavailable("evidence_store")


@dataclass(frozen=True)
class RunClaimEvidenceSetMember:
    """One (row, contributor) member of a served verified-T3 evidence set."""

    evidence_id: str
    locator_id: str
    locator_hash: str
    producing_event_id: str
    locator: bytes


@dataclass(frozen=True)
class RunClaimEvidenceSetObservation:
    """The COMPLETE served verified-T3 evidence set behind one local Run claim."""

    protected_ref: str
    protected_result_hash: str
    evidence_set_id: str
    set_key: str
    delivery_receipt_id: str
    delivery_receipt_hash: str
    protected_rendering: bytes
    members: tuple[RunClaimEvidenceSetMember, ...]
    read_kind: Literal["verified_t3"] = "verified_t3"
    assurance: Literal["direct"] = "direct"


@dataclass(frozen=True)
class RunClaimEvidenceObservation:
    """One exact served Knowledge result loaded for a local Run claim."""

    protected_ref: str
    protected_result_hash: str
    evidence_id: str
    locator_id: str
    locator_hash: str
    producing_event_id: str
    delivery_receipt_id: str
    delivery_receipt_hash: str
    protected_rendering: bytes
    locator: bytes
    read_kind: Literal["search", "graph_path"]
    assurance: Literal["direct", "inferred"]


@dataclass(frozen=True)
class ClaimVerifierRecord:
    """Server-minted identity for one deterministic same-Run verifier result."""

    verifier_receipt_id: str
    event_id: str
    receipt_hash: str


@dataclass(frozen=True)
class ClaimDeclaration:
    claim_key: str
    claim_index: int
    claim_verdict: Literal["verified", "grounded", "unavailable"]
    display_state: Literal["protected_source_free_slice", "protected_source_slice"]
    protected_ref: str
    served_output_hash: str
    span_start_byte: int
    span_end_byte: int
    claim_content_hash: str
    association_status: Literal["associated", "association_unavailable"]
    protected_domain: Literal["protected_turn_content", "protected_source_output"] = (
        "protected_turn_content"
    )


@dataclass(frozen=True)
class ClaimEvidenceDeclaration:
    claim_key: str
    evidence_id: str
    locator_hash: str
    served_citation_ref_hash: str
    producing_event_id: str
    delivery_receipt_id: str | None = None


@dataclass(frozen=True)
class ClaimVerifierDeclaration:
    claim_key: str
    claim_content_hash: str
    served_output_hash: str
    span_start_byte: int
    span_end_byte: int
    verifier_method: str
    verifier_version: str
    result_hash: str
    event_id: str


class ProtectedSliceUnavailable(RuntimeError):
    """One indistinguishable outcome for every refused claim-slice request.

    Foreign owner, wrong tuple, wrong ref, wrong output hash, source-bound content,
    an unopenable envelope, and a claim that simply does not exist all raise THIS,
    with this message. Distinct errors would make the surface an existence oracle:
    a caller could enumerate which claims exist by reading the refusals.
    """

    def __init__(self) -> None:
        super().__init__("protected claim slice unavailable")


class ProtectedTurnUnavailable(RuntimeError):
    """One indistinguishable outcome for refused whole-turn materialization."""

    def __init__(self) -> None:
        super().__init__("protected turn unavailable")


class EvidenceProjectionUnavailable(RuntimeError):
    """One opaque outcome for every refused evidence-projection payload read."""

    def __init__(self) -> None:
        super().__init__("evidence projection payload unavailable")


@dataclass(frozen=True)
class PrivateKeyReadiness:
    """Content-free readiness/reference counts for the current private-payload key."""

    current_key_id: str
    rows_under_current_key: int
    rows_under_other_keys: int
    reference_counts: dict[str, int]

    @property
    def ready(self) -> bool:
        """Every stored private row is wrapped under the CURRENT key."""
        return self.rows_under_other_keys == 0


@dataclass(frozen=True)
class ProtectedPayloadRef:
    domain: PrivatePayloadDomain
    row_id: str
    owner_principal_id: str
    target_kind: str
    target_hash: str
    plaintext_hash: str
    stamp_set_hash: str | None


@dataclass(frozen=True)
class StoredSourcePayload:
    """Material returned only by a target-specific protected-source reader."""

    plaintext: bytes
    stamps: tuple[SourceAuthorityStamp, ...]
    plaintext_hash: str
    evidence_ids: tuple[str, ...] = ()


def _run(row: Any) -> AgentRun:
    run_id = str(row.run_id)
    predecessor = getattr(row, "predecessor_run_id", None)
    root = getattr(row, "root_run_id", None)
    ordinal = getattr(row, "retry_ordinal", None)
    return AgentRun(
        run_id=run_id,
        conversation_id=str(row.conversation_id),
        owner_principal_id=str(row.owner_principal_id),
        department=str(row.department),
        conversation_agent_name=str(row.conversation_agent_name),
        agent_id=str(row.agent_id),
        agent_version=int(row.agent_version),
        strategy=str(row.strategy),
        config_hash=str(row.config_hash),
        capability_snapshot_hash=str(row.capability_snapshot_hash),
        capability_snapshot_receipt_id=str(row.capability_snapshot_receipt_id),
        agent_definition_hash=(
            None
            if getattr(row, "agent_definition_hash", None) is None
            else str(row.agent_definition_hash)
        ),
        model_policy_id=(
            None if getattr(row, "model_policy_id", None) is None else str(row.model_policy_id)
        ),
        authorized_model=(
            None if getattr(row, "authorized_model", None) is None else str(row.authorized_model)
        ),
        authorized_model_lane=(
            None
            if getattr(row, "authorized_model_lane", None) is None
            else str(row.authorized_model_lane)
        ),
        model_authorization_receipt_hash=(
            None
            if getattr(row, "model_authorization_receipt_hash", None) is None
            else str(row.model_authorization_receipt_hash)
        ),
        context_snapshot_hash=(
            None
            if getattr(row, "context_snapshot_hash", None) is None
            else str(row.context_snapshot_hash)
        ),
        requested_model=(
            None if getattr(row, "requested_model", None) is None else str(row.requested_model)
        ),
        requested_model_lane=(
            None
            if getattr(row, "requested_model_lane", None) is None
            else str(row.requested_model_lane)
        ),
        sequence=int(row.sequence),
        display_label=str(row.display_label),
        status=RunState(str(row.status)),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        terminal_at=None if row.terminal_at is None else _utc(row.terminal_at),
        version=int(row.version),
        output_verdict=(
            None if getattr(row, "output_verdict", None) is None else str(row.output_verdict)
        ),
        predecessor_run_id=None if predecessor is None else str(predecessor),
        root_run_id=run_id if root is None else str(root),
        retry_ordinal=0 if ordinal is None else int(ordinal),
    )


def _runs_with_lineage() -> sa.Select[Any]:
    """Select Run rows with immutable user-retry lineage, if any."""

    return sa.select(
        ne_agent_runs,
        ne_agent_run_retry_lineage.c.predecessor_run_id,
        ne_agent_run_retry_lineage.c.root_run_id,
        ne_agent_run_retry_lineage.c.retry_ordinal,
    ).select_from(
        ne_agent_runs.outerjoin(
            ne_agent_run_retry_lineage,
            ne_agent_run_retry_lineage.c.successor_run_id == ne_agent_runs.c.run_id,
        )
    )


def _attempt(row: Any) -> RunAttemptLink:
    return RunAttemptLink(
        attempt_id=str(row.attempt_id),
        run_id=str(row.run_id),
        conversation_id=str(row.conversation_id),
        predecessor_attempt_id=(
            None if row.predecessor_attempt_id is None else str(row.predecessor_attempt_id)
        ),
        attempt_number=int(row.attempt_number),
        created_at=_utc(row.created_at),
    )


def _skill_activation_receipt(row: Any) -> RunSkillActivationReceipt:
    raw_pins = json.loads(str(row.pins_json))
    if not isinstance(raw_pins, list):  # pragma: no cover - database constraint boundary
        raise RuntimeError("stored Run Skill activation pins are invalid")
    pins: list[tuple[str, int, str]] = []
    for raw in raw_pins:
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or not isinstance(raw[0], str)
            or not isinstance(raw[1], int)
            or not isinstance(raw[2], str)
        ):
            raise RuntimeError("stored Run Skill activation pins are invalid")
        pins.append((raw[0], raw[1], raw[2]))
    return RunSkillActivationReceipt(
        activation_receipt_id=str(row.activation_receipt_id),
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        agent_id=str(row.agent_id),
        agent_version=int(row.agent_version),
        surface_key=str(row.surface_key),
        pins=tuple(pins),
        index_hash=str(row.index_hash),
        surface_hash=str(row.surface_hash),
        receipt_hash=str(row.receipt_hash),
        created_at=_utc(row.created_at),
    )


def _execution_profile_receipt(row: Any) -> RunExecutionProfileReceipt:
    input_schema = json.loads(str(row.input_schema_json))
    ordered_plan = json.loads(str(row.ordered_plan_json))
    if not isinstance(input_schema, dict) or not isinstance(ordered_plan, list):
        raise RuntimeError("stored Run execution profile receipt is invalid")
    if any(not isinstance(step, dict) for step in ordered_plan):
        raise RuntimeError("stored Run execution profile plan is invalid")
    return RunExecutionProfileReceipt(
        profile_receipt_id=str(row.profile_receipt_id),
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        workflow_id=str(row.workflow_id),
        profile_version=int(row.profile_version),
        profile_hash=str(row.profile_hash),
        input_schema=cast(Mapping[str, object], input_schema),
        input_schema_hash=str(row.input_schema_hash),
        ordered_plan=tuple(cast(Mapping[str, object], step) for step in ordered_plan),
        ordered_plan_hash=str(row.ordered_plan_hash),
        receipt_hash=str(row.receipt_hash),
        created_at=_utc(row.created_at),
    )


def _audit_receipt(row: Any) -> RunAuditReceipt:
    return RunAuditReceipt(
        run_audit_receipt_id=str(row.run_audit_receipt_id),
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        run_event_id=str(row.run_event_id),
        step_id=None if row.step_id is None else str(row.step_id),
        tool_call_id=None if row.tool_call_id is None else str(row.tool_call_id),
        approval_id=None if row.approval_id is None else str(row.approval_id),
        decision_kind=RunAuditDecisionKind(str(row.decision_kind)),
        audit_event_id=str(row.audit_event_id),
        audit_sequence=int(row.audit_sequence),
        audit_record_hash=str(row.audit_record_hash),
        config_hash=str(row.config_hash),
        created_at=_utc(row.created_at),
    )


def _event(row: Any) -> RunEvent:
    detail = json.loads(str(row.safe_detail))
    if not isinstance(detail, dict):
        raise ValueError("RunEvent safe detail is not an object")
    return RunEvent(
        event_id=str(row.event_id),
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        sequence=int(row.sequence),
        event_kind=str(row.event_kind),
        event_status=str(row.event_status),
        parent_event_id=None if row.parent_event_id is None else str(row.parent_event_id),
        step_id=None if row.step_id is None else str(row.step_id),
        safe_detail=detail,
        detail_hash=str(row.detail_hash),
        created_at=_utc(row.created_at),
    )


def _envelope_values(envelope: PrivatePayloadEnvelope) -> dict[str, object]:
    return {
        "cipher_version": envelope.cipher_version,
        "plaintext_hash": envelope.plaintext_hash,
        "binding_hash": envelope.binding_hash,
        "key_id": envelope.key_id,
        "payload_nonce": envelope.payload_nonce,
        "payload_ciphertext": envelope.payload_ciphertext,
        "key_nonce": envelope.key_nonce,
        "encrypted_dek": envelope.encrypted_dek,
    }


_PROTECTED_TABLES: dict[PrivatePayloadDomain, tuple[sa.Table, str, frozenset[str]]] = {
    PrivatePayloadDomain.PROTECTED_TURN_CONTENT: (
        ne_agent_protected_turn_contents,
        "turn_content_id",
        frozenset(
            {
                AGENT_RUN_TURN_TARGET_KIND,
                CONVERSATION_TURN_TARGET_KIND,
                SESSION_MESSAGE_TARGET_KIND,
            }
        ),
    ),
    PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT: (
        ne_agent_protected_source_outputs,
        "protected_output_id",
        frozenset({AGENT_RUN_TURN_TARGET_KIND, "assistant_turn", "workflow_execution"}),
    ),
    PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION: (
        ne_agent_protected_source_invocations,
        "protected_invocation_id",
        frozenset({"approval_action", "subagent_dispatch"}),
    ),
    PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE: (
        ne_agent_protected_workflow_states,
        "protected_workflow_state_id",
        frozenset({"agent_run", "standalone_workflow"}),
    ),
    PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT: (
        ne_agent_pending_source_tool_results,
        "pending_result_id",
        frozenset({"agent_run_tool_call", "standalone_workflow_tool_call"}),
    ),
}


def _row_envelope(
    row: Any,
    *,
    domain: PrivatePayloadDomain,
    row_id_name: str,
) -> PrivatePayloadEnvelope:
    return PrivatePayloadEnvelope(
        cipher_version=int(row.cipher_version),
        domain=domain,
        row_id=str(getattr(row, row_id_name)),
        owner_principal_id=str(row.owner_principal_id),
        binding_hash=str(row.binding_hash),
        plaintext_hash=str(row.plaintext_hash),
        key_id=str(row.key_id),
        payload_nonce=str(row.payload_nonce),
        payload_ciphertext=str(row.payload_ciphertext),
        key_nonce=str(row.key_nonce),
        encrypted_dek=str(row.encrypted_dek),
    )


def _stamp_set_hash(stamps: Sequence[SourceAuthorityStamp]) -> str:
    if not stamps:
        raise ValueError("source-bearing payload requires at least one authority stamp")
    members = []
    for stamp in stamps:
        members.append(
            _canonical_json(
                {
                    "source": stamp.source_identity.identity_hash,
                    "artifact": stamp.artifact_identity.artifact_id,
                    "head": stamp.selected_head_revision,
                    "acl": stamp.acl_snapshot_hash,
                    "contract": stamp.source_contract.identity_hash,
                    "epoch": stamp.contract_epoch,
                    "receipt_id": stamp.authority_receipt_id,
                    "receipt_hash": stamp.authority_receipt_hash,
                }
            )
        )
    if len(members) != len(set(members)):
        raise ValueError("authority stamp set contains duplicates")
    return _hash(_canonical_json(members).encode("utf-8"))


def _pack_source_bound_payload(
    plaintext: bytes,
    stamps: Sequence[SourceAuthorityStamp],
    *,
    evidence_ids: Sequence[str] = (),
) -> bytes:
    """Keep the complete immutable stamp set inside the authenticated ciphertext."""
    return _canonical_json(
        {
            "schema": (
                "ne.source-bound-private-payload.v2"
                if evidence_ids
                else "ne.source-bound-private-payload.v1"
            ),
            "payload_hex": plaintext.hex(),
            "source_stamps": [stamp.model_dump(mode="json") for stamp in stamps],
            **({"evidence_ids": list(evidence_ids)} if evidence_ids else {}),
        }
    ).encode("utf-8")


def _unpack_source_bound_payload(plaintext: bytes) -> bytes:
    return _unpack_source_bound_payload_and_stamps(plaintext)[0]


def _unpack_source_bound_payload_and_stamps(
    plaintext: bytes,
) -> tuple[bytes, tuple[SourceAuthorityStamp, ...], tuple[str, ...]]:
    try:
        value = json.loads(plaintext)
        if (
            not isinstance(value, dict)
            or value.get("schema")
            not in {
                "ne.source-bound-private-payload.v1",
                "ne.source-bound-private-payload.v2",
            }
            or not isinstance(value.get("payload_hex"), str)
            or not isinstance(value.get("source_stamps"), list)
            or not value["source_stamps"]
        ):
            raise ValueError
        payload = bytes.fromhex(value["payload_hex"])
        stamps = tuple(SourceAuthorityStamp.model_validate(raw) for raw in value["source_stamps"])
        if len(stamps) != len(set(stamps)):
            raise ValueError
        raw_evidence_ids = value.get("evidence_ids", [])
        if (
            not isinstance(raw_evidence_ids, list)
            or any(not isinstance(item, str) or not item for item in raw_evidence_ids)
            or len(raw_evidence_ids) != len(set(raw_evidence_ids))
            or (value["schema"] == "ne.source-bound-private-payload.v1" and raw_evidence_ids)
            or (value["schema"] == "ne.source-bound-private-payload.v2" and not raw_evidence_ids)
        ):
            raise ValueError
        return payload, stamps, tuple(raw_evidence_ids)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("protected source payload envelope is invalid") from exc


def _evidence_intent_fields(
    *,
    run_id: str,
    owner_principal_id: str,
    attempt_id: str,
    declaration: EvidenceDeclaration,
    locator_status: str,
    stamp_set_hash: str,
) -> dict[str, object]:
    """Every intent-bearing field of one evidence declaration.

    Membership here is the replay contract: a field absent from this map is a
    field two different declarations may silently share under one address.
    """
    return {
        "run_id": run_id,
        "owner_principal_id": owner_principal_id,
        "attempt_id": attempt_id,
        "turn_id": declaration.turn_id,
        "step_id": declaration.step_id,
        "tool_call_id": declaration.tool_call_id,
        "read_ordinal": declaration.read_ordinal,
        "content_hash": declaration.content_hash,
        "safe_event_detail_hash": _hash(
            _canonical_json(dict(declaration.safe_event_detail)).encode("utf-8")
        ),
        "locator_status": locator_status,
        "locator_plaintext_hash": (
            None if declaration.locator_plaintext is None else _hash(declaration.locator_plaintext)
        ),
        "pending_result_plaintext_hash": _hash(declaration.pending_result_plaintext),
        "stamp_set_hash": stamp_set_hash,
        "assurance": declaration.assurance.value,
    }


def _validate_locator_assurance(
    locator_plaintext: bytes | None,
    assurance: EvidenceAssurance,
    *,
    required: bool,
) -> None:
    if locator_plaintext is None:
        return
    try:
        value = json.loads(locator_plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("EvidenceLocator must be canonical JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("EvidenceLocator must be a JSON object")
    actual = value.get("evidence_assurance")
    if actual is None and not required:
        return
    if actual != assurance.value:
        raise ValueError("EvidenceLocator assurance does not match its typed declaration")


def _load_delivery_receipt(conn: sa.Connection, delivery_key: str) -> Any:
    return conn.execute(
        sa.select(
            ne_agent_delivery_receipts.c.delivery_receipt_id,
            ne_agent_delivery_receipts.c.intent_hash,
        ).where(ne_agent_delivery_receipts.c.delivery_key == delivery_key)
    ).one_or_none()


def _replayed_receipt_id(row: Any, intent_hash: str, *, id_column: str, message: str) -> str:
    """Return the winner's id ONLY for a byte-identical intent; otherwise fail closed.

    Never returns another intent's receipt: a mismatch raises rather than answering
    the caller with the winner's identifier.
    """
    if not hmac.compare_digest(str(row.intent_hash), intent_hash):
        raise ValueError(message)
    return str(getattr(row, id_column))


def _replayed_audit_receipt(row: Any, intent_hash: str, owner_principal_id: str) -> RunAuditReceipt:
    """The canonical stored receipt ONLY for a byte-identical intent by its Run's owner.

    Never answers with another intent's receipt (the ``intent_hash`` compare), and
    never answers a principal other than the owning Run's (the joined
    ``owner_principal_id`` compare — the ownership fact ``get_audit_receipt``
    used to enforce with a second post-write read).
    """
    if not hmac.compare_digest(str(row.intent_hash), intent_hash):
        raise ValueError("Run audit address was replayed under a different recorded intent")
    if str(row.owner_principal_id) != owner_principal_id:
        raise ValueError("Run audit receipt principal differs from the owning Run")
    return _audit_receipt(row)


def _terminal_intent_hash(
    *,
    run_id: str,
    owner: str,
    turn_id: str,
    attempt_id: str,
    target: CanonicalTargetTuple,
    claims: Sequence[ClaimDeclaration],
    associations: Sequence[ClaimEvidenceDeclaration],
    verifier_receipts: Sequence[ClaimVerifierDeclaration],
    status: RunState,
) -> str:
    """Every immutable field of one terminal commit, including the zero-claim case."""
    return _keyed_hash(
        "ne.run-terminal-intent.v1",
        {
            "run_id": run_id,
            "owner_principal_id": owner,
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "target": list(target.binding()),
            "terminal_status": status.value,
            "claims": [asdict(claim) for claim in claims],
            "associations": [asdict(item) for item in associations],
            "verifier_receipts": [asdict(item) for item in verifier_receipts],
        },
    )


def _lock_run(conn: sa.Connection, run_id: str) -> Any:
    """Take the owning Run's row lock BEFORE any replay read or event allocation.

    Two mechanisms guard these paths on purpose and they are redundant by design:
    this lock serializes every writer of ONE Run, and the ``ON CONFLICT DO NOTHING``
    arbitration below serializes writers that share a key without sharing a Run
    (a standalone protected payload has no Run to lock). Removing either one alone
    leaves the other holding the invariant — which is the point, and is why the
    mutation that proves this path removes BOTH.
    """
    row = conn.execute(
        sa.select(ne_agent_runs.c.run_id, ne_agent_runs.c.owner_principal_id)
        .where(ne_agent_runs.c.run_id == run_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise LookupError("unknown Run")
    return row


def _load_audit_receipt(conn: sa.Connection, intent_key: str) -> Any:
    """The full winner row for one audit address, with its Run's owner.

    Selects the whole receipt row (not just the id) plus the owning Run's
    ``owner_principal_id`` so the recording transaction can return the canonical
    :class:`RunAuditReceipt` AND enforce the owner/actor identity in the SAME
    round trip — the separate post-write ``get_audit_receipt`` read this used to
    force was one full connection + join per audited decision on the Run hot
    path (perf/audit-intent-path).
    """
    return conn.execute(
        sa.select(ne_agent_run_audit_receipts, ne_agent_runs.c.owner_principal_id)
        .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_audit_receipts.c.run_id)
        .where(ne_agent_run_audit_receipts.c.intent_key == intent_key)
    ).one_or_none()


def _load_evidence(conn: sa.Connection, invocation_key: str) -> Any:
    return conn.execute(
        sa.select(ne_agent_evidence_records).where(
            ne_agent_evidence_records.c.invocation_key == invocation_key
        )
    ).one_or_none()


def _load_evidence_set(conn: sa.Connection, set_key: str) -> Any:
    return conn.execute(
        sa.select(ne_agent_evidence_sets).where(ne_agent_evidence_sets.c.set_key == set_key)
    ).one_or_none()


def _replayed_evidence(row: Any, invocation_key: str, intent_hash: str) -> EvidenceRecord:
    """Return the winner ONLY for a byte-identical intent; otherwise fail closed."""
    if not hmac.compare_digest(str(row.intent_hash), intent_hash):
        raise ValueError("evidence invocation key was replayed under a different declared intent")
    return EvidenceRecord(
        evidence_id=str(row.evidence_id),
        invocation_key=invocation_key,
        locator_status=str(row.locator_status),  # type: ignore[arg-type]
        locator_id=None if row.locator_id is None else str(row.locator_id),
        pending_result_id=str(row.pending_result_id),
        producing_event_id=str(row.producing_event_id),
    )


def _replayed_evidence_set(
    conn: sa.Connection, row: Any, *, set_key: str, intent_hash: str
) -> EvidenceSetRecord:
    if not hmac.compare_digest(str(row.intent_hash), intent_hash):
        raise ValueError("evidence set key was replayed under a different declared intent")
    raw_ids = row.evidence_ids
    raw_events = row.row_event_ids
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(item, str) or not item for item in raw_ids)
        or len(raw_ids) != len(set(raw_ids))
        or not isinstance(raw_events, list)
        or not raw_events
        or any(not isinstance(item, str) or not item for item in raw_events)
        or len(raw_events) != len(set(raw_events))
    ):
        raise ValueError("stored evidence set membership is invalid")
    rows = conn.execute(
        sa.select(ne_agent_evidence_records).where(
            ne_agent_evidence_records.c.evidence_id.in_(raw_ids)
        )
    ).fetchall()
    by_id = {str(item.evidence_id): item for item in rows}
    if set(by_id) != set(raw_ids):
        raise ValueError("stored evidence set membership is incomplete")
    expected_record_binding = (
        str(row.run_id),
        str(row.owner_principal_id),
        str(row.attempt_id),
        str(row.step_id),
        str(row.tool_call_id),
        int(row.read_ordinal),
        str(row.pending_result_id),
        str(row.content_hash),
    )
    if any(
        (
            str(item.run_id),
            str(item.owner_principal_id),
            str(item.attempt_id),
            str(item.step_id),
            str(item.tool_call_id),
            int(item.read_ordinal),
            str(item.pending_result_id),
            str(item.content_hash),
        )
        != expected_record_binding
        for item in rows
    ):
        raise ValueError("stored evidence set member binding is invalid")
    events = conn.execute(
        sa.select(ne_agent_run_events).where(ne_agent_run_events.c.event_id.in_(raw_events))
    ).fetchall()
    events_by_id = {str(item.event_id): item for item in events}
    if set(events_by_id) != set(raw_events):
        raise ValueError("stored evidence set row events are incomplete")
    event_evidence_ids: list[str] = []
    for event_id in raw_events:
        event = events_by_id[event_id]
        try:
            detail = json.loads(event.safe_detail)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored evidence set row event is invalid") from exc
        ids = detail.get("evidence_ids") if isinstance(detail, dict) else None
        if (
            str(event.run_id) != str(row.run_id)
            or str(event.attempt_id) != str(row.attempt_id)
            or str(event.step_id) != str(row.step_id)
            or str(event.event_kind) != "evidence.verified_row"
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) or not item for item in ids)
        ):
            raise ValueError("stored evidence set row event is invalid")
        event_evidence_ids.extend(ids)
    if event_evidence_ids != raw_ids:
        raise ValueError("stored evidence set row membership is out of order")
    evidence = tuple(
        _replayed_evidence(
            by_id[evidence_id],
            str(by_id[evidence_id].invocation_key),
            str(by_id[evidence_id].intent_hash),
        )
        for evidence_id in raw_ids
    )
    return EvidenceSetRecord(
        evidence_set_id=str(row.evidence_set_id),
        set_key=set_key,
        pending_result_id=str(row.pending_result_id),
        evidence=evidence,
        row_event_ids=tuple(raw_events),
    )


#: The migration declares a status from the SAME closed set the protected row
#: AUTHENTICATES, so a migration cannot declare a status the row does not carry.
_MIGRATION_STATUSES = frozenset(status.value for status in ProtectedContentStatus)

#: The one receipt written instead of fabricating an owner for a pre-attribution
#: session row.  These are data-plane constants, not caller-selected labels.
LEGACY_TRANSPORT_GENERATION = "agent-transport-v1"
OWNERLESS_RETIREMENT_REASON = "ownerless_principal_unrecoverable"


def _assert_protected_row_matches_target(
    conn: sa.Connection,
    cipher: RunPrivatePayloadCipher,
    *,
    table: sa.Table,
    row_id_column: str,
    row_id: str,
    owner_principal_id: str,
    run_id: str | None,
    target_kind: str,
    target_binding: Sequence[str],
    content_status: ProtectedContentStatus | None,
    plaintext_hash: str,
) -> None:
    """Raise unless ``row_id`` IS the protected row for this exact target and content.

    Runs inside the erasing transaction and BEFORE any destructive statement, and
    takes the protected row's own lock so it cannot be rewrapped or replaced between
    this proof and the erase. Proves, in order: declared domain, row existence,
    owner, Run, target kind and address, content hash, and finally AUTHENTIC
    DECRYPTABILITY — the row is opened under its exact binding and the recovered
    plaintext re-hashed. A row whose ciphertext cannot be opened is not evidence
    that the plaintext is safe to destroy, however well its metadata reads.
    """
    declared = table.info.get("private_payload_domain")
    if declared is None:
        raise ValueError(f"table {table.name!r} declares no private-payload domain")
    domain = PrivatePayloadDomain(str(declared))
    row = conn.execute(
        sa.select(table).where(table.c[row_id_column] == row_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise LookupError("protected payload row for this migration does not exist")
    if str(row.owner_principal_id) != owner_principal_id:
        raise ValueError("protected payload row belongs to a different principal")
    if (None if row.run_id is None else str(row.run_id)) != run_id:
        raise ValueError("protected payload row belongs to a different Run")
    expected = protected_target_hash(domain, owner_principal_id, target_kind, target_binding)
    if str(row.target_kind) != target_kind or not hmac.compare_digest(
        str(row.target_hash), expected
    ):
        raise ValueError("protected payload row addresses a different migration target")
    if not hmac.compare_digest(str(row.plaintext_hash), plaintext_hash):
        raise ValueError("protected payload row does not hold the declared content hash")
    stored_status = (
        None if row.content_status is None else ProtectedContentStatus(str(row.content_status))
    )
    if stored_status != content_status:
        raise ValueError("protected payload row carries a different authenticated content status")
    try:
        recovered = cipher.materialize_for_target(
            _row_envelope(row, domain=domain, row_id_name=row_id_column),
            domain=domain,
            row_id=row_id,
            owner_principal_id=owner_principal_id,
            binding=protected_payload_aad_binding(
                target_kind=target_kind,
                target_binding=target_binding,
                content_status=content_status,
            ),
        )
    except (KeyEnvelopeError, ValueError) as exc:
        # Same reason as the slice path: a malformed envelope column fails in the
        # envelope CONSTRUCTOR, and it must read as "not authentically decryptable"
        # rather than leaking a base64 parser error.
        raise ValueError("protected payload row is not authentically decryptable") from exc
    if not hmac.compare_digest(_hash(recovered), plaintext_hash):
        raise ValueError("protected payload row is not authentically decryptable")


def _require_migration_hash(name: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase sha256")


def ensure_run_schema(engine: sa.Engine) -> None:
    """Create the additive dormant PRE-009A schema under the runtime schema lock."""
    # Import registers the optional PRE-010C tables on this module's metadata
    # before create_all; local import avoids a module-initialization cycle.
    from ne.runtime import run_checkpoint as _run_checkpoint

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql(_POSTGRES_RUN_SCHEMA_LOCK_SQL)
            metadata.create_all(conn)
            _ensure_submission_schema_compatibility(conn)
            _ensure_finalizer_schema_compatibility(conn)
            _ensure_protected_target_binding_columns(conn)
            _ensure_model_step_routing_columns(conn)
            _ensure_evidence_set_schema_compatibility(conn)
            _ensure_approval_join_governance_columns(conn)
            _run_checkpoint.ensure_continuation_status_compatibility(conn)
            _install_run_guards(conn)
            _install_default_ask_fence(conn)
        # Outside the advisory-locked transaction ON PURPOSE — see the function.
        _validate_deferred_outcome_constraint(engine)
    elif engine.dialect.name == "sqlite":
        ensure_transactional_sqlite(engine)
        with _SQLITE_RUN_SCHEMA_LOCK, engine.connect() as conn:
            # SQLite cannot replace a CHECK constraint in place.  Foreign keys
            # must be disabled outside the rebuilding transaction so child
            # tables continue to reference the canonical table name while its
            # exact lifecycle definition is replaced.
            driver = conn.connection.driver_connection
            if driver is None:
                raise RuntimeError("SQLite migration lacks its DBAPI connection")
            cursor = driver.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()
            try:
                with conn.begin():
                    metadata.create_all(conn)
                    _ensure_submission_schema_compatibility(conn)
                    _ensure_finalizer_schema_compatibility(conn)
                    _ensure_protected_target_binding_columns(conn)
                    _ensure_model_step_routing_columns(conn)
                    _ensure_evidence_set_schema_compatibility(conn)
                    _ensure_approval_join_governance_columns(conn)
                    _run_checkpoint.ensure_continuation_status_compatibility(conn)
                    _install_run_guards(conn)
                    _install_default_ask_fence(conn)
                    violations = list(conn.exec_driver_sql("PRAGMA foreign_key_check"))
                    if violations:
                        raise RuntimeError("Run schema upgrade produced foreign-key violations")
            finally:
                if conn.in_transaction():
                    conn.rollback()
                cursor = driver.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA foreign_keys")
                enabled = int(cursor.fetchone()[0])
                cursor.close()
                if enabled != 1:
                    raise RuntimeError("Run schema upgrade did not restore SQLite foreign keys")
    else:
        raise RuntimeError(f"run schema does not support {engine.dialect.name!r}")


def _install_default_ask_fence(conn: sa.Connection) -> None:
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql(
            """CREATE TRIGGER IF NOT EXISTS trg_legacy_turn_blocked_by_default_ask
            BEFORE INSERT ON ne_agent_conversation_turns
            WHEN EXISTS (SELECT 1 FROM ne_agent_default_ask_activation
                         WHERE singleton = 1 AND wire_generation = 'default-ask/canonical-run/v1')
            BEGIN SELECT RAISE(ABORT, 'legacy Conversation attachment writer is retired'); END"""
        )
    elif conn.dialect.name == "postgresql":
        conn.exec_driver_sql(
            """CREATE OR REPLACE FUNCTION ne_block_legacy_turn_when_default_ask_active()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              PERFORM pg_advisory_xact_lock(56453543501007);
              IF EXISTS (SELECT 1 FROM ne_agent_default_ask_activation WHERE singleton = 1
                         AND wire_generation = 'default-ask/canonical-run/v1') THEN
                RAISE EXCEPTION 'legacy Conversation attachment writer is retired';
              END IF; RETURN NEW; END $$"""
        )
        conn.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_legacy_turn_blocked_by_default_ask "
            "ON ne_agent_conversation_turns"
        )
        conn.exec_driver_sql(
            "CREATE TRIGGER trg_legacy_turn_blocked_by_default_ask BEFORE INSERT ON "
            "ne_agent_conversation_turns FOR EACH ROW EXECUTE FUNCTION "
            "ne_block_legacy_turn_when_default_ask_active()"
        )


def _ensure_approval_join_governance_columns(conn: sa.Connection) -> None:
    """Bind park-time governance into the insert-only approval join (U2 review, F2; F1).

    Additive, idempotent upgrade in the ``_initialize_runtime_schema`` ALTER
    pattern: a pre-existing ``ne_agent_run_approval_joins`` table gains nullable
    ``tier``/``action`` columns (a fresh ``create_all`` already includes them).

    DELIBERATELY NO BACKFILL (Sol cross-model review, P1): an earlier revision
    copied tier/action from the mutable ``ne_approval_requests`` row, which
    would have BLESSED an already-tampered value as park-time truth — defeating
    the very guard the columns exist for, on exactly the rows that predate it.
    There is no authenticated append-only record of a legacy row's park-time
    tier/action, so legacy joins keep NULL and stay UNTRUSTED: ``_join`` and
    ``_require_parked_governance`` fail closed, every read/decide/resume on such
    a row refuses, and the owner intervenes (re-request). Every join parked
    after this upgrade carries the values written in the park transaction.
    """
    inspector = sa.inspect(conn)
    if not inspector.has_table("ne_agent_run_approval_joins"):
        return
    columns = {
        str(column["name"]) for column in inspector.get_columns("ne_agent_run_approval_joins")
    }
    if "tier" not in columns:
        conn.execute(sa.text("ALTER TABLE ne_agent_run_approval_joins ADD COLUMN tier INTEGER"))
    if "action" not in columns:
        conn.execute(sa.text("ALTER TABLE ne_agent_run_approval_joins ADD COLUMN action VARCHAR"))
    if "standing_lifetime_s" not in columns:
        # F1: the park-time policy-resolved standing-approval lifetime. Also
        # never backfilled, for the same reason and with a stronger fail-safe:
        # NULL means "this park recorded no policy lifetime", and the mint path
        # reads NULL as MINT NOTHING. A legacy park therefore behaves exactly
        # as it did before F1 — its decision dies with its attempt — instead of
        # inheriting a lifetime nobody declared for it.
        conn.execute(
            sa.text("ALTER TABLE ne_agent_run_approval_joins ADD COLUMN standing_lifetime_s FLOAT")
        )


def _ensure_protected_target_binding_columns(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)
    for table, _, _ in _PROTECTED_TABLES.values():
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if "target_binding" in columns:
            continue
        column_type = "JSONB" if conn.dialect.name == "postgresql" else "JSON"
        conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN target_binding {column_type}")


def _ensure_model_step_routing_columns(conn: sa.Connection) -> None:
    """Add the model step's routing columns without inventing a position for old rows.

    Both are NULLABLE and neither gets a DEFAULT. A step recorded before the
    columns existed genuinely does not know whether its model was the first
    thing attempted, and a ``DEFAULT 'primary'`` would answer "served as
    requested" on every one of them -- the exact silent, unearned reassurance
    these columns exist to eliminate. NULL projects as ``"unavailable"``.
    """

    existing = {
        str(column["name"])
        for column in sa.inspect(conn).get_columns(ne_agent_run_model_steps.name)
    }
    for name, sql_type in (("route_position", "VARCHAR"), ("fallback_from", "VARCHAR(256)")):
        if name not in existing:
            conn.exec_driver_sql(
                f"ALTER TABLE {ne_agent_run_model_steps.name} ADD COLUMN {name} {sql_type}"
            )


def _ensure_finalizer_schema_compatibility(conn: sa.Connection) -> None:
    """Add 010F's nullable lifecycle bindings without rewriting old intents."""

    inspector = sa.inspect(conn)
    table_columns: dict[str, tuple[str, bool, str]] = {
        "protected_domain": ("VARCHAR", False, "'protected_turn_content'"),
    }
    existing_claims = {
        str(column["name"]) for column in inspector.get_columns(ne_agent_run_claims.name)
    }
    for name, (sql_type, nullable, default) in table_columns.items():
        if name in existing_claims:
            continue
        null_sql = "" if nullable else " NOT NULL"
        conn.exec_driver_sql(
            f"ALTER TABLE {ne_agent_run_claims.name} ADD COLUMN {name} {sql_type}"
            f"{null_sql} DEFAULT {default}"
        )
    if conn.dialect.name == "postgresql":
        # 010E's composite FK admitted only ProtectedTurnContent.  010F's
        # discriminator trigger replaces it with the exact disjoint
        # ProtectedTurnContent/ProtectedSourceOutput target check.
        for constraint in sa.inspect(conn).get_foreign_keys(ne_agent_run_claims.name):
            if (
                constraint.get("referred_table") == ne_agent_protected_turn_contents.name
                and constraint.get("name")
                and constraint.get("constrained_columns")
                == [
                    "protected_ref",
                    "owner_principal_id",
                    "conversation_id",
                    "run_id",
                    "turn_id",
                    "turn_sequence",
                    "attachment_id",
                    "attempt_id",
                ]
            ):
                name = str(constraint["name"]).replace('"', '""')
                conn.exec_driver_sql(
                    f'ALTER TABLE {ne_agent_run_claims.name} DROP CONSTRAINT "{name}"'
                )
        checks = {
            str(constraint.get("name"))
            for constraint in sa.inspect(conn).get_check_constraints(ne_agent_run_claims.name)
        }
        if "ck_run_claim_protected_domain" not in checks:
            conn.exec_driver_sql(
                f"ALTER TABLE {ne_agent_run_claims.name} "
                "ADD CONSTRAINT ck_run_claim_protected_domain CHECK ("
                "protected_domain IN ('protected_turn_content','protected_source_output'))"
            )

    existing_outbox = {
        str(column["name"])
        for column in sa.inspect(conn).get_columns(ne_agent_run_lifecycle_outbox.name)
    }
    additions = {
        "target_hash": "VARCHAR(64)",
        "protected_output_kind": "VARCHAR",
        "protected_output_ref": "VARCHAR",
        "protected_output_hash": "VARCHAR(64)",
        "mapping_state": "VARCHAR",
        "envelope_hash": "VARCHAR(64)",
        "delivery_set_id": "VARCHAR",
        "delivery_set_hash": "VARCHAR(64)",
        "audit_receipt_set_hash": "VARCHAR(64)",
        "approval_set_hash": "VARCHAR(64)",
        "evidence_set_hash": "VARCHAR(64)",
        "verifier_set_hash": "VARCHAR(64)",
    }
    for name, sql_type in additions.items():
        if name not in existing_outbox:
            conn.exec_driver_sql(
                f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} ADD COLUMN {name} {sql_type}"
            )

    # POST_DEMO item 4: admit `approved_effect_complete` on an ALREADY-DEPLOYED
    # database. The table definition above carries the widened check, so a fresh
    # deployment is correct by construction; a live one still holds the narrow
    # constraint and would reject the new word outright.
    #
    # ORDER MATTERS AND IS SAFE IN ONLY ONE DIRECTION. This WIDENS: every row legal
    # under the old constraint stays legal under the new one, so it can be applied
    # while the old code is still running and nothing previously accepted becomes
    # illegal. The reverse — deploying the new code first — would have the new
    # finalizer write a word the live constraint refuses, and the run would fail to
    # terminalize: the exact defect item 4 exists to remove. This runs at boot,
    # inside the same schema-compatibility pass as every other upgrade here, so the
    # constraint is widened before the new finalizer can be asked for a terminal.
    #
    # Idempotent: the widened form is recognised and skipped on every later boot.
    #
    # POSTGRES ONLY, and that is a DECISION, not an oversight. A review raised the
    # SQLite path — an existing SQLite database keeps the narrow constraint and a
    # resume there would fail to terminalize. Checked rather than assumed: the
    # deployed runtime is `postgresql+psycopg`, and every SQLite database in this
    # tree is built fresh from the table definition above (which already carries the
    # widened check), so the gap reaches development databases only, fails LOUDLY as
    # an IntegrityError rather than silently, and is cleared by discarding a dev
    # file. Mirroring `_rebuild_sqlite_run_lifecycle_table` for the outbox would be
    # building a migration path for an installed base that does not exist.
    #
    # 🔴 MIXED-VERSION ROLLING UPGRADES — I REFUTED THIS AND I WAS WRONG.
    # A first review raised it: a new worker persists a word an older, still-running
    # worker's closed `_workflow_outcome()` rejects. I checked `docker ps` on the
    # demo box, saw one worker, and wrote that off. That generalised a single box to
    # the product, and an adversarial pass caught it with the chart in hand:
    #   * `deploy/helm/examples/values-production-ha.yaml` ships `ne-worker` at
    #     TWO replicas, with a comment stating N>1 is DBOS-safe and supported.
    #   * The worker Deployment declares no `strategy:` block, so Kubernetes
    #     defaults to RollingUpdate with maxSurge 25%, which rounds UP to 1. Even
    #     at one replica, every upgrade runs the new pod alongside the old one.
    # There is no single-worker window to rely on. The premise was false.
    #
    # WHAT ACTUALLY HAPPENS, stated so nobody has to rediscover it: during a
    # rollout a new worker can commit `approved_effect_complete` and DBOS recovery
    # can dispatch that run onto the old pod.
    #
    # ⚠️ CORRECTED after a second adversarial pass: I first wrote that the old pod
    # "raises `Run terminal publication carries an invalid outcome` and retries
    # until it drains." Both halves were wrong. The `agent.run.execute` step output
    # is MEMOIZED by DBOS and replayed rather than re-executed, so the likelier
    # seam is a pydantic ValidationError in
    # `RunWorkflowResult.model_validate_json` (`ne-worker/workflows.py:863`) — the
    # closed `outcome` Literal rejects the word before `_workflow_outcome` is ever
    # reached. And NEITHER seam retries.
    #
    # ⚠️ AND THAT SEAM IS STILL UNVERIFIED — this note said "it FAILS CLOSED and
    # LOUD", which `deploy/helm/README.md` has already retracted and this copy
    # survived it. A resumed run's terminal is composed under
    # `agent.run.approval.resume`, whose step (`run_approval_resume_workflow`)
    # returns a bare `str` and never validates `RunWorkflowResult` — so the old
    # pod may not fail loudly at all; it may recover the run and succeed
    # SILENTLY. What IS established: the durable record is already correct,
    # nothing is mis-stated, and there is no retry storm. Plan for an old pod
    # behaving unpredictably, not for a named exception.
    #
    # The clean remedy is a two-phase rollout: ship the READER widening (the
    # `_workflow_outcome` allowlist and this constraint) in one release, and the
    # WRITER (the finalizer choosing the new word) in the next, so no old pod ever
    # meets a word it cannot read. That is a release-sequencing decision, not a
    # code change, and it is owed the moment this runs anywhere with a rollout.
    if conn.dialect.name == "postgresql":
        # Structured as find-then-act rather than a loop with an early `break`,
        # because an adversarial pass caught what the loop form silently did: if NO
        # constraint by this name exists — a table predating it, or one an operator
        # dropped during an incident — the loop body never ran and the database
        # booted clean with NO outcome-shape CHECK AT ALL. The precedent this
        # mirrors, `ensure_continuation_status_compatibility`, handles exactly that:
        # DROP only when present, then ADD unconditionally. That constraint is
        # load-bearing beyond the ledger now, since `outcome` reaches the API wire.
        current = next(
            (
                constraint
                for constraint in sa.inspect(conn).get_check_constraints(
                    ne_agent_run_lifecycle_outbox.name
                )
                if str(constraint.get("name")) == "ck_run_lifecycle_outcome_shape"
            ),
            None,
        )
        if current is None or "approved_effect_complete" not in str(current.get("sqltext") or ""):
            if current is not None:
                conn.exec_driver_sql(
                    f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
                    'DROP CONSTRAINT "ck_run_lifecycle_outcome_shape"'
                )
            # NOT VALID here, and VALIDATE **outside this transaction** — see
            # `_validate_deferred_outcome_constraint`. The reason is the table.
            # A plain ADD CONSTRAINT takes ACCESS EXCLUSIVE and FULL-SCANS to
            # validate. `ne_agent_run_lifecycle_outbox` is append-only by
            # construction (`trg_run_lifecycle_outbox_guard`), holds one row per run
            # terminal and is never pruned, so it grows monotonically with lifetime
            # run volume — and this whole pass runs inside one advisory-locked
            # transaction, so that scan would hold ACCESS EXCLUSIVE across every
            # other guard installed here. On a deployment with real history NO RUN
            # COULD TERMINALIZE until boot finished.
            #
            # Because this is a pure WIDENING the split is free: ADD ... NOT VALID
            # updates the catalog without scanning. ⚠️ A FIRST ATTEMPT AT THIS FIX
            # WAS COSMETIC — it ran VALIDATE in this same transaction, which still
            # holds the ACCESS EXCLUSIVE taken by ADD CONSTRAINT until commit, so
            # the scan blocked writers exactly as before. Codex caught that. VALIDATE
            # now runs in its OWN transaction after this pass commits, where it takes
            # only SHARE UPDATE EXCLUSIVE and does not block inserts. Every existing
            # row is already legal under the wider constraint by construction, so
            # validation cannot fail. (An adversarial pass found the original; the
            # `ensure_continuation_status_compatibility` precedent scans a table
            # holding one row per approval PARK, not one per run, which is why it
            # got away with the plain form.)
            conn.exec_driver_sql(
                f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
                "ADD CONSTRAINT ck_run_lifecycle_outcome_shape CHECK ("
                "(terminal_status = 'completed' AND ("
                "(output_verdict = 'refused' AND outcome = 'refused') OR "
                "(output_verdict != 'refused' AND outcome IN "
                "('execution_complete','approved_effect_complete')))) OR "
                "(terminal_status = 'failed' AND outcome = 'failed') OR "
                "(terminal_status = 'cancelled' AND outcome = 'cancelled') OR "
                "(terminal_status = 'expired' AND outcome = 'expired') OR "
                "(terminal_status = 'context_exhausted' AND outcome = 'context_exhausted') OR "
                "(terminal_status = 'budget_exhausted' AND outcome = 'budget_exhausted')) NOT VALID"
            )


def _validate_deferred_outcome_constraint(engine: sa.Engine) -> None:
    """VALIDATE the widened outcome CHECK, in its OWN transaction. Postgres only.

    `ADD CONSTRAINT ... NOT VALID` holds ACCESS EXCLUSIVE until its transaction
    commits, so validating inside the boot pass would block every concurrent
    `commit_run_terminal_in_transaction` for the length of a full scan of an
    append-only, never-pruned table. Here it takes only SHARE UPDATE EXCLUSIVE and
    inserts proceed underneath it.

    Idempotent and safe to skip: `convalidated` is already true after the first
    successful run, and a NOT VALID constraint still ENFORCES every new row — only
    the historical scan is outstanding, and every historical row is legal by
    construction because this is a pure widening.
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        pending = conn.exec_driver_sql(
            "SELECT 1 FROM pg_constraint WHERE conname = "
            "'ck_run_lifecycle_outcome_shape' AND NOT convalidated"
        ).first()
        if pending is None:
            return
        conn.exec_driver_sql(
            f"ALTER TABLE {ne_agent_run_lifecycle_outbox.name} "
            'VALIDATE CONSTRAINT "ck_run_lifecycle_outcome_shape"'
        )


def _ensure_submission_schema_compatibility(conn: sa.Connection) -> None:
    """Upgrade the canonical Run lifecycle columns and retire legacy refusal state."""

    # These guards may have been installed by an older process.  Remove them
    # before the one-way REFUSED migration, then reinstall the current exact
    # definitions below in ``_install_run_guards``.
    if conn.dialect.name == "sqlite":
        for trigger in (
            "trg_agent_run_capability_immutable",
            "trg_agent_run_terminal_immutable",
            "trg_agent_run_transition_legal",
        ):
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
    elif conn.dialect.name == "postgresql":
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_run_immutable ON ne_agent_runs")

    inspector = sa.inspect(conn)
    columns = {column["name"] for column in inspector.get_columns(ne_agent_runs.name)}
    expected_columns = {column.name for column in ne_agent_runs.columns}
    unknown_columns = columns - expected_columns
    additive_columns = {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "output_verdict": "VARCHAR",
        "requested_model": "VARCHAR",
        "requested_model_lane": "VARCHAR",
        "agent_definition_hash": "VARCHAR(64)",
        "model_policy_id": "VARCHAR",
        "authorized_model": "VARCHAR",
        "authorized_model_lane": "VARCHAR",
        "model_authorization_receipt_hash": "VARCHAR(64)",
        "context_snapshot_hash": "VARCHAR(64)",
    }
    missing_nonadditive = expected_columns - columns - additive_columns.keys()
    if unknown_columns or missing_nonadditive:
        raise RuntimeError("legacy Run table shape cannot be migrated without data loss")
    for name, sql_type in additive_columns.items():
        if name not in columns:
            conn.exec_driver_sql(f"ALTER TABLE {ne_agent_runs.name} ADD COLUMN {name} {sql_type}")
    refused_with_claims = int(
        conn.execute(
            sa.select(sa.func.count())
            .select_from(
                ne_agent_runs.join(
                    ne_agent_run_claims,
                    ne_agent_run_claims.c.run_id == ne_agent_runs.c.run_id,
                )
            )
            .where(ne_agent_runs.c.status == "refused")
        ).scalar_one()
    )
    if refused_with_claims:
        raise RuntimeError("claim-bearing legacy refused Run cannot be migrated truthfully")
    conn.exec_driver_sql(
        f"UPDATE {ne_agent_runs.name} SET status = 'completed', output_verdict = 'refused' "
        "WHERE status = 'refused'"
    )
    if conn.dialect.name == "sqlite":
        if _sqlite_run_lifecycle_constraints_need_rebuild(conn):
            _rebuild_sqlite_run_lifecycle_table(conn)
        return
    if conn.dialect.name != "postgresql":
        return
    lifecycle_constraints = {
        "ck_agent_run_status",
        "ck_agent_run_terminal_time",
        "ck_agent_run_output_verdict",
        "ck_agent_run_requested_model_pair",
        "ck_agent_run_requested_model_lane",
    }
    for constraint in sa.inspect(conn).get_check_constraints(ne_agent_runs.name):
        if constraint.get("name") in lifecycle_constraints:
            name = str(constraint["name"]).replace('"', '""')
            conn.exec_driver_sql(f'ALTER TABLE {ne_agent_runs.name} DROP CONSTRAINT "{name}"')
    conn.exec_driver_sql(
        f"ALTER TABLE {ne_agent_runs.name} ADD CONSTRAINT ck_agent_run_status CHECK ("
        "status IN ('submitted','queued','running','waiting_approval','cancelling','completed',"
        "'failed','cancelled','expired','context_exhausted','budget_exhausted'))"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE {ne_agent_runs.name} ADD CONSTRAINT ck_agent_run_output_verdict CHECK ("
        "output_verdict IS NULL OR ("
        "status IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') AND "
        "output_verdict IN ('verified','grounded','refused','unavailable') AND "
        "(output_verdict = 'unavailable' OR status = 'completed')))"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE {ne_agent_runs.name} ADD CONSTRAINT ck_agent_run_terminal_time CHECK ("
        "(status IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') AND terminal_at IS NOT NULL) OR "
        "(status NOT IN ('completed','failed','cancelled','expired','context_exhausted',"
        "'budget_exhausted') AND terminal_at IS NULL))"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE {ne_agent_runs.name} ADD CONSTRAINT ck_agent_run_requested_model_pair "
        "CHECK ((requested_model IS NULL) = (requested_model_lane IS NULL))"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE {ne_agent_runs.name} ADD CONSTRAINT ck_agent_run_requested_model_lane "
        "CHECK (requested_model_lane IS NULL OR "
        "requested_model_lane IN ('frontier-api','private-vllm'))"
    )


def _sqlite_run_lifecycle_constraints_need_rebuild(conn: sa.Connection) -> bool:
    constraints = {
        str(constraint.get("name")): "".join(str(constraint.get("sqltext") or "").lower().split())
        for constraint in sa.inspect(conn).get_check_constraints(ne_agent_runs.name)
    }
    status = constraints.get("ck_agent_run_status")
    terminal = constraints.get("ck_agent_run_terminal_time")
    verdict = constraints.get("ck_agent_run_output_verdict")
    requested_pair = constraints.get("ck_agent_run_requested_model_pair")
    requested_lane = constraints.get("ck_agent_run_requested_model_lane")
    if (
        status is None
        or terminal is None
        or verdict is None
        or requested_pair is None
        or requested_lane is None
    ):
        return True
    return (
        "'refused'" in status
        or "'context_exhausted'" not in status
        or "'budget_exhausted'" not in status
        or "'context_exhausted'" not in terminal
        or "'budget_exhausted'" not in terminal
        or "output_verdict='unavailable'orstatus='completed'" not in verdict
    )


def _rebuild_sqlite_run_lifecycle_table(conn: sa.Connection) -> None:
    """Replace only the canonical Run table with current lifecycle constraints."""

    from ne.runtime.store import ne_agent_conversations

    preparer = conn.dialect.identifier_preparer
    temporary_name = "ne_agent_runs_lifecycle_next"
    temporary = preparer.quote(temporary_name)
    canonical = preparer.quote(ne_agent_runs.name)
    replacement_metadata = sa.MetaData()
    ne_agent_conversations.to_metadata(replacement_metadata)
    replacement = ne_agent_runs.to_metadata(
        replacement_metadata,
        name=temporary_name,
    )
    if (
        conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": temporary_name},
        ).first()
        is not None
    ):
        raise RuntimeError("stale Run lifecycle migration table requires operator recovery")
    conn.execute(sa.schema.CreateTable(replacement))
    column_names = [column.name for column in ne_agent_runs.columns]
    columns_sql = ", ".join(preparer.quote(name) for name in column_names)
    conn.exec_driver_sql(
        f"INSERT INTO {temporary} ({columns_sql}) SELECT {columns_sql} FROM {canonical}"
    )
    source_count = int(conn.exec_driver_sql(f"SELECT count(*) FROM {canonical}").scalar_one())
    target_count = int(conn.exec_driver_sql(f"SELECT count(*) FROM {temporary}").scalar_one())
    if source_count != target_count:
        raise RuntimeError("Run lifecycle migration row count changed")
    conn.exec_driver_sql(f"DROP TABLE {canonical}")
    conn.exec_driver_sql(f"ALTER TABLE {temporary} RENAME TO {canonical}")
    for index in sorted(ne_agent_runs.indexes, key=lambda value: str(value.name)):
        if index.name is None:
            raise RuntimeError("canonical Run index lacks an explicit name")
        index_name = preparer.quote(index.name)
        index_columns = ", ".join(preparer.quote(column.name) for column in index.columns)
        uniqueness = "UNIQUE " if index.unique else ""
        conn.exec_driver_sql(
            f"CREATE {uniqueness}INDEX {index_name} ON {canonical} ({index_columns})"
        )


def _ensure_evidence_set_schema_compatibility(conn: sa.Connection) -> None:
    """Remove the pre-set one-result-per-record constraint on Postgres.

    Fresh SQLite schemas are built from current metadata. Existing SQLite
    developer databases retain the old constraint and fail closed on a
    multi-record set rather than being destructively rebuilt at process boot.
    """

    if conn.dialect.name != "postgresql":
        return
    inspector = sa.inspect(conn)
    for constraint in inspector.get_unique_constraints(ne_agent_evidence_records.name):
        if constraint.get("column_names") == ["pending_result_id"] and constraint.get("name"):
            name = str(constraint["name"]).replace('"', '""')
            conn.exec_driver_sql(
                f'ALTER TABLE {ne_agent_evidence_records.name} DROP CONSTRAINT "{name}"'
            )


@dataclass(frozen=True)
class _LegacyProtectionSpec:
    """One legacy table's protected-ref trio and the plaintext it must have replaced."""

    table: str
    short: str
    #: (status, ref, hash) — all three present, or all three absent. Never a partial.
    trio: tuple[str, str, str]
    #: ref column -> the plaintext column that must be empty once that ref is set.
    plaintext: tuple[tuple[str, str], ...]

    def partial_predicate(self, row: str) -> str:
        present = " + ".join(
            f"(CASE WHEN {row}.{col} IS NULL THEN 0 ELSE 1 END)" for col in self.trio
        )
        return f"({present}) NOT IN (0, {len(self.trio)})"

    def plaintext_predicate(self, row: str) -> str:
        return " OR ".join(
            f"({row}.{ref} IS NOT NULL AND {row}.{col} IS NOT NULL AND {row}.{col} <> '')"
            for ref, col in self.plaintext
        )

    def plaintext_guard_sql(self, row: str) -> str:
        """plpgsql for the plaintext check, or nothing when this spec has no pairs."""
        if not self.plaintext:
            return ""
        return (
            f"IF {self.plaintext_predicate(row)} THEN "
            "RAISE EXCEPTION 'protected row still carries plaintext'; END IF;"
        )


#: A row that was never migrated has all-NULL trios and keeps its plaintext, so it
#: stays legal. What these guards forbid is the two half-states a crashed or partial
#: migration produces: a protected reference without its status/hash, and a
#: protected reference sitting beside plaintext that was supposed to be gone.
_LEGACY_PROTECTION_SPECS: tuple[_LegacyProtectionSpec, ...] = (
    _LegacyProtectionSpec(
        table="ne_agent_conversation_turns",
        short="conversation_turn",
        trio=("input_status", "protected_input_ref", "protected_input_hash"),
        plaintext=(
            ("protected_input_ref", "private_context"),
            ("protected_terminal_ref", "served_output"),
        ),
    ),
    _LegacyProtectionSpec(
        table="ne_agent_conversation_turns",
        short="conversation_turn_terminal",
        trio=("terminal_status", "protected_terminal_ref", "protected_terminal_hash"),
        plaintext=(),
    ),
    _LegacyProtectionSpec(
        table="ne_agent_session_messages",
        short="session_message",
        trio=("content_status", "protected_content_ref", "protected_content_hash"),
        plaintext=(("protected_content_ref", "payload"),),
    ),
)


def _install_run_guards(conn: sa.Connection) -> None:
    """Install database-side immutability guards on load-bearing receipt fields."""
    from ne.runtime.run_checkpoint import ne_agent_run_approval_resume_intents

    encrypted_specs = [
        (ne_agent_run_inputs, "run_input"),
        (ne_agent_evidence_locators, "evidence_locator"),
        *[(table, domain.value) for domain, (table, _, _) in _PROTECTED_TABLES.items()],
    ]
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_capability_immutable
            BEFORE UPDATE OF capability_snapshot_hash, capability_snapshot_receipt_id
            ON ne_agent_runs
            WHEN NEW.capability_snapshot_hash <> OLD.capability_snapshot_hash
              OR NEW.capability_snapshot_receipt_id <> OLD.capability_snapshot_receipt_id
            BEGIN SELECT RAISE(ABORT, 'Run capability snapshot is immutable'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_terminal_immutable
            BEFORE UPDATE ON ne_agent_runs
            WHEN OLD.status IN ('completed','failed','cancelled','expired',
                                'context_exhausted','budget_exhausted')
            BEGIN SELECT RAISE(ABORT, 'terminal Run is immutable'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_transition_legal
            BEFORE UPDATE OF status ON ne_agent_runs
            WHEN NEW.status <> OLD.status AND NOT (
              (OLD.status = 'submitted' AND NEW.status IN ('queued','cancelling','failed')) OR
              (OLD.status = 'queued' AND NEW.status IN ('running','cancelling','failed')) OR
              (OLD.status = 'running' AND NEW.status IN
                ('waiting_approval','completed','failed','cancelling','expired',
                 'context_exhausted','budget_exhausted')) OR
              (OLD.status = 'waiting_approval' AND NEW.status IN
                ('running','completed','failed','cancelling','expired',
                 'context_exhausted','budget_exhausted')) OR
              (OLD.status = 'cancelling' AND NEW.status IN ('cancelled','failed'))
            )
            BEGIN SELECT RAISE(ABORT, 'illegal Run state transition'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_delete_forbidden
            BEFORE DELETE ON ne_agent_runs
            BEGIN SELECT RAISE(ABORT, 'Run deletion is forbidden'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_refusal_has_no_claims
            BEFORE UPDATE OF status, output_verdict ON ne_agent_runs
            WHEN NEW.status = 'completed' AND NEW.output_verdict = 'refused'
              AND EXISTS (
                SELECT 1 FROM ne_agent_run_claims WHERE run_id = NEW.run_id
              )
            BEGIN SELECT RAISE(ABORT, 'refused completion must contain zero claims'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_output_verdict_shape_insert
            BEFORE INSERT ON ne_agent_runs
            WHEN NEW.output_verdict IS NOT NULL AND NOT (
              NEW.status IN ('completed','failed','cancelled','expired',
                             'context_exhausted','budget_exhausted')
              AND NEW.output_verdict IN ('verified','grounded','refused','unavailable')
              AND (NEW.output_verdict = 'unavailable' OR NEW.status = 'completed')
            )
            BEGIN SELECT RAISE(ABORT, 'Run output verdict does not match lifecycle state'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_output_verdict_shape_update
            BEFORE UPDATE OF status, output_verdict ON ne_agent_runs
            WHEN NEW.output_verdict IS NOT NULL AND NOT (
              NEW.status IN ('completed','failed','cancelled','expired',
                             'context_exhausted','budget_exhausted')
              AND NEW.output_verdict IN ('verified','grounded','refused','unavailable')
              AND (NEW.output_verdict = 'unavailable' OR NEW.status = 'completed')
            )
            BEGIN SELECT RAISE(ABORT, 'Run output verdict does not match lifecycle state'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_terminal_requires_outbox
            BEFORE UPDATE OF status ON ne_agent_runs
            WHEN NEW.status IN ('completed','failed','cancelled','expired',
                                'context_exhausted','budget_exhausted')
              AND OLD.status NOT IN ('completed','failed','cancelled','expired',
                                     'context_exhausted','budget_exhausted')
              AND NOT EXISTS (
                SELECT 1
                FROM ne_agent_run_lifecycle_outbox AS terminal_outbox
                JOIN ne_agent_run_events AS terminal_event
                  ON terminal_event.event_id = terminal_outbox.event_id
                 AND terminal_event.run_id = terminal_outbox.run_id
                 AND terminal_event.attempt_id = terminal_outbox.attempt_id
                WHERE terminal_outbox.run_id = NEW.run_id
                  AND terminal_outbox.terminal_status = NEW.status
                  AND terminal_outbox.output_verdict IS NEW.output_verdict
                  AND terminal_event.event_kind = 'run.terminal'
                  AND terminal_event.event_status = NEW.status
              )
            BEGIN SELECT RAISE(ABORT, 'terminal Run requires atomic lifecycle outbox'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_run_lifecycle_outbox_insert_guard
            BEFORE INSERT ON ne_agent_run_lifecycle_outbox
            WHEN NEW.claimed_at IS NOT NULL
              OR NEW.published_at IS NOT NULL
              OR NEW.audit_event_id IS NOT NULL
              OR NOT EXISTS (
                SELECT 1 FROM ne_agent_run_events
                WHERE event_id = NEW.event_id
                  AND run_id = NEW.run_id
                  AND attempt_id = NEW.attempt_id
                  AND event_kind = 'run.terminal'
                  AND event_status = NEW.terminal_status
              )
            BEGIN SELECT RAISE(ABORT, 'Run lifecycle outbox insert is not canonical'); END
            """
        )
        # Replace the prior-version guard so newly added finalizer bindings are
        # immutable on upgraded SQLite databases as well as fresh schemas.
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_run_lifecycle_outbox_update_guard")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_run_lifecycle_outbox_update_guard
            BEFORE UPDATE ON ne_agent_run_lifecycle_outbox
            WHEN OLD.published_at IS NOT NULL
              OR NEW.publication_id IS NOT OLD.publication_id
              OR NEW.run_id IS NOT OLD.run_id
              OR NEW.attempt_id IS NOT OLD.attempt_id
              OR NEW.event_id IS NOT OLD.event_id
              OR NEW.terminal_status IS NOT OLD.terminal_status
              OR NEW.output_verdict IS NOT OLD.output_verdict
              OR NEW.outcome IS NOT OLD.outcome
              OR NEW.terminal_intent_hash IS NOT OLD.terminal_intent_hash
              OR NEW.target_hash IS NOT OLD.target_hash
              OR NEW.protected_output_kind IS NOT OLD.protected_output_kind
              OR NEW.protected_output_ref IS NOT OLD.protected_output_ref
              OR NEW.protected_output_hash IS NOT OLD.protected_output_hash
              OR NEW.mapping_state IS NOT OLD.mapping_state
              OR NEW.envelope_hash IS NOT OLD.envelope_hash
              OR NEW.delivery_set_id IS NOT OLD.delivery_set_id
              OR NEW.delivery_set_hash IS NOT OLD.delivery_set_hash
              OR NEW.audit_receipt_set_hash IS NOT OLD.audit_receipt_set_hash
              OR NEW.approval_set_hash IS NOT OLD.approval_set_hash
              OR NEW.evidence_set_hash IS NOT OLD.evidence_set_hash
              OR NEW.verifier_set_hash IS NOT OLD.verifier_set_hash
              OR NEW.created_at IS NOT OLD.created_at
              OR ((NEW.published_at IS NULL) <> (NEW.audit_event_id IS NULL))
              OR (NEW.published_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM ne_agent_run_audit_receipts
                WHERE run_id = NEW.run_id
                  AND attempt_id = NEW.attempt_id
                  AND step_id = 'run-terminal-publication'
                  AND decision_kind = 'terminal'
                  AND audit_event_id = NEW.audit_event_id
                  AND config_hash = (
                    SELECT config_hash FROM ne_agent_runs WHERE run_id = NEW.run_id
                  )
              ))
            BEGIN SELECT RAISE(ABORT, 'Run lifecycle outbox intent is immutable'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_run_lifecycle_outbox_delete_forbidden
            BEFORE DELETE ON ne_agent_run_lifecycle_outbox
            BEGIN SELECT RAISE(ABORT, 'Run lifecycle outbox deletion is forbidden'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_claim_refusal_forbidden
            BEFORE INSERT ON ne_agent_run_claims
            WHEN EXISTS (
              SELECT 1 FROM ne_agent_runs
              WHERE run_id = NEW.run_id
                AND status = 'completed'
                AND output_verdict = 'refused'
            )
            BEGIN SELECT RAISE(ABORT, 'refused completion cannot acquire claims'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_claim_protected_target
            BEFORE INSERT ON ne_agent_run_claims
            WHEN (
              NEW.protected_domain NOT IN ('protected_turn_content', 'protected_source_output')
            ) OR (
              NEW.protected_domain = 'protected_turn_content' AND NOT EXISTS (
                SELECT 1 FROM ne_agent_protected_turn_contents
                WHERE turn_content_id = NEW.protected_ref
                  AND owner_principal_id = NEW.owner_principal_id
                  AND conversation_id = NEW.conversation_id
                  AND run_id = NEW.run_id
                  AND turn_id = NEW.turn_id
                  AND turn_sequence = NEW.turn_sequence
                  AND attachment_id = NEW.attachment_id
                  AND attempt_id = NEW.attempt_id
              )
            ) OR (
              NEW.protected_domain = 'protected_source_output' AND NOT EXISTS (
                SELECT 1 FROM ne_agent_protected_source_outputs
                WHERE protected_output_id = NEW.protected_ref
                  AND owner_principal_id = NEW.owner_principal_id
                  AND conversation_id = NEW.conversation_id
                  AND run_id = NEW.run_id
                  AND turn_id = NEW.turn_id
                  AND turn_sequence = NEW.turn_sequence
                  AND attachment_id = NEW.attachment_id
                  AND attempt_id = NEW.attempt_id
              )
            )
            BEGIN SELECT RAISE(ABORT, 'Run claim protected target is mismatched'); END
            """
        )
        for table, label in encrypted_specs:
            conn.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{label}_payload_immutable
                BEFORE UPDATE OF cipher_version, plaintext_hash, binding_hash,
                    payload_nonce, payload_ciphertext, owner_principal_id
                ON {table.name}
                WHEN NEW.cipher_version <> OLD.cipher_version
                  OR NEW.plaintext_hash <> OLD.plaintext_hash
                  OR NEW.binding_hash <> OLD.binding_hash
                  OR NEW.payload_nonce <> OLD.payload_nonce
                  OR NEW.payload_ciphertext <> OLD.payload_ciphertext
                  OR NEW.owner_principal_id <> OLD.owner_principal_id
                BEGIN SELECT RAISE(ABORT, 'private payload ciphertext is immutable'); END
                """
            )
        for table in (
            ne_agent_evidence_records,
            ne_agent_evidence_sets,
            ne_agent_run_events,
            ne_agent_delivery_receipts,
            ne_agent_run_claims,
            ne_agent_claim_verifier_receipts,
            ne_agent_run_claim_evidence,
            ne_agent_run_audit_receipts,
            ne_agent_run_turn_targets,
            ne_agent_run_history_attachments,
            ne_agent_run_finalizations,
            ne_agent_run_skill_activations,
            ne_agent_run_authenticated_contexts,
            ne_agent_run_execution_profiles,
            ne_agent_run_approval_resume_intents,
        ):
            trigger = f"trg_{table.name.removeprefix('ne_agent_')}_append_only"
            delete_trigger = f"{trigger}_delete"
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {delete_trigger}")
            for trigger_name, verb in (
                (trigger, "UPDATE"),
                (delete_trigger, "DELETE"),
            ):
                conn.exec_driver_sql(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE {verb} ON {table.name}
                    BEGIN SELECT RAISE(ABORT, 'Run receipt row is append-only'); END
                    """
                )
        for table, label in (
            (ne_agent_run_attempt_links, "attempt"),
            (ne_agent_run_retry_lineage, "retry"),
        ):
            message = (
                "Run attempt lineage is immutable"
                if label == "attempt"
                else "Run retry lineage is immutable"
            )
            for verb in ("UPDATE", "DELETE"):
                conn.exec_driver_sql(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS trg_run_{label}_{verb.lower()}_immutable
                    BEFORE {verb} ON {table.name}
                    BEGIN SELECT RAISE(ABORT, '{message}'); END
                    """
                )
        for spec in _LEGACY_PROTECTION_SPECS:
            for verb in ("INSERT", "UPDATE"):
                conn.exec_driver_sql(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS
                        trg_{spec.short}_{verb.lower()}_protected_shape
                    BEFORE {verb} ON {spec.table}
                    WHEN {spec.partial_predicate("NEW")}
                    BEGIN SELECT RAISE(ABORT, 'partial protected tuple'); END
                    """
                )
                if spec.plaintext:
                    conn.exec_driver_sql(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS
                            trg_{spec.short}_{verb.lower()}_protected_plaintext
                        BEFORE {verb} ON {spec.table}
                        WHEN {spec.plaintext_predicate("NEW")}
                        BEGIN SELECT RAISE(ABORT, 'protected row still carries plaintext'); END
                        """
                    )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_session_message_retirement_shape_insert
            BEFORE INSERT ON ne_agent_session_messages
            WHEN (
              (CASE WHEN NEW.legacy_transport_retired IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_generation IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_reason IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_fence IS NULL THEN 0 ELSE 1 END)
            ) NOT IN (0, 4)
            BEGIN SELECT RAISE(ABORT, 'partial legacy retirement tuple'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_session_message_retirement_shape_update
            BEFORE UPDATE ON ne_agent_session_messages
            WHEN (
              (CASE WHEN NEW.legacy_transport_retired IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_generation IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_reason IS NULL THEN 0 ELSE 1 END) +
              (CASE WHEN NEW.legacy_transport_fence IS NULL THEN 0 ELSE 1 END)
            ) NOT IN (0, 4)
            BEGIN SELECT RAISE(ABORT, 'partial legacy retirement tuple'); END
            """
        )
        conn.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_session_message_retirement_exact_insert
            BEFORE INSERT ON ne_agent_session_messages
            WHEN NEW.legacy_transport_retired IS NOT NULL AND NOT (
              NEW.legacy_transport_retired = 1
              AND NEW.legacy_transport_generation = '{LEGACY_TRANSPORT_GENERATION}'
              AND NEW.legacy_transport_reason = '{OWNERLESS_RETIREMENT_REASON}'
              AND length(NEW.legacy_transport_fence) = 64
              AND NEW.legacy_transport_fence NOT GLOB '*[^0-9a-f]*'
              AND NEW.payload = ''
              AND NEW.principal_id IS NULL
              AND NEW.trace_id IS NULL
              AND NEW.content_status IS NULL
              AND NEW.protected_content_ref IS NULL
              AND NEW.protected_content_hash IS NULL
            )
            BEGIN SELECT RAISE(ABORT, 'invalid legacy retirement receipt'); END
            """
        )
        conn.exec_driver_sql(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_session_message_retirement_exact_update
            BEFORE UPDATE ON ne_agent_session_messages
            WHEN NEW.legacy_transport_retired IS NOT NULL AND NOT (
              NEW.legacy_transport_retired = 1
              AND NEW.legacy_transport_generation = '{LEGACY_TRANSPORT_GENERATION}'
              AND NEW.legacy_transport_reason = '{OWNERLESS_RETIREMENT_REASON}'
              AND length(NEW.legacy_transport_fence) = 64
              AND NEW.legacy_transport_fence NOT GLOB '*[^0-9a-f]*'
              AND NEW.payload = ''
              AND NEW.principal_id IS NULL
              AND NEW.trace_id IS NULL
              AND NEW.content_status IS NULL
              AND NEW.protected_content_ref IS NULL
              AND NEW.protected_content_hash IS NULL
            )
            BEGIN SELECT RAISE(ABORT, 'invalid legacy retirement receipt'); END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_session_message_retirement_immutable
            BEFORE UPDATE ON ne_agent_session_messages
            WHEN OLD.legacy_transport_retired = 1 AND NOT (
              NEW.legacy_transport_retired IS OLD.legacy_transport_retired
              AND NEW.legacy_transport_generation IS OLD.legacy_transport_generation
              AND NEW.legacy_transport_reason IS OLD.legacy_transport_reason
              AND NEW.legacy_transport_fence IS OLD.legacy_transport_fence
              AND NEW.payload IS OLD.payload
              AND NEW.principal_id IS OLD.principal_id
              AND NEW.trace_id IS OLD.trace_id
              AND NEW.content_status IS OLD.content_status
              AND NEW.protected_content_ref IS OLD.protected_content_ref
              AND NEW.protected_content_hash IS OLD.protected_content_hash
            )
            BEGIN SELECT RAISE(ABORT, 'legacy retirement receipt is immutable'); END
            """
        )
        return
    if conn.dialect.name != "postgresql":
        raise RuntimeError(f"run guards do not support {conn.dialect.name!r}")
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_agent_run_capability_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.capability_snapshot_hash IS DISTINCT FROM OLD.capability_snapshot_hash
             OR NEW.capability_snapshot_receipt_id IS DISTINCT FROM
                OLD.capability_snapshot_receipt_id THEN
            RAISE EXCEPTION 'Run capability snapshot is immutable';
          END IF;
          IF OLD.status IN ('completed','failed','cancelled','expired',
                            'context_exhausted','budget_exhausted') THEN
            RAISE EXCEPTION 'terminal Run is immutable';
          END IF;
          IF NEW.status = 'completed' AND NEW.output_verdict = 'refused'
             AND EXISTS (SELECT 1 FROM ne_agent_run_claims WHERE run_id = NEW.run_id) THEN
            RAISE EXCEPTION 'refused completion must contain zero claims';
          END IF;
          IF NEW.output_verdict IS NOT NULL AND NOT (
             NEW.status IN ('completed','failed','cancelled','expired',
                            'context_exhausted','budget_exhausted')
             AND NEW.output_verdict IN ('verified','grounded','refused','unavailable')
             AND (NEW.output_verdict = 'unavailable' OR NEW.status = 'completed')
          ) THEN
            RAISE EXCEPTION 'Run output verdict does not match lifecycle state';
          END IF;
          IF NEW.status IN ('completed','failed','cancelled','expired',
                            'context_exhausted','budget_exhausted')
             AND OLD.status NOT IN ('completed','failed','cancelled','expired',
                                    'context_exhausted','budget_exhausted')
             AND NOT EXISTS (
               SELECT 1
               FROM ne_agent_run_lifecycle_outbox AS terminal_outbox
               JOIN ne_agent_run_events AS terminal_event
                 ON terminal_event.event_id = terminal_outbox.event_id
                AND terminal_event.run_id = terminal_outbox.run_id
                AND terminal_event.attempt_id = terminal_outbox.attempt_id
               WHERE terminal_outbox.run_id = NEW.run_id
                 AND terminal_outbox.terminal_status = NEW.status
                 AND terminal_outbox.output_verdict IS NOT DISTINCT FROM NEW.output_verdict
                 AND terminal_event.event_kind = 'run.terminal'
                 AND terminal_event.event_status = NEW.status
             ) THEN
            RAISE EXCEPTION 'terminal Run requires atomic lifecycle outbox';
          END IF;
          IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
             (OLD.status = 'submitted' AND NEW.status IN ('queued','cancelling','failed')) OR
             (OLD.status = 'queued' AND NEW.status IN ('running','cancelling','failed')) OR
             (OLD.status = 'running' AND NEW.status IN
               ('waiting_approval','completed','failed','cancelling','expired',
                'context_exhausted','budget_exhausted')) OR
             (OLD.status = 'waiting_approval' AND NEW.status IN
               ('running','completed','failed','cancelling','expired',
                'context_exhausted','budget_exhausted')) OR
             (OLD.status = 'cancelling' AND NEW.status IN ('cancelled','failed'))
          ) THEN
            RAISE EXCEPTION 'illegal Run state transition';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_run_immutable ON ne_agent_runs")
    conn.exec_driver_sql(
        """
        CREATE TRIGGER trg_agent_run_immutable BEFORE UPDATE ON ne_agent_runs
        FOR EACH ROW EXECUTE FUNCTION ne_reject_agent_run_capability_update()
        """
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_agent_run_delete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Run deletion is forbidden'; END $$
        """
    )
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_run_delete_forbidden ON ne_agent_runs")
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_agent_run_delete_forbidden BEFORE DELETE ON ne_agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION ne_reject_agent_run_delete()"
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_guard_run_lifecycle_outbox()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Run lifecycle outbox deletion is forbidden';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.claimed_at IS NOT NULL
               OR NEW.published_at IS NOT NULL
               OR NEW.audit_event_id IS NOT NULL
               OR NOT EXISTS (
                 SELECT 1 FROM ne_agent_run_events
                 WHERE event_id = NEW.event_id
                   AND run_id = NEW.run_id
                   AND attempt_id = NEW.attempt_id
                   AND event_kind = 'run.terminal'
                   AND event_status = NEW.terminal_status
               ) THEN
              RAISE EXCEPTION 'Run lifecycle outbox insert is not canonical';
            END IF;
            RETURN NEW;
          END IF;
          IF OLD.published_at IS NOT NULL
             OR NEW.publication_id IS DISTINCT FROM OLD.publication_id
             OR NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
             OR NEW.event_id IS DISTINCT FROM OLD.event_id
             OR NEW.terminal_status IS DISTINCT FROM OLD.terminal_status
             OR NEW.output_verdict IS DISTINCT FROM OLD.output_verdict
             OR NEW.outcome IS DISTINCT FROM OLD.outcome
             OR NEW.terminal_intent_hash IS DISTINCT FROM OLD.terminal_intent_hash
             OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
             OR NEW.protected_output_kind IS DISTINCT FROM OLD.protected_output_kind
             OR NEW.protected_output_ref IS DISTINCT FROM OLD.protected_output_ref
             OR NEW.protected_output_hash IS DISTINCT FROM OLD.protected_output_hash
             OR NEW.mapping_state IS DISTINCT FROM OLD.mapping_state
             OR NEW.envelope_hash IS DISTINCT FROM OLD.envelope_hash
             OR NEW.delivery_set_id IS DISTINCT FROM OLD.delivery_set_id
             OR NEW.delivery_set_hash IS DISTINCT FROM OLD.delivery_set_hash
             OR NEW.audit_receipt_set_hash IS DISTINCT FROM OLD.audit_receipt_set_hash
             OR NEW.approval_set_hash IS DISTINCT FROM OLD.approval_set_hash
             OR NEW.evidence_set_hash IS DISTINCT FROM OLD.evidence_set_hash
             OR NEW.verifier_set_hash IS DISTINCT FROM OLD.verifier_set_hash
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
             OR ((NEW.published_at IS NULL) <> (NEW.audit_event_id IS NULL))
             OR (NEW.published_at IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM ne_agent_run_audit_receipts
               WHERE run_id = NEW.run_id
                 AND attempt_id = NEW.attempt_id
                 AND step_id = 'run-terminal-publication'
                 AND decision_kind = 'terminal'
                 AND audit_event_id = NEW.audit_event_id
                 AND config_hash = (
                   SELECT config_hash FROM ne_agent_runs WHERE run_id = NEW.run_id
                 )
             )) THEN
            RAISE EXCEPTION 'Run lifecycle outbox intent is immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_run_lifecycle_outbox_guard ON ne_agent_run_lifecycle_outbox"
    )
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_run_lifecycle_outbox_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON ne_agent_run_lifecycle_outbox FOR EACH ROW "
        "EXECUTE FUNCTION ne_guard_run_lifecycle_outbox()"
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_claim_for_refused_run()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM ne_agent_runs
            WHERE run_id = NEW.run_id
              AND status = 'completed'
              AND output_verdict = 'refused'
          ) THEN
            RAISE EXCEPTION 'refused completion cannot acquire claims';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_agent_run_claim_refusal_forbidden ON ne_agent_run_claims"
    )
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_agent_run_claim_refusal_forbidden BEFORE INSERT "
        "ON ne_agent_run_claims FOR EACH ROW "
        "EXECUTE FUNCTION ne_reject_claim_for_refused_run()"
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_require_run_claim_protected_target()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.protected_domain NOT IN ('protected_turn_content', 'protected_source_output')
          OR (NEW.protected_domain = 'protected_turn_content' AND NOT EXISTS (
            SELECT 1 FROM ne_agent_protected_turn_contents
            WHERE turn_content_id = NEW.protected_ref
              AND owner_principal_id = NEW.owner_principal_id
              AND conversation_id = NEW.conversation_id
              AND run_id = NEW.run_id
              AND turn_id = NEW.turn_id
              AND turn_sequence = NEW.turn_sequence
              AND attachment_id = NEW.attachment_id
              AND attempt_id = NEW.attempt_id
          )) OR (NEW.protected_domain = 'protected_source_output' AND NOT EXISTS (
            SELECT 1 FROM ne_agent_protected_source_outputs
            WHERE protected_output_id = NEW.protected_ref
              AND owner_principal_id = NEW.owner_principal_id
              AND conversation_id = NEW.conversation_id
              AND run_id = NEW.run_id
              AND turn_id = NEW.turn_id
              AND turn_sequence = NEW.turn_sequence
              AND attachment_id = NEW.attachment_id
              AND attempt_id = NEW.attempt_id
          )) THEN
            RAISE EXCEPTION 'Run claim protected target is mismatched';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_agent_run_claim_protected_target ON ne_agent_run_claims"
    )
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_agent_run_claim_protected_target BEFORE INSERT "
        "ON ne_agent_run_claims FOR EACH ROW "
        "EXECUTE FUNCTION ne_require_run_claim_protected_target()"
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_private_payload_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.cipher_version IS DISTINCT FROM OLD.cipher_version
             OR NEW.plaintext_hash IS DISTINCT FROM OLD.plaintext_hash
             OR NEW.binding_hash IS DISTINCT FROM OLD.binding_hash
             OR NEW.payload_nonce IS DISTINCT FROM OLD.payload_nonce
             OR NEW.payload_ciphertext IS DISTINCT FROM OLD.payload_ciphertext
             OR NEW.owner_principal_id IS DISTINCT FROM OLD.owner_principal_id THEN
            RAISE EXCEPTION 'private payload ciphertext is immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    for table, label in encrypted_specs:
        trigger = f"trg_{label}_payload_immutable"
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger} ON {table.name}")
        conn.exec_driver_sql(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table.name} "
            "FOR EACH ROW EXECUTE FUNCTION ne_reject_private_payload_update()"
        )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_run_receipt_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Run receipt row is append-only'; END $$
        """
    )
    for table in (
        ne_agent_evidence_records,
        ne_agent_evidence_sets,
        ne_agent_run_events,
        ne_agent_delivery_receipts,
        ne_agent_run_claims,
        ne_agent_claim_verifier_receipts,
        ne_agent_run_claim_evidence,
        ne_agent_run_audit_receipts,
        ne_agent_run_turn_targets,
        ne_agent_run_history_attachments,
        ne_agent_run_finalizations,
        ne_agent_run_skill_activations,
        ne_agent_run_authenticated_contexts,
        ne_agent_run_execution_profiles,
        ne_agent_run_approval_resume_intents,
    ):
        trigger = f"trg_{table.name.removeprefix('ne_agent_')}_append_only"
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger} ON {table.name}")
        conn.exec_driver_sql(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table.name} "
            "FOR EACH ROW EXECUTE FUNCTION ne_reject_run_receipt_update()"
        )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION ne_reject_run_attempt_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Run attempt lineage is immutable'; END $$
        """
    )
    for table, trigger in (
        (ne_agent_run_attempt_links, "trg_run_attempt_immutable"),
        (ne_agent_run_retry_lineage, "trg_run_retry_immutable"),
    ):
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger} ON {table.name}")
        conn.exec_driver_sql(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table.name} "
            "FOR EACH ROW EXECUTE FUNCTION ne_reject_run_attempt_mutation()"
        )
    for spec in _LEGACY_PROTECTION_SPECS:
        function = f"ne_reject_{spec.short}_protected_shape"
        conn.exec_driver_sql(
            f"""
            CREATE OR REPLACE FUNCTION {function}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF {spec.partial_predicate("NEW")} THEN
                RAISE EXCEPTION 'partial protected tuple';
              END IF;
              {spec.plaintext_guard_sql("NEW")}
              RETURN NEW;
            END $$
            """
        )
        trigger = f"trg_{spec.short}_protected_shape"
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger} ON {spec.table}")
        conn.exec_driver_sql(
            f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {spec.table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    conn.exec_driver_sql(
        f"""
        CREATE OR REPLACE FUNCTION ne_reject_session_message_retirement_shape()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt_fields integer;
        BEGIN
          receipt_fields :=
            (CASE WHEN NEW.legacy_transport_retired IS NULL THEN 0 ELSE 1 END) +
            (CASE WHEN NEW.legacy_transport_generation IS NULL THEN 0 ELSE 1 END) +
            (CASE WHEN NEW.legacy_transport_reason IS NULL THEN 0 ELSE 1 END) +
            (CASE WHEN NEW.legacy_transport_fence IS NULL THEN 0 ELSE 1 END);
          IF receipt_fields NOT IN (0, 4) THEN
            RAISE EXCEPTION 'partial legacy retirement tuple';
          END IF;
          IF NEW.legacy_transport_retired IS NOT NULL AND NOT (
            NEW.legacy_transport_retired IS TRUE
            AND NEW.legacy_transport_generation = '{LEGACY_TRANSPORT_GENERATION}'
            AND NEW.legacy_transport_reason = '{OWNERLESS_RETIREMENT_REASON}'
            AND NEW.legacy_transport_fence ~ '^[0-9a-f]{{64}}$'
            AND NEW.payload = ''
            AND NEW.principal_id IS NULL
            AND NEW.trace_id IS NULL
            AND NEW.content_status IS NULL
            AND NEW.protected_content_ref IS NULL
            AND NEW.protected_content_hash IS NULL
          ) THEN
            RAISE EXCEPTION 'invalid legacy retirement receipt';
          END IF;
          IF TG_OP = 'UPDATE' AND OLD.legacy_transport_retired IS TRUE AND NOT (
            NEW.legacy_transport_retired IS NOT DISTINCT FROM OLD.legacy_transport_retired
            AND NEW.legacy_transport_generation IS NOT DISTINCT FROM OLD.legacy_transport_generation
            AND NEW.legacy_transport_reason IS NOT DISTINCT FROM OLD.legacy_transport_reason
            AND NEW.legacy_transport_fence IS NOT DISTINCT FROM OLD.legacy_transport_fence
            AND NEW.payload IS NOT DISTINCT FROM OLD.payload
            AND NEW.principal_id IS NOT DISTINCT FROM OLD.principal_id
            AND NEW.trace_id IS NOT DISTINCT FROM OLD.trace_id
            AND NEW.content_status IS NOT DISTINCT FROM OLD.content_status
            AND NEW.protected_content_ref IS NOT DISTINCT FROM OLD.protected_content_ref
            AND NEW.protected_content_hash IS NOT DISTINCT FROM OLD.protected_content_hash
          ) THEN
            RAISE EXCEPTION 'legacy retirement receipt is immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_session_message_retirement_shape ON ne_agent_session_messages"
    )
    conn.exec_driver_sql(
        "CREATE TRIGGER trg_session_message_retirement_shape BEFORE INSERT OR UPDATE "
        "ON ne_agent_session_messages FOR EACH ROW "
        "EXECUTE FUNCTION ne_reject_session_message_retirement_shape()"
    )


class AgentRunStore:
    """Atomic dormant resource store; authorization remains a caller responsibility."""

    def __init__(
        self,
        engine: sa.Engine,
        cipher: RunPrivatePayloadCipher,
        *,
        offload_db_io: bool | None = None,
    ) -> None:
        self._engine = engine
        self._cipher = cipher
        self._offload = default_offload(engine) if offload_db_io is None else offload_db_io

    async def _call_db(self, function: Any, *args: Any) -> Any:
        """Run a connection-owning internal primitive with this store's policy."""
        return await call_db(self._offload, function, *args)

    async def activate_default_ask(self, wire_generation: str) -> None:
        """Install the database-visible one-way fence before routing canonical Ask."""
        if wire_generation != "default-ask/canonical-run/v1":
            raise ValueError("unsupported default Ask generation")
        await call_db(self._offload, self._activate_default_ask_sync, wire_generation)

    def activate_default_ask_at_boot(self, wire_generation: str) -> None:
        """Synchronous composition-root form; call before serving requests."""
        self._activate_default_ask_sync(wire_generation)

    def _activate_default_ask_sync(self, wire_generation: str) -> None:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(56453543501007)")
            existing = conn.execute(sa.select(ne_agent_default_ask_activation)).one_or_none()
            if existing is not None:
                if str(existing.wire_generation) != wire_generation:
                    raise RuntimeError("default Ask generation is already pinned")
                return
            conn.execute(
                ne_agent_default_ask_activation.insert().values(
                    singleton=1, wire_generation=wire_generation, activated_at=now
                )
            )

    async def admit_default_ask(self, command: Any) -> Any:
        return await call_db(self._offload, self._admit_default_ask_sync, command)

    def _admit_default_ask_sync(self, command: Any) -> Any:
        """Atomically admit one canonical Ask without touching the legacy turn table."""
        from ne.runtime.store import ne_agent_conversations

        start = command.start
        self._validate_submission_command(start)
        _require_nonempty(
            turn_id=command.turn_id, attachment_id=command.attachment_id, trace_id=command.trace_id
        )
        if command.activation_wire_generation != "default-ask/canonical-run/v1":
            raise RunSubmissionConflictError("default Ask generation changed")
        now = datetime.now(UTC)
        run_id, attempt_id, enqueue_id, input_id = _id(), _id(), _id(), _id()
        try:
            with self._engine.begin() as conn:
                fence = conn.execute(
                    sa.select(ne_agent_default_ask_activation.c.wire_generation)
                    .where(ne_agent_default_ask_activation.c.singleton == 1)
                    .with_for_update()
                ).scalar_one_or_none()
                if fence != command.activation_wire_generation:
                    raise RunSubmissionConflictError("default Ask activation fence is not active")
                replay = self._submission_replay(conn, start, "start")
                if replay is not None:
                    target_row = conn.execute(
                        sa.select(ne_agent_run_turn_targets).where(
                            ne_agent_run_turn_targets.c.run_id == replay.run_id
                        )
                    ).one()
                    return self._default_ask_result(replay, target_row, start)
                parent = conn.execute(
                    sa.select(ne_agent_conversations.c.last_sequence)
                    .where(
                        ne_agent_conversations.c.conversation_id == start.conversation_id,
                        ne_agent_conversations.c.owner_principal_id == start.owner_principal_id,
                        ne_agent_conversations.c.department == start.department,
                        ne_agent_conversations.c.agent_name == start.conversation_agent_name,
                    )
                    .with_for_update()
                ).one_or_none()
                if parent is None:
                    raise RunSubmissionNotFoundError
                # A concurrent identical admission may have committed while
                # this transaction waited for the Conversation lock. Recheck
                # the durable idempotency receipt before interpreting the now-
                # advanced Conversation version as a conflicting request.
                replay = self._submission_replay(conn, start, "start")
                if replay is not None:
                    target_row = conn.execute(
                        sa.select(ne_agent_run_turn_targets).where(
                            ne_agent_run_turn_targets.c.run_id == replay.run_id
                        )
                    ).one()
                    return self._default_ask_result(replay, target_row, start)
                if int(parent.last_sequence) != start.expected_version:
                    raise RunSubmissionConflictError("stale Conversation version")
                sequence = int(parent.last_sequence) + 1
                envelope = self._cipher.encrypt_for_target(
                    domain=PrivatePayloadDomain.RUN_INPUT,
                    row_id=input_id,
                    owner_principal_id=start.owner_principal_id,
                    binding=(
                        start.conversation_id,
                        run_id,
                        str(sequence),
                        start.agent_id,
                        str(start.agent_version),
                    ),
                    plaintext=start.input_plaintext,
                )
                conn.execute(
                    ne_agent_runs.insert().values(
                        run_id=run_id,
                        conversation_id=start.conversation_id,
                        owner_principal_id=start.owner_principal_id,
                        department=start.department,
                        conversation_agent_name=start.conversation_agent_name,
                        agent_id=start.agent_id,
                        agent_version=start.agent_version,
                        strategy=str(start.strategy),
                        config_hash=start.config_hash,
                        capability_snapshot_hash=start.capability_snapshot_hash,
                        capability_snapshot_receipt_id=start.capability_snapshot_receipt_id,
                        agent_definition_hash=getattr(start, "agent_definition_hash", None),
                        model_policy_id=getattr(start, "model_policy_id", None),
                        authorized_model=getattr(start, "authorized_model", None),
                        authorized_model_lane=(
                            None
                            if getattr(start, "authorized_model_lane", None) is None
                            else str(start.authorized_model_lane)
                        ),
                        model_authorization_receipt_hash=getattr(
                            start, "model_authorization_receipt_hash", None
                        ),
                        requested_model=getattr(start, "requested_model", None),
                        requested_model_lane=(
                            None
                            if getattr(start, "requested_model_lane", None) is None
                            else str(start.requested_model_lane)
                        ),
                        sequence=sequence,
                        display_label=(
                            f"Run {sequence} · {str(start.strategy).title()} · {now:%H:%M}"
                        ),
                        status=RunState.SUBMITTED.value,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        terminal_at=None,
                    )
                )
                boundary_detail = (
                    self._insert_model_boundary_receipt(
                        conn,
                        command=start,
                        run_id=run_id,
                        sequence=sequence,
                        now=now,
                    )
                    if getattr(start, "model_policy_id", None) is not None
                    else None
                )
                conn.execute(
                    ne_agent_run_inputs.insert().values(
                        input_id=input_id,
                        run_id=run_id,
                        owner_principal_id=start.owner_principal_id,
                        created_at=now,
                        **_envelope_values(envelope),
                    )
                )
                self._insert_authenticated_context(
                    conn,
                    run_id=run_id,
                    owner_principal_id=start.owner_principal_id,
                    context=start.authenticated_context,
                    now=now,
                )
                self._insert_attempt_event_enqueue(
                    conn,
                    command=start,
                    action="start",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    predecessor_attempt_id=None,
                    attempt_number=1,
                    version=1,
                    status=RunState.SUBMITTED,
                    enqueue_id=enqueue_id,
                    now=now,
                )
                if boundary_detail is not None:
                    self._append_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_kind="model.changed",
                        event_status="changed",
                        safe_detail=boundary_detail,
                        step_id="model-boundary",
                    )
                conn.execute(
                    ne_agent_run_turn_targets.insert().values(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        conversation_id=start.conversation_id,
                        turn_id=command.turn_id,
                        turn_sequence=sequence,
                        attachment_id=command.attachment_id,
                        created_at=now,
                    )
                )
                updated = conn.execute(
                    ne_agent_conversations.update()
                    .where(
                        ne_agent_conversations.c.conversation_id == start.conversation_id,
                        ne_agent_conversations.c.owner_principal_id == start.owner_principal_id,
                        ne_agent_conversations.c.last_sequence == start.expected_version,
                    )
                    .values(last_sequence=sequence)
                )
                if updated.rowcount != 1:
                    raise RunSubmissionConflictError("Conversation sequence changed")
                target_row = conn.execute(
                    sa.select(ne_agent_run_turn_targets).where(
                        ne_agent_run_turn_targets.c.run_id == run_id
                    )
                ).one()
        except IntegrityError as exc:
            replay = self._resolve_submission_race(start, "start", exc)
            with self._engine.connect() as conn:
                target_row = conn.execute(
                    sa.select(ne_agent_run_turn_targets).where(
                        ne_agent_run_turn_targets.c.run_id == replay.run_id
                    )
                ).one()
            return self._default_ask_result(replay, target_row, start)
        admission = AcceptedRunAdmission("start", run_id, attempt_id, 1, "submitted", enqueue_id)
        return self._default_ask_result(admission, target_row, start)

    @staticmethod
    def _default_ask_result(admission: AcceptedRunAdmission, row: Any, start: Any) -> Any:
        from ne.api.default_ask_submission import DefaultAskAccepted
        from ne.api.run_submission import AcceptedRunSubmission, RunSubmissionStatus
        from ne.runtime.run_finalizer import RunTurnTarget
        from ne.runtime.run_workflow import RunWorkflowRequest

        target = RunTurnTarget(
            conversation_id=str(row.conversation_id),
            run_id=admission.run_id,
            turn_id=str(row.turn_id),
            turn_sequence=int(row.turn_sequence),
            attachment_id=str(row.attachment_id),
            attempt_id=admission.attempt_id,
        )
        submission = AcceptedRunSubmission(
            action="start",
            run_id=admission.run_id,
            attempt_id=admission.attempt_id,
            version=admission.version,
            status=cast(RunSubmissionStatus, admission.status),
            enqueue_id=admission.enqueue_id,
            replayed=admission.replayed,
        )
        return DefaultAskAccepted(
            submission=submission,
            workflow_request=RunWorkflowRequest(
                dispatch_id=admission.enqueue_id,
                run_id=admission.run_id,
                owner_principal_id=start.owner_principal_id,
                config_hash=start.config_hash,
                target=target,
                authenticated_context=start.authenticated_context,
            ),
        )

    async def create_run(
        self,
        *,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        conversation_agent_name: str,
        agent_id: str,
        agent_version: int,
        strategy: str,
        config_hash: str,
        capability_snapshot_hash: str,
        capability_snapshot_receipt_id: str,
        input_plaintext: bytes,
    ) -> AgentRun:
        return await call_db(
            self._offload,
            self._create_run_sync,
            conversation_id,
            owner_principal_id,
            department,
            conversation_agent_name,
            agent_id,
            agent_version,
            strategy,
            config_hash,
            capability_snapshot_hash,
            capability_snapshot_receipt_id,
            input_plaintext,
        )

    def _create_run_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        conversation_agent_name: str,
        agent_id: str,
        agent_version: int,
        strategy: str,
        config_hash: str,
        capability_snapshot_hash: str,
        capability_snapshot_receipt_id: str,
        input_plaintext: bytes,
    ) -> AgentRun:
        _require_nonempty(
            conversation_id=conversation_id,
            owner_principal_id=owner_principal_id,
            department=department,
            conversation_agent_name=conversation_agent_name,
            agent_id=agent_id,
            strategy=strategy,
            capability_snapshot_receipt_id=capability_snapshot_receipt_id,
        )
        _require_sha256("config_hash", config_hash)
        _require_sha256("capability_snapshot_hash", capability_snapshot_hash)
        if agent_version < 1:
            raise ValueError("agent_version must be positive")
        run_id = _id()
        input_id = _id()
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            # Serialize sequence allocation on the parent Conversation row.  This also
            # proves the Run belongs to an existing 007 identity without duplicating it.
            from ne.runtime.store import ne_agent_conversations

            parent = conn.execute(
                sa.select(ne_agent_conversations.c.last_sequence)
                .where(
                    ne_agent_conversations.c.conversation_id == conversation_id,
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                    ne_agent_conversations.c.agent_name == conversation_agent_name,
                )
                .with_for_update()
            ).one_or_none()
            if parent is None:
                raise LookupError("unknown Conversation")
            latest_run_sequence = conn.execute(
                sa.select(sa.func.coalesce(sa.func.max(ne_agent_runs.c.sequence), 0)).where(
                    ne_agent_runs.c.conversation_id == conversation_id
                )
            ).scalar_one()
            sequence = int(latest_run_sequence) + 1
            envelope = self._cipher.encrypt_for_target(
                domain=PrivatePayloadDomain.RUN_INPUT,
                row_id=input_id,
                owner_principal_id=owner_principal_id,
                binding=(conversation_id, run_id, str(sequence), agent_id, str(agent_version)),
                plaintext=input_plaintext,
            )
            conn.execute(
                ne_agent_runs.insert().values(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    owner_principal_id=owner_principal_id,
                    department=department,
                    conversation_agent_name=conversation_agent_name,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    strategy=strategy,
                    config_hash=config_hash,
                    capability_snapshot_hash=capability_snapshot_hash,
                    capability_snapshot_receipt_id=capability_snapshot_receipt_id,
                    sequence=sequence,
                    display_label=f"Run {sequence} · {strategy.title()} · {now:%H:%M}",
                    status=RunState.SUBMITTED.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    terminal_at=None,
                )
            )
            conn.execute(
                ne_agent_run_inputs.insert().values(
                    input_id=input_id,
                    run_id=run_id,
                    owner_principal_id=owner_principal_id,
                    created_at=now,
                    **_envelope_values(envelope),
                )
            )
            row = conn.execute(_runs_with_lineage().where(ne_agent_runs.c.run_id == run_id)).one()
        return _run(row)

    async def admit_start(self, command: Any) -> AcceptedRunAdmission:
        return await call_db(self._offload, self._admit_start_sync, command)

    def _admit_start_sync(self, command: Any) -> AcceptedRunAdmission:
        self._validate_submission_command(command)
        now = datetime.now(UTC)
        run_id, attempt_id, enqueue_id = _id(), _id(), _id()
        input_id = _id()
        try:
            with self._engine.begin() as conn:
                replay = self._submission_replay(conn, command, "start")
                if replay is not None:
                    return replay
                from ne.runtime.store import ne_agent_conversations

                parent = conn.execute(
                    sa.select(ne_agent_conversations.c.last_sequence)
                    .where(
                        ne_agent_conversations.c.conversation_id == command.conversation_id,
                        ne_agent_conversations.c.owner_principal_id == command.owner_principal_id,
                        ne_agent_conversations.c.department == command.department,
                        ne_agent_conversations.c.agent_name == command.conversation_agent_name,
                    )
                    .with_for_update()
                ).one_or_none()
                if parent is None:
                    raise RunSubmissionNotFoundError
                # Recheck after the parent lock for the same convergence reason
                # as canonical default Ask: a duplicate can commit while this
                # transaction waits, advancing the Conversation version.
                replay = self._submission_replay(conn, command, "start")
                if replay is not None:
                    return replay
                if int(parent.last_sequence) != command.expected_version:
                    raise RunSubmissionConflictError("stale Conversation version")
                sequence = (
                    int(
                        conn.execute(
                            sa.select(
                                sa.func.coalesce(sa.func.max(ne_agent_runs.c.sequence), 0)
                            ).where(ne_agent_runs.c.conversation_id == command.conversation_id)
                        ).scalar_one()
                    )
                    + 1
                )
                envelope = self._cipher.encrypt_for_target(
                    domain=PrivatePayloadDomain.RUN_INPUT,
                    row_id=input_id,
                    owner_principal_id=command.owner_principal_id,
                    binding=(
                        command.conversation_id,
                        run_id,
                        str(sequence),
                        command.agent_id,
                        str(command.agent_version),
                    ),
                    plaintext=command.input_plaintext,
                )
                conn.execute(
                    ne_agent_runs.insert().values(
                        run_id=run_id,
                        conversation_id=command.conversation_id,
                        owner_principal_id=command.owner_principal_id,
                        department=command.department,
                        conversation_agent_name=command.conversation_agent_name,
                        agent_id=command.agent_id,
                        agent_version=command.agent_version,
                        strategy=str(command.strategy),
                        config_hash=command.config_hash,
                        capability_snapshot_hash=command.capability_snapshot_hash,
                        capability_snapshot_receipt_id=command.capability_snapshot_receipt_id,
                        agent_definition_hash=getattr(command, "agent_definition_hash", None),
                        model_policy_id=getattr(command, "model_policy_id", None),
                        authorized_model=getattr(command, "authorized_model", None),
                        authorized_model_lane=(
                            None
                            if getattr(command, "authorized_model_lane", None) is None
                            else str(command.authorized_model_lane)
                        ),
                        model_authorization_receipt_hash=getattr(
                            command, "model_authorization_receipt_hash", None
                        ),
                        requested_model=getattr(command, "requested_model", None),
                        requested_model_lane=(
                            None
                            if getattr(command, "requested_model_lane", None) is None
                            else str(command.requested_model_lane)
                        ),
                        sequence=sequence,
                        display_label=(
                            f"Run {sequence} · {str(command.strategy).title()} · {now:%H:%M}"
                        ),
                        status=RunState.SUBMITTED.value,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        terminal_at=None,
                    )
                )
                boundary_detail = (
                    self._insert_model_boundary_receipt(
                        conn,
                        command=command,
                        run_id=run_id,
                        sequence=sequence,
                        now=now,
                    )
                    if getattr(command, "model_policy_id", None) is not None
                    else None
                )
                conn.execute(
                    ne_agent_run_inputs.insert().values(
                        input_id=input_id,
                        run_id=run_id,
                        owner_principal_id=command.owner_principal_id,
                        created_at=now,
                        **_envelope_values(envelope),
                    )
                )
                self._insert_authenticated_context(
                    conn,
                    run_id=run_id,
                    owner_principal_id=command.owner_principal_id,
                    context=command.authenticated_context,
                    now=now,
                )
                self._insert_attempt_event_enqueue(
                    conn,
                    command=command,
                    action="start",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    predecessor_attempt_id=None,
                    attempt_number=1,
                    version=1,
                    status=RunState.SUBMITTED,
                    enqueue_id=enqueue_id,
                    now=now,
                )
                conn.execute(
                    ne_agent_run_turn_targets.insert().values(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        conversation_id=command.conversation_id,
                        turn_id=run_id,
                        turn_sequence=sequence,
                        attachment_id=run_id,
                        created_at=now,
                    )
                )
                if boundary_detail is not None:
                    self._append_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_kind="model.changed",
                        event_status="changed",
                        safe_detail=boundary_detail,
                        step_id="model-boundary",
                    )
                advanced = conn.execute(
                    ne_agent_conversations.update()
                    .where(
                        ne_agent_conversations.c.conversation_id == command.conversation_id,
                        ne_agent_conversations.c.owner_principal_id == command.owner_principal_id,
                        ne_agent_conversations.c.last_sequence == command.expected_version,
                    )
                    .values(last_sequence=sequence)
                )
                if advanced.rowcount != 1:
                    raise RunSubmissionConflictError("Conversation sequence changed")
        except IntegrityError as exc:
            return self._resolve_submission_race(command, "start", exc)
        return AcceptedRunAdmission("start", run_id, attempt_id, 1, "submitted", enqueue_id)

    def _insert_model_boundary_receipt(
        self,
        conn: sa.Connection,
        *,
        command: Any,
        run_id: str,
        sequence: int,
        now: datetime,
    ) -> dict[str, object]:
        """Freeze the safe boundary and exact pre-message context in the Run transaction."""
        previous = conn.execute(
            sa.select(ne_agent_runs)
            .where(
                ne_agent_runs.c.conversation_id == command.conversation_id,
                ne_agent_runs.c.owner_principal_id == command.owner_principal_id,
                ne_agent_runs.c.department == command.department,
                ne_agent_runs.c.conversation_agent_name == command.conversation_agent_name,
                ne_agent_runs.c.agent_id == command.agent_id,
                ne_agent_runs.c.sequence < sequence,
            )
            .order_by(ne_agent_runs.c.sequence.desc())
            .limit(1)
        ).one_or_none()
        if previous is not None and str(previous.status) not in {
            state.value for state in _TERMINAL_STATES
        }:
            raise RunSubmissionConflictError("model change has no safe between-Run boundary")

        component_rows = list(
            conn.execute(
                sa.select(ne_agent_run_history_attachments)
                .join(
                    ne_agent_runs,
                    ne_agent_runs.c.run_id == ne_agent_run_history_attachments.c.run_id,
                )
                .where(
                    ne_agent_runs.c.conversation_id == command.conversation_id,
                    ne_agent_runs.c.owner_principal_id == command.owner_principal_id,
                    ne_agent_runs.c.department == command.department,
                    ne_agent_runs.c.conversation_agent_name == command.conversation_agent_name,
                    ne_agent_runs.c.agent_id == command.agent_id,
                    ne_agent_runs.c.sequence < sequence,
                )
                .order_by(ne_agent_run_history_attachments.c.turn_sequence)
            ).all()
        )
        component_ids = [
            context_component_id(
                run_id=str(row.run_id),
                attempt_id=str(row.attempt_id),
                turn_id=str(row.turn_id),
                turn_sequence=int(row.turn_sequence),
                attachment_id=str(row.attachment_id),
                protected_hash=str(row.protected_hash),
                content_status=str(row.content_status),
            )[1]
            for row in component_rows
        ]
        snapshot_hash = context_snapshot_hash(
            agent_id=command.agent_id,
            conversation_id=command.conversation_id,
            component_ids=component_ids,
        )
        conn.execute(
            ne_agent_runs.update()
            .where(ne_agent_runs.c.run_id == run_id)
            .values(context_snapshot_hash=snapshot_hash)
        )
        current = conn.execute(
            sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id)
        ).one()
        try:
            turns = self._materialize_prior_turns_in_transaction(conn, current)
        except ProtectedTurnUnavailable as exc:
            raise RunSubmissionConflictError("context-window fit is unknown") from exc
        history_bytes = sum(len(prompt) + len(output) for prompt, output in turns)
        estimated_tokens = (
            history_bytes + len(command.input_plaintext) + CONTINUITY_PROMPT_RESERVE_TOKENS
        )
        if estimated_tokens > int(command.target_input_budget):
            raise RunSubmissionConflictError("context requires unavailable governed compaction")

        before_served = None
        if previous is not None:
            before_served = conn.execute(
                sa.select(ne_agent_run_model_steps.c.served_model)
                .where(ne_agent_run_model_steps.c.run_id == str(previous.run_id))
                .order_by(ne_agent_run_model_steps.c.ordinal.desc())
                .limit(1)
            ).scalar_one_or_none()
        after_requested = getattr(command, "requested_model", None)
        cache_impact = (
            "unknown"
            if previous is not None and before_served is None
            else "reuse_possible"
            if before_served == command.authorized_model
            else "reset_expected"
        )
        receipt_id = f"model-boundary:v1:{command.request_hash}"
        before_config_hash = None if previous is None else str(previous.config_hash)
        detail: dict[str, object] = {
            "actor": command.owner_principal_id,
            "safe_boundary": "between-runs",
            "model_policy_id": command.model_policy_id,
            "before_requested_model": None if previous is None else previous.requested_model,
            "before_served_model": before_served,
            "after_requested_model": after_requested,
            "after_authorized_model": command.authorized_model,
            "after_authorized_lane": str(command.authorized_model_lane),
            "context_snapshot_hash": snapshot_hash,
            "context_fit": "fits",
            "estimated_input_tokens": estimated_tokens,
            "target_input_budget": int(command.target_input_budget),
            "compaction": "not_required",
            "cache_impact": cache_impact,
            "authorization_receipt_hash": command.model_authorization_receipt_hash,
            "before_config_hash": before_config_hash,
            "config_hash": command.config_hash,
        }
        conn.execute(
            ne_agent_model_boundary_receipts.insert().values(
                receipt_id=receipt_id,
                run_id=run_id,
                previous_run_id=None if previous is None else str(previous.run_id),
                conversation_id=command.conversation_id,
                owner_principal_id=command.owner_principal_id,
                department=command.department,
                agent_id=command.agent_id,
                agent_version=command.agent_version,
                agent_definition_hash=command.agent_definition_hash,
                model_policy_id=command.model_policy_id,
                before_requested_model=(None if previous is None else previous.requested_model),
                before_served_model=before_served,
                after_requested_model=after_requested,
                after_authorized_model=command.authorized_model,
                after_authorized_lane=str(command.authorized_model_lane),
                context_snapshot_hash=snapshot_hash,
                context_fit="fits",
                estimated_input_tokens=estimated_tokens,
                target_input_budget=int(command.target_input_budget),
                compaction="not_required",
                cache_impact=cache_impact,
                authorization_receipt_hash=command.model_authorization_receipt_hash,
                before_config_hash=before_config_hash,
                config_hash=command.config_hash,
                created_at=now,
            )
        )
        return detail

    async def admit_cancel(self, command: Any) -> AcceptedRunAdmission:
        return await call_db(self._offload, self._admit_control_sync, command, "cancel")

    async def admit_retry(self, command: Any) -> AcceptedRunAdmission:
        return await call_db(self._offload, self._admit_control_sync, command, "retry")

    def _admit_control_sync(
        self, command: Any, action: Literal["cancel", "retry"]
    ) -> AcceptedRunAdmission:
        self._validate_submission_command(command)
        now, enqueue_id = datetime.now(UTC), _id()
        result_run_id = command.run_id
        try:
            with self._engine.begin() as conn:
                replay = self._submission_replay(conn, command, action)
                if replay is not None:
                    return replay
                run = conn.execute(
                    sa.select(ne_agent_runs)
                    .where(
                        ne_agent_runs.c.run_id == command.run_id,
                        ne_agent_runs.c.owner_principal_id == command.owner_principal_id,
                    )
                    .with_for_update()
                ).one_or_none()
                if run is None:
                    raise RunSubmissionNotFoundError
                # A concurrent identical control may have committed while this
                # transaction waited for the predecessor Run lock.  Recheck the
                # idempotency receipt under that lock before interpreting its
                # now-terminal/now-cancelling state as a different request.
                replay = self._submission_replay(conn, command, action)
                if replay is not None:
                    return replay
                if int(run.version) != command.expected_version:
                    raise RunSubmissionConflictError("stale Run version")
                frozen = (
                    int(run.agent_version),
                    str(run.strategy),
                    str(run.config_hash),
                    str(run.capability_snapshot_hash),
                    str(run.capability_snapshot_receipt_id),
                )
                presented = (
                    command.agent_version,
                    str(command.strategy),
                    command.config_hash,
                    command.capability_snapshot_hash,
                    command.capability_snapshot_receipt_id,
                )
                if frozen != presented or str(run.agent_id) != command.agent_id:
                    raise RunSubmissionConflictError("Run snapshot changed")
                version = int(run.version) + 1
                retry_boundary_detail: dict[str, object] | None = None
                if action == "cancel":
                    if (
                        RunState(str(run.status)) in _TERMINAL_STATES
                        or str(run.status) == "cancelling"
                    ):
                        raise RunSubmissionConflictError("Run cannot be cancelled")
                    attempt = conn.execute(
                        sa.select(ne_agent_run_attempt_links)
                        .where(ne_agent_run_attempt_links.c.run_id == command.run_id)
                        .order_by(ne_agent_run_attempt_links.c.attempt_number.desc())
                        .limit(1)
                    ).one_or_none()
                    if attempt is None:
                        raise RunSubmissionConflictError("Run has no attempt")
                    attempt_id = str(attempt.attempt_id)
                    status = RunState.CANCELLING
                    conn.execute(
                        ne_agent_runs.update()
                        .where(ne_agent_runs.c.run_id == command.run_id)
                        .values(status=status.value, version=version, updated_at=now)
                    )
                else:
                    if RunState(str(run.status)) not in _TERMINAL_STATES:
                        raise RunSubmissionConflictError("only terminal Runs can be retried")
                    prior = conn.execute(
                        sa.select(ne_agent_run_attempt_links)
                        .where(ne_agent_run_attempt_links.c.run_id == command.run_id)
                        .order_by(ne_agent_run_attempt_links.c.attempt_number.desc())
                        .limit(1)
                    ).one_or_none()
                    if prior is None:
                        raise RunSubmissionConflictError("Run has no attempt")
                    prior_retry = conn.execute(
                        sa.select(ne_agent_run_retry_lineage).where(
                            ne_agent_run_retry_lineage.c.successor_run_id == command.run_id
                        )
                    ).one_or_none()
                    root_run_id = (
                        command.run_id if prior_retry is None else str(prior_retry.root_run_id)
                    )
                    retry_ordinal = 1 if prior_retry is None else int(prior_retry.retry_ordinal) + 1
                    if (
                        conn.execute(
                            sa.select(ne_agent_run_retry_lineage.c.retry_id).where(
                                sa.or_(
                                    ne_agent_run_retry_lineage.c.predecessor_run_id
                                    == command.run_id,
                                    sa.and_(
                                        ne_agent_run_retry_lineage.c.root_run_id == root_run_id,
                                        ne_agent_run_retry_lineage.c.retry_ordinal == retry_ordinal,
                                    ),
                                )
                            )
                        ).first()
                        is not None
                    ):
                        raise RunSubmissionConflictError(
                            "Run already has a durable retry successor"
                        )
                    # A retry is a new durable Run/attempt.  The predecessor's
                    # terminal primary state and terminal timestamp remain
                    # immutable; lineage joins the two opaque identities.
                    result_run_id = _id()
                    attempt_id = _id()
                    input_id = _id()
                    status = RunState.QUEUED
                    from ne.runtime.store import ne_agent_conversations

                    conversation = conn.execute(
                        sa.select(ne_agent_conversations.c.conversation_id)
                        .where(
                            ne_agent_conversations.c.conversation_id == str(run.conversation_id),
                            ne_agent_conversations.c.owner_principal_id
                            == command.owner_principal_id,
                        )
                        .with_for_update()
                    ).one_or_none()
                    if conversation is None:
                        raise RunSubmissionNotFoundError
                    input_row = conn.execute(
                        sa.select(ne_agent_run_inputs).where(
                            ne_agent_run_inputs.c.run_id == command.run_id
                        )
                    ).one()
                    plaintext = self._cipher.materialize_for_target(
                        _row_envelope(
                            input_row,
                            domain=PrivatePayloadDomain.RUN_INPUT,
                            row_id_name="input_id",
                        ),
                        domain=PrivatePayloadDomain.RUN_INPUT,
                        row_id=str(input_row.input_id),
                        owner_principal_id=command.owner_principal_id,
                        binding=(
                            str(run.conversation_id),
                            command.run_id,
                            str(run.sequence),
                            str(run.agent_id),
                            str(run.agent_version),
                        ),
                    )
                    sequence = (
                        int(
                            conn.execute(
                                sa.select(
                                    sa.func.coalesce(sa.func.max(ne_agent_runs.c.sequence), 0)
                                ).where(ne_agent_runs.c.conversation_id == str(run.conversation_id))
                            ).scalar_one()
                        )
                        + 1
                    )
                    envelope = self._cipher.encrypt_for_target(
                        domain=PrivatePayloadDomain.RUN_INPUT,
                        row_id=input_id,
                        owner_principal_id=command.owner_principal_id,
                        binding=(
                            str(run.conversation_id),
                            result_run_id,
                            str(sequence),
                            str(run.agent_id),
                            str(run.agent_version),
                        ),
                        plaintext=plaintext,
                    )
                    conn.execute(
                        ne_agent_runs.insert().values(
                            run_id=result_run_id,
                            conversation_id=str(run.conversation_id),
                            owner_principal_id=str(run.owner_principal_id),
                            department=str(run.department),
                            conversation_agent_name=str(run.conversation_agent_name),
                            agent_id=str(run.agent_id),
                            agent_version=int(run.agent_version),
                            strategy=str(run.strategy),
                            config_hash=str(run.config_hash),
                            capability_snapshot_hash=str(run.capability_snapshot_hash),
                            capability_snapshot_receipt_id=str(run.capability_snapshot_receipt_id),
                            agent_definition_hash=(
                                None
                                if run.agent_definition_hash is None
                                else str(run.agent_definition_hash)
                            ),
                            model_policy_id=(
                                None if run.model_policy_id is None else str(run.model_policy_id)
                            ),
                            authorized_model=(
                                None if run.authorized_model is None else str(run.authorized_model)
                            ),
                            authorized_model_lane=(
                                None
                                if run.authorized_model_lane is None
                                else str(run.authorized_model_lane)
                            ),
                            model_authorization_receipt_hash=(
                                None
                                if run.model_authorization_receipt_hash is None
                                else str(run.model_authorization_receipt_hash)
                            ),
                            requested_model=(
                                None if run.requested_model is None else str(run.requested_model)
                            ),
                            requested_model_lane=(
                                None
                                if run.requested_model_lane is None
                                else str(run.requested_model_lane)
                            ),
                            sequence=sequence,
                            display_label=(
                                f"Run {sequence} · {str(run.strategy).title()} · {now:%H:%M}"
                            ),
                            status=status.value,
                            version=1,
                            created_at=now,
                            updated_at=now,
                            terminal_at=None,
                        )
                    )
                    if run.model_policy_id is not None:
                        prior_boundary = conn.execute(
                            sa.select(ne_agent_model_boundary_receipts.c.target_input_budget).where(
                                ne_agent_model_boundary_receipts.c.run_id == command.run_id
                            )
                        ).one_or_none()
                        if prior_boundary is None:
                            raise RunSubmissionConflictError(
                                "model-bound retry has no frozen predecessor boundary"
                            )
                        retry_boundary_detail = self._insert_model_boundary_receipt(
                            conn,
                            command=SimpleNamespace(
                                conversation_id=str(run.conversation_id),
                                owner_principal_id=str(run.owner_principal_id),
                                department=str(run.department),
                                conversation_agent_name=str(run.conversation_agent_name),
                                agent_id=str(run.agent_id),
                                agent_version=int(run.agent_version),
                                agent_definition_hash=str(run.agent_definition_hash),
                                model_policy_id=str(run.model_policy_id),
                                authorized_model=str(run.authorized_model),
                                authorized_model_lane=str(run.authorized_model_lane),
                                model_authorization_receipt_hash=str(
                                    run.model_authorization_receipt_hash
                                ),
                                requested_model=(
                                    None
                                    if run.requested_model is None
                                    else str(run.requested_model)
                                ),
                                config_hash=str(run.config_hash),
                                input_plaintext=plaintext,
                                target_input_budget=int(prior_boundary.target_input_budget),
                                request_hash=command.request_hash,
                            ),
                            run_id=result_run_id,
                            sequence=sequence,
                            now=now,
                        )
                    conn.execute(
                        ne_agent_run_attempt_links.insert().values(
                            attempt_id=attempt_id,
                            run_id=result_run_id,
                            conversation_id=str(run.conversation_id),
                            predecessor_attempt_id=None,
                            attempt_number=1,
                            created_at=now,
                        )
                    )
                    conn.execute(
                        ne_agent_run_event_seq.insert().values(
                            run_id=result_run_id, last_sequence=0
                        )
                    )
                    conn.execute(
                        ne_agent_run_inputs.insert().values(
                            input_id=input_id,
                            run_id=result_run_id,
                            owner_principal_id=command.owner_principal_id,
                            created_at=now,
                            **_envelope_values(envelope),
                        )
                    )
                    self._insert_authenticated_context(
                        conn,
                        run_id=result_run_id,
                        owner_principal_id=command.owner_principal_id,
                        context=command.authenticated_context,
                        now=now,
                    )
                    conn.execute(
                        ne_agent_run_turn_targets.insert().values(
                            run_id=result_run_id,
                            attempt_id=attempt_id,
                            conversation_id=str(run.conversation_id),
                            turn_id=result_run_id,
                            turn_sequence=sequence,
                            attachment_id=result_run_id,
                            created_at=now,
                        )
                    )
                    conn.execute(
                        ne_agent_run_retry_lineage.insert().values(
                            retry_id=_id(),
                            predecessor_run_id=command.run_id,
                            predecessor_attempt_id=str(prior.attempt_id),
                            successor_run_id=result_run_id,
                            successor_attempt_id=attempt_id,
                            root_run_id=root_run_id,
                            retry_ordinal=retry_ordinal,
                            created_at=now,
                        )
                    )
                    version = 1
                self._insert_submission_event_enqueue(
                    conn,
                    command=command,
                    action=action,
                    run_id=result_run_id,
                    attempt_id=attempt_id,
                    version=version,
                    status=status,
                    enqueue_id=enqueue_id,
                    now=now,
                )
                if retry_boundary_detail is not None:
                    self._append_event(
                        conn,
                        run_id=result_run_id,
                        attempt_id=attempt_id,
                        event_kind="model.changed",
                        event_status="changed",
                        safe_detail=retry_boundary_detail,
                        step_id="model-boundary",
                    )
        except IntegrityError as exc:
            return self._resolve_submission_race(command, action, exc)
        return AcceptedRunAdmission(
            action, result_run_id, attempt_id, version, status.value, enqueue_id
        )

    @staticmethod
    def _insert_authenticated_context(
        conn: sa.Connection,
        *,
        run_id: str,
        owner_principal_id: str,
        context: AuthenticatedRequestContext,
        now: datetime,
    ) -> None:
        if not isinstance(context, AuthenticatedRequestContext):
            raise ValueError("Run admission requires a verifier-authored authenticated context")
        if context.principal_id != owner_principal_id:
            raise ValueError("Run authenticated context differs from its accepted owner")
        conn.execute(
            ne_agent_run_authenticated_contexts.insert().values(
                run_id=run_id,
                owner_principal_id=owner_principal_id,
                schema_version=context.schema_version,
                authorization_groups_json=_canonical_json(list(context.authorization_groups)),
                department=context.department,
                issuer=context.issuer,
                expires_at=context.expires_at,
                context_hash=context.context_hash,
                created_at=now,
            )
        )

    @staticmethod
    def _validate_submission_command(command: Any) -> None:
        _require_nonempty(
            idempotency_key=command.idempotency_key,
            request_hash=command.request_hash,
            owner_principal_id=command.owner_principal_id,
        )
        _require_sha256("request_hash", command.request_hash)
        context = getattr(command, "authenticated_context", None)
        if context is not None:
            if not isinstance(context, AuthenticatedRequestContext):
                raise ValueError("authenticated context has an invalid type")
            if context.principal_id != command.owner_principal_id:
                raise ValueError("authenticated context differs from the submission owner")
            department = getattr(command, "department", context.department)
            if context.department != department:
                raise ValueError("authenticated context differs from the submission department")
        requested_model = getattr(command, "requested_model", None)
        requested_lane = getattr(command, "requested_model_lane", None)
        if (requested_model is None) != (requested_lane is None):
            raise ValueError("requested model identity must be complete")
        if requested_model is not None:
            _require_nonempty(requested_model=requested_model)
            if str(requested_lane) not in {"frontier-api", "private-vllm"}:
                raise ValueError("requested model lane is invalid")
        continuity_values = tuple(
            getattr(command, field, None)
            for field in (
                "agent_definition_hash",
                "model_policy_id",
                "authorized_model",
                "authorized_model_lane",
                "model_authorization_receipt_hash",
                "target_input_budget",
            )
        )
        if hasattr(command, "conversation_id") and any(
            value is not None for value in continuity_values
        ):
            if any(value is None for value in continuity_values):
                raise ValueError("Run model authorization must be complete")
            _require_nonempty(
                agent_definition_hash=str(continuity_values[0]),
                model_policy_id=str(continuity_values[1]),
                authorized_model=str(continuity_values[2]),
                model_authorization_receipt_hash=str(continuity_values[4]),
            )
            _require_sha256("agent_definition_hash", str(continuity_values[0]))
            _require_sha256("model_authorization_receipt_hash", str(continuity_values[4]))
            if str(continuity_values[3]) not in {
                "frontier-api",
                "private-vllm",
            }:
                raise ValueError("authorized model lane is invalid")
            if int(str(continuity_values[5])) < 1:
                raise ValueError("target input budget must be positive")

    def _submission_replay(
        self,
        conn: sa.Connection,
        command: Any,
        action: Literal["start", "cancel", "retry"],
    ) -> AcceptedRunAdmission | None:
        row = conn.execute(
            sa.select(ne_agent_run_submissions).where(
                ne_agent_run_submissions.c.owner_principal_id == command.owner_principal_id,
                ne_agent_run_submissions.c.action == action,
                ne_agent_run_submissions.c.idempotency_key == command.idempotency_key,
            )
        ).one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(str(row.request_hash), command.request_hash):
            raise RunSubmissionConflictError("idempotency key changed request")
        return AcceptedRunAdmission(
            action,
            str(row.run_id),
            str(row.attempt_id),
            int(row.run_version),
            str(row.status),
            str(row.enqueue_id),
            True,
        )

    def _resolve_submission_race(
        self,
        command: Any,
        action: Literal["start", "cancel", "retry"],
        exc: IntegrityError,
    ) -> AcceptedRunAdmission:
        with self._engine.connect() as conn:
            replay = self._submission_replay(conn, command, action)
        if replay is not None:
            return replay
        raise exc

    def _insert_attempt_event_enqueue(
        self,
        conn: sa.Connection,
        *,
        command: Any,
        action: str,
        run_id: str,
        attempt_id: str,
        predecessor_attempt_id: str | None,
        attempt_number: int,
        version: int,
        status: RunState,
        enqueue_id: str,
        now: datetime,
    ) -> None:
        conn.execute(
            ne_agent_run_attempt_links.insert().values(
                attempt_id=attempt_id,
                run_id=run_id,
                conversation_id=command.conversation_id,
                predecessor_attempt_id=predecessor_attempt_id,
                attempt_number=attempt_number,
                created_at=now,
            )
        )
        self._insert_submission_event_enqueue(
            conn,
            command=command,
            action=action,
            run_id=run_id,
            attempt_id=attempt_id,
            version=version,
            status=status,
            enqueue_id=enqueue_id,
            now=now,
        )

    def _insert_submission_event_enqueue(
        self,
        conn: sa.Connection,
        *,
        command: Any,
        action: str,
        run_id: str,
        attempt_id: str,
        version: int,
        status: RunState,
        enqueue_id: str,
        now: datetime,
    ) -> None:
        if action == "start":
            sequence = 1
            conn.execute(ne_agent_run_event_seq.insert().values(run_id=run_id, last_sequence=1))
        else:
            sequence = self._claim_event_sequence(conn, run_id)
        detail = _canonical_json({"action": action, "status": status.value, "version": version})
        conn.execute(
            ne_agent_run_events.insert().values(
                event_id=_id(),
                run_id=run_id,
                attempt_id=attempt_id,
                sequence=sequence,
                event_kind=f"run.{action}.accepted",
                event_status=status.value,
                parent_event_id=None,
                step_id=None,
                safe_detail=detail,
                detail_hash=_hash(detail.encode()),
                created_at=now,
            )
        )
        conn.execute(
            ne_agent_run_enqueue_outbox.insert().values(
                enqueue_id=enqueue_id,
                action=action,
                run_id=run_id,
                attempt_id=attempt_id,
                run_version=version,
                created_at=now,
                claimed_at=None,
            )
        )
        conn.execute(
            ne_agent_run_submissions.insert().values(
                submission_id=_id(),
                owner_principal_id=command.owner_principal_id,
                action=action,
                idempotency_key=command.idempotency_key,
                request_hash=command.request_hash,
                run_id=run_id,
                attempt_id=attempt_id,
                run_version=version,
                status=status.value,
                enqueue_id=enqueue_id,
                created_at=now,
            )
        )

    async def get_run(self, *, run_id: str, owner_principal_id: str) -> AgentRun | None:
        return await call_db(self._offload, self._get_run_sync, run_id, owner_principal_id)

    def _get_run_sync(self, run_id: str, owner_principal_id: str) -> AgentRun | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                _runs_with_lineage().where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.owner_principal_id == owner_principal_id,
                )
            ).one_or_none()
        return None if row is None else _run(row)

    async def latest_context_snapshot_estimate(
        self, *, conversation_id: str, owner_principal_id: str
    ) -> int | None:
        """The newest boundary receipt's ``estimated_input_tokens`` for one Conversation.

        NE-CTX-IN-FORCE round 7 (SPEC PRE-DEMO-014): the selection surfaces gate
        an explicit model switch against the CURRENT conversation snapshot, and
        this read is where they learn its size — the durable counts
        :meth:`_insert_model_boundary_receipt` froze at the last admission, in
        the admission estimator's own units, never a second estimator. It is a
        LOWER bound (the last admitted Run's output is not yet counted), which
        is the safe direction for a disable gate: a target this read already
        overflows can never fit, while the exact in-transaction check at the
        next admission remains the enforcement. ``None`` — no receipt, or a
        Conversation this principal does not own — means no fit data, never a
        fabricated zero.
        """
        return await call_db(
            self._offload,
            self._latest_context_snapshot_estimate_sync,
            conversation_id,
            owner_principal_id,
        )

    def _latest_context_snapshot_estimate_sync(
        self, conversation_id: str, owner_principal_id: str
    ) -> int | None:
        with self._engine.connect() as conn:
            estimated = conn.execute(
                sa.select(ne_agent_model_boundary_receipts.c.estimated_input_tokens)
                .where(
                    ne_agent_model_boundary_receipts.c.conversation_id == conversation_id,
                    ne_agent_model_boundary_receipts.c.owner_principal_id == owner_principal_id,
                )
                .order_by(ne_agent_model_boundary_receipts.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if estimated is None else int(estimated)

    async def largest_live_context_snapshot_estimate(self, task: str) -> int | None:
        """The LARGEST live conversation snapshot recorded for one task.

        NE-CTX-IN-FORCE round 9 (SPEC PRE-DEMO-014 on the repoint surface): a
        governed task REPOINT governs every conversation on the task and names
        no single snapshot, so the routing gate measures its target against the
        largest one still live. "Live" is structural in this deployment:
        conversations are never closed or deleted (Postgres even carries a
        delete-rejecting trigger on runs), so every conversation with a
        boundary receipt can take a next turn.

        One bounded aggregate over the SAME durable counts the per-conversation
        gate (:meth:`latest_context_snapshot_estimate`) reads — the newest
        receipt per ``conversation_id``, maxed across conversations whose
        newest receipt for this ``model_policy_id`` — never a second estimator
        and never a scan of receipt history in Python. Each per-conversation
        count is a LOWER bound (the last admitted Run's output is not yet
        counted), the safe direction for a disable gate; the exact
        in-transaction check at each conversation's next admission remains the
        enforcement. ``None`` means NO conversation has a receipt for this task
        — genuinely nothing to fit — never a fabricated zero; a store failure
        raises to the caller (fail closed), it never degrades to ``None``.
        """
        return await call_db(
            self._offload,
            self.largest_live_context_snapshot_estimate_sync,
            task,
        )

    def largest_live_context_snapshot_estimate_sync(self, task: str) -> int | None:
        """Sync body of :meth:`largest_live_context_snapshot_estimate`.

        Public and sync on purpose: the routing store's judgement runs on
        worker threads (``apply`` under ``anyio.to_thread``, the binding's
        ``validate_args``) where the async wrapper cannot be awaited — the
        ``ModelRouteStore.read_state`` precedent.
        """
        receipts = ne_agent_model_boundary_receipts
        newest = (
            sa.select(
                receipts.c.conversation_id,
                sa.func.max(receipts.c.created_at).label("created_at"),
            )
            .where(receipts.c.model_policy_id == task)
            .group_by(receipts.c.conversation_id)
            .subquery("newest_receipt")
        )
        stmt = (
            sa.select(sa.func.max(receipts.c.estimated_input_tokens))
            .select_from(
                receipts.join(
                    newest,
                    sa.and_(
                        receipts.c.conversation_id == newest.c.conversation_id,
                        receipts.c.created_at == newest.c.created_at,
                    ),
                )
            )
            .where(receipts.c.model_policy_id == task)
        )
        with self._engine.connect() as conn:
            largest = conn.execute(stmt).scalar_one_or_none()
        return None if largest is None else int(largest)

    async def list_runs(
        self,
        *,
        owner_principal_id: str,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> RunPage:
        if limit < 1 or limit > 200:
            raise ValueError("Run page limit must be between 1 and 200")
        return await call_db(self._offload, self._list_runs_sync, owner_principal_id, limit, after)

    def _list_runs_sync(
        self, owner: str, limit: int, after: tuple[datetime, str] | None
    ) -> RunPage:
        predicate: Any = ne_agent_runs.c.owner_principal_id == owner
        if after is not None:
            created_at, run_id = after
            predicate = sa.and_(
                predicate,
                sa.or_(
                    ne_agent_runs.c.created_at < created_at,
                    sa.and_(
                        ne_agent_runs.c.created_at == created_at,
                        ne_agent_runs.c.run_id < run_id,
                    ),
                ),
            )
        stmt = (
            _runs_with_lineage()
            .where(predicate)
            .order_by(ne_agent_runs.c.created_at.desc(), ne_agent_runs.c.run_id.desc())
            .limit(limit + 1)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).all())
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page_rows:
            last = page_rows[-1]
            next_cursor = (_utc(last.created_at), str(last.run_id))
        return RunPage(runs=tuple(_run(row) for row in page_rows), next_cursor=next_cursor)

    async def list_events(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> RunEventPage:
        if after_sequence < 0 or limit < 1 or limit > 500:
            raise ValueError("Run event cursor or limit is invalid")
        return await call_db(
            self._offload,
            self._list_events_sync,
            run_id,
            owner_principal_id,
            after_sequence,
            limit,
        )

    def _list_events_sync(
        self, run_id: str, owner: str, after_sequence: int, limit: int
    ) -> RunEventPage:
        stmt = (
            sa.select(ne_agent_run_events)
            .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_events.c.run_id)
            .where(
                ne_agent_run_events.c.run_id == run_id,
                ne_agent_runs.c.owner_principal_id == owner,
                ne_agent_run_events.c.sequence > after_sequence,
            )
            .order_by(ne_agent_run_events.c.sequence)
            .limit(limit + 1)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).all())
        page_rows = rows[:limit]
        next_sequence = int(page_rows[-1].sequence) if len(rows) > limit and page_rows else None
        return RunEventPage(
            events=tuple(_event(row) for row in page_rows), next_sequence=next_sequence
        )

    async def materialize_run_input(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
    ) -> bytes:
        return await call_db(
            self._offload, self._materialize_run_input_sync, run_id, owner_principal_id
        )

    def _materialize_run_input_sync(self, run_id: str, owner: str) -> bytes:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_inputs, ne_agent_runs)
                .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_inputs.c.run_id)
                .where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.owner_principal_id == owner,
                )
            ).one_or_none()
        if row is None:
            raise LookupError("Run input unavailable")
        envelope = _row_envelope(row, domain=PrivatePayloadDomain.RUN_INPUT, row_id_name="input_id")
        return self._cipher.materialize_for_target(
            envelope,
            domain=PrivatePayloadDomain.RUN_INPUT,
            row_id=str(row.input_id),
            owner_principal_id=owner,
            binding=(
                str(row.conversation_id),
                run_id,
                str(row.sequence),
                str(row.agent_id),
                str(row.agent_version),
            ),
        )

    async def materialize_conversation_history(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
    ) -> list[ModelMessage]:
        """Assemble the complete ordered prior history before one accepted Run.

        Both terminal shapes are carried, differently. A SOURCE-FREE prior turn
        materializes its plaintext here. A SOURCE-BOUND prior turn NEVER
        materializes here: its assistant part carries the opaque
        ``ProtectedSourceOutputRef`` reference string instead, which the
        model-send guard (``source_results.ProtectedSourceResultGuard
        .materialize_history``) reauthorizes under the CURRENT request's
        principal on every send — claiming a ``history_output_served`` (or
        ``withheld``) delivery receipt and substituting the fixed
        source-withheld text on any non-authorized decision. Integrity of the
        stored container is verified during assembly; authorization is decided
        only at delivery (SPEC "Conversation continuity": integrity is not
        authorization).
        """
        turns = await call_db(
            self._offload,
            self._materialize_conversation_history_sync,
            run_id,
            owner_principal_id,
        )
        history: list[ModelMessage] = []
        for prompt, output in turns:
            history.extend(
                (
                    ModelRequest(parts=[UserPromptPart(content=prompt.decode("utf-8"))]),
                    ModelResponse(
                        parts=[TextPart(content=output.decode("utf-8"))],
                        model_name="protected",
                    ),
                )
            )
        return history

    async def record_model_step(
        self,
        *,
        run_id: str,
        attempt_id: str,
        ordinal: int,
        model_policy_id: str,
        request_mode: Literal["auto", "explicit"],
        requested_model: str | None,
        served_model: str,
        served_lane: str,
        route_position: Literal["primary", "fallback"],
        fallback_from: str | None,
        authorization_receipt_hash: str,
    ) -> RunEvent:
        """Persist requested/served truth before the model result is consumed.

        ``route_position``/``fallback_from`` say WHY the served model is the
        served model, and they are REQUIRED of every caller: a writer that could
        omit them would silently produce exactly the unattributable row this
        pair exists to abolish. ``"primary"`` means nothing was attempted before
        the candidate that served; ``"fallback"`` means something was, and
        ``fallback_from`` names it. The two are one fact and are validated as
        one -- see :func:`_require_route_position`.
        """
        _require_nonempty(
            run_id=run_id,
            attempt_id=attempt_id,
            model_policy_id=model_policy_id,
            served_model=served_model,
        )
        _require_sha256("authorization_receipt_hash", authorization_receipt_hash)
        if (
            ordinal < 0
            or request_mode not in {"auto", "explicit"}
            or served_lane not in {"frontier-api", "private-vllm"}
        ):
            raise ValueError("model step ordinal or lane is invalid")
        if (request_mode == "auto") != (requested_model is None):
            raise ValueError("model step requested/auto truth is invalid")
        _require_route_position(route_position, fallback_from)
        return await call_db(
            self._offload,
            self._record_model_step_sync,
            run_id,
            attempt_id,
            ordinal,
            model_policy_id,
            request_mode,
            requested_model,
            served_model,
            served_lane,
            route_position,
            fallback_from,
            authorization_receipt_hash,
        )

    async def verify_model_boundary(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        agent_definition_hash: str,
        model_policy_id: str,
        authorization_receipt_hash: str,
        context_snapshot_hash: str,
    ) -> None:
        """Require the immutable admission receipt immediately before an effect."""
        await call_db(
            self._offload,
            self._verify_model_boundary_sync,
            run_id,
            owner_principal_id,
            agent_definition_hash,
            model_policy_id,
            authorization_receipt_hash,
            context_snapshot_hash,
        )

    def _verify_model_boundary_sync(
        self,
        run_id: str,
        owner: str,
        definition_hash: str,
        model_policy_id: str,
        authorization_hash: str,
        snapshot_hash: str,
    ) -> None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_model_boundary_receipts)
                .join(
                    ne_agent_runs,
                    ne_agent_runs.c.run_id == ne_agent_model_boundary_receipts.c.run_id,
                )
                .where(
                    ne_agent_model_boundary_receipts.c.run_id == run_id,
                    ne_agent_model_boundary_receipts.c.owner_principal_id == owner,
                    ne_agent_model_boundary_receipts.c.agent_definition_hash == definition_hash,
                    ne_agent_model_boundary_receipts.c.model_policy_id == model_policy_id,
                    ne_agent_model_boundary_receipts.c.authorization_receipt_hash
                    == authorization_hash,
                    ne_agent_model_boundary_receipts.c.context_snapshot_hash == snapshot_hash,
                    ne_agent_runs.c.context_snapshot_hash == snapshot_hash,
                )
            ).one_or_none()
        if row is None:
            raise RuntimeError("Run model boundary receipt is unavailable or drifted")

    def _record_model_step_sync(
        self,
        run_id: str,
        attempt_id: str,
        ordinal: int,
        model_policy_id: str,
        request_mode: str,
        requested_model: str | None,
        served_model: str,
        served_lane: str,
        route_position: str,
        fallback_from: str | None,
        authorization_receipt_hash: str,
    ) -> RunEvent:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id).with_for_update()
            ).one_or_none()
            if (
                run is None
                or str(run.model_policy_id) != model_policy_id
                or str(run.model_authorization_receipt_hash) != authorization_receipt_hash
            ):
                raise RuntimeError("Run model authorization drifted before step receipt")
            if request_mode == "explicit" and (
                str(run.requested_model) != requested_model
                or str(run.authorized_model) != served_model
                or str(run.authorized_model_lane) != served_lane
            ):
                raise RuntimeError("explicit Run requested/served model truth drifted")
            if request_mode == "auto" and run.requested_model is not None:
                raise RuntimeError("automatic Run acquired an explicit requested model")
            existing = conn.execute(
                sa.select(ne_agent_run_model_steps).where(
                    ne_agent_run_model_steps.c.run_id == run_id,
                    ne_agent_run_model_steps.c.attempt_id == attempt_id,
                    ne_agent_run_model_steps.c.ordinal == ordinal,
                )
            ).one_or_none()
            intent = {
                "model_policy_id": model_policy_id,
                "request_mode": request_mode,
                "requested_model": requested_model,
                "served_model": served_model,
                "served_lane": served_lane,
                "route_position": route_position,
                "fallback_from": fallback_from,
                "authorization_receipt_hash": authorization_receipt_hash,
            }
            if existing is not None:
                if any(str(getattr(existing, key)) != str(value) for key, value in intent.items()):
                    raise RuntimeError("Run model step replay changed requested/served truth")
                return _event(
                    conn.execute(
                        sa.select(ne_agent_run_events).where(
                            ne_agent_run_events.c.event_id == existing.run_event_id
                        )
                    ).one()
                )
            event = self._append_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_kind="model.served",
                event_status="served",
                safe_detail=intent,
                step_id=f"model:{ordinal}",
            )
            conn.execute(
                ne_agent_run_model_steps.insert().values(
                    model_step_id=_id(),
                    run_id=run_id,
                    attempt_id=attempt_id,
                    run_event_id=event.event_id,
                    ordinal=ordinal,
                    created_at=now,
                    **intent,
                )
            )
            return event

    def _materialize_conversation_history_sync(
        self, run_id: str, owner: str
    ) -> list[tuple[bytes, bytes]]:
        with self._engine.connect() as conn:
            current = conn.execute(
                sa.select(ne_agent_runs).where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.owner_principal_id == owner,
                )
            ).one_or_none()
            if current is None:
                raise ProtectedTurnUnavailable
            return self._materialize_prior_turns_in_transaction(conn, current)

    def _materialize_prior_turns_in_transaction(
        self, conn: sa.Connection, current: Any
    ) -> list[tuple[bytes, bytes]]:
        prior = list(
            conn.execute(
                sa.select(ne_agent_runs)
                .join(
                    ne_agent_run_history_attachments,
                    ne_agent_run_history_attachments.c.run_id == ne_agent_runs.c.run_id,
                )
                .where(
                    ne_agent_runs.c.conversation_id == str(current.conversation_id),
                    ne_agent_runs.c.owner_principal_id == str(current.owner_principal_id),
                    ne_agent_runs.c.department == str(current.department),
                    ne_agent_runs.c.conversation_agent_name == str(current.conversation_agent_name),
                    ne_agent_runs.c.agent_id == str(current.agent_id),
                    ne_agent_runs.c.sequence < int(current.sequence),
                    ne_agent_runs.c.status.in_(state.value for state in _TERMINAL_STATES),
                )
                .order_by(ne_agent_runs.c.sequence)
            ).all()
        )
        turns: list[tuple[bytes, bytes]] = []
        for run in prior:
            input_row = conn.execute(
                sa.select(ne_agent_run_inputs).where(ne_agent_run_inputs.c.run_id == run.run_id)
            ).one_or_none()
            target = conn.execute(
                sa.select(ne_agent_run_turn_targets).where(
                    ne_agent_run_turn_targets.c.run_id == run.run_id
                )
            ).one_or_none()
            attachment = conn.execute(
                sa.select(ne_agent_run_history_attachments).where(
                    ne_agent_run_history_attachments.c.run_id == run.run_id
                )
            ).one_or_none()
            if input_row is None or target is None or attachment is None:
                raise ProtectedTurnUnavailable
            # The finalizer stores exactly two terminal shapes (NE-GA-RUN1):
            # source-free decision text in `protected_turn_content`, and every
            # evidence-serving answer as a source-bound container in
            # `protected_source_output`. History-carry admits both — or the
            # first cited answer would make every later turn in its
            # Conversation inadmissible — but only the source-free shape yields
            # plaintext HERE. The source-bound shape yields its opaque
            # protected reference: authorization for that text belongs to the
            # model-send guard, per delivery, under the current principal.
            source_free = (
                str(attachment.content_status) == ProtectedContentStatus.SOURCE_FREE.value
                and str(attachment.protected_domain)
                == PrivatePayloadDomain.PROTECTED_TURN_CONTENT.value
            )
            source_bound = (
                str(attachment.content_status)
                == ProtectedContentStatus.SOURCE_BOUND_CONTAINER.value
                and str(attachment.protected_domain)
                == PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT.value
            )
            if not (source_free or source_bound):
                raise ProtectedTurnUnavailable
            try:
                prompt = self._cipher.materialize_for_target(
                    _row_envelope(
                        input_row,
                        domain=PrivatePayloadDomain.RUN_INPUT,
                        row_id_name="input_id",
                    ),
                    domain=PrivatePayloadDomain.RUN_INPUT,
                    row_id=str(input_row.input_id),
                    owner_principal_id=str(current.owner_principal_id),
                    binding=(
                        str(run.conversation_id),
                        str(run.run_id),
                        str(run.sequence),
                        str(run.agent_id),
                        str(run.agent_version),
                    ),
                )
                materialize_output = (
                    self._materialize_source_free_turn_from_connection
                    if source_free
                    else self._history_reference_for_source_bound_turn_from_connection
                )
                output = materialize_output(
                    conn,
                    str(current.owner_principal_id),
                    CanonicalTargetTuple(
                        conversation_id=str(target.conversation_id),
                        run_id=str(target.run_id),
                        turn_id=str(target.turn_id),
                        turn_sequence=int(target.turn_sequence),
                        attachment_id=str(target.attachment_id),
                        attempt_id=str(target.attempt_id),
                    ),
                    str(attachment.protected_ref),
                    str(attachment.protected_hash),
                )
                prompt.decode("utf-8")
                output.decode("utf-8")
            except (KeyEnvelopeError, UnicodeDecodeError, ProtectedTurnUnavailable) as exc:
                raise ProtectedTurnUnavailable from exc
            turns.append((prompt, output))
        return turns

    async def transition(self, *, run_id: str, expected: RunState, target: RunState) -> AgentRun:
        if target in _TERMINAL_STATES:
            raise ValueError("terminal Run transition requires the lifecycle/finalizer writer")
        if target not in _TRANSITIONS[expected]:
            raise ValueError(f"illegal Run transition {expected.value}->{target.value}")
        return await call_db(self._offload, self._transition_sync, run_id, expected, target)

    def _transition_sync(self, run_id: str, expected: RunState, target: RunState) -> AgentRun:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == run_id, ne_agent_runs.c.status == expected.value)
                .values(
                    status=target.value,
                    output_verdict=(
                        RunOutputVerdict.UNAVAILABLE.value if target in _TERMINAL_STATES else None
                    ),
                    version=ne_agent_runs.c.version + 1,
                    updated_at=now,
                    terminal_at=now if target in _TERMINAL_STATES else None,
                )
            )
            if result.rowcount != 1:
                raise LookupError("unknown Run or stale transition")
            row = conn.execute(_runs_with_lineage().where(ne_agent_runs.c.run_id == run_id)).one()
        return _run(row)

    async def link_attempt(
        self, *, run_id: str, predecessor_attempt_id: str | None = None
    ) -> RunAttemptLink:
        return await call_db(self._offload, self._link_attempt_sync, run_id, predecessor_attempt_id)

    def _link_attempt_sync(self, run_id: str, predecessor_attempt_id: str | None) -> RunAttemptLink:
        attempt_id = _id()
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(
                sa.select(ne_agent_runs.c.conversation_id)
                .where(ne_agent_runs.c.run_id == run_id)
                .with_for_update()
            ).one_or_none()
            if run is None:
                raise LookupError("unknown Run")
            prior = conn.execute(
                sa.select(
                    ne_agent_run_attempt_links.c.attempt_id,
                    ne_agent_run_attempt_links.c.attempt_number,
                )
                .where(ne_agent_run_attempt_links.c.run_id == run_id)
                .order_by(ne_agent_run_attempt_links.c.attempt_number.desc())
                .limit(1)
            ).one_or_none()
            expected_predecessor = None if prior is None else str(prior.attempt_id)
            if predecessor_attempt_id != expected_predecessor:
                raise ValueError("attempt predecessor is not the exact current Run attempt")
            number = 1 if prior is None else int(prior.attempt_number) + 1
            conn.execute(
                ne_agent_run_attempt_links.insert().values(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    conversation_id=str(run.conversation_id),
                    predecessor_attempt_id=predecessor_attempt_id,
                    attempt_number=number,
                    created_at=now,
                )
            )
        return RunAttemptLink(
            attempt_id=attempt_id,
            run_id=run_id,
            conversation_id=str(run.conversation_id),
            predecessor_attempt_id=predecessor_attempt_id,
            attempt_number=number,
            created_at=now,
        )

    async def get_attempt(
        self, *, run_id: str, attempt_id: str, owner_principal_id: str
    ) -> RunAttemptLink | None:
        return await call_db(
            self._offload, self._get_attempt_sync, run_id, attempt_id, owner_principal_id
        )

    def _get_attempt_sync(
        self, run_id: str, attempt_id: str, owner_principal_id: str
    ) -> RunAttemptLink | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_attempt_links)
                .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_attempt_links.c.run_id)
                .where(
                    ne_agent_run_attempt_links.c.run_id == run_id,
                    ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                    ne_agent_runs.c.owner_principal_id == owner_principal_id,
                )
            ).one_or_none()
        return None if row is None else _attempt(row)

    async def record_skill_activation(
        self,
        *,
        run_id: str,
        attempt_id: str,
        agent_id: str,
        agent_version: int,
        surface_key: str,
        pins: Sequence[tuple[str, int, str]],
        index_hash: str,
        surface_hash: str,
    ) -> RunSkillActivationReceipt:
        """Persist the immutable, content-free Skill activation before execution.

        The unique address is one deterministic Run attempt. An exact retry returns
        the original receipt; any changed pin or activation hash fails closed.

        ``pins`` may be EMPTY, and an empty receipt is a positive fact, not a
        degenerate one: an Agent that declares no Skill pins still materializes a
        (deliberately empty — see ``ne.skills.active_set.render_index``) surface,
        and this row records "this attempt activated exactly no Skills" under the
        same keyed hash and the same replay/mismatch discipline as any other
        activation. Absence of the row therefore keeps exactly one meaning — the
        attempt died before activation — instead of being ambiguous with
        "the Agent had nothing to activate". Exactness for Agents that DO pin
        Skills is enforced upstream (the worker refuses when activated != declared)
        and per-pin here; this floor is not that property.
        """

        _require_nonempty(
            run_id=run_id,
            attempt_id=attempt_id,
            agent_id=agent_id,
            surface_key=surface_key,
        )
        if agent_version < 1:
            raise ValueError("agent_version must be positive")
        _require_sha256("index_hash", index_hash)
        _require_sha256("surface_hash", surface_hash)
        canonical_pins: list[list[str | int]] = []
        skill_ids: set[str] = set()
        for skill_id, skill_version, content_hash in pins:
            _require_nonempty(skill_id=skill_id)
            if skill_id != skill_id.strip():
                raise ValueError("skill_id must be canonical")
            if skill_version < 1:
                raise ValueError("Skill pin version must be positive")
            _require_sha256("Skill pin content_hash", content_hash)
            if skill_id in skill_ids:
                raise ValueError("Skill activation contains duplicate pins")
            skill_ids.add(skill_id)
            canonical_pins.append([skill_id, skill_version, content_hash])
        # Zero canonical pins is valid — the empty receipt IS the record that a
        # zero-pin Agent activated exactly nothing (see the docstring above).
        pins_json = _canonical_json(canonical_pins)
        receipt_hash = _keyed_hash(
            "ne.agent.run.skill-activation.v1",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "surface_key": surface_key,
                "pins": canonical_pins,
                "index_hash": index_hash,
                "surface_hash": surface_hash,
            },
        )
        return await call_db(
            self._offload,
            self._record_skill_activation_sync,
            run_id,
            attempt_id,
            agent_id,
            agent_version,
            surface_key,
            pins_json,
            index_hash,
            surface_hash,
            receipt_hash,
        )

    def _record_skill_activation_sync(
        self,
        run_id: str,
        attempt_id: str,
        agent_id: str,
        agent_version: int,
        surface_key: str,
        pins_json: str,
        index_hash: str,
        surface_hash: str,
        receipt_hash: str,
    ) -> RunSkillActivationReceipt:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            _lock_run(conn, run_id)
            attempt = conn.execute(
                sa.select(
                    ne_agent_run_attempt_links.c.attempt_id,
                    ne_agent_runs.c.agent_id,
                    ne_agent_runs.c.agent_version,
                )
                .select_from(
                    ne_agent_run_attempt_links.join(
                        ne_agent_runs,
                        ne_agent_runs.c.run_id == ne_agent_run_attempt_links.c.run_id,
                    )
                )
                .where(
                    ne_agent_run_attempt_links.c.run_id == run_id,
                    ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                )
            ).one_or_none()
            if attempt is None:
                raise LookupError("unknown Run attempt")
            if str(attempt.agent_id) != agent_id or int(attempt.agent_version) != agent_version:
                raise RuntimeError("Skill activation does not match the immutable Run Agent pin")
            existing = conn.execute(
                sa.select(ne_agent_run_skill_activations).where(
                    ne_agent_run_skill_activations.c.run_id == run_id,
                    ne_agent_run_skill_activations.c.attempt_id == attempt_id,
                )
            ).one_or_none()
            if existing is not None:
                if not hmac.compare_digest(str(existing.receipt_hash), receipt_hash):
                    raise RuntimeError(
                        "Run attempt was replayed under a different Skill activation"
                    )
                return _skill_activation_receipt(existing)
            receipt_id = _id()
            conn.execute(
                ne_agent_run_skill_activations.insert().values(
                    activation_receipt_id=receipt_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    surface_key=surface_key,
                    pins_json=pins_json,
                    index_hash=index_hash,
                    surface_hash=surface_hash,
                    receipt_hash=receipt_hash,
                    created_at=now,
                )
            )
            row = conn.execute(
                sa.select(ne_agent_run_skill_activations).where(
                    ne_agent_run_skill_activations.c.activation_receipt_id == receipt_id
                )
            ).one()
        return _skill_activation_receipt(row)

    async def load_skill_activation(
        self, *, run_id: str, attempt_id: str, owner_principal_id: str
    ) -> RunSkillActivationReceipt | None:
        """Load only the exact persisted attempt receipt; never consult the Skill catalog."""

        def load() -> RunSkillActivationReceipt | None:
            with self._engine.connect() as conn:
                row = conn.execute(
                    sa.select(ne_agent_run_skill_activations)
                    .join(
                        ne_agent_runs,
                        ne_agent_runs.c.run_id == ne_agent_run_skill_activations.c.run_id,
                    )
                    .where(
                        ne_agent_run_skill_activations.c.run_id == run_id,
                        ne_agent_run_skill_activations.c.attempt_id == attempt_id,
                        ne_agent_runs.c.owner_principal_id == owner_principal_id,
                    )
                ).one_or_none()
            return None if row is None else _skill_activation_receipt(row)

        return await self._call_db(load)

    async def record_execution_profile(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        profile_version: int,
        profile_hash: str,
        input_schema: Mapping[str, object],
        ordered_plan: Sequence[Mapping[str, object]],
    ) -> RunExecutionProfileReceipt:
        """Freeze the exact deterministic profile observed before attempt execution."""

        _require_nonempty(run_id=run_id, attempt_id=attempt_id, workflow_id=workflow_id)
        if profile_version < 1:
            raise ValueError("profile_version must be positive")
        _require_sha256("profile_hash", profile_hash)
        schema_json = _canonical_json(dict(input_schema))
        plan_values = [dict(step) for step in ordered_plan]
        if not plan_values:
            raise ValueError("ordered deterministic plan must not be empty")
        plan_json = _canonical_json(plan_values)
        schema_hash = _hash(schema_json.encode("utf-8"))
        plan_hash = _hash(plan_json.encode("utf-8"))
        receipt_hash = _keyed_hash(
            "ne.agent.run.execution-profile.v1",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "workflow_id": workflow_id,
                "profile_version": profile_version,
                "profile_hash": profile_hash,
                "input_schema_hash": schema_hash,
                "ordered_plan_hash": plan_hash,
            },
        )
        return await self._call_db(
            self._record_execution_profile_sync,
            run_id,
            attempt_id,
            workflow_id,
            profile_version,
            profile_hash,
            schema_json,
            schema_hash,
            plan_json,
            plan_hash,
            receipt_hash,
        )

    def _record_execution_profile_sync(
        self,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        profile_version: int,
        profile_hash: str,
        schema_json: str,
        schema_hash: str,
        plan_json: str,
        plan_hash: str,
        receipt_hash: str,
    ) -> RunExecutionProfileReceipt:
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(
                sa.select(ne_agent_runs.c.strategy)
                .join(
                    ne_agent_run_attempt_links,
                    ne_agent_run_attempt_links.c.run_id == ne_agent_runs.c.run_id,
                )
                .where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                )
                .with_for_update()
            ).one_or_none()
            if run is None:
                raise LookupError("unknown Run attempt")
            if str(run.strategy) != "deterministic":
                raise RuntimeError("agentic Run cannot acquire a deterministic profile receipt")
            existing = conn.execute(
                sa.select(ne_agent_run_execution_profiles).where(
                    ne_agent_run_execution_profiles.c.run_id == run_id,
                    ne_agent_run_execution_profiles.c.attempt_id == attempt_id,
                )
            ).one_or_none()
            if existing is not None:
                if not hmac.compare_digest(str(existing.receipt_hash), receipt_hash):
                    raise RuntimeError("Run attempt was replayed under a different profile")
                return _execution_profile_receipt(existing)
            receipt_id = _id()
            conn.execute(
                ne_agent_run_execution_profiles.insert().values(
                    profile_receipt_id=receipt_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    workflow_id=workflow_id,
                    profile_version=profile_version,
                    profile_hash=profile_hash,
                    input_schema_json=schema_json,
                    input_schema_hash=schema_hash,
                    ordered_plan_json=plan_json,
                    ordered_plan_hash=plan_hash,
                    receipt_hash=receipt_hash,
                    created_at=now,
                )
            )
            row = conn.execute(
                sa.select(ne_agent_run_execution_profiles).where(
                    ne_agent_run_execution_profiles.c.profile_receipt_id == receipt_id
                )
            ).one()
        return _execution_profile_receipt(row)

    async def load_execution_profile(
        self, *, run_id: str, attempt_id: str, owner_principal_id: str
    ) -> RunExecutionProfileReceipt | None:
        """Load frozen attempt truth without reading the current profile registry."""

        def load() -> RunExecutionProfileReceipt | None:
            with self._engine.connect() as conn:
                row = conn.execute(
                    sa.select(ne_agent_run_execution_profiles)
                    .join(
                        ne_agent_runs,
                        ne_agent_runs.c.run_id == ne_agent_run_execution_profiles.c.run_id,
                    )
                    .where(
                        ne_agent_run_execution_profiles.c.run_id == run_id,
                        ne_agent_run_execution_profiles.c.attempt_id == attempt_id,
                        ne_agent_runs.c.owner_principal_id == owner_principal_id,
                    )
                ).one_or_none()
            return None if row is None else _execution_profile_receipt(row)

        return await self._call_db(load)

    async def load_authenticated_context(
        self, *, run_id: str, owner_principal_id: str
    ) -> AuthenticatedRequestContext:
        """Load and revalidate the exact verifier-authored delayed-work snapshot."""

        def load() -> AuthenticatedRequestContext:
            with self._engine.connect() as conn:
                row = conn.execute(
                    sa.select(ne_agent_run_authenticated_contexts).where(
                        ne_agent_run_authenticated_contexts.c.run_id == run_id,
                        ne_agent_run_authenticated_contexts.c.owner_principal_id
                        == owner_principal_id,
                    )
                ).one_or_none()
            if row is None:
                raise LookupError("Run authenticated context is unavailable")
            groups = json.loads(str(row.authorization_groups_json))
            if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
                raise RuntimeError("stored Run authenticated context is invalid")
            return AuthenticatedRequestContext(
                schema_version=int(row.schema_version),
                principal_id=str(row.owner_principal_id),
                authorization_groups=tuple(groups),
                department=None if row.department is None else str(row.department),
                issuer=str(row.issuer),
                expires_at=int(row.expires_at),
                context_hash=str(row.context_hash),
            )

        return await self._call_db(load)

    async def load_workflow_request(
        self,
        *,
        enqueue_id: str,
        run_id: str,
        attempt_id: str,
        owner_principal_id: str,
    ) -> Any:
        """Project one exact persisted dispatch target plus authentication snapshot."""

        from ne.runtime.run_finalizer import RunTurnTarget
        from ne.runtime.run_workflow import RunWorkflowRequest

        context = await self.load_authenticated_context(
            run_id=run_id, owner_principal_id=owner_principal_id
        )

        def load() -> Any:
            with self._engine.connect() as conn:
                row = conn.execute(
                    sa.select(
                        ne_agent_runs.c.config_hash,
                        ne_agent_run_enqueue_outbox.c.enqueue_id,
                        ne_agent_run_enqueue_outbox.c.action,
                        ne_agent_run_turn_targets,
                    )
                    .join(
                        ne_agent_run_enqueue_outbox,
                        ne_agent_run_enqueue_outbox.c.run_id == ne_agent_runs.c.run_id,
                    )
                    .join(
                        ne_agent_run_turn_targets,
                        ne_agent_run_turn_targets.c.run_id == ne_agent_runs.c.run_id,
                    )
                    .where(
                        ne_agent_runs.c.run_id == run_id,
                        ne_agent_runs.c.owner_principal_id == owner_principal_id,
                        ne_agent_run_enqueue_outbox.c.enqueue_id == enqueue_id,
                        ne_agent_run_enqueue_outbox.c.attempt_id == attempt_id,
                        ne_agent_run_enqueue_outbox.c.action.in_(("start", "retry")),
                        ne_agent_run_turn_targets.c.attempt_id == attempt_id,
                    )
                ).one_or_none()
            if row is None:
                raise LookupError("Run durable dispatch target is unavailable")
            target = RunTurnTarget(
                conversation_id=str(row.conversation_id),
                run_id=run_id,
                turn_id=str(row.turn_id),
                turn_sequence=int(row.turn_sequence),
                attachment_id=str(row.attachment_id),
                attempt_id=attempt_id,
            )
            return RunWorkflowRequest(
                dispatch_id=enqueue_id,
                run_id=run_id,
                owner_principal_id=owner_principal_id,
                config_hash=str(row.config_hash),
                target=target,
                authenticated_context=context,
            )

        return await self._call_db(load)

    async def load_pending_workflow_requests(self, *, limit: int = 100) -> tuple[Any, ...]:
        """Load pending exact dispatches for the production outbox reconciler.

        A successful DBOS enqueue is deliberately not marked here: the canonical
        workflow claim owns ``claimed_at`` and DBOS deduplicates on the persisted
        ``enqueue_id``. Re-reading a row after an API crash can therefore only
        re-enqueue the same workflow identity, never mint a second execution.
        """

        if limit < 1 or limit > 1000:
            raise ValueError("pending Run dispatch limit must be between 1 and 1000")

        def load_targets() -> tuple[tuple[str, str, str, str], ...]:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sa.select(
                        ne_agent_run_enqueue_outbox.c.enqueue_id,
                        ne_agent_run_enqueue_outbox.c.run_id,
                        ne_agent_run_enqueue_outbox.c.attempt_id,
                        ne_agent_runs.c.owner_principal_id,
                    )
                    .join(
                        ne_agent_runs,
                        ne_agent_runs.c.run_id == ne_agent_run_enqueue_outbox.c.run_id,
                    )
                    .where(
                        ne_agent_run_enqueue_outbox.c.action.in_(("start", "retry")),
                        ne_agent_run_enqueue_outbox.c.claimed_at.is_(None),
                        ne_agent_runs.c.status.in_(
                            (
                                RunState.SUBMITTED.value,
                                RunState.QUEUED.value,
                                RunState.CANCELLING.value,
                            )
                        ),
                    )
                    .order_by(ne_agent_run_enqueue_outbox.c.created_at)
                    .limit(limit)
                ).all()
            return tuple(
                (
                    str(row.enqueue_id),
                    str(row.run_id),
                    str(row.attempt_id),
                    str(row.owner_principal_id),
                )
                for row in rows
            )

        targets = await self._call_db(load_targets)
        return tuple(
            [
                await self.load_workflow_request(
                    enqueue_id=enqueue_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    owner_principal_id=owner_principal_id,
                )
                for enqueue_id, run_id, attempt_id, owner_principal_id in targets
            ]
        )

    @staticmethod
    def _claim_event_sequence(conn: sa.Connection, run_id: str) -> int:
        """CLAIM the next RunEvent sequence under the counter row lock.

        Two concurrent writers must not both read ``max(sequence)`` and choose the
        same successor.  The counter is seeded once, idempotently, from any legacy
        rows; the claim itself is an atomic ``last_sequence = last_sequence + 1``
        that Postgres serializes on the counter row (SQLite on its writer lock),
        so the claimed value is read back on the SAME connection under that lock.
        """
        conn.execute(
            _insert_ignore(ne_agent_run_event_seq, conn, index_elements=["run_id"]).from_select(
                ["run_id", "last_sequence"],
                sa.select(
                    sa.literal(run_id),
                    sa.func.coalesce(sa.func.max(ne_agent_run_events.c.sequence), 0),
                ).where(ne_agent_run_events.c.run_id == run_id),
            )
        )
        result = conn.execute(
            ne_agent_run_event_seq.update()
            .where(ne_agent_run_event_seq.c.run_id == run_id)
            .values(last_sequence=ne_agent_run_event_seq.c.last_sequence + 1)
        )
        if result.rowcount != 1:  # pragma: no cover - the seed above guarantees a row
            raise RuntimeError(
                f"RunEvent sequence claim for run {run_id!r} matched {result.rowcount} counters"
            )
        return int(
            conn.execute(
                sa.select(ne_agent_run_event_seq.c.last_sequence).where(
                    ne_agent_run_event_seq.c.run_id == run_id
                )
            ).scalar_one()
        )

    def _append_event(
        self,
        conn: sa.Connection,
        *,
        run_id: str,
        attempt_id: str,
        event_kind: str,
        event_status: str,
        safe_detail: Mapping[str, object],
        parent_event_id: str | None = None,
        step_id: str | None = None,
    ) -> RunEvent:
        forbidden = {"prompt", "output", "content", "arguments", "claim_text", "source_title"}
        if forbidden.intersection(safe_detail):
            raise ValueError("RunEvent safe detail cannot carry protected content fields")
        detail = _canonical_json(dict(safe_detail))
        sequence = self._claim_event_sequence(conn, run_id)
        event = RunEvent(
            event_id=_id(),
            run_id=run_id,
            attempt_id=attempt_id,
            sequence=sequence,
            event_kind=event_kind,
            event_status=event_status,
            parent_event_id=parent_event_id,
            step_id=step_id,
            safe_detail=dict(safe_detail),
            detail_hash=_hash(detail.encode("utf-8")),
            created_at=datetime.now(UTC),
        )
        conn.execute(
            ne_agent_run_events.insert().values(**{**asdict(event), "safe_detail": detail})
        )
        return event

    async def claim_evidence(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceDeclaration,
    ) -> EvidenceRecord:
        return await call_db(
            self._offload,
            self._claim_evidence_sync,
            run_id,
            owner_principal_id,
            attempt_id,
            declaration,
        )

    def _claim_evidence_sync(
        self,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceDeclaration,
    ) -> EvidenceRecord:
        _require_sha256("content_hash", declaration.content_hash)
        if not isinstance(declaration.assurance, EvidenceAssurance):
            raise TypeError("evidence assurance must use the typed EvidenceAssurance contract")
        _require_nonempty(
            run_id=run_id,
            owner_principal_id=owner_principal_id,
            attempt_id=attempt_id,
            turn_id=declaration.turn_id,
            step_id=declaration.step_id,
            tool_call_id=declaration.tool_call_id,
        )
        if declaration.read_ordinal < 0:
            raise ValueError("read_ordinal must be non-negative")
        # invocation_key ADDRESSES one source read; intent_hash carries what was
        # declared for it.  Splitting the two is what lets a replay under the same
        # address be discriminated instead of silently answered.
        invocation_key = _keyed_hash(
            "ne.evidence-invocation.v1",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "step_id": declaration.step_id,
                "tool_call_id": declaration.tool_call_id,
                "read_ordinal": declaration.read_ordinal,
            },
        )
        stamp_hash = _stamp_set_hash(declaration.source_stamps)
        locator_status = (
            "evidence_locator_unavailable" if declaration.locator_plaintext is None else "located"
        )
        intent_hash = _keyed_hash(
            "ne.evidence-intent.v1",
            _evidence_intent_fields(
                run_id=run_id,
                owner_principal_id=owner_principal_id,
                attempt_id=attempt_id,
                declaration=declaration,
                locator_status=locator_status,
                stamp_set_hash=stamp_hash,
            ),
        )
        try:
            with self._engine.begin() as conn:
                # Lock FIRST: the replay read below and the RunEvent sequence claim
                # further down must both happen under the owning Run's lock.
                _lock_run(conn, run_id)
                existing = _load_evidence(conn, invocation_key)
                if existing is not None:
                    return _replayed_evidence(existing, invocation_key, intent_hash)
                run = conn.execute(
                    sa.select(ne_agent_runs.c.agent_version).where(
                        ne_agent_runs.c.run_id == run_id,
                        ne_agent_runs.c.owner_principal_id == owner_principal_id,
                    )
                ).one_or_none()
                if run is None:
                    raise LookupError("unknown Run")
                evidence_id = _id()
                pending_id = _id()
                locator_id = _id() if declaration.locator_plaintext is not None else None
                now = datetime.now(UTC)
                pending_binding = (
                    run_id,
                    attempt_id,
                    declaration.step_id,
                    declaration.tool_call_id,
                    evidence_id,
                )
                pending_envelope = self._cipher.encrypt_for_target(
                    domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                    row_id=pending_id,
                    owner_principal_id=owner_principal_id,
                    binding=protected_payload_aad_binding(
                        target_kind="agent_run_tool_call",
                        target_binding=pending_binding,
                        content_status=None,
                    ),
                    plaintext=_pack_source_bound_payload(
                        declaration.pending_result_plaintext, declaration.source_stamps
                    ),
                )
                # ARBITRATION.  ``ON CONFLICT DO NOTHING`` against an UNCOMMITTED
                # conflicting row blocks until that writer finishes, so a zero
                # rowcount here means the winner is already committed and readable.
                # Losing rolls the whole transaction back — including the RunEvent
                # sequence claim below, which is why no sequence is ever burned.
                arbitration = conn.execute(
                    _insert_ignore(
                        ne_agent_pending_source_tool_results,
                        conn,
                        index_elements=["target_kind", "target_hash"],
                    )
                    .values(
                        pending_result_id=pending_id,
                        run_id=run_id,
                        owner_principal_id=owner_principal_id,
                        target_kind="agent_run_tool_call",
                        target_hash=protected_target_hash(
                            PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                            owner_principal_id,
                            "agent_run_tool_call",
                            pending_binding,
                        ),
                        target_binding=list(pending_binding),
                        stamp_set_hash=stamp_hash,
                        created_at=now,
                        **_envelope_values(pending_envelope),
                    )
                    .returning(ne_agent_pending_source_tool_results.c.pending_result_id)
                )
                if not arbitration.fetchall():
                    raise _ClaimRaceLost
                if locator_id is not None and declaration.locator_plaintext is not None:
                    locator_envelope = self._cipher.encrypt_for_target(
                        domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                        row_id=locator_id,
                        owner_principal_id=owner_principal_id,
                        binding=(run_id, attempt_id, declaration.step_id, declaration.tool_call_id),
                        plaintext=declaration.locator_plaintext,
                    )
                    conn.execute(
                        ne_agent_evidence_locators.insert().values(
                            locator_id=locator_id,
                            run_id=run_id,
                            owner_principal_id=owner_principal_id,
                            locator_hash=locator_envelope.plaintext_hash,
                            created_at=now,
                            **_envelope_values(locator_envelope),
                        )
                    )
                event = self._append_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_kind="evidence.produced",
                    event_status="preterminal",
                    safe_detail=declaration.safe_event_detail,
                    step_id=declaration.step_id,
                )
                conn.execute(
                    ne_agent_evidence_records.insert().values(
                        evidence_id=evidence_id,
                        run_id=run_id,
                        turn_id=declaration.turn_id,
                        owner_principal_id=owner_principal_id,
                        agent_version=int(run.agent_version),
                        attempt_id=attempt_id,
                        step_id=declaration.step_id,
                        tool_call_id=declaration.tool_call_id,
                        read_ordinal=declaration.read_ordinal,
                        invocation_key=invocation_key,
                        intent_hash=intent_hash,
                        locator_status=locator_status,
                        locator_id=locator_id,
                        pending_result_id=pending_id,
                        producing_event_id=event.event_id,
                        content_hash=declaration.content_hash,
                        created_at=now,
                    )
                )
                return EvidenceRecord(
                    evidence_id=evidence_id,
                    invocation_key=invocation_key,
                    locator_status=locator_status,  # type: ignore[arg-type]
                    locator_id=locator_id,
                    pending_result_id=pending_id,
                    producing_event_id=event.event_id,
                )
        except _ClaimRaceLost:
            pass
        with self._engine.begin() as conn:
            winner = _load_evidence(conn, invocation_key)
        if winner is None:  # pragma: no cover - the blocking arbitration guarantees a winner
            raise RuntimeError("evidence claim lost its arbitration with no visible winner")
        return _replayed_evidence(winner, invocation_key, intent_hash)

    async def claim_evidence_set(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceSetDeclaration,
    ) -> EvidenceSetRecord:
        """Atomically claim one protected result and its complete contributor set."""

        return await call_db(
            self._offload,
            self._claim_evidence_set_sync,
            run_id,
            owner_principal_id,
            attempt_id,
            declaration,
        )

    def _claim_evidence_set_sync(
        self,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        declaration: EvidenceSetDeclaration,
    ) -> EvidenceSetRecord:
        _require_sha256("content_hash", declaration.content_hash)
        _require_nonempty(
            run_id=run_id,
            owner_principal_id=owner_principal_id,
            attempt_id=attempt_id,
            turn_id=declaration.turn_id,
            step_id=declaration.step_id,
            tool_call_id=declaration.tool_call_id,
        )
        if declaration.read_ordinal < 0:
            raise ValueError("read_ordinal must be non-negative")
        if not declaration.rows or len(declaration.rows) > 128:
            raise ValueError("verified evidence set requires 1..128 rows")
        if len({row.row_anchor for row in declaration.rows}) != len(declaration.rows):
            raise ValueError("verified evidence row anchors must be unique")

        member_count = 0
        row_intents: list[dict[str, object]] = []
        all_stamps: list[SourceAuthorityStamp] = []
        for row in declaration.rows:
            _require_sha256("row_anchor", row.row_anchor)
            _require_sha256("receipt_hash", row.receipt_hash)
            if not row.contributors or len(row.contributors) > 128:
                raise ValueError("verified evidence row requires 1..128 contributors")
            if any(
                not isinstance(item.source_stamp, SourceAuthorityStamp) for item in row.contributors
            ):
                raise TypeError("verified evidence contributor requires a typed source stamp")
            if len(
                canonical_source_authority_stamps(
                    tuple(item.source_stamp for item in row.contributors)
                )
            ) != len(row.contributors):
                raise ValueError("verified evidence row contributors must be unique")
            contributors: list[dict[str, object]] = []
            for contributor in row.contributors:
                if not isinstance(contributor.assurance, EvidenceAssurance):
                    raise TypeError(
                        "verified evidence contributor requires typed EvidenceAssurance"
                    )
                _validate_locator_assurance(
                    contributor.locator_plaintext, contributor.assurance, required=True
                )
                contributors.append(
                    {
                        "locator_hash": _hash(contributor.locator_plaintext),
                        "stamp_hash": _stamp_set_hash((contributor.source_stamp,)),
                        "assurance": contributor.assurance.value,
                        "safe_event_detail_hash": _hash(
                            _canonical_json(dict(contributor.safe_event_detail)).encode("utf-8")
                        ),
                    }
                )
                all_stamps.append(contributor.source_stamp)
                member_count += 1
            row_intents.append(
                {
                    "row_anchor": row.row_anchor,
                    "receipt_hash": row.receipt_hash,
                    "contributors": contributors,
                }
            )
        if member_count > 1024:
            raise ValueError("verified evidence set exceeds 1024 contributors")

        source_stamps = canonical_source_authority_stamps(all_stamps)
        set_key = _keyed_hash(
            "ne.evidence-set-address.v1",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "step_id": declaration.step_id,
                "tool_call_id": declaration.tool_call_id,
                "read_ordinal": declaration.read_ordinal,
            },
        )
        intent_hash = _keyed_hash(
            "ne.evidence-set-intent.v1",
            {
                "run_id": run_id,
                "owner_principal_id": owner_principal_id,
                "attempt_id": attempt_id,
                "turn_id": declaration.turn_id,
                "step_id": declaration.step_id,
                "tool_call_id": declaration.tool_call_id,
                "read_ordinal": declaration.read_ordinal,
                "content_hash": declaration.content_hash,
                "pending_result_plaintext_hash": _hash(declaration.pending_result_plaintext),
                "rows": row_intents,
            },
        )
        try:
            with self._engine.begin() as conn:
                _lock_run(conn, run_id)
                existing = _load_evidence_set(conn, set_key)
                if existing is not None:
                    return _replayed_evidence_set(
                        conn, existing, set_key=set_key, intent_hash=intent_hash
                    )
                run = conn.execute(
                    sa.select(ne_agent_runs.c.agent_version).where(
                        ne_agent_runs.c.run_id == run_id,
                        ne_agent_runs.c.owner_principal_id == owner_principal_id,
                    )
                ).one_or_none()
                if run is None:
                    raise LookupError("unknown Run")

                evidence_set_id = _id()
                pending_id = _id()
                evidence_ids = tuple(_id() for _ in range(member_count))
                pending_binding = (
                    run_id,
                    attempt_id,
                    declaration.step_id,
                    declaration.tool_call_id,
                    set_key,
                )
                now = datetime.now(UTC)
                pending_envelope = self._cipher.encrypt_for_target(
                    domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                    row_id=pending_id,
                    owner_principal_id=owner_principal_id,
                    binding=protected_payload_aad_binding(
                        target_kind="agent_run_tool_call",
                        target_binding=pending_binding,
                        content_status=None,
                    ),
                    plaintext=_pack_source_bound_payload(
                        declaration.pending_result_plaintext,
                        source_stamps,
                        evidence_ids=evidence_ids,
                    ),
                )
                arbitration = conn.execute(
                    _insert_ignore(
                        ne_agent_pending_source_tool_results,
                        conn,
                        index_elements=["target_kind", "target_hash"],
                    )
                    .values(
                        pending_result_id=pending_id,
                        run_id=run_id,
                        owner_principal_id=owner_principal_id,
                        target_kind="agent_run_tool_call",
                        target_hash=protected_target_hash(
                            PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                            owner_principal_id,
                            "agent_run_tool_call",
                            pending_binding,
                        ),
                        target_binding=list(pending_binding),
                        stamp_set_hash=_stamp_set_hash(source_stamps),
                        created_at=now,
                        **_envelope_values(pending_envelope),
                    )
                    .returning(ne_agent_pending_source_tool_results.c.pending_result_id)
                )
                if not arbitration.fetchall():
                    raise _ClaimRaceLost

                records: list[EvidenceRecord] = []
                row_event_ids: list[str] = []
                flat_index = 0
                for row in declaration.rows:
                    row_ids: list[str] = []
                    for contributor_index, contributor in enumerate(row.contributors):
                        evidence_id = evidence_ids[flat_index]
                        locator_id = _id()
                        invocation_key = _keyed_hash(
                            "ne.evidence-set-member-address.v1",
                            {
                                "set_key": set_key,
                                "row_anchor": row.row_anchor,
                                "contributor_index": contributor_index,
                            },
                        )
                        member_intent = _keyed_hash(
                            "ne.evidence-set-member-intent.v1",
                            {
                                "set_intent_hash": intent_hash,
                                "evidence_id": evidence_id,
                                "row_anchor": row.row_anchor,
                                "contributor_index": contributor_index,
                            },
                        )
                        locator_envelope = self._cipher.encrypt_for_target(
                            domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                            row_id=locator_id,
                            owner_principal_id=owner_principal_id,
                            binding=(
                                run_id,
                                attempt_id,
                                declaration.step_id,
                                declaration.tool_call_id,
                                evidence_id,
                            ),
                            plaintext=contributor.locator_plaintext,
                        )
                        conn.execute(
                            ne_agent_evidence_locators.insert().values(
                                locator_id=locator_id,
                                run_id=run_id,
                                owner_principal_id=owner_principal_id,
                                locator_hash=locator_envelope.plaintext_hash,
                                created_at=now,
                                **_envelope_values(locator_envelope),
                            )
                        )
                        event = self._append_event(
                            conn,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            event_kind="evidence.produced",
                            event_status="preterminal",
                            safe_detail=contributor.safe_event_detail,
                            step_id=declaration.step_id,
                        )
                        conn.execute(
                            ne_agent_evidence_records.insert().values(
                                evidence_id=evidence_id,
                                run_id=run_id,
                                turn_id=declaration.turn_id,
                                owner_principal_id=owner_principal_id,
                                agent_version=int(run.agent_version),
                                attempt_id=attempt_id,
                                step_id=declaration.step_id,
                                tool_call_id=declaration.tool_call_id,
                                read_ordinal=declaration.read_ordinal,
                                invocation_key=invocation_key,
                                intent_hash=member_intent,
                                locator_status="located",
                                locator_id=locator_id,
                                pending_result_id=pending_id,
                                producing_event_id=event.event_id,
                                content_hash=declaration.content_hash,
                                created_at=now,
                            )
                        )
                        record = EvidenceRecord(
                            evidence_id=evidence_id,
                            invocation_key=invocation_key,
                            locator_status="located",
                            locator_id=locator_id,
                            pending_result_id=pending_id,
                            producing_event_id=event.event_id,
                        )
                        records.append(record)
                        row_ids.append(evidence_id)
                        flat_index += 1
                    # The row event names the read kind its own contributors declared
                    # (every contributor of one row shares it), so an empty-member
                    # COVERAGE row (NE-KG-MULTIKEY) is not mislabeled as a data row
                    # in the plaintext event trail.
                    row_read_kind = str(
                        dict(row.contributors[0].safe_event_detail).get("read_kind", "verified_t3")
                    )
                    row_event = self._append_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_kind="evidence.verified_row",
                        event_status="preterminal",
                        safe_detail={
                            "read_kind": row_read_kind,
                            "row_anchor": row.row_anchor,
                            "receipt_hash": row.receipt_hash,
                            "evidence_ids": row_ids,
                        },
                        step_id=declaration.step_id,
                    )
                    row_event_ids.append(row_event.event_id)

                conn.execute(
                    ne_agent_evidence_sets.insert().values(
                        evidence_set_id=evidence_set_id,
                        set_key=set_key,
                        intent_hash=intent_hash,
                        run_id=run_id,
                        turn_id=declaration.turn_id,
                        owner_principal_id=owner_principal_id,
                        agent_version=int(run.agent_version),
                        attempt_id=attempt_id,
                        step_id=declaration.step_id,
                        tool_call_id=declaration.tool_call_id,
                        read_ordinal=declaration.read_ordinal,
                        pending_result_id=pending_id,
                        evidence_ids=list(evidence_ids),
                        row_event_ids=row_event_ids,
                        content_hash=declaration.content_hash,
                        created_at=now,
                    )
                )
                return EvidenceSetRecord(
                    evidence_set_id=evidence_set_id,
                    set_key=set_key,
                    pending_result_id=pending_id,
                    evidence=tuple(records),
                    row_event_ids=tuple(row_event_ids),
                )
        except _ClaimRaceLost:
            pass
        with self._engine.begin() as conn:
            winner = _load_evidence_set(conn, set_key)
            if winner is None:  # pragma: no cover - arbitration makes it visible
                raise RuntimeError("evidence set claim lost with no visible winner")
            return _replayed_evidence_set(conn, winner, set_key=set_key, intent_hash=intent_hash)

    async def delivered_receipt_ids(self, *, run_id: str, attempt_id: str) -> tuple[str, ...]:
        """Every persisted delivery receipt id for one exact Run attempt, in set order.

        The order is the SAME (created_at, delivery_receipt_id) order the
        finalizer's observation comparison reads, so a delivery observer seeded
        from this list agrees with the durable set an approval resume (same
        attempt, fresh process) must account for.
        """

        return await self._call_db(self._delivered_receipt_ids_sync, run_id, attempt_id)

    def _delivered_receipt_ids_sync(self, run_id: str, attempt_id: str) -> tuple[str, ...]:
        with self._engine.connect() as conn:
            return tuple(
                str(value)
                for value in conn.execute(
                    sa.select(ne_agent_delivery_receipts.c.delivery_receipt_id)
                    .where(
                        ne_agent_delivery_receipts.c.run_id == run_id,
                        ne_agent_delivery_receipts.c.receiving_attempt_id == attempt_id,
                    )
                    .order_by(
                        ne_agent_delivery_receipts.c.created_at,
                        ne_agent_delivery_receipts.c.delivery_receipt_id,
                    )
                ).scalars()
            )

    async def claim_delivery(
        self,
        *,
        run_id: str | None,
        owner_principal_id: str,
        delivery_kind: Literal[
            "tool_result_served", "history_output_served", "direct_result_served", "withheld"
        ],
        protected_ref: str,
        protected_hash: str,
        source_stamps: Sequence[SourceAuthorityStamp],
        receiving_attempt_id: str | None,
    ) -> str:
        """Record one delivery decision, and — when Run-bound — its RunEvent atomically.

        Two shapes only. RUN-BOUND (``run_id`` and ``receiving_attempt_id`` both
        given) appends a real safe ``RunEvent`` in the same transaction under the
        Run's lock. STANDALONE (both ``None``) belongs to a Workflow with no Run,
        so it stores no event reference at all. A half-filled shape is refused here
        and again by ``ck_delivery_run_bound_shape`` in the database.
        """
        _require_sha256("protected_hash", protected_hash)
        _require_nonempty(owner_principal_id=owner_principal_id, protected_ref=protected_ref)
        if (run_id is None) != (receiving_attempt_id is None):
            raise ValueError("delivery must be Run-bound (Run and attempt) or standalone (neither)")
        if run_id is not None and receiving_attempt_id is not None:
            _require_nonempty(run_id=run_id, receiving_attempt_id=receiving_attempt_id)
        stamp_hash = _stamp_set_hash(source_stamps)
        decision = "withheld" if delivery_kind == "withheld" else "served"
        # ADDRESS: which protected ref was decided, for whom, on which attempt.
        delivery_key = _keyed_hash(
            "ne.delivery-address.v1",
            {
                "run_id": run_id,
                "owner_principal_id": owner_principal_id,
                "protected_ref": protected_ref,
                "receiving_attempt_id": receiving_attempt_id,
            },
        )
        # INTENT: the complete content of that decision. A second decision on the
        # same address carrying a different content hash, stamp set, kind or
        # verdict is a conflict, not a replay.
        intent_hash = _keyed_hash(
            "ne.delivery-intent.v1",
            {
                "run_id": run_id,
                "owner_principal_id": owner_principal_id,
                "protected_ref": protected_ref,
                "receiving_attempt_id": receiving_attempt_id,
                "delivery_kind": delivery_kind,
                "decision": decision,
                "protected_hash": protected_hash,
                "stamp_set_hash": stamp_hash,
            },
        )
        return await call_db(
            self._offload,
            self._claim_delivery_sync,
            delivery_key,
            intent_hash,
            run_id,
            owner_principal_id,
            delivery_kind,
            decision,
            protected_ref,
            protected_hash,
            stamp_hash,
            receiving_attempt_id,
        )

    def _claim_delivery_sync(
        self,
        delivery_key: str,
        intent_hash: str,
        run_id: str | None,
        owner: str,
        kind: str,
        decision: str,
        ref: str,
        protected_hash: str,
        stamp_hash: str,
        attempt_id: str | None,
    ) -> str:
        try:
            with self._engine.begin() as conn:
                if run_id is not None:
                    # Run-bound delivery shares the Run lock with evidence and
                    # terminal commits, so replay and event allocation serialize.
                    _lock_run(conn, run_id)
                existing = _load_delivery_receipt(conn, delivery_key)
                if existing is not None:
                    return _replayed_receipt_id(
                        existing,
                        intent_hash,
                        id_column="delivery_receipt_id",
                        message="delivery address was replayed under a different decided intent",
                    )
                event_id: str | None = None
                if run_id is not None and attempt_id is not None:
                    # The receipt's event is CREATED here, in this transaction, and
                    # the composite (event_id, run_id) foreign key makes that
                    # binding a database constraint: neither row outlives the other.
                    event_id = self._append_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_kind="delivery.decided",
                        event_status=decision,
                        safe_detail={
                            "delivery_kind": kind,
                            "decision": decision,
                            "protected_hash": protected_hash,
                            "stamp_set_hash": stamp_hash,
                        },
                    ).event_id
                receipt_id = _id()
                arbitration = conn.execute(
                    _insert_ignore(
                        ne_agent_delivery_receipts, conn, index_elements=["delivery_key"]
                    )
                    .values(
                        delivery_receipt_id=receipt_id,
                        delivery_key=delivery_key,
                        intent_hash=intent_hash,
                        run_id=run_id,
                        owner_principal_id=owner,
                        delivery_kind=kind,
                        decision=decision,
                        protected_ref=ref,
                        protected_hash=protected_hash,
                        stamp_set_hash=stamp_hash,
                        receiving_attempt_id=attempt_id,
                        event_id=event_id,
                        created_at=datetime.now(UTC),
                    )
                    .returning(ne_agent_delivery_receipts.c.delivery_receipt_id)
                )
                if not arbitration.fetchall():
                    # Losing rolls back this transaction, so the RunEvent minted
                    # above never reaches the log: no orphan event, no gap.
                    raise _ClaimRaceLost
                return receipt_id
        except _ClaimRaceLost:
            pass
        with self._engine.begin() as conn:
            winner = _load_delivery_receipt(conn, delivery_key)
        if winner is None:  # pragma: no cover - the blocking arbitration guarantees a winner
            raise RuntimeError("delivery claim lost its arbitration with no visible winner")
        return _replayed_receipt_id(
            winner,
            intent_hash,
            id_column="delivery_receipt_id",
            message="delivery address was replayed under a different decided intent",
        )

    async def load_run_claim_evidence(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        turn_id: str,
        protected_ref: str,
    ) -> RunClaimEvidenceObservation | RunClaimEvidenceSetObservation:
        """Load one exact served Knowledge result for a same-Run local claim.

        The protected reference is only an address. Every authoritative identity,
        delivery decision and content-bearing byte is reloaded from the owning Run
        store and checked under its exact owner/attempt/turn tuple.
        """

        return await call_db(
            self._offload,
            self._load_run_claim_evidence_sync,
            run_id,
            owner_principal_id,
            attempt_id,
            turn_id,
            protected_ref,
        )

    def _load_run_claim_evidence_sync(
        self,
        run_id: str,
        owner: str,
        attempt_id: str,
        turn_id: str,
        protected_ref: str,
    ) -> RunClaimEvidenceObservation | RunClaimEvidenceSetObservation:
        from ne.contracts.source_authority import (
            SourceBoundToolResultRef,
            SourceBoundToolResultSetRef,
        )
        from ne.runtime.source_results import decode_protected_reference

        _require_nonempty(
            run_id=run_id,
            owner_principal_id=owner,
            attempt_id=attempt_id,
            turn_id=turn_id,
            protected_ref=protected_ref,
        )
        decoded_reference = decode_protected_reference(protected_ref)
        if not isinstance(decoded_reference, SourceBoundToolResultRef):
            raise _claim_contract_unavailable()
        # NE-CLAIM-VERIFIED-SET. A verified (T3) read serves ONE protected result
        # backed by an evidence SET — one record per (row, contributor) — so this
        # load has two shapes, not one. Which shape applies is decided by the
        # SERVER'S OWN set row, never by the caller's reference; the two must then
        # agree, and `_load_run_claim_evidence_set_sync` re-checks that the sealed
        # reference names exactly the members that set row lists.
        with self._engine.connect() as conn:
            evidence_set = conn.execute(
                sa.select(ne_agent_evidence_sets).where(
                    ne_agent_evidence_sets.c.pending_result_id == decoded_reference.reference_id
                )
            ).one_or_none()
        if evidence_set is not None:
            if not isinstance(decoded_reference, SourceBoundToolResultSetRef):
                raise _claim_contract_unavailable()
            return self._load_run_claim_evidence_set_sync(
                cast(SourceBoundToolResultSetRef, decoded_reference),
                evidence_set,
                run_id=run_id,
                owner=owner,
                attempt_id=attempt_id,
                turn_id=turn_id,
            )
        if isinstance(decoded_reference, SourceBoundToolResultSetRef):
            # A set reference with no set behind it is not a single record.
            raise _claim_contract_unavailable()
        reference = cast(SourceBoundToolResultRef, decoded_reference)
        with self._engine.connect() as conn:
            evidence_rows = list(
                conn.execute(
                    sa.select(ne_agent_evidence_records)
                    .where(
                        ne_agent_evidence_records.c.run_id == run_id,
                        ne_agent_evidence_records.c.owner_principal_id == owner,
                        ne_agent_evidence_records.c.attempt_id == attempt_id,
                        ne_agent_evidence_records.c.turn_id == turn_id,
                        ne_agent_evidence_records.c.pending_result_id == reference.reference_id,
                        ne_agent_evidence_records.c.locator_status == "located",
                    )
                    .limit(2)
                ).all()
            )
            if not evidence_rows:
                # Nothing this turn served under that address.
                raise _claim_contract_unavailable()
            if len(evidence_rows) != 1:
                # Several located records under one pending result and NO set row
                # to declare them a set: the store disagrees with itself.
                raise _evidence_store_unavailable()
            evidence = evidence_rows[0]
            pending = conn.execute(
                sa.select(ne_agent_pending_source_tool_results).where(
                    ne_agent_pending_source_tool_results.c.pending_result_id
                    == reference.reference_id
                )
            ).one_or_none()
            locator = conn.execute(
                sa.select(ne_agent_evidence_locators).where(
                    ne_agent_evidence_locators.c.locator_id == evidence.locator_id,
                    ne_agent_evidence_locators.c.run_id == run_id,
                    ne_agent_evidence_locators.c.owner_principal_id == owner,
                )
            ).one_or_none()
            event = conn.execute(
                sa.select(ne_agent_run_events).where(
                    ne_agent_run_events.c.event_id == evidence.producing_event_id,
                    ne_agent_run_events.c.run_id == run_id,
                    ne_agent_run_events.c.attempt_id == attempt_id,
                )
            ).one_or_none()
            deliveries = list(
                conn.execute(
                    sa.select(ne_agent_delivery_receipts).where(
                        ne_agent_delivery_receipts.c.run_id == run_id,
                        ne_agent_delivery_receipts.c.owner_principal_id == owner,
                        ne_agent_delivery_receipts.c.receiving_attempt_id == attempt_id,
                        ne_agent_delivery_receipts.c.protected_ref == reference.reference_id,
                        ne_agent_delivery_receipts.c.delivery_kind == "tool_result_served",
                        ne_agent_delivery_receipts.c.decision == "served",
                    )
                ).all()
            )
            delivery_event = (
                None
                if len(deliveries) != 1
                else conn.execute(
                    sa.select(ne_agent_run_events).where(
                        ne_agent_run_events.c.event_id == deliveries[0].event_id,
                        ne_agent_run_events.c.run_id == run_id,
                        ne_agent_run_events.c.attempt_id == attempt_id,
                    )
                ).one_or_none()
            )
        if not deliveries:
            # The evidence exists but this attempt was never SERVED it, so the
            # answer cited something it was not given.
            raise _claim_contract_unavailable()
        if (
            pending is None
            or locator is None
            or event is None
            or len(deliveries) != 1
            or delivery_event is None
        ):
            raise _evidence_store_unavailable()
        delivery = deliveries[0]
        expected_binding = [
            run_id,
            attempt_id,
            str(evidence.step_id),
            str(evidence.tool_call_id),
            str(evidence.evidence_id),
        ]
        if (
            str(pending.run_id) != run_id
            or str(pending.owner_principal_id) != owner
            or str(pending.target_kind) != "agent_run_tool_call"
            or pending.target_binding != expected_binding
            or str(pending.target_hash)
            != protected_target_hash(
                PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                owner,
                "agent_run_tool_call",
                tuple(expected_binding),
            )
            or str(evidence.content_hash) != reference.result_hash
            or str(delivery.protected_hash) != reference.result_hash
            or str(event.event_kind) != "evidence.produced"
            or str(event.event_status) != "preterminal"
            or str(delivery_event.event_kind) != "delivery.decided"
            or str(delivery_event.event_status) != "served"
        ):
            raise _evidence_store_unavailable()
        try:
            packed = self._cipher.materialize_for_target(
                _row_envelope(
                    pending,
                    domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                    row_id_name="pending_result_id",
                ),
                domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                row_id=reference.reference_id,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind="agent_run_tool_call",
                    target_binding=tuple(expected_binding),
                    content_status=None,
                ),
            )
            protected_rendering, stamps, evidence_ids = _unpack_source_bound_payload_and_stamps(
                packed
            )
            locator_bytes = self._cipher.materialize_for_target(
                _row_envelope(
                    locator,
                    domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                    row_id_name="locator_id",
                ),
                domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                row_id=str(locator.locator_id),
                owner_principal_id=owner,
                binding=(run_id, attempt_id, str(evidence.step_id), str(evidence.tool_call_id)),
            )
        except (KeyEnvelopeError, ValueError) as exc:
            raise _evidence_store_unavailable() from exc
        canonical_stamps = canonical_source_authority_stamps(stamps)
        if (
            evidence_ids
            or not hmac.compare_digest(_hash(protected_rendering), reference.result_hash)
            or canonical_stamps != canonical_source_authority_stamps(reference.stamps)
            or not hmac.compare_digest(
                _stamp_set_hash(canonical_stamps), str(pending.stamp_set_hash)
            )
            or not hmac.compare_digest(
                _stamp_set_hash(canonical_stamps), str(delivery.stamp_set_hash)
            )
            or not hmac.compare_digest(_hash(locator_bytes), str(locator.locator_hash))
        ):
            raise _evidence_store_unavailable()
        try:
            detail = json.loads(str(event.safe_detail))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _evidence_store_unavailable() from exc
        if not isinstance(detail, dict) or frozenset(detail) != frozenset(
            {"read_kind", "evidence_status", "contributor_count", "evidence_assurance"}
        ):
            raise _evidence_store_unavailable()
        if not hmac.compare_digest(
            str(event.detail_hash),
            _hash(_canonical_json(detail).encode("utf-8")),
        ):
            raise _evidence_store_unavailable()
        try:
            delivery_detail = json.loads(str(delivery_event.safe_detail))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _evidence_store_unavailable() from exc
        expected_delivery_detail = {
            "delivery_kind": "tool_result_served",
            "decision": "served",
            "protected_hash": reference.result_hash,
            "stamp_set_hash": _stamp_set_hash(canonical_stamps),
        }
        if delivery_detail != expected_delivery_detail or not hmac.compare_digest(
            str(delivery_event.detail_hash),
            _hash(_canonical_json(expected_delivery_detail).encode("utf-8")),
        ):
            raise _evidence_store_unavailable()
        read_kind = detail.get("read_kind")
        assurance = detail.get("evidence_assurance")
        if (
            detail.get("evidence_status") != "located"
            or isinstance(detail.get("contributor_count"), bool)
            or not isinstance(detail.get("contributor_count"), int)
            or int(detail["contributor_count"]) < 1
            or (read_kind, assurance) not in {("search", "direct"), ("graph_path", "inferred")}
        ):
            raise _evidence_store_unavailable()
        delivery_receipt_hash = _keyed_hash(
            "ne.delivery-receipt-observation.v1",
            {
                "delivery_receipt_id": str(delivery.delivery_receipt_id),
                "intent_hash": str(delivery.intent_hash),
                "event_id": str(delivery.event_id),
            },
        )
        return RunClaimEvidenceObservation(
            # NE-REF-WIDTH: the reference's ID, not the reference. Everything in
            # this family already means the id by `protected_ref`: the delivery
            # receipts are matched above on
            # `ne_agent_delivery_receipts.c.protected_ref == reference.reference_id`,
            # `run_finalizer` FAILS a turn when
            # `row.pending_result_id != delivery.protected_ref`, and it uses
            # `delivery.protected_ref` directly as the `pending_result_id` primary
            # key. This one site echoed the CALLER'S argument instead of the id it
            # had just looked up by, so a 2,773-character reference reached
            # `DirectSearchEvidenceObservation.protected_ref: OpaqueId` (<=512) and
            # the first claim that ever resolved died there.
            #
            # The cap was never the problem. `reference_id` is itself `OpaqueId`
            # on `_StampedReference`, so this field and its value are now bounded
            # by the SAME declared type — the width cannot drift apart again as
            # references grow with their stamp sets.
            protected_ref=reference.reference_id,
            protected_result_hash=reference.result_hash,
            evidence_id=str(evidence.evidence_id),
            locator_id=str(locator.locator_id),
            locator_hash=str(locator.locator_hash),
            producing_event_id=str(evidence.producing_event_id),
            delivery_receipt_id=str(delivery.delivery_receipt_id),
            delivery_receipt_hash=delivery_receipt_hash,
            protected_rendering=protected_rendering,
            locator=locator_bytes,
            read_kind=cast(Literal["search", "graph_path"], read_kind),
            assurance=cast(Literal["direct", "inferred"], assurance),
        )

    def _load_run_claim_evidence_set_sync(
        self,
        reference: Any,
        evidence_set: Any,
        *,
        run_id: str,
        owner: str,
        attempt_id: str,
        turn_id: str,
    ) -> RunClaimEvidenceSetObservation:
        """Load one exact served verified-T3 result and its COMPLETE evidence set.

        The set arrives whole or not at all. The reference is only an address: the
        set row's own ``evidence_ids`` are authoritative, the sealed payload must
        name the same ids in the same order, every one of those records must be
        present and located under this exact owner/attempt/turn tuple, and every
        member's locator and producing event are re-read and re-hashed here. A
        member that is missing, extra, reordered or unreadable fails the load —
        there is no partial set and no "representative" member.
        """

        set_key = str(evidence_set.set_key)
        evidence_set_id = str(evidence_set.evidence_set_id)
        declared_ids = evidence_set.evidence_ids
        if (
            str(evidence_set.run_id) != run_id
            or str(evidence_set.owner_principal_id) != owner
            or str(evidence_set.attempt_id) != attempt_id
            or str(evidence_set.turn_id) != turn_id
        ):
            raise _claim_contract_unavailable()
        if (
            not isinstance(declared_ids, list)
            or not declared_ids
            or any(not isinstance(item, str) or not item for item in declared_ids)
            or len(set(declared_ids)) != len(declared_ids)
        ):
            raise _evidence_store_unavailable()
        member_ids = tuple(str(item) for item in declared_ids)
        # The reference the answer cited must name this set EXACTLY — same members,
        # same order. A reference naming a subset would let a claim be verified
        # against part of the table the model was shown.
        if tuple(reference.evidence_ids) != member_ids:
            raise _claim_contract_unavailable()

        step_id = str(evidence_set.step_id)
        tool_call_id = str(evidence_set.tool_call_id)
        expected_binding = [run_id, attempt_id, step_id, tool_call_id, set_key]
        with self._engine.connect() as conn:
            evidence_rows = {
                str(row.evidence_id): row
                for row in conn.execute(
                    sa.select(ne_agent_evidence_records).where(
                        ne_agent_evidence_records.c.run_id == run_id,
                        ne_agent_evidence_records.c.owner_principal_id == owner,
                        ne_agent_evidence_records.c.attempt_id == attempt_id,
                        ne_agent_evidence_records.c.turn_id == turn_id,
                        ne_agent_evidence_records.c.pending_result_id == reference.reference_id,
                        ne_agent_evidence_records.c.locator_status == "located",
                    )
                ).all()
            }
            pending = conn.execute(
                sa.select(ne_agent_pending_source_tool_results).where(
                    ne_agent_pending_source_tool_results.c.pending_result_id
                    == reference.reference_id
                )
            ).one_or_none()
            deliveries = list(
                conn.execute(
                    sa.select(ne_agent_delivery_receipts).where(
                        ne_agent_delivery_receipts.c.run_id == run_id,
                        ne_agent_delivery_receipts.c.owner_principal_id == owner,
                        ne_agent_delivery_receipts.c.receiving_attempt_id == attempt_id,
                        ne_agent_delivery_receipts.c.protected_ref == reference.reference_id,
                        ne_agent_delivery_receipts.c.delivery_kind == "tool_result_served",
                        ne_agent_delivery_receipts.c.decision == "served",
                    )
                ).all()
            )
            delivery_event = (
                None
                if len(deliveries) != 1
                else conn.execute(
                    sa.select(ne_agent_run_events).where(
                        ne_agent_run_events.c.event_id == deliveries[0].event_id,
                        ne_agent_run_events.c.run_id == run_id,
                        ne_agent_run_events.c.attempt_id == attempt_id,
                    )
                ).one_or_none()
            )
            locator_rows = {
                str(row.locator_id): row
                for row in conn.execute(
                    sa.select(ne_agent_evidence_locators).where(
                        ne_agent_evidence_locators.c.run_id == run_id,
                        ne_agent_evidence_locators.c.owner_principal_id == owner,
                        ne_agent_evidence_locators.c.locator_id.in_(
                            [
                                evidence_rows[member].locator_id
                                for member in member_ids
                                if member in evidence_rows
                                and evidence_rows[member].locator_id is not None
                            ]
                        ),
                    )
                ).all()
            }
            event_rows = {
                str(row.event_id): row
                for row in conn.execute(
                    sa.select(ne_agent_run_events).where(
                        ne_agent_run_events.c.run_id == run_id,
                        ne_agent_run_events.c.attempt_id == attempt_id,
                        ne_agent_run_events.c.event_id.in_(
                            [
                                evidence_rows[member].producing_event_id
                                for member in member_ids
                                if member in evidence_rows
                            ]
                        ),
                    )
                ).all()
            }

        if not deliveries:
            raise _claim_contract_unavailable()
        # COMPLETENESS. Every declared member is present and located, and no
        # located record hides under this pending result that the set never
        # declared.
        if len(evidence_rows) != len(member_ids) or any(
            member not in evidence_rows for member in member_ids
        ):
            raise _evidence_store_unavailable()
        if pending is None or len(deliveries) != 1 or delivery_event is None:
            raise _evidence_store_unavailable()
        delivery = deliveries[0]
        if (
            str(pending.run_id) != run_id
            or str(pending.owner_principal_id) != owner
            or str(pending.target_kind) != "agent_run_tool_call"
            or pending.target_binding != expected_binding
            or str(pending.target_hash)
            != protected_target_hash(
                PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                owner,
                "agent_run_tool_call",
                tuple(expected_binding),
            )
            or str(evidence_set.content_hash) != reference.result_hash
            or str(delivery.protected_hash) != reference.result_hash
            or str(delivery_event.event_kind) != "delivery.decided"
            or str(delivery_event.event_status) != "served"
        ):
            raise _evidence_store_unavailable()

        try:
            packed = self._cipher.materialize_for_target(
                _row_envelope(
                    pending,
                    domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                    row_id_name="pending_result_id",
                ),
                domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                row_id=reference.reference_id,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind="agent_run_tool_call",
                    target_binding=tuple(expected_binding),
                    content_status=None,
                ),
            )
            protected_rendering, stamps, sealed_ids = _unpack_source_bound_payload_and_stamps(
                packed
            )
        except (KeyEnvelopeError, ValueError) as exc:
            raise _evidence_store_unavailable() from exc
        canonical_stamps = canonical_source_authority_stamps(stamps)
        if (
            # The SEALED payload names the set's members: the set row, the sealed
            # payload and the cited reference are three independent statements of
            # the same membership and all three must agree.
            sealed_ids != member_ids
            or not hmac.compare_digest(_hash(protected_rendering), reference.result_hash)
            or canonical_stamps != canonical_source_authority_stamps(reference.stamps)
            or not hmac.compare_digest(
                _stamp_set_hash(canonical_stamps), str(pending.stamp_set_hash)
            )
            or not hmac.compare_digest(
                _stamp_set_hash(canonical_stamps), str(delivery.stamp_set_hash)
            )
        ):
            raise _evidence_store_unavailable()

        expected_delivery_detail = {
            "delivery_kind": "tool_result_served",
            "decision": "served",
            "protected_hash": reference.result_hash,
            "stamp_set_hash": _stamp_set_hash(canonical_stamps),
        }
        try:
            delivery_detail = json.loads(str(delivery_event.safe_detail))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _evidence_store_unavailable() from exc
        if delivery_detail != expected_delivery_detail or not hmac.compare_digest(
            str(delivery_event.detail_hash),
            _hash(_canonical_json(expected_delivery_detail).encode("utf-8")),
        ):
            raise _evidence_store_unavailable()

        members: list[RunClaimEvidenceSetMember] = []
        for member_id in member_ids:
            evidence = evidence_rows[member_id]
            locator = locator_rows.get(str(evidence.locator_id))
            event = event_rows.get(str(evidence.producing_event_id))
            if (
                locator is None
                or event is None
                or str(evidence.step_id) != step_id
                or str(evidence.tool_call_id) != tool_call_id
                or str(evidence.content_hash) != reference.result_hash
                or str(event.event_kind) != "evidence.produced"
                or str(event.event_status) != "preterminal"
            ):
                raise _evidence_store_unavailable()
            try:
                locator_bytes = self._cipher.materialize_for_target(
                    _row_envelope(
                        locator,
                        domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                        row_id_name="locator_id",
                    ),
                    domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                    row_id=str(locator.locator_id),
                    owner_principal_id=owner,
                    # A set member's locator is sealed to its OWN evidence id, so a
                    # locator cannot be replayed under a sibling member.
                    binding=(run_id, attempt_id, step_id, tool_call_id, member_id),
                )
            except (KeyEnvelopeError, ValueError) as exc:
                raise _evidence_store_unavailable() from exc
            if not hmac.compare_digest(_hash(locator_bytes), str(locator.locator_hash)):
                raise _evidence_store_unavailable()
            try:
                detail = json.loads(str(event.safe_detail))
            except (TypeError, json.JSONDecodeError) as exc:
                raise _evidence_store_unavailable() from exc
            # Two member shapes share one set (NE-KG-MULTIKEY): a data ROW member
            # (``verified_t3`` — carries its row anchor) and an empty-member
            # COVERAGE member (``verified_t3_empty`` — receipt only, no row to
            # anchor). Anything else is refused exactly as before.
            _row_detail_keys = frozenset(
                {
                    "read_kind",
                    "evidence_status",
                    "contributor_count",
                    "evidence_assurance",
                    "row_anchor",
                    "receipt_hash",
                }
            )
            _empty_detail_keys = _row_detail_keys - {"row_anchor"}
            if not isinstance(detail, dict) or frozenset(detail) not in (
                _row_detail_keys,
                _empty_detail_keys,
            ):
                raise _evidence_store_unavailable()
            if not hmac.compare_digest(
                str(event.detail_hash),
                _hash(_canonical_json(detail).encode("utf-8")),
            ):
                raise _evidence_store_unavailable()
            expected_read_kind = (
                "verified_t3" if frozenset(detail) == _row_detail_keys else "verified_t3_empty"
            )
            if (
                detail.get("evidence_status") != "located"
                or detail.get("read_kind") != expected_read_kind
                or detail.get("evidence_assurance") != "direct"
                or isinstance(detail.get("contributor_count"), bool)
                or not isinstance(detail.get("contributor_count"), int)
                or int(detail["contributor_count"]) < 1
            ):
                raise _evidence_store_unavailable()
            members.append(
                RunClaimEvidenceSetMember(
                    evidence_id=member_id,
                    locator_id=str(locator.locator_id),
                    locator_hash=str(locator.locator_hash),
                    producing_event_id=str(evidence.producing_event_id),
                    locator=locator_bytes,
                )
            )

        return RunClaimEvidenceSetObservation(
            protected_ref=reference.reference_id,
            protected_result_hash=reference.result_hash,
            evidence_set_id=evidence_set_id,
            set_key=set_key,
            delivery_receipt_id=str(delivery.delivery_receipt_id),
            delivery_receipt_hash=_keyed_hash(
                "ne.delivery-receipt-observation.v1",
                {
                    "delivery_receipt_id": str(delivery.delivery_receipt_id),
                    "intent_hash": str(delivery.intent_hash),
                    "event_id": str(delivery.event_id),
                },
            ),
            protected_rendering=protected_rendering,
            members=tuple(members),
        )

    async def claim_run_claim_verifier(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        attempt_id: str,
        claim_key: str,
        claim_content_hash: str,
        served_output_hash: str,
        span_start_byte: int,
        span_end_byte: int,
        verifier_method: str,
        verifier_version: str,
        result_hash: str,
    ) -> ClaimVerifierRecord:
        """Claim one exact preterminal verifier event; exact replay returns its identity."""

        return await call_db(
            self._offload,
            self._claim_run_claim_verifier_sync,
            run_id,
            owner_principal_id,
            attempt_id,
            claim_key,
            claim_content_hash,
            served_output_hash,
            span_start_byte,
            span_end_byte,
            verifier_method,
            verifier_version,
            result_hash,
        )

    def _claim_run_claim_verifier_sync(
        self,
        run_id: str,
        owner: str,
        attempt_id: str,
        claim_key: str,
        claim_content_hash: str,
        served_output_hash: str,
        span_start: int,
        span_end: int,
        verifier_method: str,
        verifier_version: str,
        result_hash: str,
    ) -> ClaimVerifierRecord:
        _require_nonempty(
            run_id=run_id,
            owner_principal_id=owner,
            attempt_id=attempt_id,
            claim_key=claim_key,
            verifier_method=verifier_method,
            verifier_version=verifier_version,
        )
        for name, value in (
            ("claim_content_hash", claim_content_hash),
            ("served_output_hash", served_output_hash),
            ("result_hash", result_hash),
        ):
            _require_sha256(name, value)
        if (
            isinstance(span_start, bool)
            or isinstance(span_end, bool)
            or span_start < 0
            or span_end <= span_start
        ):
            raise ValueError("claim verifier span must be non-empty")
        detail = {
            "claim_key": claim_key,
            "claim_content_hash": claim_content_hash,
            "served_output_hash": served_output_hash,
            "span_start_byte": span_start,
            "span_end_byte": span_end,
            "verifier_method": verifier_method,
            "verifier_version": verifier_version,
            "result_hash": result_hash,
        }
        with self._engine.begin() as conn:
            run = _lock_run(conn, run_id)
            if str(run.owner_principal_id) != owner:
                raise LookupError("unknown Run")
            if (
                conn.execute(
                    sa.select(ne_agent_run_attempt_links.c.attempt_id).where(
                        ne_agent_run_attempt_links.c.run_id == run_id,
                        ne_agent_run_attempt_links.c.attempt_id == attempt_id,
                    )
                ).scalar_one_or_none()
                is None
            ):
                raise LookupError("unknown Run attempt")
            rows = conn.execute(
                sa.select(ne_agent_run_events).where(
                    ne_agent_run_events.c.run_id == run_id,
                    ne_agent_run_events.c.attempt_id == attempt_id,
                    ne_agent_run_events.c.event_kind == "claim.verifier",
                )
            ).all()
            matches = []
            for row in rows:
                try:
                    existing_detail = json.loads(str(row.safe_detail))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("claim verifier event detail is invalid") from exc
                if (
                    isinstance(existing_detail, dict)
                    and existing_detail.get("claim_key") == claim_key
                ):
                    matches.append((row, existing_detail))
            if len(matches) > 1:
                raise ValueError("claim verifier local key is ambiguous")
            if matches:
                event, existing_detail = matches[0]
                if existing_detail != detail:
                    raise ValueError("claim verifier replay changed its exact intent")
                event_id = str(event.event_id)
            else:
                event_id = self._append_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_kind="claim.verifier",
                    event_status="verified",
                    safe_detail=detail,
                    step_id="run-finalizer",
                ).event_id
        verifier_receipt_id = str(
            uuid.uuid5(_CLAIM_VERIFIER_RECEIPT_NAMESPACE, f"{run_id}\0{event_id}")
        )
        receipt_hash = _hash(
            _canonical_json(
                [
                    run_id,
                    claim_key,
                    claim_content_hash,
                    served_output_hash,
                    span_start,
                    span_end,
                    verifier_method,
                    verifier_version,
                    result_hash,
                    event_id,
                ]
            ).encode("utf-8")
        )
        return ClaimVerifierRecord(
            verifier_receipt_id=verifier_receipt_id,
            event_id=event_id,
            receipt_hash=receipt_hash,
        )

    async def claim_protected_payload(
        self,
        *,
        domain: PrivatePayloadDomain,
        run_id: str | None,
        owner_principal_id: str,
        target_kind: str,
        target_binding: tuple[str, ...] = (),
        target: CanonicalTargetTuple | None = None,
        content_status: ProtectedContentStatus | None = None,
        plaintext: bytes,
        source_stamps: Sequence[SourceAuthorityStamp] = (),
    ) -> ProtectedPayloadRef:
        """Claim-or-load one exact target-bound protected row.

        This is not a general content bag: the domain fixes the legal target kinds,
        and every target component participates in both the idempotency key and AAD.
        """
        if domain not in _PROTECTED_TABLES:
            raise ValueError("domain is not a claimable protected-row kind")
        table, row_id_name, target_kinds = _PROTECTED_TABLES[domain]
        if target_kind not in target_kinds:
            raise ValueError("protected payload target kind is invalid for its domain")
        # A tupled kind derives its address FROM the tuple; a non-tupled kind keeps
        # its own binding. Supplying both, or neither, is a caller error rather than
        # a silent preference for whichever happens to be present.
        if is_claimable_target_kind(target_kind):
            if target is None:
                raise ValueError("a claimable protected target requires its canonical tuple")
            if target_binding:
                raise ValueError("a claimable protected target derives its binding from the tuple")
            # P1-1. The AAD and target hash bind target.run_id while the row PERSISTS
            # the separate run_id argument. Accepting a disagreement would let the
            # authenticated identity and the queryable identity name different Runs,
            # which poisons the address. Refuse BEFORE any write.
            if run_id != target.run_id:
                raise ValueError(
                    "protected payload run_id disagrees with its canonical target tuple"
                )
            target_binding = target.binding()
        else:
            if target is not None:
                raise ValueError("a non-tupled protected target has no canonical tuple")
            if not target_binding or any(not value for value in target_binding):
                raise ValueError("protected payload target binding must be complete")
        # The authenticated content discriminator is required exactly where the
        # schema requires it, and forbidden everywhere else.
        status_required = bool(table.info.get("content_status_required"))
        if status_required and content_status is None:
            raise ValueError("this protected domain requires an authenticated content status")
        if not status_required and content_status is not None:
            raise ValueError("this protected domain has no authenticated content status")
        if content_status is not None:
            # A model or client must not be able to declare source-free content that
            # carries source-derived bytes, nor hide stamps behind a legacy status.
            if content_status.carries_source_bytes and not source_stamps:
                raise ValueError("a source-bound container requires its authority stamp set")
            if not content_status.carries_source_bytes and source_stamps:
                raise ValueError("only a source-bound container may carry authority stamps")
        needs_stamps = domain in {
            PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
            PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
            PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
            PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE,
        }
        stamp_hash = _stamp_set_hash(source_stamps) if needs_stamps else None
        if not needs_stamps and source_stamps:
            stamp_hash = _stamp_set_hash(source_stamps)
        storage_plaintext = (
            _pack_source_bound_payload(plaintext, source_stamps) if source_stamps else plaintext
        )
        target_hash = protected_target_hash(domain, owner_principal_id, target_kind, target_binding)
        return await call_db(
            self._offload,
            self._claim_protected_payload_sync,
            table,
            row_id_name,
            domain,
            run_id,
            owner_principal_id,
            target_kind,
            target_binding,
            target_hash,
            storage_plaintext,
            stamp_hash,
            target,
            content_status,
        )

    def _claim_protected_payload_sync(
        self,
        table: sa.Table,
        row_id_name: str,
        domain: PrivatePayloadDomain,
        run_id: str | None,
        owner: str,
        target_kind: str,
        target_binding: tuple[str, ...],
        target_hash: str,
        plaintext: bytes,
        stamp_hash: str | None,
        target: CanonicalTargetTuple | None,
        content_status: ProtectedContentStatus | None,
    ) -> ProtectedPayloadRef:
        plaintext_hash = _hash(plaintext)
        tuple_columns: dict[str, object] = {}
        if target is None:
            tuple_columns = {column: None for column in _TARGET_TUPLE_COLUMNS}
        else:
            tuple_columns = dict(target.as_columns())
            tuple_columns.pop("run_id", None)
        row_id = _id()
        envelope = self._cipher.encrypt_for_target(
            domain=domain,
            row_id=row_id,
            owner_principal_id=owner,
            binding=protected_payload_aad_binding(
                target_kind=target_kind,
                target_binding=target_binding,
                content_status=content_status,
            ),
            plaintext=plaintext,
        )
        with self._engine.begin() as conn:
            # Speculative claim first, winner read second: a losing SELECT-then-INSERT
            # would surface the unique violation as a raw IntegrityError instead.
            claimed = conn.execute(
                _insert_ignore(table, conn, index_elements=["target_kind", "target_hash"])
                .values(
                    **{
                        row_id_name: row_id,
                        "run_id": run_id,
                        "owner_principal_id": owner,
                        "target_kind": target_kind,
                        "target_hash": target_hash,
                        "target_binding": list(target_binding),
                        **tuple_columns,
                        "content_status": None if content_status is None else content_status.value,
                        "stamp_set_hash": stamp_hash,
                        "created_at": datetime.now(UTC),
                        **_envelope_values(envelope),
                    }
                )
                .returning(table.c[row_id_name])
            )
            # RETURNING, not ``rowcount``: SQLAlchemy reports -1 for an INSERT with
            # no RETURNING clause, so a rowcount test here silently never fires and
            # every caller takes the replay branch.
            if claimed.fetchall():
                return ProtectedPayloadRef(
                    domain=domain,
                    row_id=row_id,
                    owner_principal_id=owner,
                    target_kind=target_kind,
                    target_hash=target_hash,
                    plaintext_hash=plaintext_hash,
                    stamp_set_hash=stamp_hash,
                )
            winner = conn.execute(
                sa.select(table).where(
                    table.c.target_kind == target_kind,
                    table.c.target_hash == target_hash,
                )
            ).one_or_none()
        if winner is None:  # pragma: no cover - the blocking arbitration guarantees a winner
            raise RuntimeError("protected payload claim lost its arbitration with no winner")
        winner_run_id = None if winner.run_id is None else str(winner.run_id)
        winner_stamp_hash = None if winner.stamp_set_hash is None else str(winner.stamp_set_hash)
        winner_status = (
            None if winner.content_status is None else ProtectedContentStatus(winner.content_status)
        )
        if not (
            hmac.compare_digest(str(winner.owner_principal_id), owner)
            and hmac.compare_digest(str(winner.plaintext_hash), plaintext_hash)
            and winner_run_id == run_id
            and winner_stamp_hash == stamp_hash
            and winner_status == content_status
            and int(winner.cipher_version) == envelope.cipher_version
            and hmac.compare_digest(str(winner.binding_hash), envelope.binding_hash)
        ):
            raise ValueError("protected target was replayed under a different declared intent")
        return ProtectedPayloadRef(
            domain=domain,
            row_id=str(getattr(winner, row_id_name)),
            owner_principal_id=owner,
            target_kind=target_kind,
            target_hash=target_hash,
            plaintext_hash=plaintext_hash,
            stamp_set_hash=stamp_hash,
        )

    async def load_pending_source_tool_result(
        self, *, reference_id: str, owner_principal_id: str
    ) -> StoredSourcePayload:
        return await call_db(
            self._offload,
            self._materialize_source_row_sync,
            PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
            reference_id,
            owner_principal_id,
            frozenset({"agent_run_tool_call", "standalone_workflow_tool_call"}),
            None,
        )

    async def load_evidence_projection_locator(
        self,
        *,
        owner_principal_id: str,
        run_id: str,
        locator_ref: str,
        expected_hash: str,
    ) -> StoredSourcePayload:
        """Open one exact same-Run locator with its retained source stamp set.

        The opaque locator id is joined back to its canonical EvidenceRecord and
        pending protected result before decryption.  Callers cannot supply a
        binding, source identity, stamp set, or plaintext window.
        """

        return await call_db(
            self._offload,
            self._load_evidence_projection_locator_sync,
            owner_principal_id,
            run_id,
            locator_ref,
            expected_hash,
        )

    def _load_evidence_projection_locator_sync(
        self,
        owner: str,
        run_id: str,
        locator_ref: str,
        expected_hash: str,
    ) -> StoredSourcePayload:
        if not all((owner, run_id, locator_ref, expected_hash)):
            raise EvidenceProjectionUnavailable
        with self._engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(
                        ne_agent_evidence_records.c.evidence_id,
                        ne_agent_evidence_records.c.attempt_id,
                        ne_agent_evidence_records.c.step_id,
                        ne_agent_evidence_records.c.tool_call_id,
                        ne_agent_evidence_records.c.pending_result_id,
                        ne_agent_evidence_records.c.content_hash,
                        ne_agent_evidence_locators,
                    )
                    .join(
                        ne_agent_evidence_locators,
                        sa.and_(
                            ne_agent_evidence_locators.c.locator_id
                            == ne_agent_evidence_records.c.locator_id,
                            ne_agent_evidence_locators.c.run_id
                            == ne_agent_evidence_records.c.run_id,
                            ne_agent_evidence_locators.c.owner_principal_id
                            == ne_agent_evidence_records.c.owner_principal_id,
                        ),
                    )
                    .where(
                        ne_agent_evidence_records.c.run_id == run_id,
                        ne_agent_evidence_records.c.owner_principal_id == owner,
                        ne_agent_evidence_records.c.locator_status == "located",
                        ne_agent_evidence_records.c.locator_id == locator_ref,
                        ne_agent_evidence_locators.c.locator_hash == expected_hash,
                    )
                    .limit(2)
                ).all()
            )
        if len(rows) != 1:
            raise EvidenceProjectionUnavailable
        row = rows[0]
        try:
            pending = self._materialize_source_row_sync(
                PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                str(row.pending_result_id),
                owner,
                frozenset({"agent_run_tool_call"}),
                (run_id, str(row.attempt_id), str(row.step_id), str(row.tool_call_id)),
            )
            if (
                not pending.stamps
                or not hmac.compare_digest(pending.plaintext_hash, str(row.content_hash))
                or (pending.evidence_ids and str(row.evidence_id) not in pending.evidence_ids)
            ):
                raise EvidenceProjectionUnavailable
            envelope = _row_envelope(
                row,
                domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                row_id_name="locator_id",
            )
            candidates = (
                (
                    run_id,
                    str(row.attempt_id),
                    str(row.step_id),
                    str(row.tool_call_id),
                    str(row.evidence_id),
                ),
                (run_id, str(row.attempt_id), str(row.step_id), str(row.tool_call_id)),
            )
            locator: bytes | None = None
            for binding in candidates:
                try:
                    locator = self._cipher.materialize_for_target(
                        envelope,
                        domain=PrivatePayloadDomain.EVIDENCE_LOCATOR,
                        row_id=locator_ref,
                        owner_principal_id=owner,
                        binding=binding,
                    )
                    break
                except KeyEnvelopeError:
                    continue
            if locator is None or not hmac.compare_digest(_hash(locator), expected_hash):
                raise EvidenceProjectionUnavailable
        except (KeyEnvelopeError, LookupError, ValueError, EvidenceProjectionUnavailable) as exc:
            raise EvidenceProjectionUnavailable from exc
        return StoredSourcePayload(
            plaintext=locator,
            stamps=pending.stamps,
            plaintext_hash=expected_hash,
        )

    async def load_evidence_projection_output(
        self,
        *,
        owner_principal_id: str,
        run_id: str,
        protected_ref: str,
        expected_hash: str,
    ) -> StoredSourcePayload:
        """Open only the exact finalized source output for an owned Run."""

        return await call_db(
            self._offload,
            self._load_evidence_projection_output_sync,
            owner_principal_id,
            run_id,
            protected_ref,
            expected_hash,
        )

    def _load_evidence_projection_output_sync(
        self,
        owner: str,
        run_id: str,
        protected_ref: str,
        expected_hash: str,
    ) -> StoredSourcePayload:
        if not all((owner, run_id, protected_ref, expected_hash)):
            raise EvidenceProjectionUnavailable
        with self._engine.connect() as conn:
            rows = list(
                conn.execute(
                    sa.select(ne_agent_protected_source_outputs.c.target_binding)
                    .select_from(ne_agent_run_finalizations)
                    .join(
                        ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_finalizations.c.run_id
                    )
                    .join(
                        ne_agent_protected_source_outputs,
                        sa.and_(
                            ne_agent_protected_source_outputs.c.protected_output_id
                            == ne_agent_run_finalizations.c.protected_output_ref,
                            ne_agent_protected_source_outputs.c.run_id
                            == ne_agent_run_finalizations.c.run_id,
                            ne_agent_protected_source_outputs.c.owner_principal_id
                            == ne_agent_runs.c.owner_principal_id,
                            ne_agent_protected_source_outputs.c.attempt_id
                            == ne_agent_run_finalizations.c.attempt_id,
                            ne_agent_protected_source_outputs.c.turn_id
                            == ne_agent_run_finalizations.c.turn_id,
                        ),
                    )
                    .where(
                        ne_agent_run_finalizations.c.run_id == run_id,
                        ne_agent_runs.c.owner_principal_id == owner,
                        ne_agent_run_finalizations.c.protected_output_kind
                        == "protected_source_output",
                        ne_agent_run_finalizations.c.protected_output_ref == protected_ref,
                        ne_agent_run_finalizations.c.protected_output_hash == expected_hash,
                        ne_agent_protected_source_outputs.c.target_kind
                        == AGENT_RUN_TURN_TARGET_KIND,
                    )
                    .limit(2)
                ).scalars()
            )
        if (
            len(rows) != 1
            or not isinstance(rows[0], list)
            or not rows[0]
            or any(not isinstance(item, str) or not item for item in rows[0])
        ):
            raise EvidenceProjectionUnavailable
        try:
            stored = self._materialize_source_row_sync(
                PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                protected_ref,
                owner,
                frozenset({AGENT_RUN_TURN_TARGET_KIND}),
                tuple(rows[0]),
            )
            if not stored.stamps or not hmac.compare_digest(stored.plaintext_hash, expected_hash):
                raise EvidenceProjectionUnavailable
            return stored
        except (LookupError, ValueError, EvidenceProjectionUnavailable) as exc:
            raise EvidenceProjectionUnavailable from exc

    async def load_evidence_projection_claim_slice(
        self,
        *,
        owner_principal_id: str,
        run_id: str,
        protected_ref: str,
        served_output_hash: str,
        span_start_byte: int,
        span_end_byte: int,
        claim_content_hash: str,
    ) -> StoredSourcePayload:
        """Open one stored source claim span without accepting a claim identity."""

        return await call_db(
            self._offload,
            self._load_evidence_projection_claim_slice_sync,
            owner_principal_id,
            run_id,
            protected_ref,
            served_output_hash,
            span_start_byte,
            span_end_byte,
            claim_content_hash,
        )

    def _load_evidence_projection_claim_slice_sync(
        self,
        owner: str,
        run_id: str,
        protected_ref: str,
        served_output_hash: str,
        span_start: int,
        span_end: int,
        claim_content_hash: str,
    ) -> StoredSourcePayload:
        if (
            not all((owner, run_id, protected_ref, served_output_hash, claim_content_hash))
            or isinstance(span_start, bool)
            or isinstance(span_end, bool)
            or span_start < 0
            or span_end <= span_start
        ):
            raise EvidenceProjectionUnavailable
        with self._engine.connect() as conn:
            claim_exists = conn.execute(
                sa.select(ne_agent_run_claims.c.claim_id)
                .where(
                    ne_agent_run_claims.c.run_id == run_id,
                    ne_agent_run_claims.c.owner_principal_id == owner,
                    ne_agent_run_claims.c.display_state == "protected_source_slice",
                    ne_agent_run_claims.c.protected_ref == protected_ref,
                    ne_agent_run_claims.c.served_output_hash == served_output_hash,
                    ne_agent_run_claims.c.span_start_byte == span_start,
                    ne_agent_run_claims.c.span_end_byte == span_end,
                    ne_agent_run_claims.c.claim_content_hash == claim_content_hash,
                )
                .limit(1)
            ).scalar_one_or_none()
        # Claim identity is deliberately not an input. Multiple server-minted
        # claims may truthfully cite the same exact byte span; every such row
        # resolves to the same Run-bound output/ref/hash tuple.
        if claim_exists is None:
            raise EvidenceProjectionUnavailable
        output = self._load_evidence_projection_output_sync(
            owner,
            run_id,
            protected_ref,
            served_output_hash,
        )
        if span_end > len(output.plaintext):
            raise EvidenceProjectionUnavailable
        sliced = output.plaintext[span_start:span_end]
        if not hmac.compare_digest(_hash(sliced), claim_content_hash):
            raise EvidenceProjectionUnavailable
        return StoredSourcePayload(
            plaintext=sliced,
            stamps=output.stamps,
            plaintext_hash=claim_content_hash,
        )

    async def load_protected_source_output(
        self,
        *,
        reference_id: str,
        owner_principal_id: str,
        target_id: str,
        attempt_id: str,
        delivery_site: Literal["model_send", "workflow_projection"],
    ) -> StoredSourcePayload:
        return await call_db(
            self._offload,
            self._materialize_source_output_row_sync,
            reference_id,
            owner_principal_id,
            target_id,
            attempt_id,
            delivery_site,
        )

    def _materialize_source_output_row_sync(
        self,
        reference_id: str,
        owner: str,
        target_id: str,
        attempt_id: str,
        delivery_site: Literal["model_send", "workflow_projection"],
    ) -> StoredSourcePayload:
        """Dispatch a protected-output load on the row's authenticated target kind.

        Two families share ``ne_agent_protected_source_outputs``. A mid-run
        protected output (``assistant_turn``/``workflow_execution``) is bound to
        exactly ``(target_id, attempt_id)`` and loads through the generic
        prefix-checked reader. A terminal history container
        (``agent_run_turn``, written by ``run_finalizer._protect_output``) is
        bound to a PRIOR turn's canonical target, so its load is instead pinned
        to the current Run's own Conversation history — and ONLY for the
        model-send delivery site (``delivery_site="model_send"``), under an
        attempt that is a real linked attempt of that Run. A workflow
        projection asking for a history container, or a model send naming an
        attempt the Run never linked, is a ``LookupError``: carried history is
        Conversation continuity for the Run's own model context, not a general
        read capability over prior answers.
        """
        table, row_id_name, _ = _PROTECTED_TABLES[PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT]
        with self._engine.connect() as conn:
            kind = conn.execute(
                sa.select(table.c.target_kind).where(table.c[row_id_name] == reference_id)
            ).scalar_one_or_none()
        if kind is not None and str(kind) == AGENT_RUN_TURN_TARGET_KIND:
            if delivery_site != "model_send":
                raise LookupError("protected source payload unavailable")
            return self._materialize_history_container_sync(
                reference_id, owner, target_id, attempt_id
            )
        return self._materialize_source_row_sync(
            PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
            reference_id,
            owner,
            frozenset({"assistant_turn", "workflow_execution"}),
            (target_id, attempt_id),
        )

    def _materialize_history_container_sync(
        self, reference_id: str, owner: str, current_run_id: str, current_attempt_id: str
    ) -> StoredSourcePayload:
        """Load one terminal source-bound container as CARRIED HISTORY, fail closed.

        The row must be the admitted terminal answer of a PRIOR turn of the
        current Run's own Conversation: same owner, department, Conversation
        agent and Agent, an earlier sequence, a terminal status, and the history
        attachment naming exactly this protected row — and the consuming
        ``current_attempt_id`` must be an attempt the current Run actually
        linked (``ne_agent_run_attempt_links``), so the delivery receipt this
        load feeds can only ever bind to a real attempt of the Run it serves.
        Everything else — ownership drift, another Conversation's answer, a
        forged binding, an arbitrary attempt id — is ``LookupError``, which the
        resolver maps to a non-committal refusal. This loader makes NO
        authority decision: the caller
        (``source_results.RunStoreSourceBoundResultResolver``) re-evaluates
        every returned stamp under the current principal and claims the
        delivery receipt.
        """
        table = ne_agent_protected_source_outputs
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(table).where(table.c.protected_output_id == reference_id)
            ).one_or_none()
            if row is None or str(row.owner_principal_id) != owner:
                raise LookupError("protected source payload unavailable")
            current = conn.execute(
                sa.select(ne_agent_runs).where(
                    ne_agent_runs.c.run_id == current_run_id,
                    ne_agent_runs.c.owner_principal_id == owner,
                )
            ).one_or_none()
            current_attempt = conn.execute(
                sa.select(ne_agent_run_attempt_links).where(
                    ne_agent_run_attempt_links.c.attempt_id == current_attempt_id,
                    ne_agent_run_attempt_links.c.run_id == current_run_id,
                )
            ).one_or_none()
            prior = conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == str(row.run_id))
            ).one_or_none()
            prior_target = conn.execute(
                sa.select(ne_agent_run_turn_targets).where(
                    ne_agent_run_turn_targets.c.run_id == str(row.run_id)
                )
            ).one_or_none()
            attachment = conn.execute(
                sa.select(ne_agent_run_history_attachments).where(
                    ne_agent_run_history_attachments.c.run_id == str(row.run_id)
                )
            ).one_or_none()
        if (
            current is None
            or current_attempt is None
            or prior is None
            or prior_target is None
            or attachment is None
        ):
            raise LookupError("protected source payload unavailable")
        admitted_prior_turn = (
            str(prior.conversation_id) == str(current.conversation_id)
            and str(prior.owner_principal_id) == owner
            and str(prior.department) == str(current.department)
            and str(prior.conversation_agent_name) == str(current.conversation_agent_name)
            and str(prior.agent_id) == str(current.agent_id)
            and int(prior.sequence) < int(current.sequence)
            and str(prior.status) in {state.value for state in _TERMINAL_STATES}
            and str(attachment.protected_ref) == reference_id
            and str(attachment.content_status)
            == ProtectedContentStatus.SOURCE_BOUND_CONTAINER.value
            and str(attachment.protected_domain)
            == PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT.value
        )
        if not admitted_prior_turn:
            raise LookupError("protected source payload unavailable")
        target = CanonicalTargetTuple(
            conversation_id=str(prior_target.conversation_id),
            run_id=str(prior_target.run_id),
            turn_id=str(prior_target.turn_id),
            turn_sequence=int(prior_target.turn_sequence),
            attachment_id=str(prior_target.attachment_id),
            attempt_id=str(prior_target.attempt_id),
        )
        try:
            plaintext, stamps, evidence_ids = self._open_source_bound_history_container(
                row,
                owner=owner,
                target_binding=target.binding(),
                served_output_hash=str(attachment.protected_hash),
            )
        except ProtectedTurnUnavailable as exc:
            raise LookupError("protected source payload unavailable") from exc
        return StoredSourcePayload(
            plaintext=plaintext,
            stamps=stamps,
            plaintext_hash=_hash(plaintext),
            evidence_ids=evidence_ids,
        )

    async def load_protected_source_invocation(
        self,
        *,
        reference_id: str,
        owner_principal_id: str,
        target_kind: str,
        target_id: str,
        attempt_id: str,
    ) -> StoredSourcePayload:
        return await call_db(
            self._offload,
            self._materialize_source_row_sync,
            PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
            reference_id,
            owner_principal_id,
            frozenset({target_kind}),
            (target_id, attempt_id),
        )

    async def load_protected_workflow_state(
        self,
        *,
        reference_id: str,
        owner_principal_id: str,
        workflow_execution_id: str,
    ) -> StoredSourcePayload:
        return await call_db(
            self._offload,
            self._materialize_source_row_sync,
            PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE,
            reference_id,
            owner_principal_id,
            frozenset({"agent_run", "standalone_workflow"}),
            (workflow_execution_id,),
        )

    def _materialize_source_row_sync(
        self,
        domain: PrivatePayloadDomain,
        reference_id: str,
        owner: str,
        allowed_kinds: frozenset[str],
        expected_prefix: tuple[str, ...] | None,
    ) -> StoredSourcePayload:
        table, row_id_name, _ = _PROTECTED_TABLES[domain]
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(table).where(table.c[row_id_name] == reference_id)
            ).one_or_none()
        if row is None or str(row.owner_principal_id) != owner:
            raise LookupError("protected source payload unavailable")
        kind = str(row.target_kind)
        raw_binding = row.target_binding
        if (
            kind not in allowed_kinds
            or not isinstance(raw_binding, list)
            or not raw_binding
            or any(not isinstance(value, str) or not value for value in raw_binding)
        ):
            raise LookupError("protected source payload unavailable")
        binding = tuple(raw_binding)
        if expected_prefix is not None and binding[: len(expected_prefix)] != expected_prefix:
            raise LookupError("protected source payload unavailable")
        expected_hash = protected_target_hash(domain, owner, kind, binding)
        if not hmac.compare_digest(str(row.target_hash), expected_hash):
            raise LookupError("protected source payload unavailable")
        try:
            packed = self._cipher.materialize_for_target(
                _row_envelope(row, domain=domain, row_id_name=row_id_name),
                domain=domain,
                row_id=reference_id,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind=kind,
                    target_binding=binding,
                    content_status=None,
                ),
            )
            plaintext, stamps, evidence_ids = _unpack_source_bound_payload_and_stamps(packed)
        except (KeyEnvelopeError, ValueError) as exc:
            raise LookupError("protected source payload unavailable") from exc
        if not hmac.compare_digest(_stamp_set_hash(stamps), str(row.stamp_set_hash)):
            raise LookupError("protected source payload unavailable")
        return StoredSourcePayload(
            plaintext=plaintext,
            stamps=stamps,
            plaintext_hash=_hash(plaintext),
            evidence_ids=evidence_ids,
        )

    async def materialize_source_free_turn(
        self,
        *,
        owner_principal_id: str,
        target: CanonicalTargetTuple,
        protected_ref: str,
        plaintext_hash: str,
    ) -> bytes:
        """Open one exact canonical source-free turn, never a generic protected row."""
        return await call_db(
            self._offload,
            self._materialize_source_free_turn_sync,
            owner_principal_id,
            target,
            protected_ref,
            plaintext_hash,
        )

    def _materialize_source_free_turn_sync(
        self, owner: str, target: CanonicalTargetTuple, protected_ref: str, plaintext_hash: str
    ) -> bytes:
        with self._engine.connect() as conn:
            return self._materialize_source_free_turn_from_connection(
                conn, owner, target, protected_ref, plaintext_hash
            )

    def _materialize_source_free_turn_from_connection(
        self,
        conn: sa.Connection,
        owner: str,
        target: CanonicalTargetTuple,
        protected_ref: str,
        plaintext_hash: str,
    ) -> bytes:
        table = ne_agent_protected_turn_contents
        row = conn.execute(
            sa.select(table).where(
                table.c.turn_content_id == protected_ref,
                table.c.owner_principal_id == owner,
                table.c.run_id == target.run_id,
                table.c.conversation_id == target.conversation_id,
                table.c.turn_id == target.turn_id,
                table.c.turn_sequence == target.turn_sequence,
                table.c.attachment_id == target.attachment_id,
                table.c.attempt_id == target.attempt_id,
            )
        ).one_or_none()
        if row is None:
            raise ProtectedTurnUnavailable
        try:
            status = ProtectedContentStatus(str(row.content_status))
            if (
                status is not ProtectedContentStatus.SOURCE_FREE
                or row.stamp_set_hash is not None
                or str(row.target_kind) != AGENT_RUN_TURN_TARGET_KIND
                or not hmac.compare_digest(str(row.plaintext_hash), plaintext_hash)
                or not hmac.compare_digest(
                    str(row.target_hash),
                    protected_target_hash(
                        PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                        owner,
                        AGENT_RUN_TURN_TARGET_KIND,
                        target.binding(),
                    ),
                )
            ):
                raise ProtectedTurnUnavailable
            plaintext = self._cipher.materialize_for_target(
                _row_envelope(
                    row,
                    domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                    row_id_name="turn_content_id",
                ),
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                row_id=protected_ref,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind=AGENT_RUN_TURN_TARGET_KIND,
                    target_binding=target.binding(),
                    content_status=status,
                ),
            )
            if not hmac.compare_digest(_hash(plaintext), plaintext_hash):
                raise ProtectedTurnUnavailable
            return plaintext
        except (KeyEnvelopeError, ValueError, ProtectedTurnUnavailable) as exc:
            raise ProtectedTurnUnavailable from exc

    def _history_reference_for_source_bound_turn_from_connection(
        self,
        conn: sa.Connection,
        owner: str,
        target: CanonicalTargetTuple,
        protected_ref: str,
        plaintext_hash: str,
    ) -> bytes:
        """Mint the opaque history reference for one source-bound terminal answer.

        NEVER returns the stored answer text. The finalizer stores an
        evidence-serving answer as a stamped container in
        ``ne_agent_protected_source_outputs`` (`_protect_output`); integrity of
        that container is verified here (target hash, AAD, stamp-set hash, and
        the history attachment's served-output ``plaintext_hash``), and the
        result is the encoded ``ProtectedSourceOutputRef`` — reference id,
        served-output hash and the exact stamp set. Integrity is not
        authorization: the model-send guard resolves this reference per
        delivery under the CURRENT principal (``source_results
        .RunStoreSourceBoundResultResolver.for_model_send`` via
        ``load_protected_source_output``), claims the ``history_output_served``
        or ``withheld`` receipt, and substitutes the fixed source-withheld text
        on any non-authorized decision.
        """
        table = ne_agent_protected_source_outputs
        row = conn.execute(
            sa.select(table).where(
                table.c.protected_output_id == protected_ref,
                table.c.owner_principal_id == owner,
                table.c.run_id == target.run_id,
                table.c.conversation_id == target.conversation_id,
                table.c.turn_id == target.turn_id,
                table.c.turn_sequence == target.turn_sequence,
                table.c.attachment_id == target.attachment_id,
                table.c.attempt_id == target.attempt_id,
            )
        ).one_or_none()
        if row is None:
            raise ProtectedTurnUnavailable
        _, stamps, _ = self._open_source_bound_history_container(
            row, owner=owner, target_binding=target.binding(), served_output_hash=plaintext_hash
        )
        # Deferred import: ``source_results`` (the delivery guard layer above
        # this store) imports ``run_store`` at module level; the pure reference
        # codec is borrowed at call time to avoid the cycle without duplicating
        # the encoding.
        from ne.runtime.source_results import encode_protected_reference

        try:
            reference = ProtectedSourceOutputRef(
                reference_id=protected_ref, output_hash=plaintext_hash, stamps=stamps
            )
        except ValueError as exc:  # pragma: no cover - stamp-set hash already pinned
            raise ProtectedTurnUnavailable from exc
        return encode_protected_reference(reference).encode("ascii")

    def _open_source_bound_history_container(
        self,
        row: Any,
        *,
        owner: str,
        target_binding: tuple[str, ...],
        served_output_hash: str,
    ) -> tuple[bytes, tuple[SourceAuthorityStamp, ...], tuple[str, ...]]:
        """Integrity-open one terminal source-bound container row — no authority.

        Verifies the finalizer's exact sealing (target hash over
        ``target_binding``, AAD-bound decryption, stamp-set hash, and the served
        answer's ``served_output_hash``) and returns the unpacked
        ``(plaintext, stamps, evidence_ids)`` triple, discarding nothing.
        Callers decide what may leave: history assembly keeps only the stamps
        (to mint the opaque reference) and the governed model-send loader
        returns the full payload for the decision path.
        """
        try:
            if (
                row.content_status is not None
                or row.stamp_set_hash is None
                or str(row.target_kind) != AGENT_RUN_TURN_TARGET_KIND
                or not hmac.compare_digest(
                    str(row.target_hash),
                    protected_target_hash(
                        PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                        owner,
                        AGENT_RUN_TURN_TARGET_KIND,
                        target_binding,
                    ),
                )
            ):
                raise ProtectedTurnUnavailable
            packed = self._cipher.materialize_for_target(
                _row_envelope(
                    row,
                    domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                    row_id_name="protected_output_id",
                ),
                domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                row_id=str(row.protected_output_id),
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind=AGENT_RUN_TURN_TARGET_KIND,
                    target_binding=target_binding,
                    content_status=None,
                ),
            )
            plaintext, stamps, evidence_ids = _unpack_source_bound_payload_and_stamps(packed)
            if not hmac.compare_digest(_stamp_set_hash(stamps), str(row.stamp_set_hash)):
                raise ProtectedTurnUnavailable
            if not hmac.compare_digest(_hash(plaintext), served_output_hash):
                raise ProtectedTurnUnavailable
            return plaintext, stamps, evidence_ids
        except (KeyEnvelopeError, ValueError, ProtectedTurnUnavailable) as exc:
            raise ProtectedTurnUnavailable from exc

    async def materialize_legacy_conversation_turn(
        self,
        *,
        owner_principal_id: str,
        conversation_id: str,
        attachment_id: str,
        attempt_id: str,
        slot: Literal["input", "terminal"],
        protected_ref: str,
        plaintext_hash: str,
    ) -> bytes:
        """Open one pre-010G source-free Conversation slot by its exact legacy address."""
        binding = conversation_turn_content_binding(
            conversation_id=conversation_id,
            attachment_id=attachment_id,
            attempt_id=attempt_id,
            slot=slot,
        )
        return await call_db(
            self._offload,
            self._materialize_legacy_conversation_turn_sync,
            owner_principal_id,
            binding,
            protected_ref,
            plaintext_hash,
        )

    def _materialize_legacy_conversation_turn_sync(
        self, owner: str, binding: tuple[str, ...], protected_ref: str, plaintext_hash: str
    ) -> bytes:
        table = ne_agent_protected_turn_contents
        expected_target_hash = protected_target_hash(
            PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            owner,
            CONVERSATION_TURN_TARGET_KIND,
            binding,
        )
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(table).where(
                    table.c.turn_content_id == protected_ref,
                    table.c.owner_principal_id == owner,
                    table.c.run_id.is_(None),
                    table.c.target_kind == CONVERSATION_TURN_TARGET_KIND,
                    table.c.target_hash == expected_target_hash,
                )
            ).one_or_none()
        if row is None:
            raise ProtectedTurnUnavailable
        try:
            status = ProtectedContentStatus(str(row.content_status))
            if (
                status is not ProtectedContentStatus.SOURCE_FREE
                or row.stamp_set_hash is not None
                or not hmac.compare_digest(str(row.plaintext_hash), plaintext_hash)
            ):
                raise ProtectedTurnUnavailable
            plaintext = self._cipher.materialize_for_target(
                _row_envelope(
                    row,
                    domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                    row_id_name="turn_content_id",
                ),
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                row_id=protected_ref,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind=CONVERSATION_TURN_TARGET_KIND,
                    target_binding=binding,
                    content_status=status,
                ),
            )
            if not hmac.compare_digest(_hash(plaintext), plaintext_hash):
                raise ProtectedTurnUnavailable
            return plaintext
        except (KeyEnvelopeError, ValueError, ProtectedTurnUnavailable) as exc:
            raise ProtectedTurnUnavailable from exc

    async def materialize_source_free_claim_slice(
        self,
        *,
        claim_id: str,
        owner_principal_id: str,
        target: CanonicalTargetTuple,
        protected_ref: str,
        served_output_hash: str,
    ) -> bytes:
        """Return the bytes of ONE stored source-free claim span. Nothing else.

        This is the only materialization surface above the cipher for protected
        rows, and it is deliberately not a decryptor with extra arguments:

        * The caller CANNOT select a span. The offsets come from the stored claim
          row, so nobody can walk a protected payload by asking for a window.
        * SOURCE-BOUND content cannot pass. A claim displayed as
          ``protected_source_slice``, or a protected row carrying an authority
          stamp set, is refused: releasing source-derived bytes is the
          source-authority resolver's decision, not this store's.
        * Every failure raises ONE :class:`ProtectedSliceUnavailable` with one
          message, so the surface is not an existence oracle.
        """
        return await call_db(
            self._offload,
            self._materialize_source_free_claim_slice_sync,
            claim_id,
            owner_principal_id,
            target,
            protected_ref,
            served_output_hash,
        )

    def _materialize_source_free_claim_slice_sync(
        self,
        claim_id: str,
        owner: str,
        target: CanonicalTargetTuple,
        protected_ref: str,
        served_output_hash: str,
    ) -> bytes:
        table = ne_agent_protected_turn_contents
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_run_claims.c.display_state,
                    ne_agent_run_claims.c.span_start_byte,
                    ne_agent_run_claims.c.span_end_byte,
                    ne_agent_run_claims.c.claim_content_hash,
                    table,
                )
                .join(
                    table,
                    sa.and_(
                        table.c.turn_content_id == ne_agent_run_claims.c.protected_ref,
                        table.c.owner_principal_id == ne_agent_run_claims.c.owner_principal_id,
                        table.c.conversation_id == ne_agent_run_claims.c.conversation_id,
                        table.c.run_id == ne_agent_run_claims.c.run_id,
                        table.c.turn_id == ne_agent_run_claims.c.turn_id,
                        table.c.turn_sequence == ne_agent_run_claims.c.turn_sequence,
                        table.c.attachment_id == ne_agent_run_claims.c.attachment_id,
                        table.c.attempt_id == ne_agent_run_claims.c.attempt_id,
                    ),
                )
                .where(
                    ne_agent_run_claims.c.claim_id == claim_id,
                    ne_agent_run_claims.c.owner_principal_id == owner,
                    ne_agent_run_claims.c.protected_ref == protected_ref,
                    ne_agent_run_claims.c.served_output_hash == served_output_hash,
                    ne_agent_run_claims.c.conversation_id == target.conversation_id,
                    ne_agent_run_claims.c.run_id == target.run_id,
                    ne_agent_run_claims.c.turn_id == target.turn_id,
                    ne_agent_run_claims.c.turn_sequence == target.turn_sequence,
                    ne_agent_run_claims.c.attachment_id == target.attachment_id,
                    ne_agent_run_claims.c.attempt_id == target.attempt_id,
                )
            ).one_or_none()
        # From here every rejection is the SAME outcome, deliberately.
        if row is None:
            raise ProtectedSliceUnavailable
        # AUTHORITY: the row's own AUTHENTICATED status decides, never the caller's
        # display_state. display_state is claim-authored, so trusting it would let a
        # caller relabel source-bearing content as a source-free slice -- exactly
        # what the authenticated status exists to make unrepresentable.
        if row.content_status is None:
            raise ProtectedSliceUnavailable
        try:
            stored_status = ProtectedContentStatus(str(row.content_status))
        except ValueError as exc:
            raise ProtectedSliceUnavailable from exc
        if stored_status is not ProtectedContentStatus.SOURCE_FREE:
            raise ProtectedSliceUnavailable
        # A caller whose display_state disagrees with the authenticated status is
        # refused too: the substitution itself is the signal, not a rounding error.
        if str(row.display_state) != "protected_source_free_slice":
            raise ProtectedSliceUnavailable
        if row.stamp_set_hash is not None:
            raise ProtectedSliceUnavailable
        if not is_claimable_target_kind(str(row.target_kind)):
            raise ProtectedSliceUnavailable
        if not hmac.compare_digest(
            str(row.target_hash),
            protected_target_hash(
                PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                owner,
                str(row.target_kind),
                target.binding(),
            ),
        ):
            raise ProtectedSliceUnavailable
        try:
            plaintext = self._cipher.materialize_for_target(
                _row_envelope(
                    row,
                    domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                    row_id_name="turn_content_id",
                ),
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                row_id=protected_ref,
                owner_principal_id=owner,
                binding=protected_payload_aad_binding(
                    target_kind=str(row.target_kind),
                    target_binding=target.binding(),
                    content_status=stored_status,
                ),
            )
        except (KeyEnvelopeError, ValueError) as exc:
            # ValueError too: `_row_envelope` CONSTRUCTS a PrivatePayloadEnvelope
            # inside this try, and its __post_init__ rejects a malformed column with
            # ValueError. Catching only KeyEnvelopeError let that escape as a
            # DISTINGUISHABLE outcome -- the exact oracle the single-outcome property
            # denies -- for a row written malformed-at-birth by something other than
            # this store's own writer.
            raise ProtectedSliceUnavailable from exc
        span_start, span_end = int(row.span_start_byte), int(row.span_end_byte)
        if span_end > len(plaintext):
            raise ProtectedSliceUnavailable
        sliced = plaintext[span_start:span_end]
        if not hmac.compare_digest(_hash(sliced), str(row.claim_content_hash)):
            raise ProtectedSliceUnavailable
        return sliced

    def current_key_readiness(self) -> PrivateKeyReadiness:
        """SYNCHRONOUS readiness seam for a later ``build_platform``.

        Deliberately synchronous and side-effect free: composition and readiness
        probes run before an event loop exists. PRE-009A only EXPOSES this; A3 is
        what consumes it. It reports counts, never key material.
        """
        current = self._cipher.current_key_id
        counts = self._key_reference_counts_sync()
        return PrivateKeyReadiness(
            current_key_id=current,
            rows_under_current_key=counts.get(current, 0),
            rows_under_other_keys=sum(v for k, v in counts.items() if k != current),
            reference_counts=dict(counts),
        )

    async def key_reference_counts(self) -> dict[str, int]:
        return await call_db(self._offload, self._key_reference_counts_sync)

    def _key_reference_counts_sync(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        tables = [
            ne_agent_run_inputs,
            ne_agent_evidence_locators,
            *[entry[0] for entry in _PROTECTED_TABLES.values()],
        ]
        with self._engine.connect() as conn:
            for table in tables:
                for key_id, count in conn.execute(
                    sa.select(table.c.key_id, sa.func.count()).group_by(table.c.key_id)
                ):
                    counts[str(key_id)] = counts.get(str(key_id), 0) + int(count)
        return counts

    async def rewrap_private_payload_keys(
        self, *, expected_old_key_id: str, target_key_id: str
    ) -> int:
        """Transactionally rewrap every referenced DEK while services are quiesced."""
        if not expected_old_key_id or not target_key_id:
            raise ValueError("rotation key ids must be non-empty")
        return await call_db(
            self._offload,
            self._rewrap_private_payload_keys_sync,
            expected_old_key_id,
            target_key_id,
        )

    def _rewrap_private_payload_keys_sync(
        self, expected_old_key_id: str, target_key_id: str
    ) -> int:
        table_specs: list[tuple[sa.Table, str, PrivatePayloadDomain]] = [
            (ne_agent_run_inputs, "input_id", PrivatePayloadDomain.RUN_INPUT),
            (ne_agent_evidence_locators, "locator_id", PrivatePayloadDomain.EVIDENCE_LOCATOR),
            *[(table, row_id, domain) for domain, (table, row_id, _) in _PROTECTED_TABLES.items()],
        ]
        changed = 0
        with self._engine.begin() as conn:
            all_key_ids = {
                str(value)
                for table, _, _ in table_specs
                for value in conn.execute(sa.select(table.c.key_id).distinct()).scalars()
            }
            unexpected = all_key_ids - {expected_old_key_id, target_key_id}
            if unexpected:
                raise ValueError("private payload store contains an unexpected key id")
            for table, row_id_name, domain in table_specs:
                rows = list(
                    conn.execute(
                        sa.select(table)
                        .where(table.c.key_id == expected_old_key_id)
                        .with_for_update()
                    ).all()
                )
                for row in rows:
                    rotated = self._cipher.rewrap(
                        _row_envelope(row, domain=domain, row_id_name=row_id_name),
                        target_key_id=target_key_id,
                    )
                    conn.execute(
                        table.update()
                        .where(
                            table.c[row_id_name] == getattr(row, row_id_name),
                            table.c.key_id == expected_old_key_id,
                        )
                        .values(
                            key_id=rotated.key_id,
                            key_nonce=rotated.key_nonce,
                            encrypted_dek=rotated.encrypted_dek,
                        )
                    )
                    changed += 1
            remaining = sum(
                int(
                    conn.execute(
                        sa.select(sa.func.count())
                        .select_from(table)
                        .where(table.c.key_id == expected_old_key_id)
                    ).scalar_one()
                )
                for table, _, _ in table_specs
            )
            if remaining:
                raise RuntimeError("private payload key rotation left old references")
        return changed

    async def commit_terminal_claims(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        turn_id: str,
        attempt_id: str,
        target: CanonicalTargetTuple,
        claims: Sequence[ClaimDeclaration],
        associations: Sequence[ClaimEvidenceDeclaration] = (),
        verifier_receipts: Sequence[ClaimVerifierDeclaration] = (),
        terminal_status: RunState,
    ) -> tuple[str, ...]:
        if terminal_status not in _TERMINAL_STATES:
            raise ValueError("terminal claim transaction requires a terminal Run state")
        # The tuple must BE this Run's turn, not merely a well-formed tuple.
        if (target.run_id, target.turn_id, target.attempt_id) != (run_id, turn_id, attempt_id):
            raise ValueError("terminal claim tuple does not address this Run turn")
        return await call_db(
            self._offload,
            self._commit_terminal_claims_sync,
            run_id,
            owner_principal_id,
            turn_id,
            attempt_id,
            target,
            tuple(claims),
            tuple(associations),
            tuple(verifier_receipts),
            terminal_status,
        )

    def _commit_terminal_claims_sync(
        self,
        run_id: str,
        owner: str,
        turn_id: str,
        attempt_id: str,
        target: CanonicalTargetTuple,
        claims: tuple[ClaimDeclaration, ...],
        associations: tuple[ClaimEvidenceDeclaration, ...],
        verifier_receipts: tuple[ClaimVerifierDeclaration, ...],
        status: RunState,
    ) -> tuple[str, ...]:
        if len({claim.claim_key for claim in claims}) != len(claims):
            raise ValueError("terminal claim keys must be unique")
        claim_keys = {claim.claim_key for claim in claims}
        association_by_key = {item.claim_key: item for item in associations}
        verifier_by_key = {item.claim_key: item for item in verifier_receipts}
        if (
            len(association_by_key) != len(associations)
            or not set(association_by_key) <= claim_keys
        ):
            raise ValueError("claim associations must bind unique declared local keys")
        if len(verifier_by_key) != len(verifier_receipts) or not set(verifier_by_key) <= claim_keys:
            raise ValueError("verifier receipts must bind unique declared local keys")
        for claim in claims:
            if (claim.association_status == "associated") != (
                claim.claim_key in association_by_key
            ):
                raise ValueError("associated claim requires exactly one evidence declaration")
            if (claim.claim_verdict == "verified") != (claim.claim_key in verifier_by_key):
                raise ValueError("Verified claim requires exactly one verifier receipt")
        terminal_intent_hash = _terminal_intent_hash(
            run_id=run_id,
            owner=owner,
            turn_id=turn_id,
            attempt_id=attempt_id,
            target=target,
            claims=claims,
            associations=associations,
            verifier_receipts=verifier_receipts,
            status=status,
        )
        now = datetime.now(UTC)
        claim_ids: list[str] = []
        claim_id_by_key: dict[str, str] = {}
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_agent_runs.c.agent_version, ne_agent_runs.c.status)
                .where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.owner_principal_id == owner,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise LookupError("unknown Run")
            if RunState(str(row.status)) in _TERMINAL_STATES:
                # REPLAY. The Run is already terminal, so the only honest answers
                # are "this is the same terminal intent, here are its claim ids" or
                # a refusal. Returning the stored ids without comparing intent
                # would answer a DIFFERENT terminal request with the first one's
                # claims — including the zero-claim case, where claim_count alone
                # cannot tell two turns apart.
                recorded = conn.execute(
                    sa.select(ne_agent_run_events.c.safe_detail)
                    .where(
                        ne_agent_run_events.c.run_id == run_id,
                        ne_agent_run_events.c.event_kind == "run.terminal",
                    )
                    .order_by(ne_agent_run_events.c.sequence.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if recorded is None:
                    raise ValueError("terminal Run carries no terminal RunEvent to replay against")
                detail = json.loads(str(recorded))
                if not hmac.compare_digest(
                    str(detail.get("terminal_intent_hash", "")), terminal_intent_hash
                ):
                    raise ValueError("terminal Run was replayed under a different terminal intent")
                return tuple(
                    str(value)
                    for value in conn.execute(
                        sa.select(ne_agent_run_claims.c.claim_id)
                        .where(ne_agent_run_claims.c.run_id == run_id)
                        .order_by(ne_agent_run_claims.c.claim_index)
                    ).scalars()
                )
            current_status = RunState(str(row.status))
            if status not in _TRANSITIONS[current_status]:
                raise ValueError(f"illegal Run transition {current_status.value}->{status.value}")
            for claim in claims:
                _require_sha256("served_output_hash", claim.served_output_hash)
                _require_sha256("claim_content_hash", claim.claim_content_hash)
                if claim.span_start_byte < 0 or claim.span_end_byte <= claim.span_start_byte:
                    raise ValueError("claim span must be non-empty UTF-8 byte offsets")
                if (
                    claim.association_status == "association_unavailable"
                    and claim.claim_verdict != "unavailable"
                ):
                    raise ValueError("unassociated claim must be unavailable")
                claim_id = _id()
                claim_ids.append(claim_id)
                claim_id_by_key[claim.claim_key] = claim_id
                conn.execute(
                    ne_agent_run_claims.insert().values(
                        claim_id=claim_id,
                        run_id=run_id,
                        turn_id=turn_id,
                        owner_principal_id=owner,
                        agent_version=int(row.agent_version),
                        conversation_id=target.conversation_id,
                        turn_sequence=target.turn_sequence,
                        attachment_id=target.attachment_id,
                        attempt_id=target.attempt_id,
                        **asdict(claim),
                        created_at=now,
                    )
                )
            for receipt in verifier_receipts:
                claim = next(item for item in claims if item.claim_key == receipt.claim_key)
                if (
                    receipt.claim_content_hash != claim.claim_content_hash
                    or receipt.served_output_hash != claim.served_output_hash
                    or receipt.span_start_byte != claim.span_start_byte
                    or receipt.span_end_byte != claim.span_end_byte
                ):
                    raise ValueError("verifier receipt does not bind its exact claim span")
                _require_sha256("verifier result_hash", receipt.result_hash)
                receipt_hash = _hash(
                    _canonical_json(
                        [
                            run_id,
                            receipt.claim_key,
                            receipt.claim_content_hash,
                            receipt.served_output_hash,
                            receipt.span_start_byte,
                            receipt.span_end_byte,
                            receipt.verifier_method,
                            receipt.verifier_version,
                            receipt.result_hash,
                            receipt.event_id,
                        ]
                    ).encode("utf-8")
                )
                conn.execute(
                    ne_agent_claim_verifier_receipts.insert().values(
                        verifier_receipt_id=_id(),
                        run_id=run_id,
                        **asdict(receipt),
                        receipt_hash=receipt_hash,
                        created_at=now,
                    )
                )
            for association in associations:
                _require_sha256("association locator_hash", association.locator_hash)
                _require_sha256(
                    "association served_citation_ref_hash", association.served_citation_ref_hash
                )
                evidence = conn.execute(
                    sa.select(
                        ne_agent_evidence_records.c.run_id,
                        ne_agent_evidence_records.c.producing_event_id,
                        ne_agent_evidence_locators.c.locator_hash,
                    )
                    .join(
                        ne_agent_evidence_locators,
                        ne_agent_evidence_locators.c.locator_id
                        == ne_agent_evidence_records.c.locator_id,
                    )
                    .where(ne_agent_evidence_records.c.evidence_id == association.evidence_id)
                ).one_or_none()
                if (
                    evidence is None
                    or str(evidence.run_id) != run_id
                    or str(evidence.producing_event_id) != association.producing_event_id
                    or str(evidence.locator_hash) != association.locator_hash
                ):
                    raise ValueError(
                        "claim evidence does not bind the exact same-Run locator/event"
                    )
                if association.delivery_receipt_id is not None:
                    delivery_run = conn.execute(
                        sa.select(ne_agent_delivery_receipts.c.run_id).where(
                            ne_agent_delivery_receipts.c.delivery_receipt_id
                            == association.delivery_receipt_id
                        )
                    ).scalar_one_or_none()
                    if delivery_run != run_id:
                        raise ValueError("claim evidence delivery receipt belongs to another Run")
                association_hash = _hash(
                    _canonical_json(
                        [
                            run_id,
                            claim_id_by_key[association.claim_key],
                            association.evidence_id,
                            association.locator_hash,
                            association.served_citation_ref_hash,
                            association.producing_event_id,
                            association.delivery_receipt_id or "",
                        ]
                    ).encode("utf-8")
                )
                conn.execute(
                    ne_agent_run_claim_evidence.insert().values(
                        association_id=_id(),
                        run_id=run_id,
                        claim_id=claim_id_by_key[association.claim_key],
                        evidence_id=association.evidence_id,
                        locator_hash=association.locator_hash,
                        served_citation_ref_hash=association.served_citation_ref_hash,
                        producing_event_id=association.producing_event_id,
                        delivery_receipt_id=association.delivery_receipt_id,
                        association_hash=association_hash,
                        created_at=now,
                    )
                )
            event = self._append_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_kind="run.terminal",
                event_status=status.value,
                # turn_id is an OPAQUE identifier, not content: without it two
                # zero-claim terminal intents on different turns are indistinguishable
                # in the log and to the replay comparison above.
                safe_detail={
                    "turn_id": turn_id,
                    "claim_count": len(claims),
                    "output_verdict": RunOutputVerdict.UNAVAILABLE.value,
                    "terminal_intent_hash": terminal_intent_hash,
                },
            )
            conn.execute(
                ne_agent_run_lifecycle_outbox.insert().values(
                    publication_id=_id(),
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_id=event.event_id,
                    terminal_status=status.value,
                    output_verdict=RunOutputVerdict.UNAVAILABLE.value,
                    outcome=(
                        "execution_complete" if status is RunState.COMPLETED else status.value
                    ),
                    terminal_intent_hash=terminal_intent_hash,
                    created_at=now,
                    claimed_at=None,
                    published_at=None,
                    audit_event_id=None,
                )
            )
            result = conn.execute(
                ne_agent_runs.update()
                .where(
                    ne_agent_runs.c.run_id == run_id,
                    ne_agent_runs.c.status.not_in(tuple(state.value for state in _TERMINAL_STATES)),
                )
                .values(
                    status=status.value,
                    output_verdict=RunOutputVerdict.UNAVAILABLE.value,
                    version=ne_agent_runs.c.version + 1,
                    updated_at=now,
                    terminal_at=now,
                )
            )
            if result.rowcount != 1:
                raise LookupError("Run terminal state changed concurrently")
        return tuple(claim_ids)

    async def record_audit_receipt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        run_event_id: str,
        decision_kind: RunAuditDecisionKind,
        audit_event_id: str,
        audit_sequence: int,
        audit_record_hash: str,
        config_hash: str,
        owner_principal_id: str,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> RunAuditReceipt:
        return await call_db(
            self._offload,
            lambda: self.record_audit_receipt_sync(
                run_id=run_id,
                attempt_id=attempt_id,
                run_event_id=run_event_id,
                decision_kind=decision_kind,
                audit_event_id=audit_event_id,
                audit_sequence=audit_sequence,
                audit_record_hash=audit_record_hash,
                config_hash=config_hash,
                owner_principal_id=owner_principal_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                approval_id=approval_id,
            ),
        )

    def record_audit_receipt_sync(
        self,
        *,
        run_id: str,
        attempt_id: str,
        run_event_id: str,
        decision_kind: RunAuditDecisionKind,
        audit_event_id: str,
        audit_sequence: int,
        audit_record_hash: str,
        config_hash: str,
        owner_principal_id: str,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> RunAuditReceipt:
        """Record one audit receipt and return the CANONICAL stored row.

        Returns the winner :class:`RunAuditReceipt` read inside the recording
        transaction itself, with the owning Run's principal enforced there too —
        so the caller needs NO second read to obtain or verify the receipt
        (perf/audit-intent-path: the old id-then-``get_audit_receipt`` shape cost
        one extra connection + joined select per audited decision).  The sync
        body is public so a caller already off-loop (``call_db``) can compose it
        with other blocking work in ONE thread hop.
        """
        _require_sha256("audit_record_hash", audit_record_hash)
        _require_sha256("config_hash", config_hash)
        _require_nonempty(
            run_id=run_id,
            attempt_id=attempt_id,
            run_event_id=run_event_id,
            audit_event_id=audit_event_id,
            owner_principal_id=owner_principal_id,
        )
        if audit_sequence < 1:
            raise ValueError("audit_sequence must be positive")
        address = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "run_event_id": run_event_id,
            "step_id": step_id,
            "tool_call_id": tool_call_id,
            "approval_id": approval_id,
            "decision_kind": decision_kind.value,
        }
        intent_key = _keyed_hash("ne.run-audit-address.v1", address)
        # The recorded audit fact itself — event id, sequence, record hash and the
        # generating config — is NOT part of the address, so it must be compared on
        # replay or the same address silently answers with a stale receipt.
        intent_hash = _keyed_hash(
            "ne.run-audit-intent.v1",
            {
                **address,
                "audit_event_id": audit_event_id,
                "audit_sequence": audit_sequence,
                "audit_record_hash": audit_record_hash,
                "config_hash": config_hash,
            },
        )
        receipt_id = _id()
        try:
            with self._engine.begin() as conn:
                try:
                    conn.execute(
                        _insert_ignore(
                            ne_agent_run_audit_receipts, conn, index_elements=["intent_key"]
                        ).values(
                            run_audit_receipt_id=receipt_id,
                            intent_key=intent_key,
                            intent_hash=intent_hash,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            run_event_id=run_event_id,
                            step_id=step_id,
                            tool_call_id=tool_call_id,
                            approval_id=approval_id,
                            decision_kind=decision_kind.value,
                            audit_event_id=audit_event_id,
                            audit_sequence=audit_sequence,
                            audit_record_hash=audit_record_hash,
                            config_hash=config_hash,
                            created_at=datetime.now(UTC),
                        )
                    )
                except IntegrityError as exc:
                    # ON CONFLICT arbitrates on intent_key ONLY. This row also carries
                    # uq_run_audit_exact_event and a unique audit_event_id, and Postgres
                    # checks those non-arbiter indexes in its own order — so a concurrent
                    # SAME-intent writer can raise here instead of being absorbed. Roll
                    # back and resolve it by reading below: a same-intent winner is a
                    # race, anything else is a genuine conflict.
                    raise _ClaimRaceLost from exc
                winner = _load_audit_receipt(conn, intent_key)
                if winner is None:  # pragma: no cover - insert-or-existing guarantees a row
                    raise RuntimeError("Run audit receipt upsert left no visible row")
                return _replayed_audit_receipt(winner, intent_hash, owner_principal_id)
        except _ClaimRaceLost:
            pass
        with self._engine.begin() as conn:
            winner = _load_audit_receipt(conn, intent_key)
        if winner is None:
            # Nothing holds this address, so the rejection came from a DIFFERENT
            # decision already bound to this exact Run event. Fail closed.
            raise ValueError("Run audit receipt conflicts with a different recorded decision")
        return _replayed_audit_receipt(winner, intent_hash, owner_principal_id)

    async def get_audit_receipt(
        self,
        *,
        run_id: str,
        run_audit_receipt_id: str,
        owner_principal_id: str,
    ) -> RunAuditReceipt | None:
        return await call_db(
            self._offload,
            self._get_audit_receipt_sync,
            run_id,
            run_audit_receipt_id,
            owner_principal_id,
        )

    def _get_audit_receipt_sync(
        self, run_id: str, run_audit_receipt_id: str, owner_principal_id: str
    ) -> RunAuditReceipt | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_audit_receipts)
                .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_audit_receipts.c.run_id)
                .where(
                    ne_agent_run_audit_receipts.c.run_id == run_id,
                    ne_agent_run_audit_receipts.c.run_audit_receipt_id == run_audit_receipt_id,
                    ne_agent_runs.c.owner_principal_id == owner_principal_id,
                )
            ).one_or_none()
        return None if row is None else _audit_receipt(row)

    async def migrate_conversation_turn_to_protected_refs(
        self,
        *,
        conversation_id: str,
        owner_principal_id: str,
        run_id: str | None,
        attachment_id: str,
        attempt_id: str,
        input_status: str,
        protected_input_ref: str,
        protected_input_hash: str,
        terminal_status: str,
        protected_terminal_ref: str,
        protected_terminal_hash: str,
    ) -> None:
        """Erase one legacy turn's plaintext ONLY once both protected rows are proven.

        This primitive lives HERE, not on the session store: it spans both the
        legacy Conversation tables and the protected-payload tables, and only this
        layer may depend on both. ``ne.runtime.store`` never imports this module.

        PRE-009A provides the loud primitive; PRE-009C alone classifies legacy
        content and switches production readers/writers while quiesced.
        """
        if input_status not in _MIGRATION_STATUSES or terminal_status not in _MIGRATION_STATUSES:
            raise ValueError("protected Conversation status is invalid")
        _require_migration_hash("protected_input_hash", protected_input_hash)
        _require_migration_hash("protected_terminal_hash", protected_terminal_hash)
        _require_nonempty(
            protected_input_ref=protected_input_ref,
            protected_terminal_ref=protected_terminal_ref,
        )
        await call_db(
            self._offload,
            self._migrate_conversation_turn_sync,
            conversation_id,
            owner_principal_id,
            run_id,
            attachment_id,
            attempt_id,
            input_status,
            protected_input_ref,
            protected_input_hash,
            terminal_status,
            protected_terminal_ref,
            protected_terminal_hash,
        )

    def _migrate_conversation_turn_sync(
        self,
        conversation_id: str,
        owner: str,
        run_id: str | None,
        attachment_id: str,
        attempt_id: str,
        input_status: str,
        input_ref: str,
        input_hash: str,
        terminal_status: str,
        terminal_ref: str,
        terminal_hash: str,
    ) -> None:
        table = ne_agent_protected_turn_contents
        with self._engine.begin() as conn:
            # Lock the LEGACY row first, then each referenced encrypted row inside
            # the verifier. Both sides stay pinned for the whole proof-then-erase.
            legacy = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.input_status,
                    ne_agent_conversation_turns.c.protected_input_ref,
                    ne_agent_conversation_turns.c.protected_input_hash,
                    ne_agent_conversation_turns.c.terminal_status,
                    ne_agent_conversation_turns.c.protected_terminal_ref,
                    ne_agent_conversation_turns.c.protected_terminal_hash,
                )
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                )
                .with_for_update()
            ).one_or_none()
            if legacy is None:
                raise LookupError("unknown Conversation turn")
            already = (
                None if legacy.protected_input_ref is None else str(legacy.protected_input_ref),
                None
                if legacy.protected_terminal_ref is None
                else str(legacy.protected_terminal_ref),
            )
            if already != (None, None) and already != (input_ref, terminal_ref):
                # Exact replay is idempotent; a CHANGED binding never overwrites an
                # already-migrated turn, because the plaintext it would have needed
                # to re-derive is already gone.
                raise ValueError("Conversation turn is already migrated to different refs")
            for slot, ref, content_hash, declared_status in (
                ("input", input_ref, input_hash, ProtectedContentStatus(input_status)),
                ("terminal", terminal_ref, terminal_hash, ProtectedContentStatus(terminal_status)),
            ):
                _assert_protected_row_matches_target(
                    conn,
                    self._cipher,
                    table=table,
                    row_id_column="turn_content_id",
                    row_id=ref,
                    owner_principal_id=owner,
                    run_id=run_id,
                    target_kind=CONVERSATION_TURN_TARGET_KIND,
                    target_binding=conversation_turn_content_binding(
                        conversation_id=conversation_id,
                        attachment_id=attachment_id,
                        attempt_id=attempt_id,
                        slot=slot,  # type: ignore[arg-type]
                    ),
                    content_status=declared_status,
                    plaintext_hash=content_hash,
                )
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                )
                .values(
                    private_context="",
                    served_output="",
                    citation_refs="[]",
                    served_citations="[]",
                    served_verified="",
                    served_grounded="",
                    input_status=input_status,
                    protected_input_ref=input_ref,
                    protected_input_hash=input_hash,
                    terminal_status=terminal_status,
                    protected_terminal_ref=terminal_ref,
                    protected_terminal_hash=terminal_hash,
                )
            )
            if result.rowcount != 1:
                raise LookupError("unknown Conversation turn")

    async def migrate_session_message_to_protected_ref(
        self,
        *,
        session_id: str,
        sequence: int,
        owner_principal_id: str,
        content_status: str,
        protected_content_ref: str,
        protected_content_hash: str,
    ) -> None:
        """Same contract as the turn primitive, plus a principal-attribution refusal."""
        if content_status not in _MIGRATION_STATUSES:
            raise ValueError("protected session status is invalid")
        _require_migration_hash("protected_content_hash", protected_content_hash)
        _require_nonempty(
            protected_content_ref=protected_content_ref,
            owner_principal_id=owner_principal_id,
        )
        await call_db(
            self._offload,
            self._migrate_session_message_sync,
            session_id,
            sequence,
            owner_principal_id,
            content_status,
            protected_content_ref,
            protected_content_hash,
        )

    def _migrate_session_message_sync(
        self, session_id: str, sequence: int, owner: str, status: str, ref: str, content_hash: str
    ) -> None:
        with self._engine.begin() as conn:
            message = conn.execute(
                sa.select(
                    ne_agent_session_messages.c.principal_id,
                    ne_agent_session_messages.c.protected_content_ref,
                )
                .where(
                    ne_agent_session_messages.c.session_id == session_id,
                    ne_agent_session_messages.c.seq == sequence,
                )
                .with_for_update()
            ).one_or_none()
            if message is None:
                raise LookupError("unknown session message")
            attributed = None if message.principal_id is None else str(message.principal_id)
            if attributed is not None and attributed != owner:
                raise ValueError("session message is attributed to a different principal")
            migrated = (
                None
                if message.protected_content_ref is None
                else str(message.protected_content_ref)
            )
            if migrated is not None and migrated != ref:
                raise ValueError("session message is already migrated to a different ref")
            _assert_protected_row_matches_target(
                conn,
                self._cipher,
                table=ne_agent_protected_turn_contents,
                row_id_column="turn_content_id",
                row_id=ref,
                owner_principal_id=owner,
                run_id=None,
                target_kind=SESSION_MESSAGE_TARGET_KIND,
                target_binding=session_message_content_binding(
                    session_id=session_id, sequence=sequence
                ),
                content_status=ProtectedContentStatus(status),
                plaintext_hash=content_hash,
            )
            result = conn.execute(
                ne_agent_session_messages.update()
                .where(
                    ne_agent_session_messages.c.session_id == session_id,
                    ne_agent_session_messages.c.seq == sequence,
                )
                .values(
                    payload="",
                    content_status=status,
                    protected_content_ref=ref,
                    protected_content_hash=content_hash,
                )
            )
            if result.rowcount != 1:
                raise LookupError("unknown session message")

    async def retire_ownerless_session_message(
        self,
        *,
        session_id: str,
        sequence: int,
        generation: str,
        reason: str,
        retirement_fence: str,
    ) -> None:
        """Erase one genuinely ownerless legacy row without authoring new identity.

        The receipt is deliberately row-local.  No owner, protected payload,
        hash/ref, canonical Run, RunEvent, or delivery receipt is inferred from
        bytes which predate trustworthy principal attribution.
        """
        if generation != LEGACY_TRANSPORT_GENERATION:
            raise ValueError("legacy transport generation is invalid")
        if reason != OWNERLESS_RETIREMENT_REASON:
            raise ValueError("ownerless retirement reason is invalid")
        _require_migration_hash("retirement_fence", retirement_fence)
        await call_db(
            self._offload,
            self._retire_ownerless_session_message_sync,
            session_id,
            sequence,
            generation,
            reason,
            retirement_fence,
        )

    def _retire_ownerless_session_message_sync(
        self, session_id: str, sequence: int, generation: str, reason: str, fence: str
    ) -> None:
        table = ne_agent_session_messages
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(table)
                .where(table.c.session_id == session_id, table.c.seq == sequence)
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise LookupError("unknown session message")
            receipt = (
                row.legacy_transport_retired,
                row.legacy_transport_generation,
                row.legacy_transport_reason,
                row.legacy_transport_fence,
            )
            expected = (True, generation, reason, fence)
            if any(value is not None for value in receipt):
                if receipt != expected:
                    raise ValueError("session message carries a different retirement receipt")
                if (
                    str(row.payload) != ""
                    or row.principal_id is not None
                    or row.trace_id is not None
                    or row.content_status is not None
                    or row.protected_content_ref is not None
                    or row.protected_content_hash is not None
                ):
                    raise ValueError("retired session message has an invalid protected shape")
                return
            if row.principal_id is not None:
                raise ValueError("session message has an attributed principal")
            protected = (
                row.content_status,
                row.protected_content_ref,
                row.protected_content_hash,
            )
            if any(value is not None for value in protected):
                raise ValueError(
                    "ownerless session message has a partial or foreign protected tuple"
                )
            result = conn.execute(
                table.update()
                .where(table.c.session_id == session_id, table.c.seq == sequence)
                .values(
                    payload="",
                    principal_id=None,
                    trace_id=None,
                    content_status=None,
                    protected_content_ref=None,
                    protected_content_hash=None,
                    legacy_transport_retired=True,
                    legacy_transport_generation=generation,
                    legacy_transport_reason=reason,
                    legacy_transport_fence=fence,
                )
            )
            if result.rowcount != 1:
                raise LookupError("unknown session message")
