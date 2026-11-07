# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only U-P-B collectors over three existing public seams (NE-068).

Consumes ONLY the public read APIs of already-merged packages:

* ``ne.audit.replay`` — usage/turn events (Utilization).
* ``ne.eval.store.EvalRunStore.list_runs`` — eval pass-rates (Proficiency).
* ``ne.flywheel.SqlAlchemyCorrectionSignalStore.read_signals`` — the NE-064
  cursor-paginated correction-signal read API (Proficiency).

Nothing in this module ever calls a write/append surface on a consumed store —
it is READ-ONLY consumption end to end (plan §1.4 hazard: keeps this unit
disjoint from NE-065, which builds concurrently in ne-flywheel). No
``datetime.now()``/``uuid`` anywhere here either: every value is a pure
function of the caller-supplied ``period`` and the rows the seams return
(determinism, SPEC §2.1).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import replay
from ne.eval.store import EvalRunStore
from ne.flywheel import SqlAlchemyCorrectionSignalStore
from ne.obs.kpi import (
    BusinessValueMetrics,
    ProficiencyMetrics,
    ReportPeriod,
    SpendAttribution,
    SpendByOutcome,
    UtilizationMetrics,
    WindowMetrics,
)

#: Audit actions counted as a successful agent turn (proxy — see README gap note).
DEFAULT_TURN_ACTIONS: tuple[str, ...] = ("ne.gateway.complete", "ne.gateway.stream")
#: Audit actions counted as a tool invocation.
DEFAULT_TOOL_ACTIONS: tuple[str, ...] = ("tool.execute",)
#: ``ne.schema.CorrectionKind`` values that count AGAINST proficiency.
NEGATIVE_KINDS: frozenset[str] = frozenset(
    {"thumbs_down", "wrong_source", "re_query", "approval_rejection"}
)
#: ``ne.schema.CorrectionKind`` values that count FOR proficiency.
#: (``edit_distance`` counts only in the total — its magnitude semantics vary,
#: so it is deliberately kept neutral rather than classified positive/negative.)
POSITIVE_KINDS: frozenset[str] = frozenset({"thumbs_up"})
#: Default page size for the ``read_signals`` cursor pagination loop.
SIGNAL_PAGE_SIZE = 500
#: Cap on the number of eval runs scanned in-memory per window (documented gap:
#: ``EvalRunStore.list_runs`` has no time-window filter yet).
EVAL_SCAN_LIMIT = 100_000

_SESSION_ID_KEY = "session_id"
_COST_USD_KEY = "cost_usd"

#: Audit actions that carry model spend — the two the gateway emits for a model
#: call, and the only actions whose provenance carries a ``cost_usd``
#: (``ne.gateway.gateway._usage_provenance``). Any other action counted here would
#: invent spend that no model call incurred.
#:
#: NOT a claim that every such row has a sibling ``CostEvent``: the gateway also
#: emits an ``ne.gateway.stream`` row on the all-candidates-failed arm with no
#: cost and no tag, which lands in ``calls_missing_cost``. See the KNOWN LIMITS
#: on :func:`collect_spend`.
DEFAULT_SPEND_ACTIONS: tuple[str, ...] = ("ne.gateway.complete", "ne.gateway.stream")

#: Decimal places sums are rounded to. Frontier per-call costs run to ~1e-7 USD,
#: so this keeps a long window's sum free of float drift without discarding a
#: real sub-cent charge.
_USD_PLACES = 8
#: Decimal places the served attribution share is rounded to.
_SHARE_PLACES = 6


def _recorded_cost(payload: dict[str, Any]) -> float | None:
    """The call's recorded price, or ``None`` when it was never recorded.

    ``cost_usd`` rides the provenance as the gateway's shortest round-tripping
    ``str(cost)``. ABSENT means the price is UNKNOWN — never zero: a recorded
    ``0.0`` (a private-lane or unknown-in-cost-map model, which
    ``ne.gateway.cost.extract_cost`` documents as legitimate) is a MEASUREMENT
    and is returned as ``0.0``. An unparseable value is treated as unrecorded
    rather than raising: a malformed cell must not take down a monthly report,
    and it is surfaced in ``calls_missing_cost`` rather than silently dropped.
    """
    raw = _provenance_value(payload, _COST_USD_KEY)
    if raw is None:
        return None
    try:
        cost = float(raw)
    except ValueError:
        return None
    # NaN/Infinity parse fine and would poison every sum, the share, and the JSON
    # response downstream. They are not prices — treat them as UNRECORDED, which
    # routes them to ``calls_missing_cost`` where the screen states the gap.
    if not math.isfinite(cost):
        return None
    return cost


def collect_spend(
    audit_engine: sa.Engine,
    period: ReportPeriod,
    *,
    spend_actions: Sequence[str] = DEFAULT_SPEND_ACTIONS,
) -> SpendAttribution:
    """Attribute model spend over ``period`` to the outcomes the calls declared (C8).

    READ SEAM: ``ne.audit.replay`` — the SAME payload-authoritative seam
    :func:`collect_utilization` uses. Deliberately NOT the ne-events outbox,
    even though the outbox denormalizes ``kpi_tag`` for "the reporting path":
    ``ne.events.prune_acked`` DELETES outbox rows once every consumer has acked
    them, so the outbox cannot answer a question about last month. The audit
    chain is durable and hash-covered. That the gateway stamps the SAME
    ``kpi_tag`` on the ``AuditEvent`` as on the sibling ``CostEvent`` — the join
    this whole surface rests on — is pinned by
    ``ne-gateway/tests/test_cost_events.py::test_kpi_tag_is_stamped_on_the_audit_event_too``,
    not asserted here.

    HONESTY RULES, each pinned by a named test in ``test_collect_spend.py``:

    * A call with no tag (or an empty tag, which names nobody) lands in the
      unattributed bucket. It is never imputed into a named outcome and no
      default tag is ever substituted.
    * A call with no recorded price KEEPS its outcome (attribution and price are
      separate facts) but enters no dollar sum — summing it as $0.00 would make a
      short total look complete.
    * ``attributed_share`` is ``None`` when nothing was spent, and is
      DOLLAR-weighted; ``attributed_call_share`` is the CALL-weighted coverage
      figure. A 0/0 window is not fully attributed; it has no denominator.

    KNOWN LIMITS (stated, not silently absorbed):

    * A provider failure inside ``complete()`` emits NO audit row at all
      (``gateway.py`` appends to ``attempts`` and rolls to the next candidate),
      so a provider that bills for a failed call produces spend invisible to
      both the totals and ``calls_missing_cost``.
    * ``calls_missing_cost`` counts AUDIT ROWS with no recorded price. The
      all-candidates-failed stream arm emits such a row for a call that cost
      nothing, so this figure can overcount.
    * Attribution is by OUTCOME only. ``CostEvent`` has no department field, and
      this collector does not group by ``task`` — see the ne-api spec §1.6 #12.
    """
    result = replay(audit_engine, start=period.start, end=period.end_inclusive_micro)
    spend_set = frozenset(spend_actions)

    # tag -> [usd, priced_calls, unpriced_calls]
    by_tag: dict[str, list[float | int]] = {}
    unattributed_usd = 0.0
    unattributed_priced = 0
    unattributed_unpriced = 0

    for record in result.events:
        if record.action not in spend_set:
            continue
        cost = _recorded_cost(record.payload)
        # Payload-authoritative: ``AuditRecord.kpi_tag`` is read off the
        # hash-covered payload by ``ne.audit.store._record_from_row``, so a
        # tampered denormalized column cannot re-attribute spend.
        tag = record.kpi_tag
        if tag is None or tag == "":
            if cost is None:
                unattributed_unpriced += 1
            else:
                unattributed_usd += cost
                unattributed_priced += 1
            continue
        # An UNPRICED call keeps its outcome. Attribution and price are separate
        # facts: the call was declared against this outcome whether or not the
        # provider told us what it cost, and dropping it here would erase real
        # attributed activity from the only screen that reports it.
        bucket = by_tag.setdefault(tag, [0.0, 0, 0])
        if cost is None:
            bucket[2] = int(bucket[2]) + 1
        else:
            bucket[0] = float(bucket[0]) + cost
            bucket[1] = int(bucket[1]) + 1

    outcomes = tuple(
        SpendByOutcome(
            kpi_tag=tag,
            cost_usd=round(float(usd), _USD_PLACES),
            calls=int(priced) + int(unpriced),
            priced_calls=int(priced),
            unpriced_calls=int(unpriced),
        )
        for tag, (usd, priced, unpriced) in sorted(by_tag.items())
    )
    attributed_usd = round(sum(o.cost_usd for o in outcomes), _USD_PLACES)
    attributed_calls = sum(o.calls for o in outcomes)
    unattributed_usd = round(unattributed_usd, _USD_PLACES)
    unattributed_calls = unattributed_priced + unattributed_unpriced
    total_usd = round(attributed_usd + unattributed_usd, _USD_PLACES)
    total_calls = attributed_calls + unattributed_calls
    calls_missing_cost = sum(o.unpriced_calls for o in outcomes) + unattributed_unpriced
    attributed_share = (
        round(attributed_usd / total_usd, _SHARE_PLACES) if total_usd != 0.0 else None
    )
    attributed_call_share = (
        round(attributed_calls / total_calls, _SHARE_PLACES) if total_calls != 0 else None
    )

    return SpendAttribution(
        period=period,
        outcomes=outcomes,
        attributed_usd=attributed_usd,
        attributed_calls=attributed_calls,
        unattributed_usd=unattributed_usd,
        unattributed_calls=unattributed_calls,
        total_usd=total_usd,
        total_calls=total_calls,
        attributed_share=attributed_share,
        attributed_call_share=attributed_call_share,
        calls_missing_cost=calls_missing_cost,
    )


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive — parity guard)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _provenance_value(payload: dict[str, Any], key: str) -> str | None:
    """Read a provenance string value, accepting BOTH shapes ``ne.schema`` allows.

    Provenance values are typed ``str | ProvenanceEntry``; a ``ProvenanceEntry``
    round-trips through JSON as ``{"key": ..., "value": ...}``. Reimplemented
    locally (not imported) — ``ne.audit.replay._provenance_context_ids`` is a
    private helper of another package and must not be reached into.
    """
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    raw = provenance.get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def collect_utilization(
    audit_engine: sa.Engine,
    period: ReportPeriod,
    *,
    turn_actions: Sequence[str] = DEFAULT_TURN_ACTIONS,
    tool_actions: Sequence[str] = DEFAULT_TOOL_ACTIONS,
) -> UtilizationMetrics:
    """Collect usage metrics over ``period`` from the real audit chain.

    ``ne.audit.replay`` is payload-authoritative — every value returned derives
    from the hash-covered payload, so a tampered denormalized column can never
    surface forged usage figures.
    """
    result = replay(audit_engine, start=period.start, end=period.end_inclusive_micro)
    events = result.events

    turn_set = frozenset(turn_actions)
    tool_set = frozenset(tool_actions)

    agent_turns = sum(1 for r in events if r.action in turn_set and r.outcome == "success")
    tool_calls = sum(1 for r in events if r.action in tool_set)
    active_users = len({r.actor for r in events if r.actor.startswith("user:")})

    session_ids: set[str] = set()
    saw_session_key = False
    for r in events:
        session_id = _provenance_value(r.payload, _SESSION_ID_KEY)
        if session_id is not None:
            saw_session_key = True
            session_ids.add(session_id)
    sessions = len(session_ids) if saw_session_key else None

    return UtilizationMetrics(
        agent_turns=agent_turns,
        tool_calls=tool_calls,
        active_users=active_users,
        sessions=sessions,
        events_total=len(events),
    )


def collect_proficiency(
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    period: ReportPeriod,
    *,
    agent_turns: int,
    page_size: int = SIGNAL_PAGE_SIZE,
) -> ProficiencyMetrics:
    """Collect quality metrics over ``period`` from the real eval + signal stores.

    ``EvalRunStore.list_runs`` has no time-window filter (documented gap): this
    scans up to :data:`EVAL_SCAN_LIMIT` newest runs and filters in-memory rather
    than modifying ne-eval. ``read_signals`` IS a cursor-paginated public API
    (NE-064) — this loops it to exhaustion rather than assuming one page covers
    the whole window.
    """
    all_runs = eval_store.list_runs(limit=EVAL_SCAN_LIMIT)
    windowed_runs = [
        r for r in all_runs if period.start <= _as_utc(r.created_at) < period.end_exclusive
    ]
    eval_runs = len(windowed_runs)
    passed = sum(1 for r in windowed_runs if r.passed)
    eval_pass_rate = (passed / eval_runs) if eval_runs > 0 else None

    positive_signals = 0
    negative_signals = 0
    correction_signals_total = 0
    after_id = 0
    while True:
        batch = signal_store.read_signals(
            after_id=after_id,
            limit=page_size,
            since=period.start,
            until=period.end_inclusive_micro,
        )
        for signal in batch:
            correction_signals_total += 1
            if signal.signal_kind in NEGATIVE_KINDS:
                negative_signals += 1
            elif signal.signal_kind in POSITIVE_KINDS:
                positive_signals += 1
        if len(batch) < page_size:
            break
        after_id = batch[-1].id

    negative_signal_rate = (negative_signals / agent_turns) if agent_turns > 0 else None

    return ProficiencyMetrics(
        eval_runs=eval_runs,
        eval_pass_rate=eval_pass_rate,
        correction_signals_total=correction_signals_total,
        positive_signals=positive_signals,
        negative_signals=negative_signals,
        negative_signal_rate=negative_signal_rate,
    )


def compute_business_value(
    utilization: UtilizationMetrics,
    proficiency: ProficiencyMetrics,
    *,
    minutes_saved_per_turn: float,
    floor_discount: float,
) -> BusinessValueMetrics:
    """Pure computation of the conservative hours-saved FLOOR.

    See ``ne.obs.report.METHODOLOGY`` for the full, customer-facing formula
    statement — this is its literal implementation: successful assisted turns
    minus negative correction signals (floored at zero), times a per-turn
    minutes-saved assumption, times a conservatism discount, divided by 60.
    """
    assisted_turns = utilization.agent_turns
    discounted_turns = max(0, assisted_turns - proficiency.negative_signals)
    hours_saved_floor = round(discounted_turns * minutes_saved_per_turn * floor_discount / 60.0, 2)
    return BusinessValueMetrics(
        assisted_turns=assisted_turns,
        discounted_turns=discounted_turns,
        minutes_saved_per_turn=minutes_saved_per_turn,
        floor_discount=floor_discount,
        hours_saved_floor=hours_saved_floor,
    )


def collect_window(
    *,
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    period: ReportPeriod,
    turn_actions: Sequence[str] = DEFAULT_TURN_ACTIONS,
    tool_actions: Sequence[str] = DEFAULT_TOOL_ACTIONS,
    minutes_saved_per_turn: float,
    floor_discount: float,
) -> WindowMetrics:
    """Collect the full U-P-B metric set for one :class:`ReportPeriod`."""
    utilization = collect_utilization(
        audit_engine, period, turn_actions=turn_actions, tool_actions=tool_actions
    )
    proficiency = collect_proficiency(
        eval_store, signal_store, period, agent_turns=utilization.agent_turns
    )
    business_value = compute_business_value(
        utilization,
        proficiency,
        minutes_saved_per_turn=minutes_saved_per_turn,
        floor_discount=floor_discount,
    )
    return WindowMetrics(
        period=period,
        utilization=utilization,
        proficiency=proficiency,
        business_value=business_value,
    )
