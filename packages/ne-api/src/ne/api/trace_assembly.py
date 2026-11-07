# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Trace assembly (NE-167a) — the read-side of the Logfire-style /trace view.

:func:`assemble_trace` fetches one agent run's correlated audit records via the
EXISTING :func:`ne.audit.replay.replay` seam (keyed on ``trace_id``), nests them
into a nested SPAN TREE, derives typed per-node fields from each event's action
(reading NE-163a's model-call cost/token provenance DEFENSIVELY — absent, as it
is until that unit merges, the cost fields are simply ``None``, never an error),
computes per-model AGGREGATES + a run ROLLUP, and reuses the audit chain
:func:`ne.audit.verify.verify_chain` machinery for a server-authoritative
tamper-verification verdict (FULL-CHAIN, not windowed — see below).

PURE / SYNC: no FastAPI imports here. The route (``routers/trace.py``) and the
``read.trace`` governed tool (``tools.py``) both call :func:`assemble_trace` off
the event loop (one ``anyio.to_thread`` hop each) and ship its ``TraceAssemblyView``
verbatim — the SAME pydantic model on both surfaces, so route/tool structural
parity holds by construction.

FLAGGED DISCREPANCY — nesting is NOT by ``parent_id`` (decided, documented here
for NE-167b and any future parent_id-emitting unit): the unit brief that spawned
this module assumed ``subagent.*`` audit events carry a ``parent_id`` in their
provenance (citing ``dispatch.py:281``). That is FALSE at this HEAD:
``dispatch.py:281`` passes ``parent_id`` only into the in-memory
``ProcessRegistry`` record (never audited), and the dispatcher's audit ``_emit``
(``dispatch.py:97,455-473``) stamps a FIXED
``provenance={"source": "ne-runtime", "component": "dispatch"}`` with detail
dicts carrying only ``agent``/``depth`` (dispatch, ``dispatch.py:304``) /
``agent`` (complete, ``dispatch.py:380``) / route fields
(``dispatch.py:415-423``) — no ``parent_id`` ever reaches an audit payload.
Since the emitters are FROZEN for this unit (read-side only, §0 law 5: never
fabricate a field that isn't actually emitted), nesting is instead a
deterministic, SEQ-ORDERED BRACKET reconstruction: a ``subagent.dispatch``
(outcome ``success``) OPENS a span and PUSHES it; the matching
``subagent.complete`` CLOSES the innermost open span for that agent (any
outcome) and is folded into it (no separate node — the completion is rendered
as the span's end, not a sibling event). This is EXACT for the runtime's actual
behavior: ``GovernedSubAgentDispatcher._dispatch_reserved`` AWAITS the child run
inline, so brackets for ONE dispatcher can never interleave within a single
trace. A pathological concurrent multi-dispatcher fan-out sharing one trace_id
could in principle attach a `subagent.complete` to the nearest open span rather
than its true parent if agent names collide — best-effort until a future unit
threads real parent linkage into the audit payload.

VERIFICATION IS FULL-CHAIN, NOT WINDOWED (fixed post-review; documented here for
NE-167b and any future reader): an earlier revision called
``verify_chain(engine, start=started_at, end=ended_at)`` — a verify WINDOW
derived from ``started_at``/``ended_at``, which are in turn derived from the
events ``replay()`` actually returned for this ``trace_id``. That is unsound:
``replay()`` selects rows by the DENORMALIZED ``trace_id`` COLUMN (a fast query
column, not the hash-covered payload — see ``replay.py``'s own
PAYLOAD-AUTHORITATIVE-READ note). An attacker who directly UPDATEs that column
on the trace's first or last row (leaving the row's ``record_hash``/``prev_hash``
untouched) makes ``replay()`` silently omit that row from ``events`` — which
also shifts ``started_at``/``ended_at`` to exclude it. A verify WINDOWED to that
shrunken range never walks the tampered row, so ``ne.audit.verify._column_mismatch``
(which only runs over rows inside ``[lo, hi]``) never sees the trace_id/payload
divergence, and the hash-chain linkage into/out of the window is untouched (the
tamper never touched a hash) — so the windowed verdict comes back ``valid=True``
over an INCOMPLETE, silently-truncated trace. Calling :func:`verify_chain` with
no window instead walks every row's column/payload congruence unconditionally,
so a boundary-row column tamper can no longer hide by falling outside a
window that its own tamper helped shrink. The cost is a whole-log walk per
request rather than a windowed one; that trade is deliberately taken here
because a false "valid" verdict on a *governed, ops-facing* trace view is the
more dangerous failure mode.

C7 (HARD, no-ACL-to-client): every typed span reads ONLY the enumerated
identifier/enum/count/delta/model-lane/cost keys named in this module. Unknown
provenance keys NEVER cross the assembler — there is no wildcard payload/
provenance copy anywhere below. Free-text ``detail`` crosses only for the
``guardrail``, ``tool``, and raw ``event`` (law 5) node kinds, whose emitters
already ship only rule names / redacted write-audit shape / scrubbed text
(identical exposure class to the existing ops-gated ``GET /v1/audit/replay``,
which already ships full payloads to ops — the ``tool.execute`` detail is the
C6 executor's own binding-derived redacted detail: names, tiers, statuses,
error classes, never values). No ACL label VALUES, principal labels, query
text, or message content are ever read or surfaced by this module.

B14 (execution→approval join): a ``tool.execute`` record renders as a typed
``tool`` node carrying ``approval_id`` read from the provenance the C6
executor stamped AT THE EXECUTION SITE (``ne.runtime.middleware``). A record
written before B14 (or an ungated tier-0/1 execution) has no such key and the
node serves ``approval_id: null`` — the console renders NO link then; the
join is never inferred here from adjacent approval events.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field

from ne.api.wire import WireVerificationBasis
from ne.audit import verify_chain
from ne.audit.replay import replay
from ne.audit.store import AuditRecord

_KNOWLEDGE_DETAIL_RE = re.compile(r"candidates=(\d+) returned=(\d+) acl_trim_delta=(-?\d+)")

#: The verified-read emitter's counts-only detail line
#: (``ne.brain_ontology.verified.answers``): ``rows=120 row_limit=500
#: truncated=false truncation_reason=none``. Parsed with the same discipline as
#: ``_KNOWLEDGE_DETAIL_RE`` — no match ⇒ every field ``None``, never an error.
_VERIFIED_READ_DETAIL_RE = re.compile(r"rows=(\d+) row_limit=(\d+) truncated=(true|false)")


# ---------------------------------------------------------------------------
# Wire models — one optional field PER span kind (no pydantic union: unions of
# all-optional models cross-coerce in pydantic's smart mode).
# ---------------------------------------------------------------------------


class SubagentSpan(BaseModel):
    """A ``subagent.dispatch``/``subagent.complete`` bracket (NE-161 dispatch)."""

    agent: str | None = None
    depth: int | None = None
    completed: bool = False
    ended_at: datetime | None = None
    duration_ms: float | None = None
    outcome: str | None = None


class RouteSpan(BaseModel):
    """A ``subagent.route`` decision (NE-161 per-child routing)."""

    agent: str | None = None
    route_task: str | None = None
    route_lane: str | None = None
    tier: str | None = None
    task_source: str | None = None
    lane_source: str | None = None
    hint_clamped: bool | None = None
    reason: str | None = None


class ModelCallSpan(BaseModel):
    """A ``ne.gateway.complete``/``ne.gateway.stream`` model call.

    ``requested_model``/``response_model``/``cost_usd``/``input_tokens``/
    ``output_tokens`` are read
    DEFENSIVELY from NE-163a provenance (written as strings — parsed here to
    numbers) — absent (pre-NE-163a, or a stream whose usage never lands) ⇒
    ``None``, never an error. ``latency_ms``/``ttft_ms`` (B12) follow the same
    discipline, with the absent-vs-zero distinction load-bearing: a provenance
    ``"0"`` parses to ``0`` (a measured sub-millisecond call), while an absent
    key (every pre-B12 historical row; ``ttft_ms`` on every non-stream call)
    stays ``None`` and renders as absent — never a fabricated zero, never a
    zero read as absent. B12's ``principal`` provenance key is deliberately
    NOT copied here: this module's C7 posture (docstring above) surfaces no
    principal labels; per-principal reads are SQL aggregations over the chain
    (B11/B4-prime), not trace-node fields.
    """

    task: str | None = None
    lane: str | None = None
    #: The boot-registry alias the caller explicitly selected. ``None`` on a
    #: routed/default call and on every row written before BD-15 — never inferred
    #: from ``model_name``, because a fallback may have served a different model.
    requested_model: str | None = None
    model_name: str | None = None
    response_model: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    #: WHERE in the request's attempt sequence the model that answered sat, as
    #: the gateway recorded it: ``"primary"`` (nothing was attempted before it)
    #: or ``"fallback"`` (something was, and did not serve). ``None`` for every
    #: record written before the key existed — and it MUST stay ``None`` there:
    #: defaulting it to ``"primary"`` would manufacture the reassurance the
    #: field exists to withhold, that the route's first choice served.
    route_position: str | None = None
    #: The declared NAMES of the candidates passed over, in attempt order,
    #: comma-joined by the emitter. Present only alongside a ``fallback``
    #: position. Names only (C12) — the emitter never puts provider error text
    #: or an ``api_base`` on this key.
    fallback_from: str | None = None


class RedactionSpan(BaseModel):
    """A ``redact.pre_egress`` frontier-egress redaction pass (NE-162)."""

    hits: int | None = None
    categories: dict[str, int] = Field(default_factory=dict)
    task: str | None = None
    lane: str | None = None
    model_name: str | None = None


class KnowledgeSpan(BaseModel):
    """A ``knowledge.retrieve`` retrieval (NE-164)."""

    candidates: int | None = None
    returned: int | None = None
    acl_trim_delta: int | None = None


class PdpSpan(BaseModel):
    """A ``pdp.decide`` policy decision."""

    effect: str | None = None
    reason: str | None = None
    tier: int | None = None
    policy_version: str | None = None
    requested_action: str | None = None


class ApprovalSpan(BaseModel):
    """An ``ApprovalEvent`` row (``event_type == 'approval'``)."""

    approval_id: str | None = None
    tier: int | None = None
    decision: str | None = None
    subject_ref: str | None = None


class GuardrailSpan(BaseModel):
    """A ``guardrail.block`` denial — the emitter ships rule names only."""

    detail: str | None = None


class ToolSpan(BaseModel):
    """A ``tool.execute`` governed execution (the C6 executor's emit).

    ``approval_id`` is the durable approval request id the execution consumed,
    stamped at the execution site (B14, ``ne.runtime.middleware``). ``None``
    for records written before the field existed and for ungated (tier-0/1)
    executions — rendered as no link, never inferred.

    The remaining fields are the verified-answer executor's own redacted audit
    detail (``ne.brain_ontology.verified.tools._make_audit_detail``) lifted
    into typed fields so the console can state what ran in plain language:
    ``template_id`` names the template, ``args`` carries the ARGUMENT NAMES
    ONLY (the emitter's ``args_shape`` — values never reach the chain, C12),
    and ``result_count`` is the row count the caller saw. ``result_count`` is
    served only when the detail carries a NON-NEGATIVE integer: the emitter
    writes ``-1`` when it could not parse its own result header, and serving
    that sentinel would fabricate a count nobody measured — absent and zero
    are different facts (0 = the query genuinely matched nothing).
    """

    approval_id: str | None = None
    template_id: str | None = None
    template_version: int | None = None
    args: list[str] = Field(default_factory=list)
    result_count: int | None = None


class VerifiedReadSpan(BaseModel):
    """A ``knowledge.verified_read`` counts-only read receipt (T3 read core)."""

    rows: int | None = None
    row_limit: int | None = None
    truncated: bool | None = None


class GroundingSpan(BaseModel):
    """A ``grounding.assist`` gate event — counts/enums only, never draft text.

    ``phase`` is the emitter's own vocabulary (``"pass-polish"`` /
    ``"fail-redraft"``); ``adopted`` says whether the assist's text was emitted.
    """

    phase: str | None = None
    adopted: bool | None = None
    error_class: str | None = None


class SessionSpan(BaseModel):
    """A ``session.run`` terminal record — the turn's conversation coordinates.

    ``session_id`` is the durable conversation record this run belongs to; the
    trace carries the POINTER only, never the conversation text (the chain is
    metadata — bodies live in the governed session store).
    """

    agent: str | None = None
    department: str | None = None
    session_id: str | None = None
    succeeded: bool | None = None


class TraceNodeView(BaseModel):
    """One node in the assembled trace tree."""

    event_id: str
    seq: int
    action: str | None = None
    event_type: str
    actor: str
    resource: str | None = None
    outcome: str | None = None
    occurred_at: datetime
    kind: str
    subagent: SubagentSpan | None = None
    route: RouteSpan | None = None
    model_call: ModelCallSpan | None = None
    redaction: RedactionSpan | None = None
    knowledge: KnowledgeSpan | None = None
    pdp: PdpSpan | None = None
    approval: ApprovalSpan | None = None
    guardrail: GuardrailSpan | None = None
    tool: ToolSpan | None = None
    verified_read: VerifiedReadSpan | None = None
    grounding: GroundingSpan | None = None
    session: SessionSpan | None = None
    #: Raw-event fallback (§0 law 5): an action whose typed emit isn't recognized
    #: renders with its scrubbed ``detail`` text and no typed span — never
    #: fabricated fields.
    detail: str | None = None
    children: list[TraceNodeView] = Field(default_factory=list)


TraceNodeView.model_rebuild()


class ModelAggregateView(BaseModel):
    """Per-model rollup over every ``model_call`` node in the tree."""

    model: str
    lane: str | None = None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
    #: The distinct TASKS this model served in this run, in first-served order
    #: — the run's task→model routing, readable without opening every span. A
    #: call whose record never named a task contributes nothing (the list is
    #: shorter, never padded with a placeholder), so an empty list means no
    #: call on this model named its task, not that it served no task.
    tasks: list[str] = Field(default_factory=list)


class TraceRollupView(BaseModel):
    """The run-level rollup: event count, total cost, and wall-clock span."""

    trace_id: str
    total_events: int
    total_cost_usd: float | None
    started_at: datetime
    ended_at: datetime
    duration_ms: float


class TraceVerificationView(BaseModel):
    """The server-computed chain-verification verdict for the run's window.

    Field types copied VERBATIM from ``ne.api.wire.ChainVerificationView``.
    ``basis`` is always ``"full-chain"`` here and is on the wire anyway: this
    endpoint's verdict and the audit page's are folded into ONE latch by the
    console, so they have to be comparable, and "it is always the same value
    here" is a fact about today's assembler, not about the shape.
    """

    valid: bool
    basis: WireVerificationBasis
    contradiction: bool
    checked: int
    first_break_seq: int | None
    first_break_event_id: str | None
    reason: str | None
    detail: str


class TraceAssemblyView(BaseModel):
    """``GET /v1/trace/{trace_id}`` (and ``read.trace``) response body."""

    trace_id: str
    rollup: TraceRollupView
    models: list[ModelAggregateView]
    nodes: list[TraceNodeView]
    verification: TraceVerificationView


# ---------------------------------------------------------------------------
# Defensive payload/provenance readers
# ---------------------------------------------------------------------------


def _prov_str(payload: dict[str, Any], key: str) -> str | None:
    """Read ``payload['provenance'][key]`` as a string (mirrors ``replay.py``).

    ``ne.schema`` provenance values are typed ``str | ProvenanceEntry``, and a
    ``ProvenanceEntry`` round-trips through JSON as ``{"key": ..., "value": ...}``
    — both shapes are accepted. Absent/malformed ⇒ ``None`` (never raises).
    """
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    raw = provenance.get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _detail_json(record: AuditRecord) -> dict[str, Any]:
    """Parse ``record.payload['detail']`` as a JSON object; ``{}`` on any failure."""
    detail = record.payload.get("detail")
    if not isinstance(detail, str) or not detail:
        return {}
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_categories(raw: str | None) -> dict[str, int]:
    """Parse ``'a=1,b=2'`` ⇒ ``{'a': 1, 'b': 2}``; ``None``/``'none'`` ⇒ ``{}``.

    Malformed pairs (no ``=``, non-integer count) are skipped, never raised.
    """
    if raw is None or raw == "none":
        return {}
    out: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if not key:
            continue
        try:
            out[key] = int(value.strip())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Per-node typing (STEP 2d)
# ---------------------------------------------------------------------------


def _base_kwargs(record: AuditRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "seq": record.seq,
        "action": record.action,
        "event_type": record.event_type,
        "actor": record.actor,
        "resource": record.resource,
        "outcome": record.outcome,
        "occurred_at": record.occurred_at,
    }


def _typed_node(record: AuditRecord) -> TraceNodeView:
    """Build the typed (or raw law-5) node for one audit record.

    Never raises: every field read is defensive (absence/malformed ⇒ ``None``).
    """
    base = _base_kwargs(record)
    action = record.action
    payload = record.payload

    if action == "subagent.dispatch" and record.outcome == "success":
        detail = _detail_json(record)
        span = SubagentSpan(
            agent=_opt_str(detail.get("agent")), depth=_opt_int(detail.get("depth"))
        )
        return TraceNodeView(**base, kind="subagent", subagent=span)

    if action == "subagent.route":
        detail = _detail_json(record)
        span = RouteSpan(
            agent=_opt_str(detail.get("agent")),
            route_task=_opt_str(detail.get("route_task")),
            route_lane=_opt_str(detail.get("route_lane")),
            tier=_opt_str(detail.get("tier")),
            task_source=_opt_str(detail.get("task_source")),
            lane_source=_opt_str(detail.get("lane_source")),
            hint_clamped=_opt_bool(detail.get("hint_clamped")),
            reason=_opt_str(detail.get("reason")),
        )
        return TraceNodeView(**base, kind="subagent_route", route=span)

    if action in ("ne.gateway.complete", "ne.gateway.stream"):
        span = ModelCallSpan(
            task=_prov_str(payload, "task"),
            lane=_prov_str(payload, "lane"),
            requested_model=_prov_str(payload, "requested_model"),
            model_name=_prov_str(payload, "model_name"),
            response_model=_prov_str(payload, "response_model"),
            cost_usd=_to_float(_prov_str(payload, "cost_usd")),
            input_tokens=_to_int(_prov_str(payload, "input_tokens")),
            output_tokens=_to_int(_prov_str(payload, "output_tokens")),
            latency_ms=_to_int(_prov_str(payload, "latency_ms")),
            ttft_ms=_to_int(_prov_str(payload, "ttft_ms")),
            route_position=_prov_str(payload, "route_position"),
            fallback_from=_prov_str(payload, "fallback_from"),
        )
        return TraceNodeView(**base, kind="model_call", model_call=span)

    if action == "redact.pre_egress":
        span = RedactionSpan(
            hits=_to_int(_prov_str(payload, "redaction_hits")),
            categories=_parse_categories(_prov_str(payload, "redaction_categories")),
            task=_prov_str(payload, "task"),
            lane=_prov_str(payload, "lane"),
            model_name=_prov_str(payload, "model_name"),
        )
        return TraceNodeView(**base, kind="redaction", redaction=span)

    if action == "knowledge.retrieve":
        detail_text = payload.get("detail")
        match = _KNOWLEDGE_DETAIL_RE.search(detail_text) if isinstance(detail_text, str) else None
        span = KnowledgeSpan(
            candidates=int(match.group(1)) if match else None,
            returned=int(match.group(2)) if match else None,
            acl_trim_delta=int(match.group(3)) if match else None,
        )
        return TraceNodeView(**base, kind="knowledge", knowledge=span)

    if action == "pdp.decide":
        detail = _detail_json(record)
        span = PdpSpan(
            effect=_opt_str(detail.get("effect")),
            reason=_opt_str(detail.get("reason")),
            tier=_opt_int(detail.get("tier")),
            policy_version=_opt_str(detail.get("policy_version")),
            requested_action=_prov_str(payload, "requested_action"),
        )
        return TraceNodeView(**base, kind="pdp", pdp=span)

    if record.event_type == "approval" or (action is not None and action.startswith("approval.")):
        span = ApprovalSpan(
            approval_id=_opt_str(payload.get("approval_id")),
            tier=_opt_int(payload.get("tier")),
            decision=_opt_str(payload.get("decision")),
            subject_ref=_opt_str(payload.get("subject_ref")),
        )
        return TraceNodeView(**base, kind="approval", approval=span)

    if action == "guardrail.block":
        span = GuardrailSpan(detail=_opt_str(payload.get("detail")))
        return TraceNodeView(**base, kind="guardrail", guardrail=span)

    if action == "tool.execute":
        # B14: the join reads ONLY the execution-site provenance stamp. detail
        # (the executor's redacted write-audit shape / error class) is kept so
        # the pane loses nothing it rendered before this node was typed.
        # The verified-answer executor's redacted detail (template_id /
        # args_shape / result_count — names and counts only, C12) is lifted
        # into typed fields; a tool whose detail carries none of them (write
        # tools, older records) serves the same span with every field absent.
        detail = _detail_json(record)
        raw_args = detail.get("args_shape")
        args = [a for a in raw_args if isinstance(a, str)] if isinstance(raw_args, list) else []
        # _opt_int, not bare isinstance: bool subclasses int, and a JSON
        # ``true`` served as 1 would render "1 row" — a count nobody measured.
        count = _opt_int(detail.get("result_count"))
        tool_span = ToolSpan(
            approval_id=_prov_str(payload, "approval_id"),
            template_id=_opt_str(detail.get("template_id")),
            template_version=_opt_int(detail.get("template_version")),
            args=args,
            # The emitter's -1 means "could not parse my own result header" —
            # an unknown, not a count. Only a non-negative integer is a fact.
            result_count=count if count is not None and count >= 0 else None,
        )
        return TraceNodeView(
            **base, kind="tool", tool=tool_span, detail=_opt_str(payload.get("detail"))
        )

    if action == "knowledge.verified_read":
        detail_text = payload.get("detail")
        match = (
            _VERIFIED_READ_DETAIL_RE.search(detail_text) if isinstance(detail_text, str) else None
        )
        read_span = VerifiedReadSpan(
            rows=int(match.group(1)) if match else None,
            row_limit=int(match.group(2)) if match else None,
            truncated=(match.group(3) == "true") if match else None,
        )
        return TraceNodeView(
            **base,
            kind="verified_read",
            verified_read=read_span,
            detail=_opt_str(payload.get("detail")),
        )

    if action == "grounding.assist":
        detail = _detail_json(record)
        grounding_span = GroundingSpan(
            phase=_opt_str(detail.get("phase")),
            adopted=_opt_bool(detail.get("adopted")),
            error_class=_opt_str(detail.get("error_class")),
        )
        return TraceNodeView(**base, kind="grounding", grounding=grounding_span)

    if action == "session.run":
        detail = _detail_json(record)
        session_span = SessionSpan(
            agent=_opt_str(detail.get("agent")),
            department=_opt_str(detail.get("department")),
            session_id=_opt_str(detail.get("session_id")),
            succeeded=_opt_bool(detail.get("succeeded")),
        )
        return TraceNodeView(**base, kind="session", session=session_span)

    # §0 law 5 — an unrecognized action renders as its raw event, never fabricated.
    return TraceNodeView(**base, kind="event", detail=_opt_str(payload.get("detail")))


# ---------------------------------------------------------------------------
# Tree assembly (STEP 2c) — deterministic seq-ordered bracket reconstruction.
# See the module docstring's FLAGGED DISCREPANCY note for why this is not a
# parent_id lookup.
# ---------------------------------------------------------------------------


def _fold_complete(record: AuditRecord, stack: list[TraceNodeView]) -> bool:
    """Try to fold a ``subagent.complete`` record into its opening span.

    Returns ``True`` if folded (closing node mutated, nothing to attach);
    ``False`` if the stack is empty (render the record as a raw event instead —
    law 5, never fabricate a bracket that doesn't exist).
    """
    if not stack:
        return False
    detail = _detail_json(record)
    agent = detail.get("agent") if isinstance(detail.get("agent"), str) else None
    target_idx = len(stack) - 1
    if agent is not None:
        for idx in range(len(stack) - 1, -1, -1):
            span = stack[idx].subagent
            if span is not None and span.agent == agent:
                target_idx = idx
                break
    closing = stack[target_idx]
    del stack[target_idx:]
    assert closing.subagent is not None
    closing.subagent.completed = True
    closing.subagent.ended_at = record.occurred_at
    elapsed_s = (record.occurred_at - closing.occurred_at).total_seconds()
    closing.subagent.duration_ms = elapsed_s * 1000.0
    closing.subagent.outcome = record.outcome
    return True


def _build_tree(events: tuple[AuditRecord, ...]) -> list[TraceNodeView]:
    top: list[TraceNodeView] = []
    stack: list[TraceNodeView] = []

    for record in events:
        if record.action == "subagent.complete" and _fold_complete(record, stack):
            continue

        node = _typed_node(record)
        attach_list = stack[-1].children if stack else top
        attach_list.append(node)

        if node.kind == "subagent":
            stack.append(node)

    return top


def _walk(nodes: list[TraceNodeView]) -> list[TraceNodeView]:
    out: list[TraceNodeView] = []
    for node in nodes:
        out.append(node)
        out.extend(_walk(node.children))
    return out


def _aggregate_models(nodes: list[TraceNodeView]) -> list[ModelAggregateView]:
    order: list[str] = []
    by_key: dict[str, ModelAggregateView] = {}
    for node in _walk(nodes):
        if node.kind != "model_call" or node.outcome != "success":
            continue
        call = node.model_call
        if call is None:
            continue
        key = call.response_model or call.model_name or "unknown"
        if key not in by_key:
            by_key[key] = ModelAggregateView(
                model=key, lane=call.lane, calls=0, input_tokens=0, output_tokens=0, cost_usd=None
            )
            order.append(key)
        agg = by_key[key]
        agg.calls += 1
        if call.input_tokens is not None:
            agg.input_tokens += call.input_tokens
        if call.output_tokens is not None:
            agg.output_tokens += call.output_tokens
        if call.cost_usd is not None:
            agg.cost_usd = call.cost_usd if agg.cost_usd is None else agg.cost_usd + call.cost_usd
        if agg.lane is None and call.lane is not None:
            agg.lane = call.lane
        if call.task is not None and call.task not in agg.tasks:
            agg.tasks.append(call.task)
    return [by_key[key] for key in order]


def assemble_trace(engine: sa.Engine, trace_id: str) -> TraceAssemblyView | None:
    """Assemble one run's governed trace tree, aggregates, rollup, and verdict.

    Returns ``None`` when the trace has no correlated events (unknown trace_id).
    Never raises on malformed/absent per-event fields — every reader above is
    defensive (§0 law 5 / the NE-163a pre-merge safety contract).

    The verification verdict is a FULL (unbounded) chain verify, not windowed to
    this trace's ``[started_at, ended_at]`` — see the module docstring's
    "VERIFICATION IS FULL-CHAIN, NOT WINDOWED" section for why a window derived
    from ``replay()``'s own (tamperable-column-filtered) events cannot safely
    bound the verify walk.
    """
    result = replay(engine, trace_id=trace_id)
    events = result.events
    if not events:
        return None

    nodes = _build_tree(events)
    models = _aggregate_models(nodes)

    total_events = len(events)
    started_at = min(r.occurred_at for r in events)
    ended_at = max(r.occurred_at for r in events)
    duration_ms = (ended_at - started_at).total_seconds() * 1000.0
    cost_values = [m.cost_usd for m in models if m.cost_usd is not None]
    total_cost_usd = sum(cost_values) if cost_values else None
    rollup = TraceRollupView(
        trace_id=trace_id,
        total_events=total_events,
        total_cost_usd=total_cost_usd,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )

    # FULL (unbounded) verify, not windowed to [started_at, ended_at] — see
    # "VERIFICATION IS FULL-CHAIN, NOT WINDOWED" above. A window derived from the
    # trace's own (already-filtered-by-tamperable-column) events cannot be trusted
    # to bound the verify walk: it would let a boundary-row column tamper hide
    # itself from the walk. Full verify checks the denormalized column/payload
    # congruence of EVERY row in the log, so a tampered trace_id/occurred_at
    # column can never escape detection just because it happens to fall outside
    # the (attacker-influenced) window.
    cv = verify_chain(engine)
    verification = TraceVerificationView(
        valid=cv.valid,
        # An UNBOUNDED verify_chain over the whole ledger — the strongest verdict
        # the server emits, and the one thing that makes this screen's reading
        # worth more than the polled page's anchored one.
        basis="full-chain",
        # No anchor is consulted here, so there is none to contradict.
        contradiction=False,
        checked=cv.checked,
        first_break_seq=cv.first_break_seq,
        first_break_event_id=cv.first_break_event_id,
        reason=cv.reason,
        detail=cv.detail,
    )

    return TraceAssemblyView(
        trace_id=trace_id,
        rollup=rollup,
        models=models,
        nodes=nodes,
        verification=verification,
    )
