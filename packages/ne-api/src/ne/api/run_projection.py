# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-safe projection over the dormant canonical Agent Run store."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

import sqlalchemy as sa
from anyio import to_thread
from sqlalchemy.exc import SQLAlchemyError

from ne.api.conversation_turns_wire import ConversationTurnView
from ne.api.runs_wire import (
    AgenticPlanStepView,
    DeterministicPlanStepView,
    RunClaimView,
    RunDetailView,
    RunEventPage,
    RunEventView,
    RunExecutionProfileReceiptView,
    RunPage,
    RunSkillActivationReceiptView,
    RunSkillPinView,
    RunView,
    SafeEventMetadata,
)
from ne.runtime.approval_store import PENDING_STATUSES, ne_approval_requests
from ne.runtime.model_continuity import ne_agent_run_model_steps
from ne.runtime.private_payloads import CanonicalTargetTuple
from ne.runtime.run_audit_receipts import ne_agent_run_audit_intents
from ne.runtime.run_checkpoint import ne_agent_run_approval_joins
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    ProtectedSliceUnavailable,
    ProtectedTurnUnavailable,
    RunAuditDecisionKind,
    RunEvent,
    RunExecutionProfileReceipt,
    RunSkillActivationReceipt,
    RunState,
    _run,
    ne_agent_evidence_records,
    ne_agent_run_attempt_links,
    ne_agent_run_claim_evidence,
    ne_agent_run_claims,
    ne_agent_run_finalizations,
    ne_agent_run_retry_lineage,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import ne_agent_run_execution_checkpoints

_LOG: Final[logging.Logger] = logging.getLogger(__name__)
_CURSOR_DOMAIN: Final[bytes] = b"ne.api.agent-resource-cursor.v1\x00"
_HASH_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization_receipt_hash",
        "before_config_hash",
        "config_hash",
        "context_snapshot_hash",
        "protected_hash",
        "stamp_set_hash",
        "terminal_intent_hash",
    }
)
#: The COMPLETE alphabet a projected model name may be spelled with, plus the
#: separator `fallback_from` joins them on. Mirrors the writer's own alphabet
#: (`ne.runtime.run_store._MODEL_NAME_ALPHABET`) on purpose: the rule that keeps
#: provider-controlled text off this wire is enforced where it is WRITTEN and
#: again where it is SERVED, so neither a stale row nor a future writer can put
#: an error message in front of a reader.
_MODEL_NAME_ALPHABET: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:_-"
)


def _routing_pair(position: object, passed_over: object) -> tuple[str | None, str | None]:
    """Admit a route position and its passed-over names, or admit neither.

    THE PAIR IS ONE FACT. ``"fallback"`` without names is legitimate (something
    was passed over and the names were not usable), but names without
    ``"fallback"`` is a contradiction, and a position outside the two the writer
    can produce is not a third position -- it is an unreadable record. Every
    such case fails to ``(None, None)``, which projects as ``"unavailable"``.
    """
    if position not in {"primary", "fallback"}:
        return None, None
    if position == "primary":
        return "primary", None
    if isinstance(passed_over, str) and _is_model_name_list(passed_over):
        return "fallback", passed_over
    return "fallback", None


def _is_model_name_list(value: str) -> bool:
    """Is this a comma-separated list of declared model names, and nothing else?"""
    if not value or len(value) > 256:
        return False
    return all(
        name and all(character in _MODEL_NAME_ALPHABET for character in name)
        for name in value.split(",")
    )


#: Registered tool identifiers ("read.knowledge.search"). Mirrors the shape the
#: deterministic plan wire already enforces for tool names; anything else -- an
#: error message, an argument, a sentence -- fails the check and is dropped.
_TOOL_NAME: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]{0,255}\Z")


def _is_model_name(value: object) -> bool:
    """Is this ONE declared model name -- never a list, never provider prose?"""
    return isinstance(value, str) and "," not in value and _is_model_name_list(value)


_TERMINAL_KEYS: Final[tuple[str, ...]] = (
    "turn_id",
    "claim_count",
    "terminal_intent_hash",
    # POST_DEMO item 4. WITHOUT THIS KEY THE WHOLE ITEM IS UNOBSERVABLE, and an
    # adversarial pass had to point that out: the finalizer writes
    # `approved_effect_complete` to the lifecycle outbox, ne-api never reads that
    # table, `runs_wire` has no outcome field, and every wire/SSE status comes from
    # `ne_agent_runs.status` — which this change deliberately leaves `completed`.
    # So the distinction cost a live CHECK migration and bought a fact only a hand
    # -written SQL query could see IN THE RECORD. ⚠️ Corrected: the served ANSWER
    # of a resumed run already says so in words (`run_execution.py:849` returns
    # "The approved action ... was executed after human approval"), so the fact
    # was not invisible — what was missing was a MACHINE-READABLE field, which is
    # a smaller claim than the one I first wrote. It is a server-defined closed
    # vocabulary and is re-checked as one in `_safe_scalar`.
    "outcome",
)
_DELIVERY_KEYS: Final[tuple[str, ...]] = (
    "delivery_kind",
    "decision",
    "protected_hash",
    "stamp_set_hash",
)
_MAX_CLAIMS: Final[int] = 100
# THE CAP IS A LIST-PAGE BUDGET, NOT A DISCLOSURE RULE. A page enriches up to
# 25 rows and each enrichment decrypts one protected terminal, so an uncapped
# list read is 25 whole answers on one response. The SINGLE-run read decrypts
# exactly one, and truncating it there served a 500-character stub where the
# answer belongs -- the last of the three limiters that blanked Ask. So the cap
# stays on every bulk path and is lifted, by an explicit flag, on the one-run
# read alone (`_enrich_run(..., full_output=True)`).
_MAX_OUTPUT_PREVIEW: Final[int] = 500
_AGENTIC_INTENT_STAGE: Final[re.Pattern[str]] = re.compile(r"agentic-intent:(0|[1-9][0-9]*)\Z")
_AGENTIC_CALL_STAGE: Final[re.Pattern[str]] = re.compile(r"agentic-call:(0|[1-9][0-9]*)\Z")
# WHAT A RETRIEVAL MAY SAY ABOUT ITSELF: closed vocabularies re-checked at the
# serving boundary exactly like `route_position`. The writer already constrains
# these (ne-api knowledge_source_evidence `_KnowledgeEvidenceDetail`), but the
# rule that keeps content off this wire is enforced where it is SERVED too.
_SAFE_READ_KINDS: Final[frozenset[str]] = frozenset(
    {"search", "graph_path", "verified_t3", "verified_t3_empty"}
)
_SAFE_EVIDENCE_STATUSES: Final[frozenset[str]] = frozenset(
    {"located", "evidence_locator_unavailable"}
)
_SAFE_EVIDENCE_ASSURANCES: Final[frozenset[str]] = frozenset({"direct", "inferred", "extracted"})
_SAFE_STRATEGIES: Final[frozenset[str]] = frozenset({"agentic", "deterministic"})
#: Every terminal outcome the platform itself can write (`commit_run_terminal_in_transaction`'s
#: own expectations, plus item 4's resumed-run word). A closed set, re-checked at
#: the serving boundary rather than trusted across the storage boundary.
_SAFE_TERMINAL_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "execution_complete",
        "approved_effect_complete",
        "refused",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
    }
)
# EVERY KIND THE CANONICAL STORE WRITES, because a run's timeline is the run's
# WORK and half of it was being clamped to "event.unavailable" -- on a real run,
# 21 of 41 rows. A kind is a server-enumerated NAME, never content: the safe
# projection is still exactly this allowlist, and anything outside it degrades.
_SAFE_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
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
    }
)
# The governed audit actions a Run's own receipt sink may write. These names are
# already persisted READABLE in the audit chain and name a class of step, never
# its content -- so the allowlist is what turns an opaque `run.audit.intent`
# row (whose safe detail is three hashes) into a step a reader can act on.
_SAFE_AUDIT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "run.delivery.decided",
        "run.evidence.produced",
        "run.execution.failed",
        "run.execution.pre_effect",
        "run.lifecycle.terminal",
        "run.model.pre_effect",
        "run.tool.pre_effect",
    }
)
_SAFE_DECISION_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in RunAuditDecisionKind)
_SAFE_EVENT_STATUSES: Final[frozenset[str]] = frozenset(
    {
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
    }
)


class CursorUnavailable(ValueError):
    """One non-oracular outcome for malformed, cross-resource, or stale cursors."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _receipt_hash(schema: str, fields: dict[str, object]) -> str:
    return _canonical_hash({"schema": schema, "fields": fields})


def _cursor_payload(kind: str, at: datetime, opaque_id: str) -> dict[str, object]:
    return {"at": _utc(at).isoformat(), "id": opaque_id, "kind": kind, "v": 1}


def encode_cursor(kind: str, at: datetime, opaque_id: str) -> str:
    body = json.dumps(
        _cursor_payload(kind, at, opaque_id),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    envelope = json.dumps(
        {
            "body": base64.urlsafe_b64encode(body).decode("ascii").rstrip("="),
            "check": hashlib.sha256(_CURSOR_DOMAIN + body).hexdigest(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")


def decode_cursor(token: str, *, kind: str) -> tuple[datetime, str]:
    try:
        envelope_raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        envelope = json.loads(envelope_raw.decode("ascii"))
        if not isinstance(envelope, dict) or set(envelope) != {"body", "check"}:
            raise CursorUnavailable
        encoded_body, supplied_check = envelope["body"], envelope["check"]
        if not isinstance(encoded_body, str) or not isinstance(supplied_check, str):
            raise CursorUnavailable
        body = base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4))
        expected = hashlib.sha256(_CURSOR_DOMAIN + body).hexdigest()
        if not hmac.compare_digest(expected, supplied_check):
            raise CursorUnavailable
        payload = json.loads(body.decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {"at", "id", "kind", "v"}:
            raise CursorUnavailable
        if payload["v"] != 1 or payload["kind"] != kind:
            raise CursorUnavailable
        opaque_id = payload["id"]
        if not isinstance(opaque_id, str) or not opaque_id or len(opaque_id) > 256:
            raise CursorUnavailable
        at_raw = payload["at"]
        if not isinstance(at_raw, str):
            raise CursorUnavailable
        at = datetime.fromisoformat(at_raw)
        if at.tzinfo is None:
            raise CursorUnavailable
        return _utc(at), opaque_id
    except (
        binascii.Error,
        CursorUnavailable,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise CursorUnavailable("resource cursor unavailable") from exc


def locate_table_engine(engines: Iterable[sa.Engine], table: str) -> sa.Engine | None:
    """Find the one logical database owning ``table``, or None if genuinely ambiguous.

    A production composition may hold separate runtime, audit, eval, and signal
    engine objects over the same database URL.  Seeing one table through each
    connection pool is one owner, not four competing stores.  Collapse those
    aliases by password-redacted URL while preserving the fail-closed result
    when DISTINCT databases both expose the table -- that case is a real
    ambiguity and picking either would be a guess.

    THE DEDUPLICATION IS THE WHOLE POINT, and omitting it cost a deploy cycle.
    The audit log resolved by a bare "exactly one candidate engine" count, and
    this deployment addresses one `neutron` database through both a runtime and
    an audit engine.  Two candidates, one database: the count said "ambiguous",
    the resolution produced None, and `audit_action` silently vanished from
    every event on the live wire while `decision_kind` -- read from the runtime
    engine -- kept arriving, which made the feature look half-built rather than
    unwired.  One resolver, used by both callers, is why that cannot recur.
    """

    found: dict[str, sa.Engine] = {}
    for engine in engines:
        try:
            if sa.inspect(engine).has_table(table):
                found.setdefault(engine.url.render_as_string(hide_password=True), engine)
        except SQLAlchemyError:
            continue
    return next(iter(found.values())) if len(found) == 1 else None


def locate_run_engine(engines: Iterable[sa.Engine]) -> sa.Engine | None:
    """Find the one logical database owning the canonical Run schema."""

    return locate_table_engine(engines, ne_agent_runs.name)


class RunProjectionState(Protocol):
    agent_run_store: AgentRunStore | None
    engines: list[sa.Engine]


def projection_from_state(state: RunProjectionState) -> AgentRunProjection:
    """Resolve the one composed read store/engine pair, or fail without guessing."""

    if state.agent_run_store is None:
        raise RuntimeError("agent run projection unavailable")
    engine = locate_run_engine(state.engines)
    if engine is None:
        raise RuntimeError("agent run projection unavailable")
    audit_engine: sa.Engine | None = None
    try:
        from ne.audit.store import ne_audit_log

        audit_engine = locate_table_engine(state.engines, ne_audit_log.name)
    except (ImportError, SQLAlchemyError):
        audit_engine = None
    if audit_engine is None:
        # LOUD, NEVER FATAL. Without the audit chain the projection still serves
        # every event, its kind, its status and its decision kind -- it simply
        # cannot NAME the governed action, so the console falls back to "Step".
        # That is a degraded read, not a failed request, and the reader must
        # never see an error for it. But it degraded in total silence for a
        # whole deploy cycle, so it says so here, once, at composition time.
        _LOG.warning(
            "agent run projection: no audit engine resolved over %d composed engine(s); "
            "run events will be served without their readable audit_action",
            len(state.engines),
        )
    return AgentRunProjection(state.agent_run_store, engine, audit_engine=audit_engine)


def run_view(row: AgentRun) -> RunView:
    strategy = row.strategy if row.strategy in _SAFE_STRATEGIES else "unavailable"
    display_strategy = strategy.title() if strategy != "unavailable" else "Unavailable"
    safe_verdicts = {"verified", "grounded", "refused", "unavailable"}
    if row.status is RunState.COMPLETED:
        verdict = row.output_verdict if row.output_verdict in safe_verdicts else "unavailable"
    elif row.status in {
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
        RunState.CONTEXT_EXHAUSTED,
        RunState.BUDGET_EXHAUSTED,
    }:
        verdict = "unavailable"
    else:
        verdict = None
    predecessor_run_id = row.predecessor_run_id
    root_run_id = row.root_run_id or row.run_id
    retry_ordinal = row.retry_ordinal
    if (retry_ordinal == 0 and (predecessor_run_id is not None or root_run_id != row.run_id)) or (
        retry_ordinal > 0 and (predecessor_run_id is None or root_run_id == row.run_id)
    ):
        # A malformed/missing retry join must never fabricate a visible chain.
        predecessor_run_id, root_run_id, retry_ordinal = None, row.run_id, 0
    requested_model = row.requested_model
    requested_lane = row.requested_model_lane
    if (
        not isinstance(requested_model, str)
        or not requested_model.strip()
        or len(requested_model) > 256
        or requested_lane not in {"frontier-api", "private-vllm"}
    ):
        requested_model, requested_lane = None, None
    served_model = row.served_model
    if not isinstance(served_model, str) or not served_model.strip() or len(served_model) > 256:
        served_model = None
    # WHY THAT MODEL SERVED. Both halves fail closed to "unavailable"/None
    # together: a route position we cannot vouch for must never read as
    # "primary" (which is a positive claim that nothing was attempted first),
    # and a fallback whose passed-over names are malformed still reports the
    # FALLBACK -- something was passed over -- with no invented name for it,
    # which is a case the console already renders.
    route_position = row.route_position if row.route_position in {"primary", "fallback"} else None
    fallback_from = row.fallback_from
    if route_position != "fallback" or not (
        isinstance(fallback_from, str) and _is_model_name_list(fallback_from)
    ):
        fallback_from = None
    return RunView(
        run_id=row.run_id,
        conversation_id=row.conversation_id,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        sequence=row.sequence,
        version=row.version,
        predecessor_run_id=predecessor_run_id,
        root_run_id=root_run_id,
        retry_ordinal=retry_ordinal,
        display_label=f"Run {row.sequence} · {display_strategy} · {_utc(row.created_at):%H:%M}",
        strategy=cast(Any, strategy),
        status=row.status.value,
        output_verdict=cast(Any, verdict),
        model_policy_id=(
            row.model_policy_id
            if isinstance(row.model_policy_id, str) and 0 < len(row.model_policy_id) <= 256
            else None
        ),
        model_request_mode=(
            "unavailable"
            if row.model_policy_id is None
            else "auto"
            if requested_model is None
            else "explicit"
        ),
        context_snapshot_hash=(
            row.context_snapshot_hash
            if isinstance(row.context_snapshot_hash, str)
            and len(row.context_snapshot_hash) == 64
            and all(character in "0123456789abcdef" for character in row.context_snapshot_hash)
            else None
        ),
        requested_model=requested_model,
        requested_model_lane=cast(Any, requested_lane),
        served_model=served_model,
        route_position=cast(Any, route_position or "unavailable"),
        fallback_from=fallback_from,
        output_preview=row.output_preview,
        config_hash=row.config_hash,
        capability_snapshot_hash=row.capability_snapshot_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def _safe_scalar(key: str, value: object) -> str | int | bool | None:
    # `outcome` is a closed STRING vocabulary, so a non-string on that key is
    # already drift. Checked before the bool fast-path below, which would otherwise
    # serve `outcome: true` verbatim and skip the vocabulary check entirely — a
    # hole Codex found in the first version of this guard.
    if key == "outcome" and not isinstance(value, str):
        return None
    if isinstance(value, bool):
        return value
    if (
        key in {"claim_count", "contributor_count", "estimated_input_tokens", "target_input_budget"}
        and isinstance(value, int)
        and 0 <= value <= 2_000_000
    ):
        return value
    if isinstance(value, str) and 0 < len(value) <= 256:
        # Retrieval self-description is a CLOSED vocabulary, re-checked here so
        # a drifted or attacker-shaped stored string can never ride one of these
        # keys onto the wire as if it were a class the server defined.
        if key == "read_kind" and value not in _SAFE_READ_KINDS:
            return None
        if key == "evidence_status" and value not in _SAFE_EVIDENCE_STATUSES:
            return None
        if key == "evidence_assurance" and value not in _SAFE_EVIDENCE_ASSURANCES:
            return None
        if key == "tool_name" and _TOOL_NAME.fullmatch(value) is None:
            return None
        if key in _HASH_KEYS and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            return None
        # The routing pair is the one part of `model.served` an attacker-shaped
        # string could plausibly ride, so the writer's declared-name rule is
        # RE-CHECKED here rather than trusted across a storage boundary. A
        # provider error message -- spaces, quotes, newlines -- fails this and
        # is dropped, not truncated into something that reads like a name.
        if key == "route_position" and value not in {"primary", "fallback"}:
            return None
        # POST_DEMO item 4. Re-checked here for the same reason every other
        # server-written class above is: `ne_agent_run_events.safe_detail` carries
        # NO check constraint (unlike the outbox column), so a drifted writer or a
        # partial restore could put 256 characters of prose on this key and the
        # timeline would render it as a class the server defined. My own comment on
        # `_TERMINAL_KEYS` claimed it "passes `_safe_scalar` unchanged" — true, and
        # that was the defect, not the justification. An adversarial pass caught it;
        # it is the `3944763c` shape this file already holds the line against.
        if key == "outcome" and value not in _SAFE_TERMINAL_OUTCOMES:
            return None
        if key == "fallback_from" and not _is_model_name_list(value):
            return None
        return value
    return None


def _event_metadata(event: RunEvent) -> tuple[SafeEventMetadata, ...]:
    keys: tuple[str, ...]
    if event.event_kind == "run.terminal":
        keys = _TERMINAL_KEYS
    elif event.event_kind == "delivery.decided":
        keys = _DELIVERY_KEYS
    elif event.event_kind == "model.served":
        # THE ALLOWLIST STAYS AN ALLOWLIST. `route_position` and `fallback_from`
        # are named here, one key at a time, exactly like every key beside them
        # -- the event previously reported WHICH model served and never WHY it
        # was that model, which is what made an involuntary provider fallback
        # unreadable from the record. Nothing here is permissive, and the
        # provider's error text is not among these keys.
        keys = (
            "model_policy_id",
            "request_mode",
            "requested_model",
            "served_model",
            "served_lane",
            "route_position",
            "fallback_from",
            "authorization_receipt_hash",
        )
    elif event.event_kind == "model.changed":
        keys = (
            "actor",
            "safe_boundary",
            "model_policy_id",
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
            "authorization_receipt_hash",
            "before_config_hash",
            "config_hash",
        )
    elif event.event_kind in {"evidence.produced", "evidence.verified_row"}:
        # WHAT CAME BACK, as class and count -- never as content. These four are
        # the writer's own typed self-description (`_KnowledgeEvidenceDetail`):
        # the read's kind, whether a locator exists, the assurance class, and
        # how many contributor stamps supported the result. Each value is
        # re-validated against a closed vocabulary in `_safe_scalar`; the
        # retrieved text, the locator, and the row anchors have no key here.
        keys = (
            "read_kind",
            "evidence_status",
            "evidence_assurance",
            "contributor_count",
        )
    else:
        # Evidence/model/tool writers may supply their own mappings. Never reflect an
        # open mapping merely because storage rejected a few known content keys.
        keys = ()
    projected: list[SafeEventMetadata] = []
    for key in keys:
        if key not in event.safe_detail:
            continue
        value = _safe_scalar(key, event.safe_detail[key])
        if value is not None:
            projected.append(SafeEventMetadata(key=cast(Any, key), value=value))
    return tuple(projected)


@dataclass(frozen=True)
class _AuditChainFacts:
    """The enumeratable NAMES one audit chain row holds: action, tool, model."""

    action: str
    tool_name: str | None = None
    requested_model: str | None = None


@dataclass(frozen=True)
class RunEventAudit:
    """One event's row in the Run-to-audit join: NAMES of a step, no content.

    ``audit_action`` is the readable action already committed to the governed
    audit chain (``run.tool.pre_effect`` and its siblings); the Run event itself
    stores only that action's HASH, which is why an audit-intent row rendered as
    nothing a reader could use. ``decision_kind`` and ``tool_call_id`` are the
    server's own classification and correlation id. ``tool_name`` is the
    registered tool identifier the chain's own pre-effect record named in its
    resource (``tool:read.knowledge.search``), and ``requested_model`` is the
    exact pinned model name the gateway receipt stamped into its provenance
    before the call -- both server-authored NAMES. No argument, prompt, answer,
    or hashed preimage travels on this type.
    """

    audit_action: str | None = None
    decision_kind: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    requested_model: str | None = None


def _audit_metadata(audit: RunEventAudit | None) -> tuple[SafeEventMetadata, ...]:
    """Project the join, fail-closed: an unenumerated action or kind is dropped."""

    if audit is None:
        return ()
    projected: list[SafeEventMetadata] = []
    if audit.audit_action is not None and audit.audit_action in _SAFE_AUDIT_ACTIONS:
        projected.append(SafeEventMetadata(key="audit_action", value=audit.audit_action))
    if audit.decision_kind is not None and audit.decision_kind in _SAFE_DECISION_KINDS:
        projected.append(SafeEventMetadata(key="decision_kind", value=audit.decision_kind))
    if audit.tool_call_id is not None:
        value = _safe_scalar("tool_call_id", audit.tool_call_id)
        if value is not None:
            projected.append(SafeEventMetadata(key="tool_call_id", value=value))
    # The NAMES the chain's own pre-effect records carry, validated per-key in
    # `_safe_scalar` and served ONLY beside the action that authored them: a
    # tool name belongs to a tool receipt and a pinned model name to a gateway
    # receipt. A value that fails its shape check is dropped, never truncated
    # into something that reads like a name.
    if audit.tool_name is not None and audit.audit_action == "run.tool.pre_effect":
        value = _safe_scalar("tool_name", audit.tool_name)
        if value is not None:
            projected.append(SafeEventMetadata(key="tool_name", value=value))
    if (
        audit.requested_model is not None
        and audit.audit_action == "run.model.pre_effect"
        and _is_model_name(audit.requested_model)
    ):
        projected.append(SafeEventMetadata(key="requested_model", value=audit.requested_model))
    return tuple(projected)


def event_view(event: RunEvent, audit: RunEventAudit | None = None) -> RunEventView:
    kind = event.event_kind if event.event_kind in _SAFE_EVENT_KINDS else "event.unavailable"
    status = event.event_status if event.event_status in _SAFE_EVENT_STATUSES else "unavailable"
    return RunEventView(
        event_id=event.event_id,
        run_id=event.run_id,
        attempt_id=event.attempt_id,
        sequence=event.sequence,
        event_kind=cast(Any, kind),
        event_status=cast(Any, status),
        parent_event_id=event.parent_event_id,
        step_id=event.step_id,
        metadata=_event_metadata(event) + _audit_metadata(audit),
        detail_hash=event.detail_hash,
        created_at=event.created_at,
    )


class AgentRunProjection:
    """Read-only owner/department/Agent projection; no writer exists in this type."""

    def __init__(
        self, store: AgentRunStore, engine: sa.Engine, *, audit_engine: sa.Engine | None = None
    ) -> None:
        self._store = store
        self._engine = engine
        self._audit_engine = audit_engine

    async def get_run(
        self,
        *,
        run_id: str,
        owner_principal_id: str,
        department: str,
        agent_id: str,
        conversation_id: str | None = None,
    ) -> AgentRun | None:
        row = await self._store.get_run(
            run_id=run_id,
            owner_principal_id=owner_principal_id,
        )
        if row is None:
            return None
        if row.department != department or row.agent_id != agent_id:
            return None
        if conversation_id is not None and row.conversation_id != conversation_id:
            return None
        # The one-run read serves the WHOLE answer; only bulk pages are capped.
        return await self._enrich_run(row, owner_principal_id, full_output=True)

    async def list_runs(
        self,
        *,
        owner_principal_id: str,
        department: str,
        agent_id: str,
        limit: int,
        after: tuple[datetime, str] | None,
        conversation_id: str | None = None,
    ) -> tuple[tuple[AgentRun, ...], tuple[datetime, str] | None]:
        """Filter before page completion so hidden rows never consume visible capacity."""

        visible: list[AgentRun] = []
        scan_after = after
        while len(visible) <= limit:
            page = await self._store.list_runs(
                owner_principal_id=owner_principal_id,
                limit=200,
                after=scan_after,
            )
            visible.extend(
                row
                for row in page.runs
                if row.department == department
                and row.agent_id == agent_id
                and (conversation_id is None or row.conversation_id == conversation_id)
            )
            if page.next_cursor is None:
                break
            scan_after = page.next_cursor
        rows = tuple([await self._enrich_run(row, owner_principal_id) for row in visible[:limit]])
        next_after = None
        if len(visible) > limit and rows:
            next_after = (_utc(rows[-1].created_at), rows[-1].run_id)
        return rows, next_after

    async def _enrich_run(
        self, run: AgentRun, owner: str, *, full_output: bool = False
    ) -> AgentRun:
        """Attach disclosure-safe model/output facts; every ambiguity fails closed.

        ``full_output`` lifts the list-page length budget for a caller reading
        exactly one Run. It changes nothing about WHO may read WHAT: the
        disclosure decision is `_preview_target`'s alone, and this flag is
        applied strictly after it.
        """
        served, route_position, fallback_from = await to_thread.run_sync(
            self._served_model, run.run_id
        )
        preview: str | None = None
        target_and_output = await to_thread.run_sync(self._preview_target, run, owner)
        if target_and_output is not None:
            target, protected_ref, output_hash = target_and_output
            try:
                raw = await self._store.materialize_source_free_turn(
                    owner_principal_id=owner,
                    target=target,
                    protected_ref=protected_ref,
                    plaintext_hash=output_hash,
                )
                text = raw.decode("utf-8", errors="strict")
                preview = text if full_output else text[:_MAX_OUTPUT_PREVIEW]
            except (ProtectedTurnUnavailable, UnicodeDecodeError):
                preview = None
        return replace(
            run,
            served_model=served,
            route_position=route_position,
            fallback_from=fallback_from,
            output_preview=preview,
        )

    async def conversation_turns(
        self,
        *,
        owner_principal_id: str,
        department: str,
        agent_id: str,
        conversation_id: str,
        limit: int,
        after_sequence: int | None,
    ) -> tuple[tuple[ConversationTurnView, ...], int | None]:
        target = ne_agent_run_turn_targets
        predicate = [
            ne_agent_runs.c.owner_principal_id == owner_principal_id,
            ne_agent_runs.c.department == department,
            ne_agent_runs.c.agent_id == agent_id,
            ne_agent_runs.c.conversation_id == conversation_id,
            target.c.conversation_id == conversation_id,
        ]
        if after_sequence is not None:
            predicate.append(target.c.turn_sequence > after_sequence)
        lineage = ne_agent_run_retry_lineage
        stmt = (
            sa.select(
                ne_agent_runs,
                lineage.c.predecessor_run_id,
                lineage.c.root_run_id,
                lineage.c.retry_ordinal,
                target.c.turn_id,
                target.c.turn_sequence,
                target.c.attachment_id,
                target.c.attempt_id,
            )
            .select_from(
                ne_agent_runs.outerjoin(
                    lineage, lineage.c.successor_run_id == ne_agent_runs.c.run_id
                ).join(target, target.c.run_id == ne_agent_runs.c.run_id)
            )
            .where(*predicate)
            .order_by(target.c.turn_sequence.asc())
            .limit(limit + 1)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        items: list[ConversationTurnView] = []
        for row in rows[:limit]:
            run = await self._enrich_run(_run(row), owner_principal_id)
            content: str | None = None
            try:
                raw = await self._store.materialize_run_input(
                    run_id=run.run_id, owner_principal_id=owner_principal_id
                )
                content = raw.decode("utf-8", errors="strict")
            except (LookupError, UnicodeDecodeError, ValueError):
                content = None
            items.append(
                ConversationTurnView(
                    turn_id=str(row.turn_id),
                    turn_sequence=int(row.turn_sequence),
                    user_content_state="available" if content is not None else "unavailable",
                    user_content=content,
                    run=run_view(run),
                )
            )
        next_sequence = int(rows[limit - 1].turn_sequence) if len(rows) > limit else None
        return tuple(items), next_sequence

    def _served_model(self, run_id: str) -> tuple[str | None, str | None, str | None]:
        """The model that served, and WHERE IN THE CHAIN it sat, as one answer.

        The name and its route position are returned together, and from ONE row,
        because reading them from two places is how a Run ends up reporting that
        model X served and that a fallback from Y happened without those two
        statements being about the same call.

        Two sources, in the same order the served name has always used:

        1. the durable model step (``ne_agent_run_model_steps``) -- the last
           ordinal, which is the step that produced the answer;
        2. the gateway's own audit records, for a Run that recorded no step.
           These are where ``_routing_provenance`` writes, so the routing is
           readable there too -- but only when EVERY record agrees, since a Run
           whose calls routed differently has no single route position and
           saying otherwise would be a guess.

        A position we cannot vouch for comes back as ``None``, never
        ``"primary"``: "primary" asserts nothing was attempted first, and an
        absent record asserts nothing at all.
        """
        try:
            with self._engine.connect() as conn:
                step = conn.execute(
                    sa.select(
                        ne_agent_run_model_steps.c.served_model,
                        ne_agent_run_model_steps.c.route_position,
                        ne_agent_run_model_steps.c.fallback_from,
                    )
                    .where(ne_agent_run_model_steps.c.run_id == run_id)
                    .order_by(ne_agent_run_model_steps.c.ordinal.desc())
                    .limit(1)
                ).one_or_none()
        except SQLAlchemyError:
            step = None
        if step is not None and isinstance(step.served_model, str) and step.served_model.strip():
            return (step.served_model, *_routing_pair(step.route_position, step.fallback_from))
        try:
            from ne.audit.store import ne_audit_log
        except ImportError:
            return None, None, None
        if self._audit_engine is None:
            return None, None, None
        try:
            with self._audit_engine.connect() as conn:
                rows = conn.execute(
                    sa.select(ne_audit_log.c.payload).where(
                        ne_audit_log.c.trace_id == run_id,
                        ne_audit_log.c.action == "ne.gateway.complete",
                        ne_audit_log.c.outcome == "success",
                    )
                ).all()
        except SQLAlchemyError:
            return None, None, None
        models: list[str] = []
        routings: set[tuple[str | None, str | None]] = set()
        for row in rows:
            payload = row.payload
            provenance = payload.get("provenance") if isinstance(payload, dict) else None
            if not isinstance(provenance, dict):
                return None, None, None
            model = provenance.get("response_model")
            if not isinstance(model, str) or not model.strip():
                return None, None, None
            models.append(model)
            routings.add(
                _routing_pair(
                    provenance.get("route_position"),
                    provenance.get("fallback_from"),
                )
            )
        if not models or len(set(models)) != 1:
            return None, None, None
        position, passed_over = routings.pop() if len(routings) == 1 else (None, None)
        return models[0], position, passed_over

    def _preview_target(
        self, run: AgentRun, owner: str
    ) -> tuple[CanonicalTargetTuple, str, str] | None:
        """Locate the owner-readable terminal output, or nothing -- never a guess.

        THE PREDICATE THIS REPLACES COULD NOT BE SATISFIED BY ANY RUN, so no
        Run ever served a preview and the Output pane read "no output was
        served" for all of them.  It asked for verdict Verified/Grounded AND
        `protected_output_kind == 'protected_turn_content'`, and 010F cannot
        author both at once:

          * Verified/Grounded needs every claim bound to an EvidenceRecord,
            which needs a served delivery, which makes the finalizer store the
            bytes as `protected_source_output` (run_finalizer._protect_output).
          * `protected_turn_content` is written ONLY when the server-observed
            delivery set was empty -- so there is nothing to bind, mapping is
            `unavailable` and SPEC 10F forces the aggregate verdict there too.

        (test_run_finalizer pins both shapes: source-free -> unavailable +
        protected_turn_content; source-delivered -> verified +
        protected_source_output.)

        So the readable set is decided by WHERE 010F stored the bytes, not by
        the aggregate verdict.  `protected_turn_content` holds nothing
        source-derived, and SPEC 9 requires an explicitly source-free turn stay
        readable through its exact owner/turn target.  Its `unavailable` verdict
        says the answer is UNATTESTED; it does not say the answer is absent.
        Every deterministic Workflow, and every agentic Run that called no
        retrieval tool, lands exactly there.

        Withheld here, unchanged: `protected_source_output` (source-derived
        bytes keep their separate current-source reauthorization in the evidence
        projection) and a PARTIAL mapping, which is neither attested nor
        source-free.
        """

        if run.status is not RunState.COMPLETED or run.output_verdict not in {
            "verified",
            "grounded",
            "unavailable",
            "refused",
        }:
            return None
        final = ne_agent_run_finalizations
        target = ne_agent_run_turn_targets
        stmt = (
            sa.select(final, target)
            .select_from(
                final.join(
                    target,
                    sa.and_(
                        target.c.run_id == final.c.run_id,
                        target.c.attempt_id == final.c.attempt_id,
                        target.c.turn_id == final.c.turn_id,
                    ),
                ).join(
                    ne_agent_runs,
                    sa.and_(
                        ne_agent_runs.c.run_id == final.c.run_id,
                        ne_agent_runs.c.owner_principal_id == owner,
                    ),
                )
            )
            .where(
                final.c.run_id == run.run_id,
                # Complete mapping earns an attested verdict; an unmapped
                # source-free terminal is the honest ungrounded answer.  A
                # PARTIAL mapping is neither, and stays withheld.
                sa.or_(
                    sa.and_(
                        final.c.mapping_state == "complete",
                        final.c.output_verdict.in_(("verified", "grounded")),
                    ),
                    sa.and_(
                        final.c.mapping_state == "unavailable",
                        # A REFUSAL IS A CORRECT OUTCOME AND MUST BE READABLE.
                        # `run_finalizer._mapping` returns ("unavailable",
                        # REFUSED) for a structured refusal, whose text
                        # `_protect_output` protects exactly like an answer's.
                        # Withholding it rendered the one beat where the system
                        # correctly declines to fabricate as "no output was
                        # served" -- the governance win, displayed as a bug.
                        final.c.output_verdict.in_(("unavailable", "refused")),
                    ),
                ),
                final.c.protected_output_kind == "protected_turn_content",
                final.c.protected_output_ref.is_not(None),
                final.c.protected_output_hash.is_not(None),
            )
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        if row is None:
            return None
        return (
            CanonicalTargetTuple(
                conversation_id=str(row.conversation_id),
                run_id=str(row.run_id),
                turn_id=str(row.turn_id),
                turn_sequence=int(row.turn_sequence),
                attachment_id=str(row.attachment_id),
                attempt_id=str(row.attempt_id),
            ),
            str(row.protected_output_ref),
            str(row.protected_output_hash),
        )

    def _current_attempt_id(self, run: AgentRun, owner: str) -> str | None:
        """Resolve the newest exact attempt link without consulting current Agent truth."""

        stmt = (
            sa.select(ne_agent_run_attempt_links.c.attempt_id)
            .join(ne_agent_runs, ne_agent_runs.c.run_id == ne_agent_run_attempt_links.c.run_id)
            .where(
                ne_agent_run_attempt_links.c.run_id == run.run_id,
                ne_agent_runs.c.owner_principal_id == owner,
                ne_agent_runs.c.department == run.department,
                ne_agent_runs.c.agent_id == run.agent_id,
            )
            .order_by(ne_agent_run_attempt_links.c.attempt_number.desc())
            .limit(1)
        )
        try:
            with self._engine.connect() as conn:
                attempt_id = conn.execute(stmt).scalar_one_or_none()
        except SQLAlchemyError:
            return None
        return attempt_id if isinstance(attempt_id, str) and attempt_id else None

    async def _execution_profile_receipt(
        self,
        run: AgentRun,
        owner: str,
        attempt_id: str | None,
    ) -> RunExecutionProfileReceiptView | None:
        """Project only integrity-checked profile bytes frozen for this exact attempt."""

        if run.strategy != "deterministic" or attempt_id is None:
            return None
        try:
            receipt = await self._store.load_execution_profile(
                run_id=run.run_id,
                attempt_id=attempt_id,
                owner_principal_id=owner,
            )
        except (LookupError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
            return None
        if receipt is None:
            return None
        plan = self._verified_execution_plan(run, attempt_id, receipt)
        if plan is None:
            return None
        typed_input: dict[str, Any] | None = None
        try:
            raw_input = await self._store.materialize_run_input(
                run_id=run.run_id,
                owner_principal_id=owner,
            )
            parsed_input = json.loads(raw_input)
            if isinstance(parsed_input, dict) and all(isinstance(key, str) for key in parsed_input):
                typed_input = parsed_input
        except (LookupError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            typed_input = None
        try:
            return RunExecutionProfileReceiptView(
                profile_receipt_id=receipt.profile_receipt_id,
                run_id=receipt.run_id,
                attempt_id=receipt.attempt_id,
                workflow_id=receipt.workflow_id,
                profile_version=receipt.profile_version,
                profile_hash=receipt.profile_hash,
                input_schema=dict(receipt.input_schema),
                input_schema_hash=receipt.input_schema_hash,
                typed_input_state="available" if typed_input is not None else "unavailable",
                typed_input=typed_input,
                ordered_plan=plan,
                ordered_plan_hash=receipt.ordered_plan_hash,
                receipt_hash=receipt.receipt_hash,
                created_at=receipt.created_at,
            )
        except (TypeError, ValueError):
            return None

    def _verified_execution_plan(
        self,
        run: AgentRun,
        attempt_id: str,
        receipt: RunExecutionProfileReceipt,
    ) -> tuple[DeterministicPlanStepView, ...] | None:
        if receipt.run_id != run.run_id or receipt.attempt_id != attempt_id:
            return None
        try:
            schema = dict(receipt.input_schema)
            raw_plan = [dict(step) for step in receipt.ordered_plan]
            if _canonical_hash(schema) != receipt.input_schema_hash:
                return None
            if _canonical_hash(raw_plan) != receipt.ordered_plan_hash:
                return None
            expected_receipt_hash = _receipt_hash(
                "ne.agent.run.execution-profile.v1",
                {
                    "run_id": run.run_id,
                    "attempt_id": attempt_id,
                    "workflow_id": receipt.workflow_id,
                    "profile_version": receipt.profile_version,
                    "profile_hash": receipt.profile_hash,
                    "input_schema_hash": receipt.input_schema_hash,
                    "ordered_plan_hash": receipt.ordered_plan_hash,
                },
            )
            if not hmac.compare_digest(expected_receipt_hash, receipt.receipt_hash):
                return None
            plan = tuple(DeterministicPlanStepView.model_validate(step) for step in raw_plan)
        except (TypeError, ValueError):
            return None
        if not plan or tuple(step.ordinal for step in plan) != tuple(range(1, len(plan) + 1)):
            return None
        if len({step.step_id for step in plan}) != len(plan):
            return None
        if any((step.kind == "model-judgment") != (step.model_role is not None) for step in plan):
            return None
        return plan

    async def _skill_activation_receipt(
        self,
        run: AgentRun,
        owner: str,
        attempt_id: str | None,
    ) -> RunSkillActivationReceiptView | None:
        """Project only the exact content-free Skill activation frozen for this attempt."""

        if run.strategy != "agentic" or attempt_id is None:
            return None
        try:
            receipt = await self._store.load_skill_activation(
                run_id=run.run_id,
                attempt_id=attempt_id,
                owner_principal_id=owner,
            )
        except (LookupError, RuntimeError, SQLAlchemyError, TypeError, ValueError):
            return None
        if receipt is None or not self._valid_skill_receipt(run, attempt_id, receipt):
            return None
        try:
            return RunSkillActivationReceiptView(
                activation_receipt_id=receipt.activation_receipt_id,
                run_id=receipt.run_id,
                attempt_id=receipt.attempt_id,
                agent_id=receipt.agent_id,
                agent_version=receipt.agent_version,
                surface_key=receipt.surface_key,
                pins=tuple(
                    RunSkillPinView(
                        skill_id=skill_id,
                        version=version,
                        content_hash=content_hash,
                    )
                    for skill_id, version, content_hash in receipt.pins
                ),
                index_hash=receipt.index_hash,
                surface_hash=receipt.surface_hash,
                receipt_hash=receipt.receipt_hash,
                created_at=receipt.created_at,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_skill_receipt(
        run: AgentRun,
        attempt_id: str,
        receipt: RunSkillActivationReceipt,
    ) -> bool:
        if (
            receipt.run_id != run.run_id
            or receipt.attempt_id != attempt_id
            or receipt.agent_id != run.agent_id
            or receipt.agent_version != run.agent_version
            or receipt.surface_key != f"run:{run.run_id}:attempt:{attempt_id}:skills"
            # Zero pins is a VALID receipt — a zero-pin Agent's attempt records
            # "activated exactly no Skills" as a positive fact, and dropping it
            # here would tell the reader no activation was recorded when one was.
            or len({pin[0] for pin in receipt.pins}) != len(receipt.pins)
        ):
            return False
        pins = [
            [skill_id, version, content_hash] for skill_id, version, content_hash in receipt.pins
        ]
        expected = _receipt_hash(
            "ne.agent.run.skill-activation.v1",
            {
                "run_id": run.run_id,
                "attempt_id": attempt_id,
                "agent_id": receipt.agent_id,
                "agent_version": receipt.agent_version,
                "surface_key": receipt.surface_key,
                "pins": pins,
                "index_hash": receipt.index_hash,
                "surface_hash": receipt.surface_hash,
            },
        )
        return hmac.compare_digest(expected, receipt.receipt_hash)

    async def run_detail(
        self,
        *,
        run: AgentRun,
        owner_principal_id: str,
        department: str,
        agent_id: str,
    ) -> RunDetailView:
        approval_id = await to_thread.run_sync(
            self._current_actionable_approval_id,
            run,
            owner_principal_id,
        )
        agentic_plan = await to_thread.run_sync(
            self._agentic_plan,
            run,
            owner_principal_id,
        )
        attempt_id = await to_thread.run_sync(self._current_attempt_id, run, owner_principal_id)
        # Only a terminal non-completed Run can carry one, and only that branch
        # spends the read: a working Run has not failed and has nothing to say.
        failure_reason = (
            await to_thread.run_sync(self._failure_reason, run, owner_principal_id)
            if run.status
            in {
                RunState.FAILED,
                RunState.CANCELLED,
                RunState.EXPIRED,
                RunState.CONTEXT_EXHAUSTED,
                RunState.BUDGET_EXHAUSTED,
            }
            else None
        )
        execution_profile_receipt = await self._execution_profile_receipt(
            run,
            owner_principal_id,
            attempt_id,
        )
        skill_activation_receipt = await self._skill_activation_receipt(
            run,
            owner_principal_id,
            attempt_id,
        )
        if run.status not in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
            RunState.CONTEXT_EXHAUSTED,
            RunState.BUDGET_EXHAUSTED,
        }:
            return RunDetailView(
                **run_view(run).model_dump(),
                approval_id=approval_id,
                failure_reason=failure_reason,
                agentic_plan=agentic_plan,
                execution_profile_receipt=execution_profile_receipt,
                skill_activation_receipt=skill_activation_receipt,
                claims=(),
                claim_mapping_state="mapping_pending",
                claims_truncated=False,
            )
        if run.status is RunState.COMPLETED and run.output_verdict == "refused":
            return RunDetailView(
                **run_view(run).model_dump(),
                approval_id=approval_id,
                failure_reason=failure_reason,
                agentic_plan=agentic_plan,
                execution_profile_receipt=execution_profile_receipt,
                skill_activation_receipt=skill_activation_receipt,
                claims=(),
                claim_mapping_state="unavailable",
                claims_truncated=False,
            )
        rows = await to_thread.run_sync(
            self._claim_rows,
            run.run_id,
            owner_principal_id,
            department,
            agent_id,
        )
        truncated = len(rows) > _MAX_CLAIMS
        claims: list[RunClaimView] = []
        for row in rows[:_MAX_CLAIMS]:
            display: str | None = None
            content_state: Literal["available", "content_withheld", "content_unavailable"]
            if str(row.display_state) == "protected_source_free_slice":
                target = CanonicalTargetTuple(
                    conversation_id=str(row.conversation_id),
                    run_id=str(row.run_id),
                    turn_id=str(row.turn_id),
                    turn_sequence=int(row.turn_sequence),
                    attachment_id=str(row.attachment_id),
                    attempt_id=str(row.attempt_id),
                )
                try:
                    materialized = await self._store.materialize_source_free_claim_slice(
                        claim_id=str(row.claim_id),
                        owner_principal_id=owner_principal_id,
                        target=target,
                        protected_ref=str(row.protected_ref),
                        served_output_hash=str(row.served_output_hash),
                    )
                    display = materialized.decode("utf-8", errors="strict")
                    content_state = "available"
                except (ProtectedSliceUnavailable, UnicodeDecodeError):
                    content_state = "content_unavailable"
            else:
                # Source-delivered content is 015A1's current-authority decision.
                content_state = "content_withheld"
            associated = row.association_id is not None
            claims.append(
                RunClaimView(
                    claim_id=str(row.claim_id),
                    claim_verdict=cast(Any, str(row.claim_verdict)),
                    content_state=content_state,
                    display=display,
                    served_output_hash=str(row.served_output_hash),
                    claim_content_hash=str(row.claim_content_hash),
                    association_status=cast(Any, str(row.association_status)),
                    association_hash=(
                        str(row.association_hash) if row.association_hash is not None else None
                    ),
                    evidence_id=str(row.evidence_id) if associated else None,
                    producing_event_id=(str(row.producing_event_id) if associated else None),
                    locator_status=cast(
                        Any,
                        str(row.locator_status) if associated else "association_unavailable",
                    ),
                    locator_hash=str(row.locator_hash) if associated else None,
                )
            )
        associated_count = sum(claim.association_status == "associated" for claim in claims)
        mapping: Literal["complete", "partial", "unavailable"]
        if claims and associated_count == len(claims) and not truncated:
            mapping = "complete"
        elif associated_count:
            mapping = "partial"
        else:
            mapping = "unavailable"
        return RunDetailView(
            **run_view(run).model_dump(),
            approval_id=approval_id,
            failure_reason=failure_reason,
            agentic_plan=agentic_plan,
            execution_profile_receipt=execution_profile_receipt,
            skill_activation_receipt=skill_activation_receipt,
            claims=tuple(claims),
            claim_mapping_state=mapping,
            claims_truncated=truncated,
        )

    def _failure_reason(self, run: AgentRun, owner: str) -> str | None:
        """The safe reason a failed Run published, or None.

        Mirrors `_agentic_plan`: one owned-Run checkpoint read, validated
        strictly, and ANY surprise returns None rather than a half-trusted
        string. What it must never do is manufacture a sentence — this layer
        SERVES a reason, it does not author one. The guarantee that a failed
        Run has one to serve is kept where the failure is known: the runtime
        writes `error_reason` at the failing step, and the sole terminal writer
        (`RunFinalizer._ensure_failure_reason`) writes a derived floor for any
        failure path that recorded nothing.

        `error_class` is present in the same record and is deliberately NOT
        read: it is an OpaqueId, not prose for a person.
        """

        checkpoint = ne_agent_run_execution_checkpoints
        target = ne_agent_run_turn_targets
        stmt = (
            sa.select(
                checkpoint.c.attempt_id,
                checkpoint.c.safe_detail,
                target.c.attempt_id.label("target_attempt_id"),
            )
            .select_from(
                checkpoint.join(
                    ne_agent_runs,
                    sa.and_(
                        ne_agent_runs.c.run_id == checkpoint.c.run_id,
                        ne_agent_runs.c.owner_principal_id == owner,
                    ),
                ).outerjoin(
                    target,
                    sa.and_(
                        target.c.run_id == checkpoint.c.run_id,
                        target.c.attempt_id == checkpoint.c.attempt_id,
                    ),
                )
            )
            .where(
                checkpoint.c.run_id == run.run_id,
                checkpoint.c.stage == "execution_failed",
            )
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).all()
        except SQLAlchemyError:
            return None
        for row in rows:
            # Only the attempt this Run currently targets may speak for it; a
            # superseded attempt's reason would explain the wrong failure.
            if row.target_attempt_id is None or str(row.attempt_id) != str(row.target_attempt_id):
                continue
            try:
                detail = json.loads(str(row.safe_detail))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(detail, dict):
                continue
            reason = detail.get("error_reason")
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 256
                or any(not character.isprintable() for character in reason)
            ):
                continue
            return reason
        return None

    def _current_actionable_approval_id(self, run: AgentRun, owner: str) -> str | None:
        """Return only the approval joined to this owned Run's current attempt."""

        join = ne_agent_run_approval_joins
        request = ne_approval_requests
        target = ne_agent_run_turn_targets
        stmt = (
            sa.select(join.c.approval_id)
            .select_from(
                join.join(
                    target,
                    sa.and_(
                        target.c.run_id == join.c.run_id,
                        target.c.attempt_id == join.c.attempt_id,
                    ),
                )
                .join(request, request.c.request_id == join.c.approval_id)
                .join(ne_agent_runs, ne_agent_runs.c.run_id == join.c.run_id)
            )
            .where(
                join.c.run_id == run.run_id,
                join.c.principal_id == owner,
                ne_agent_runs.c.owner_principal_id == owner,
                ne_agent_runs.c.status == RunState.WAITING_APPROVAL.value,
                request.c.principal_id == owner,
                request.c.action_scope == run.run_id,
                request.c.args_hash == join.c.action_hash,
                request.c.status.in_(PENDING_STATUSES),
            )
            .limit(2)
        )
        try:
            with self._engine.connect() as conn:
                rows = list(conn.execute(stmt).scalars())
        except SQLAlchemyError:
            return None
        if len(rows) != 1:
            return None
        approval_id = rows[0]
        if not isinstance(approval_id, str) or not approval_id or len(approval_id) > 256:
            return None
        return approval_id

    def _agentic_plan(
        self,
        run: AgentRun,
        owner: str,
    ) -> tuple[AgenticPlanStepView, ...] | None:
        """Project only the exact target attempt's complete persisted intent sequence."""

        if run.strategy != "agentic":
            return None
        checkpoint = ne_agent_run_execution_checkpoints
        target = ne_agent_run_turn_targets
        stmt = (
            sa.select(
                checkpoint.c.attempt_id,
                checkpoint.c.stage,
                checkpoint.c.safe_detail,
                target.c.attempt_id.label("target_attempt_id"),
            )
            .select_from(
                checkpoint.join(
                    ne_agent_runs,
                    sa.and_(
                        ne_agent_runs.c.run_id == checkpoint.c.run_id,
                        ne_agent_runs.c.owner_principal_id == owner,
                    ),
                ).outerjoin(
                    target,
                    sa.and_(
                        target.c.run_id == checkpoint.c.run_id,
                        target.c.attempt_id == checkpoint.c.attempt_id,
                    ),
                )
            )
            .where(
                checkpoint.c.run_id == run.run_id,
                sa.or_(
                    checkpoint.c.stage.like("agentic-intent:%"),
                    checkpoint.c.stage.like("agentic-call:%"),
                ),
            )
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).all()
        except SQLAlchemyError:
            return None
        # THE BIND ROWS ARE ENRICHMENT, THE INTENT ROWS ARE THE PLAN. An
        # `agentic-call:{ordinal}` checkpoint is written by the exact executor
        # frame that minted the step's `tool_call_id` (run_runner
        # `ReceiptGatedToolExecutor`), so it is the only record that can state
        # the join without counting. A malformed, foreign-attempt, or
        # tool-mismatched bind drops to None FOR THAT STEP -- the plan itself
        # stays served, because a step the server admitted but could not bind
        # (blocked before its receipt) is still a real plan row.
        call_rows = []
        intent_rows = []
        for row in rows:
            if _AGENTIC_CALL_STAGE.fullmatch(str(row.stage)) is not None:
                call_rows.append(row)
            else:
                intent_rows.append(row)
        rows = intent_rows
        if not rows:
            return None
        steps: dict[int, AgenticPlanStepView] = {}
        for row in rows:
            if row.target_attempt_id is None or str(row.attempt_id) != str(row.target_attempt_id):
                return None
            stage = str(row.stage)
            match = _AGENTIC_INTENT_STAGE.fullmatch(stage)
            if match is None:
                return None
            ordinal = int(match.group(1))
            try:
                detail = json.loads(str(row.safe_detail))
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
            if not isinstance(detail, dict) or set(detail) != {"ordinal", "tool", "step_intent"}:
                return None
            detail_ordinal = detail["ordinal"]
            tool = detail["tool"]
            step_intent = detail["step_intent"]
            if (
                isinstance(detail_ordinal, bool)
                or not isinstance(detail_ordinal, int)
                or detail_ordinal != ordinal
                or ordinal in steps
                or not isinstance(tool, str)
                or not tool.strip()
                or len(tool) > 256
                or any(not character.isprintable() for character in tool)
                or not isinstance(step_intent, str)
                or not step_intent.strip()
                or len(step_intent) > 256
                or any(not character.isprintable() for character in step_intent)
            ):
                return None
            steps[ordinal] = AgenticPlanStepView(
                ordinal=ordinal,
                tool=tool,
                step_intent=step_intent,
            )
        if sorted(steps) != list(range(len(steps))):
            return None
        for row in call_rows:
            if row.target_attempt_id is None or str(row.attempt_id) != str(row.target_attempt_id):
                continue
            match = _AGENTIC_CALL_STAGE.fullmatch(str(row.stage))
            if match is None:  # pragma: no cover - partitioned on this exact pattern
                continue
            ordinal = int(match.group(1))
            step = steps.get(ordinal)
            if step is None:
                continue
            try:
                detail = json.loads(str(row.safe_detail))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(detail, dict) or set(detail) != {"ordinal", "tool", "tool_call_id"}:
                continue
            tool_call_id = detail["tool_call_id"]
            if (
                isinstance(detail["ordinal"], bool)
                or detail["ordinal"] != ordinal
                or detail["tool"] != step.tool
                or not isinstance(tool_call_id, str)
                or not tool_call_id.strip()
                or len(tool_call_id) > 256
                or any(not character.isprintable() for character in tool_call_id)
            ):
                continue
            steps[ordinal] = step.model_copy(update={"tool_call_id": tool_call_id})
        return tuple(steps[ordinal] for ordinal in range(len(steps)))

    def _claim_rows(
        self,
        run_id: str,
        owner_principal_id: str,
        department: str,
        agent_id: str,
    ) -> Sequence[Any]:
        association = ne_agent_run_claim_evidence
        evidence = ne_agent_evidence_records
        stmt = (
            sa.select(
                ne_agent_run_claims,
                association.c.association_id,
                association.c.association_hash,
                association.c.evidence_id,
                association.c.locator_hash,
                association.c.producing_event_id,
                evidence.c.locator_status,
            )
            .select_from(
                ne_agent_run_claims.join(
                    ne_agent_runs,
                    sa.and_(
                        ne_agent_runs.c.run_id == ne_agent_run_claims.c.run_id,
                        ne_agent_runs.c.owner_principal_id
                        == ne_agent_run_claims.c.owner_principal_id,
                    ),
                )
                .outerjoin(
                    association,
                    sa.and_(
                        association.c.claim_id == ne_agent_run_claims.c.claim_id,
                        association.c.run_id == ne_agent_run_claims.c.run_id,
                    ),
                )
                .outerjoin(
                    evidence,
                    sa.and_(
                        evidence.c.evidence_id == association.c.evidence_id,
                        evidence.c.run_id == association.c.run_id,
                    ),
                )
            )
            .where(
                ne_agent_runs.c.run_id == run_id,
                ne_agent_runs.c.owner_principal_id == owner_principal_id,
                ne_agent_runs.c.department == department,
                ne_agent_runs.c.agent_id == agent_id,
            )
            .order_by(ne_agent_run_claims.c.claim_index, ne_agent_run_claims.c.claim_id)
            .limit(_MAX_CLAIMS + 1)
        )
        with self._engine.connect() as conn:
            return tuple(conn.execute(stmt).all())

    def _audit_actions(self, audit_event_ids: Sequence[str]) -> dict[str, _AuditChainFacts]:
        """Read the readable action name for exactly these audit chain rows.

        Alongside the action, this reads the two NAMES the chain's own
        pre-effect records already hold readable: the tool a
        ``run.tool.pre_effect`` receipt named in its ``resource``
        (``tool:read.knowledge.search``), and the exact pinned model a
        ``run.model.pre_effect`` receipt stamped into its provenance. Both are
        extracted only for their own action, shape-checked again in
        ``_audit_metadata``, and anything else in the chain row -- detail
        sentences, foreign resources, the rest of the payload -- is never
        reflected.

        EVERY EMPTY RETURN HERE SAYS WHY. An unreadable chain degrades the row
        to "Step" and nothing worse, so it may not raise -- but it may not be
        silent either: a swallowed lookup that switches the whole feature off
        with no signal is precisely what let an unresolved audit engine ship.
        """

        if not audit_event_ids:
            return {}
        if self._audit_engine is None:
            _LOG.warning(
                "run event audit_action unavailable for %d intent(s): "
                "no audit engine was resolved for this projection",
                len(audit_event_ids),
            )
            return {}
        try:
            from ne.audit.store import ne_audit_log
        except ImportError:
            _LOG.warning("run event audit_action unavailable: ne.audit.store is not importable")
            return {}
        try:
            with self._audit_engine.connect() as conn:
                rows = conn.execute(
                    sa.select(
                        ne_audit_log.c.event_id,
                        ne_audit_log.c.action,
                        ne_audit_log.c.resource,
                        ne_audit_log.c.payload,
                    ).where(ne_audit_log.c.event_id.in_(audit_event_ids))
                ).all()
        except SQLAlchemyError:
            # The exception text is the STORE'S, never the reader's, and it does
            # not travel to the response -- only to the operator's log.
            _LOG.warning(
                "run event audit_action lookup failed against the audit chain", exc_info=True
            )
            return {}
        resolved: dict[str, _AuditChainFacts] = {}
        for row in rows:
            if not isinstance(row.action, str):
                continue
            tool_name: str | None = None
            if (
                row.action == "run.tool.pre_effect"
                and isinstance(row.resource, str)
                and row.resource.startswith("tool:")
            ):
                tool_name = row.resource[len("tool:") :]
            requested_model: str | None = None
            if row.action == "run.model.pre_effect" and isinstance(row.payload, dict):
                provenance = row.payload.get("provenance")
                candidate = (
                    provenance.get("requested_model") if isinstance(provenance, dict) else None
                )
                if isinstance(candidate, str):
                    requested_model = candidate
            resolved[str(row.event_id)] = _AuditChainFacts(
                action=row.action,
                tool_name=tool_name,
                requested_model=requested_model,
            )
        if len(resolved) < len(set(audit_event_ids)):
            _LOG.info(
                "run event audit_action resolved %d of %d intent(s); "
                "the remainder have no readable action on the chain",
                len(resolved),
                len(set(audit_event_ids)),
            )
        return resolved

    def _event_audit(self, run_id: str, event_ids: Sequence[str]) -> dict[str, RunEventAudit]:
        """Name each event of ONE already-authorized Run from its own audit intent.

        The run row was authorized before its events were listed and this reads
        strictly inside it: the intent select is pinned to that ``run_id``, and
        the audit select can only reach chain rows those intents already name.
        Visibility is exactly what it was; only the READABILITY changes.
        """

        if not event_ids:
            return {}
        try:
            with self._engine.connect() as conn:
                intents = conn.execute(
                    sa.select(
                        ne_agent_run_audit_intents.c.run_event_id,
                        ne_agent_run_audit_intents.c.audit_event_id,
                        ne_agent_run_audit_intents.c.decision_kind,
                        ne_agent_run_audit_intents.c.tool_call_id,
                    ).where(
                        ne_agent_run_audit_intents.c.run_id == run_id,
                        ne_agent_run_audit_intents.c.run_event_id.in_(event_ids),
                    )
                ).all()
        except SQLAlchemyError:
            _LOG.warning(
                "run event audit intents unavailable for run %s; events will be served "
                "without their audit action or decision kind",
                run_id,
                exc_info=True,
            )
            return {}
        facts = self._audit_actions([str(row.audit_event_id) for row in intents])
        return {
            str(row.run_event_id): RunEventAudit(
                audit_action=(
                    facts[key].action if (key := str(row.audit_event_id)) in facts else None
                ),
                decision_kind=(row.decision_kind if isinstance(row.decision_kind, str) else None),
                tool_call_id=(row.tool_call_id if isinstance(row.tool_call_id, str) else None),
                tool_name=(facts[key].tool_name if key in facts else None),
                requested_model=(facts[key].requested_model if key in facts else None),
            )
            for row in intents
        }

    async def list_events(
        self,
        *,
        run: AgentRun,
        owner_principal_id: str,
        after_sequence: int,
        limit: int,
    ) -> RunEventPage:
        page = await self._store.list_events(
            run_id=run.run_id,
            owner_principal_id=owner_principal_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        audit = await to_thread.run_sync(
            self._event_audit, run.run_id, [event.event_id for event in page.events]
        )
        # THE CURSOR IS A WATERMARK, NOT AN END-OF-PAGES FLAG.
        #
        # It used to be minted only when the store reported MORE rows behind the
        # page (`next_sequence`), so a reader that had caught up got `None` and
        # had no way to say "anything after sequence 41". The Ask thread polls
        # this endpoint every two seconds for the life of a Run, and with no
        # watermark every one of those reads re-served the entire log from
        # sequence 0 -- an O(n) audit join per tick on a log that is still
        # growing, to learn about the one or two events that were actually new.
        #
        # So it now names the LAST SEQUENCE SERVED whenever the page served
        # anything, and the end of the log is signalled by an EMPTY page rather
        # than by a null cursor. On a truncated page the value is unchanged (the
        # store's `next_sequence` is that same last row's sequence), so a paging
        # reader behaves exactly as before; a tailing reader can now resume.
        items = tuple(event_view(event, audit.get(event.event_id)) for event in page.events)
        return RunEventPage(
            items=items,
            next_cursor=(
                encode_cursor(f"event:{run.run_id}", run.created_at, str(page.events[-1].sequence))
                if page.events
                else None
            ),
        )


def run_page(
    rows: Sequence[AgentRun],
    next_after: tuple[datetime, str] | None,
    *,
    cursor_kind: str,
) -> RunPage:
    return RunPage(
        items=tuple(run_view(row) for row in rows),
        next_cursor=(
            encode_cursor(cursor_kind, next_after[0], next_after[1])
            if next_after is not None
            else None
        ),
    )
