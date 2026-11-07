# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Server-authored Context Ledger assembly (BD-14).

The ledger joins three existing authoritative stores without minting a rival
truth source:

* the runtime session store supplies ordered persisted component identities;
* the boot-derived runtime settings supply the trim budget enforced each turn;
* the hash-chained audit store supplies provider-reported token/model evidence
  and model-routing change evidence.

Only ids, counts, model aliases, and server timestamps cross the wire. Prompt,
answer, and tool-result text never leave the runtime database through this path.
Missing provider usage or malformed/missing routing evidence stays absent rather
than becoming zero, a configured model, or a browser observation.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from anyio import to_thread

from ne.api.deps import ApiState
from ne.api.model_routing import MODEL_ROUTING_CHANGED_ACTION, build_route_provider
from ne.api.wire import (
    AgentContextComponentView,
    AgentContextLedgerView,
    ContextBudgetObservationView,
    ContextComponentView,
    ContextLedgerView,
    ContextModelFlipView,
)
from ne.audit import AuditRecord, replay, verify_chain
from ne.contracts.models import TurnContextBudget
from ne.contracts.tokens import ContextBudget
from ne.contracts.types import Lane
from ne.runtime.model_continuity import context_component_id, context_snapshot_hash
from ne.runtime.run_store import (
    ne_agent_run_history_attachments,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.store import ne_agent_conversations

_GATEWAY_COMPLETE_ACTION = "ne.gateway.complete"
_SESSION_RUN_ACTION = "session.run"


@dataclass(frozen=True)
class _ModelUsage:
    model: str | None
    input_tokens: int | None
    observed_at: datetime | None


@dataclass(frozen=True)
class _FlipEvidence:
    old_model: str
    new_model: str
    audit_seq: int
    changed_at: datetime


@dataclass(frozen=True)
class _StampedContext:
    context_id: str
    active_components: int
    total_components: int


_SHA256_ID = re.compile(r"^[0-9a-f]{64}$")


def _provenance_value(record: AuditRecord, key: str) -> str | None:
    provenance = record.payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    value = provenance.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _newest_terminal(records: tuple[AuditRecord, ...]) -> AuditRecord | None:
    return next(
        (record for record in reversed(records) if record.action == _SESSION_RUN_ACTION),
        None,
    )


def _latest_model_usage(engine: sa.Engine, terminal: AuditRecord | None, task: str) -> _ModelUsage:
    """Usage from the newest terminal only; later failure never inherits success."""
    if terminal is None or terminal.outcome != "success" or terminal.trace_id is None:
        return _ModelUsage(model=None, input_tokens=None, observed_at=None)

    calls = replay(engine, trace_id=terminal.trace_id).events
    candidate = next(
        (
            record
            for record in reversed(calls)
            if record.seq < terminal.seq
            and record.action == _GATEWAY_COMPLETE_ACTION
            and _provenance_value(record, "task") == task
        ),
        None,
    )
    if candidate is None or candidate.outcome != "success":
        return _ModelUsage(model=None, input_tokens=None, observed_at=None)

    input_tokens = _nonnegative_int(_provenance_value(candidate, "input_tokens"))
    return _ModelUsage(
        model=_provenance_value(candidate, "model_name"),
        input_tokens=input_tokens,
        # A timestamp without the paired provider count could be mistaken
        # for a token observation. Preserve the pair-or-absent contract.
        observed_at=candidate.occurred_at if input_tokens is not None else None,
    )


def _stamped_context(record: AuditRecord) -> _StampedContext | None:
    context_id = _provenance_value(record, "context_id")
    active = _nonnegative_int(_provenance_value(record, "context_active_components"))
    total = _nonnegative_int(_provenance_value(record, "context_total_components"))
    if (
        context_id is None
        or _SHA256_ID.fullmatch(context_id) is None
        or active is None
        or active < 1
        or total is None
        or total < active
    ):
        return None
    return _StampedContext(
        context_id=context_id,
        active_components=active,
        total_components=total,
    )


def _latest_prior_context_stamp(
    records: tuple[AuditRecord, ...], flip_seq: int, current_config_hash: str
) -> _StampedContext | None:
    terminal = next(
        (
            record
            for record in reversed(records)
            if record.seq < flip_seq
            and record.action == _SESSION_RUN_ACTION
            and record.outcome == "success"
        ),
        None,
    )
    if terminal is None or terminal.config_hash != current_config_hash:
        return None
    return _stamped_context(terminal)


def _empty_usage() -> _ModelUsage:
    return _ModelUsage(model=None, input_tokens=None, observed_at=None)


def _latest_flip(
    records: tuple[AuditRecord, ...],
    task: str,
    session_created_at: datetime,
    current_config_hash: str,
) -> _FlipEvidence | None:
    """Validate only the newest successful post-session routing record."""
    record = next(
        (
            candidate
            for candidate in reversed(records)
            if candidate.action == MODEL_ROUTING_CHANGED_ACTION
            and candidate.outcome == "success"
            and candidate.occurred_at > session_created_at
        ),
        None,
    )
    if record is None or record.config_hash != current_config_hash:
        return None
    detail = record.payload.get("detail")
    if not isinstance(detail, str):
        return None
    try:
        parsed = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("task") != task:
        return None
    models = parsed.get("model")
    if not isinstance(models, dict):
        return None
    old_model = models.get("old")
    new_model = models.get("new")
    if not isinstance(old_model, str) or not old_model:
        return None
    if not isinstance(new_model, str) or not new_model:
        return None
    return _FlipEvidence(
        old_model=old_model,
        new_model=new_model,
        audit_seq=record.seq,
        changed_at=record.occurred_at,
    )


def _current_model(state: ApiState, task: str) -> str | None:
    """CAPABILITY-LESS FALLBACK ONLY: the route register's primary.

    :func:`_resolve_in_force` is the production path — it asks the SAME
    ``TurnContextBudget`` resolver the trim guard asks, so reported == enforced
    is a shared-function property rather than a parallel derivation. This
    fallback exists for compositions whose gateway does not carry the
    capability (injected test doubles); it reads only the register's primary
    and deliberately re-implements NO candidate-order logic (no lane filter, no
    fallback chain) — a composition that wants the real answer composes the
    real gateway. Route-store failure uses #510's boot fallback.
    """
    if state.route_store is not None:
        route = build_route_provider(state.route_store)().get(task)
        return route.model if route is not None else None
    if state.gateway_settings is not None:
        route = state.gateway_settings.tasks.get(task)
        return route.model if route is not None else None
    return None


def _in_force_budget(state: ApiState) -> ContextBudget | None:
    """CAPABILITY-LESS FALLBACK for the enforced budget (see :func:`_current_model`).

    The COMPOSED runtime budget, unconditionally. A gateway without the
    ``TurnContextBudget`` capability leaves ``GatewayModel._turn_budget`` on its
    composed boot budget for EVERY turn — including every turn after a governed
    repoint — so deriving a budget here from the route register's CURRENT model
    would make this surface report a window nothing enforces (round 4: reported
    != enforced in the capability-less fallback). The model NAME beside it still
    reports the register primary — an honest routing fact — but the BUDGET
    reports exactly what the guard is armed with.
    """
    return state.runtime_settings.context_budget


async def _resolve_in_force(state: ApiState) -> tuple[str | None, ContextBudget | None]:
    """The model + budget the trim guard will enforce for the NEXT turn.

    NE-CTX-IN-FORCE round 4 (finding C): this endpoint's contract is
    "reported == enforced", and the enforcing reader is
    ``GatewayModel._turn_budget`` — ``state.gateway``'s ``TurnContextBudget``
    capability under the SAME lane rule ``GatewayModel._effective_lane``
    applies (the composed ``RuntimeSettings.lane`` pin; no per-run selection is
    in scope on an HTTP read). The previous derivation read the route
    register's PRIMARY, which is not the enforced model whenever the lane pin
    filters the primary out of the candidate list — under the air-gap beat's
    pinned private lane it reported the frontier window while the private one
    was enforced. Asking the same resolver the guard asks makes that
    divergence structurally impossible: there is no second copy of the
    candidate rule here to drift.

    The fallback mirrors the guard's own fallback: a gateway without the
    capability leaves the guard on its composed boot budget, and this
    projection then reports the register primary + that SAME composed boot
    budget (kept only for capability-less compositions — never a budget
    re-derived from the register's current model, which the guard cannot
    follow without the capability); a
    capability that answers ``None`` (unrouted task, a pin nothing matches)
    leaves the guard on the composed boot budget, reported as exactly that
    with no model name invented.
    """
    task = state.runtime_settings.task
    if isinstance(state.gateway, TurnContextBudget):
        lane = Lane(state.runtime_settings.lane) if state.runtime_settings.lane else None
        resolved = await state.gateway.context_budget_for_turn(task, lane=lane)
        if resolved is not None:
            return resolved.model, resolved.budget
        return None, state.runtime_settings.context_budget
    current = _current_model(state, task)
    return current, _in_force_budget(state)


def _read_evidence(
    state: ApiState,
    session_id: str,
    session_created_at: datetime,
    current_model: str | None,
) -> tuple[_ModelUsage, _FlipEvidence | None, _StampedContext | None]:
    """``current_model`` is :func:`_resolve_in_force`'s answer — the flip
    placard below must compare routing evidence against the model actually
    ENFORCED, which under a lane pin is not necessarily the register primary
    this function used to re-derive for itself."""
    task = state.runtime_settings.task
    session_records = replay(state.audit_engine, resource=f"session:{session_id}").events
    usage = _latest_model_usage(state.audit_engine, _newest_terminal(session_records), task)
    route_records = replay(state.audit_engine, resource=f"model:routing:{task}").events
    flip = _latest_flip(
        route_records,
        task,
        session_created_at,
        state.config_hash,
    )
    stamp: _StampedContext | None = None
    if flip is not None:
        if flip.new_model != current_model:
            flip = None
        else:
            stamp = _latest_prior_context_stamp(
                session_records,
                flip.audit_seq,
                state.config_hash,
            )
    # A hash-chain break makes every audit-derived observation untrustworthy.
    # The enforcing route register remains reportable, but provider usage and a
    # flip placard fail honest instead of presenting tampered rows as evidence.
    # Verify AFTER replay so every row used above is within the verified prefix;
    # a concurrent legitimate append may extend that prefix but cannot leave a
    # just-read, unverified tail in this response.
    if not verify_chain(state.audit_engine).valid:
        return _empty_usage(), None, None
    return usage, flip, stamp


def _storage_label(dialect: str) -> Literal["Postgres", "SQLite"]:
    if dialect == "postgresql":
        return "Postgres"
    if dialect == "sqlite":
        return "SQLite"
    # SqlAlchemySessionStore supports exactly these two write dialects. If a new
    # one is introduced, its user-facing identity is a deliberate wire decision.
    raise RuntimeError(f"unsupported context-ledger storage dialect: {dialect}")


def _agent_context_rows(
    engine: sa.Engine,
    *,
    owner_principal_id: str,
    department: str,
    agent_id: str,
    conversation_id: str,
    limit: int,
) -> tuple[list[sa.Row[Any]], int, str]:
    """Read a bounded newest window after repeating the complete owner scope."""

    conversation_scope = sa.and_(
        ne_agent_conversations.c.conversation_id == conversation_id,
        ne_agent_conversations.c.owner_principal_id == owner_principal_id,
        ne_agent_conversations.c.department == department,
        ne_agent_conversations.c.agent_name == agent_id,
    )
    run_scope = sa.and_(
        ne_agent_runs.c.conversation_id == ne_agent_conversations.c.conversation_id,
        ne_agent_runs.c.owner_principal_id == ne_agent_conversations.c.owner_principal_id,
        ne_agent_runs.c.department == ne_agent_conversations.c.department,
        ne_agent_runs.c.conversation_agent_name == ne_agent_conversations.c.agent_name,
        ne_agent_runs.c.conversation_id == conversation_id,
        ne_agent_runs.c.owner_principal_id == owner_principal_id,
        ne_agent_runs.c.department == department,
        ne_agent_runs.c.conversation_agent_name == agent_id,
        ne_agent_runs.c.agent_id == agent_id,
    )
    exact_target = sa.and_(
        ne_agent_run_turn_targets.c.run_id == ne_agent_runs.c.run_id,
        ne_agent_run_turn_targets.c.conversation_id == ne_agent_runs.c.conversation_id,
    )
    exact_history = sa.and_(
        ne_agent_run_history_attachments.c.run_id == ne_agent_runs.c.run_id,
        ne_agent_run_history_attachments.c.conversation_id == ne_agent_runs.c.conversation_id,
        ne_agent_run_history_attachments.c.owner_principal_id == ne_agent_runs.c.owner_principal_id,
        ne_agent_run_history_attachments.c.turn_id == ne_agent_run_turn_targets.c.turn_id,
        ne_agent_run_history_attachments.c.turn_sequence
        == ne_agent_run_turn_targets.c.turn_sequence,
        ne_agent_run_history_attachments.c.attachment_id
        == ne_agent_run_turn_targets.c.attachment_id,
        ne_agent_run_history_attachments.c.attempt_id == ne_agent_run_turn_targets.c.attempt_id,
    )
    valid_protected_shape = sa.and_(
        sa.func.length(ne_agent_run_history_attachments.c.protected_hash) == 64,
        sa.or_(
            sa.and_(
                ne_agent_run_history_attachments.c.content_status == "source_free",
                ne_agent_run_history_attachments.c.protected_domain == "protected_turn_content",
            ),
            sa.and_(
                ne_agent_run_history_attachments.c.content_status == "source_bound_container",
                ne_agent_run_history_attachments.c.protected_domain == "protected_source_output",
            ),
        ),
    )
    joined = (
        ne_agent_conversations.join(ne_agent_runs, run_scope)
        .join(ne_agent_run_turn_targets, exact_target)
        .join(ne_agent_run_history_attachments, exact_history)
    )
    predicate = sa.and_(conversation_scope, valid_protected_shape)
    selected = sa.select(
        ne_agent_run_turn_targets.c.run_id,
        ne_agent_run_turn_targets.c.attempt_id,
        ne_agent_run_turn_targets.c.turn_id,
        ne_agent_run_turn_targets.c.turn_sequence,
        ne_agent_run_turn_targets.c.attachment_id,
        ne_agent_run_history_attachments.c.protected_hash,
        ne_agent_run_history_attachments.c.content_status,
    ).select_from(joined)
    with engine.connect() as connection:
        if (
            connection.execute(
                sa.select(ne_agent_conversations.c.conversation_id).where(conversation_scope)
            ).one_or_none()
            is None
        ):
            raise LookupError("Agent Conversation disappeared while reading context ledger")
        component_ids: list[str] = []
        total = 0
        for row in connection.execution_options(stream_results=True).execute(
            selected.where(predicate).order_by(ne_agent_run_turn_targets.c.turn_sequence.asc())
        ):
            total += 1
            component_ids.append(_agent_context_component(row).component_id)
        newest = list(
            connection.execute(
                selected.where(predicate)
                .order_by(ne_agent_run_turn_targets.c.turn_sequence.desc())
                .limit(limit)
            ).all()
        )
    newest.reverse()
    return (
        newest,
        total,
        context_snapshot_hash(
            agent_id=agent_id,
            conversation_id=conversation_id,
            component_ids=component_ids,
        ),
    )


def _agent_context_component(row: sa.Row[Any]) -> AgentContextComponentView:
    input_target_id, component_id = context_component_id(
        run_id=str(row.run_id),
        attempt_id=str(row.attempt_id),
        turn_id=str(row.turn_id),
        turn_sequence=int(row.turn_sequence),
        attachment_id=str(row.attachment_id),
        protected_hash=str(row.protected_hash),
        content_status=str(row.content_status),
    )
    return AgentContextComponentView(
        turn_sequence=int(row.turn_sequence),
        run_id=str(row.run_id),
        turn_id=str(row.turn_id),
        input_target_id=input_target_id,
        terminal_hash=str(row.protected_hash),
        component_id=component_id,
    )


async def assemble_agent_context_ledger(
    state: ApiState,
    *,
    owner_principal_id: str,
    department: str,
    agent_id: str,
    conversation_id: str,
    limit: int,
) -> AgentContextLedgerView:
    """Project canonical completed Run turns without decrypting any content."""

    from ne.api.run_projection import locate_run_engine

    engine = locate_run_engine(state.engines)
    if engine is None:
        raise RuntimeError("agent context ledger unavailable")
    rows, total, context_id = await to_thread.run_sync(
        functools.partial(
            _agent_context_rows,
            engine,
            owner_principal_id=owner_principal_id,
            department=department,
            agent_id=agent_id,
            conversation_id=conversation_id,
            limit=limit,
        )
    )
    components = [_agent_context_component(row) for row in rows]
    return AgentContextLedgerView(
        conversation_id=conversation_id,
        agent_id=agent_id,
        context_id=context_id,
        resides=_storage_label(engine.dialect.name),
        components=components,
        total_components=total,
        active_components=len(components),
        truncated=total > len(components),
    )


async def assemble_context_ledger(state: ApiState, session_id: str) -> ContextLedgerView:
    """Build the content-free ledger for an already R3-authorized session."""
    max_components = max(1, state.runtime_settings.session_history_max_turns)
    current = await state.session_store.context_snapshot(session_id, max_components=max_components)
    if current is None:
        # The router verified the session immediately before this read. A reset or
        # deletion racing it must fail closed rather than fabricate an empty object.
        raise LookupError("session disappeared while reading context ledger")

    current_model, budget = await _resolve_in_force(state)
    usage, flip_evidence, stamped_context = await to_thread.run_sync(
        functools.partial(
            _read_evidence,
            state,
            session_id,
            current.session_created_at,
            current_model,
        )
    )

    flip: ContextModelFlipView | None = None
    # The immutable terminal stamp is the event-time identity. Mutable current
    # history may render the placard only while it still matches exactly.
    if (
        flip_evidence is not None
        and stamped_context is not None
        and stamped_context.context_id == current.context_id
        and stamped_context.active_components == len(current.components)
        and stamped_context.total_components == current.total_components
    ):
        flip = ContextModelFlipView(
            old_model=flip_evidence.old_model,
            new_model=flip_evidence.new_model,
            audit_seq=flip_evidence.audit_seq,
            changed_at=flip_evidence.changed_at,
            context_id_at_flip=stamped_context.context_id,
        )

    # NE-CTX-IN-FORCE (§0 law 5, reported == enforced): ``current_model`` and
    # ``budget`` above are ONE `_resolve_in_force` answer — the same
    # ``TurnContextBudget`` resolver the trim guard asks, under the same lane
    # rule, so the window printed here is the window enforced on the next turn
    # by construction rather than by a parallel re-derivation (round 4,
    # finding C: the register primary is not the enforced model when the
    # composed lane pin filters it out of the candidate list).
    return ContextLedgerView(
        session_id=current.session_id,
        context_id=current.context_id,
        resides=_storage_label(state.session_store.storage_dialect),
        components=[
            ContextComponentView(
                seq=component.seq,
                component_id=component.component_id,
                created_at=component.created_at,
            )
            for component in current.components
        ],
        total_components=current.total_components,
        active_components=len(current.components),
        truncated=current.truncated,
        budget=ContextBudgetObservationView(
            input_budget=budget.input_budget if budget is not None else None,
            input_tokens=usage.input_tokens,
            observed_at=usage.observed_at,
        ),
        current_model=current_model,
        served_model=usage.model,
        flip=flip,
    )
