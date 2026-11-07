# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Server-authoritative, governed per-run model selection (BD-15 Phase B).

This module is deliberately free of HTTP and composition-root imports.  A caller
injects the frozen gateway registry, the existing private serving observer, the
audit read engine, the already-governed PDP/sink, the effective AIR_GAP provider,
and a clock.  The later Ask/workflow routes therefore share one decision service
instead of translating registry or health facts independently.

This module answers two DIFFERENT questions and must never be read as one.

``configured_choices`` answers "what may a caller pick?" purely from the boot
registry plus current posture/routing.  It probes nothing, so it is fast enough
to sit in a page load, and it makes no availability claim whatsoever.

``choices``/``authorize`` answer "what is actually alive right now?", and there
availability is measured, never inferred from configuration:

* private models require a current successful ``ServingStatusService`` row;
* frontier models require a recent successful actual-provider observation from
  the verified audit chain under this exact process ``config_hash``.  This
  module never invents an egress path of its own; when — and ONLY when — a
  composition root injects ``frontier_warm`` (default ``None``, see
  :mod:`ne.api.frontier_warm`), an AUTHORIZATION path may ask that seam to
  issue one governed completion through the SAME gateway so a deliberately
  staged frontier lane can earn its first evidence instead of deadlocking.
  Display reads (``choices``) never warm;
* missing, stale, future-skewed, malformed or later-failed evidence is absence;
* an audit read/verification fault fails the whole catalog rather than returning
  a partial list that could preserve a previously-green frontier choice.

Authorization is a separate conjunctive gate.  A known requested alias is sent
to the existing PDP as ``read.model.select``/``model:<alias>``; only ``ALLOW``
continues.  The service then re-evaluates the same live availability rule and
appends one names-only ``model.selection`` record with the caller-supplied trace.
An append failure withholds the selection.  Unknown client text is never copied
to the chain: it is represented only as ``model:unregistered``.

Because the picker now offers configured models, ``authorize`` is the ONLY place
the liveness question is settled for a run.  It therefore refuses by name (see
:class:`ModelSelectionUnavailable`) and never substitutes a different model for
the one that was asked for.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import anyio.to_thread
import sqlalchemy as sa

from ne.api.frontier_warm import FrontierWarm
from ne.api.model_routing import refusal_for_selection
from ne.audit import (
    AnchoredChainVerifier,
    AuditRecord,
    replay,
    verified_extension,
)
from ne.audit.store import read_head
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import (
    AuditSink,
    DecisionEffect,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
)
from ne.contracts.tokens import ContextBudget
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.schema import AuditOutcome

__all__ = [
    "FRONTIER_OBSERVATION_MAX_AGE_S",
    "MODEL_OBSERVATION_ACTION",
    "MODEL_SELECTION_ACTION",
    "MODEL_SELECTION_EVIDENCE_ACTION",
    "OBSERVATION_FUTURE_SKEW_S",
    "AuthorizedModelSelection",
    "ModelChoice",
    "ModelChoiceCatalog",
    "ModelSelectionError",
    "ModelSelectionEvidenceError",
    "ModelSelectionPolicyRefused",
    "ModelSelectionService",
    "ModelSelectionUnavailable",
    "UnknownModelSelection",
]

MODEL_SELECTION_ACTION = "read.model.select"
MODEL_SELECTION_EVIDENCE_ACTION = "model.selection"
MODEL_OBSERVATION_ACTION = "ne.gateway.model.observed"
# Kept explicit at this authority boundary so model selection does not import
# the HTTP serving/projection module (which would invert the runtime package
# boundary through the legacy canonical-Ask result adapter).
PRIVATE_OBSERVATION_MAX_AGE_S = 5.0
# ``LiteLLMGateway.__init__`` owns this default service actor and the production
# API composition deliberately does not override it. Gateway tests independently
# pin that its audit events retain this actor even when a principal is present.
_GATEWAY_OBSERVATION_ACTOR = "ne-gateway"
_GATEWAY_OBSERVATION_OPERATIONS = frozenset({"complete", "stream"})

#: An actual frontier call is the only truthful liveness signal available without
#: creating a second, ungoverned provider egress path.  One minute is deliberately
#: short: a cold deployment must be warmed by a governed call under THIS config.
FRONTIER_OBSERVATION_MAX_AGE_S = 60.0
#: How often :meth:`ModelSelectionService.run_frontier_evidence_refresher`
#: re-measures the frontier lane.
#:
#: Deliberately DERIVED as half the currency window rather than written as a
#: literal, so the two can never drift apart.  Refreshing at half the window
#: means a successful measurement always exists INSIDE it, which turns a 60s
#: rule that a slow multi-hop turn could not structurally meet into one that is
#: trivially satisfied — WITHOUT widening the window itself.  The window is the
#: liveness assertion; this is only how often we go and earn it.
FRONTIER_EVIDENCE_REFRESH_INTERVAL_S = FRONTIER_OBSERVATION_MAX_AGE_S / 2
#: The refresher's PROACTIVE currency horizon (NE-SELECT-LATENCY).  On a tick,
#: a frontier record older than this is re-warmed even though it is still
#: inside :data:`FRONTIER_OBSERVATION_MAX_AGE_S` — because it will NOT still be
#: inside it at the NEXT tick, and every admission arriving in between would
#: pay the warm (a real provider call) plus a forced O(chain) re-read INLINE.
#: Warming reactively at the full window is what made the refresher
#: structurally unable to deliver the promise in its own docstring: a record
#: aged 30-60s passed the tick untouched, then expired mid-interval, so a
#: sliver of every cycle admitted turns against stale evidence and paid the
#: warm on the user's turn.  Derived, never a literal, so it cannot drift from
#: the window/interval pair.  This tightens only WHEN the background warm
#: happens; admission and catalog currency are still judged against the full
#: :data:`FRONTIER_OBSERVATION_MAX_AGE_S` window.
#:
#: ⚠️ The horizon alone is NOT the schedule.  "horizon + interval <= window"
#: only keeps evidence current if a tick's own cost is zero — but a tick pays
#: an O(chain) evidence read (V) before its cold filter even runs, and the
#: measured V on the live box (10-27s at 142k rows, growing) is on the order
#: of the interval itself.  With a fixed sleep the age a record reaches
#: between renewals is ``interval + 2V + W``, stale past the window as soon as
#: ``2V + W > interval``.  That is why
#: :meth:`ModelSelectionService.run_frontier_evidence_refresher` anchors its
#: wake-ups to the evidence's OWN timestamps (see
#: :meth:`ModelSelectionService._frontier_refresh_target`) instead of counting
#: a fixed interval from the end of the previous iteration.
FRONTIER_EVIDENCE_CURRENCY_HORIZON_S = (
    FRONTIER_OBSERVATION_MAX_AGE_S - FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
)
#: At an anchored wake-up (see :meth:`ModelSelectionService.
#: _frontier_refresh_target`) the oldest record's age is EXACTLY the refresh
#: interval, and the cold filter treats ``age == horizon`` as still current
#: (``_is_current`` compares ``<=``).  Waking this much later makes the
#: filter's verdict strict, so the anchored tick provably re-warms the record
#: it woke up for instead of skipping it on the boundary.
_FRONTIER_REFRESH_ANCHOR_SLACK_S = 0.05
#: The honest answers to "is frontier evidence already earned?" —
#: :meth:`ModelSelectionService.frontier_evidence_readiness`.  An ops/deploy
#: readiness signal, never an evidence source.  ``unknown`` is the fourth,
#: deliberately distinct answer: "could not determine" must never render as
#: "there is nothing to wait for".
FrontierReadiness = Literal["not-applicable", "unknown", "warming", "ready"]
#: How long ONE read of the frontier evidence rows may be REUSED before the
#: audit chain is replayed and verified again (NE-SELECT-LATENCY).
#:
#: This is a cache of the READ, never of the VERDICT.  Currency is still decided
#: by :func:`_frontier_record_is_current`, which compares ``record.occurred_at``
#: against ``now`` on every use — so a cached row that has aged past
#: ``FRONTIER_OBSERVATION_MAX_AGE_S`` is judged stale exactly as if it had just
#: been read.  Caching therefore CANNOT extend a lane's liveness claim by one
#: millisecond; it only stops re-paying an O(chain) replay + full-chain verify
#: (documented as "multi-second" on :meth:`configured_choices`) for evidence
#: that provably has not changed.  The read is invalidated the instant a warm
#: appends a new row, so the post-warm re-read always sees it — and "provably"
#: is enforced, not assumed: every reuse first proves the chain still extends
#: the read's verified baseline (:func:`ne.audit.verified_extension`) and
#: FOLDS the proven newer observation rows into the snapshot, so a row
#: appended by ANY other writer (another replica's warm, normal frontier
#: traffic — error or success) is reflected in the verdict WITHOUT re-paying
#: the O(chain) replay+verify a bare supersession probe used to force.
#:
#: Derived from the WINDOW, not the refresh interval, because the reuse
#: window must cover the refresher's whole OBSERVATION PERIOD.  A cache hit
#: never renews ``read_at``; only the forced post-warm re-read refills, and
#: with the expiry-anchored schedule those refills land ``interval + anchor
#: slack + W`` apart (W is the provider warm's cost — ~4.4s when this
#: derivation was written on 2026-09-02; 3.33s on the current measurement, see
#: the reconciliation below).
#: A TTL equal to the interval (the previous derivation; before that it was
#: 5.0s, which left ~25s of every tick uncovered) therefore expired ``W +
#: slack`` seconds BEFORE each refill, and the ~13% of admissions landing in
#: that tail paid the full inline O(chain) replay+verify this cache exists to
#: keep off the user's turn.
#:
#: The window is the largest reuse that can ever matter: every row a read can
#: hold has ``occurred_at`` at or before the read, so by the time a read is
#: window-old each of its rows has aged out of currency anyway and reuse past
#: that point is vacuous.
#:
#: ⚠️ Coverage is CONDITIONAL, and the read cost V is part of the condition,
#: not just the warm cost W: the cache is refilled only by the forced
#: post-warm re-read, which completes ``V`` seconds AFTER the new record's
#: ``occurred_at``, so by the next refill the process's view of the evidence
#: has aged to ``V + cycle``, where ``cycle = max(interval + slack, V) + W``.
#: The uncovered fraction of each cycle is
#: ``max(0, V + cycle - window) / cycle`` — zero iff
#: ``V + W <= window - interval - slack`` (29.95s here).  Measured 2026-09-02,
#: at that date's W, matching the formula to within ~0.2 points:
#: (W=4.4, V=2) → 0.0%; (4.4, 27) → 4.1%; (4.4, 35) → 36.4%; (29, 2) → 1.7%;
#: (45, 2) → 22.7%.  The W=29 and W=45 rows are deliberately synthetic — the
#: table exercises the FORMULA over a range, it does not claim any of those
#: five pairs is today's operating point.  The real cure was always reducing V
#: itself — incremental verification from the last verified head — not a longer
#: TTL, and BOTH paths now have it.  The REUSE path has
#: had it since PR #958: :func:`ne.audit.verified_extension` proves and folds
#: the chain's extension into the snapshot at O(extension).  The REFILL path
#: — the cache MISS, which was the remaining O(chain) payer and measured
#: 9.77s at 203,343 rows, growing ~1.0s/day — now verifies through
#: :class:`ne.audit.AnchoredChainVerifier` as well
#: (:meth:`ModelSelectionService._frontier_records`), so V is O(rows appended
#: since the last proof) on BOTH paths and this formula's uncovered fraction
#: collapses to ~0 for any V that stays small.
#:
#: ⏳ V IS STILL GROWING, AND THIS IS A RECORD, NOT A FIX.  Re-measured
#: 2026-09-09: V = 6.68s at 340,371 chain rows, rising ~0.5s/day.  The anchor
#: bounds what is VERIFIED, not what is READ — the two
#: ``replay(self._audit_engine, resource=model.litellm_model)`` scans in
#: :meth:`ModelSelectionService._frontier_records` still read EVERY row for
#: that resource — while the evidence refresher appends ~7,800 rows/day/model.
#: With W at the warm's measured p50 (3.33s) that is V + W ~= 10.0s against the
#: 29.95s budget, so today's uncovered fraction is 0.0%; at the current slope
#: ((29.95 - 10.01) / 0.5) the budget is reached in ~40 days.  Bounding those
#: replays by the anchor is the fix.  It is deliberately NOT made here.
#:
#: ⚖️ THE TWO FIGURES FOR W ABOVE ARE ONE QUANTITY MEASURED TWICE, AND 3.33s IS
#: THE CURRENT ONE.  ~4.4s was taken 2026-09-02 with PR #958 and is kept because
#: it dates the worked table above and because the coverage simulation in
#: ``packages/ne-api/tests/test_frontier_warm.py`` is still pinned to it; 3.33s
#: is the 2026-09-09 re-measurement — p50 over n=676 governed warms in 6h, p95
#: 3.878s — and it is the value this paragraph's arithmetic uses.  The 16 → 256
#: warm-ceiling change in this train does NOT explain the gap: the ceiling sweep
#: recorded on :data:`ne.api.frontier_warm.WARM_MAX_TOKENS` found wall clock
#: FLAT at 3.118-3.541s across a 128x ceiling range, and the 3.33s p50 was
#: itself taken at the OLD ceiling of 16.  So the difference is provider
#: round-trip variance between a coarse early figure and a large-sample p50, not
#: an effect of anything changed here.  Nothing downstream flips on the choice:
#: at today's V = 6.68s both give a 0.0% uncovered fraction, and the horizon is
#: ~40 days at W=3.33 against ~38 days at W=4.4 — the older, larger W is the
#: conservative one, which is why the simulation keeps it.
#:
#: A longer reuse window defers only the READ, never the verdict — currency is
#: judged per-use against ``occurred_at`` (see above) — and a composition
#: without the refresher merely reuses display reads a little longer.  What a
#: read VERIFIES is no longer "the full chain" on every call: it is the
#: anchored proof described on :meth:`ModelSelectionService._frontier_records`,
#: with full-chain integrity re-established on
#: :data:`ne.audit.CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S` by
#: :meth:`ModelSelectionService.run_chain_anchor_refresher` and ENFORCED by
#: the verifier refusing an anchor older than
#: :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S`.
FRONTIER_RECORDS_CACHE_TTL_S = FRONTIER_OBSERVATION_MAX_AGE_S

_LOG = logging.getLogger(__name__)

#: Cross-process clocks may differ slightly, but a timestamp materially in the
#: future is not evidence about the present.  Applies identically to both lanes.
OBSERVATION_FUTURE_SKEW_S = 5.0

SelectionEvidence = Literal["private-serving-probe", "frontier-provider-attempt"]


class ServingStatusRead(Protocol):
    """The existing private-lane observation seam, structurally injected."""

    async def observe(
        self,
        models: Sequence[ModelDef],
        *,
        force_refresh: bool = False,
    ) -> Sequence[ServingObservation]: ...


class ServingObservation(Protocol):
    """Only the authoritative fields consumed from ``LaneServingView``.

    Keeping this local structural view avoids making the pure service depend on
    the API wire module that will later expose it.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def lane(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def checked_at(self) -> datetime | None: ...

    @property
    def serves_configured_model(self) -> bool | None: ...


@dataclass(frozen=True)
class ModelChoice:
    """One boot-registered model offered for a task.

    ``evidence``/``observed_at`` are set ONLY by the measured-availability
    catalog (:meth:`ModelSelectionService.choices`).  The configuration listing
    (:meth:`ModelSelectionService.configured_choices`) leaves both ``None``,
    because it makes no liveness claim and must not be able to imply one.
    """

    model: str
    provider: str
    lane: Lane
    evidence: SelectionEvidence | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class ModelChoiceCatalog:
    """The available model choices for one boot-routed gateway task."""

    task: str
    choices: tuple[ModelChoice, ...]
    effective_default: str | None = None


@dataclass(frozen=True)
class AuthorizedModelSelection:
    """The exact server-resolved alias/lane pair a run may carry."""

    model: str
    lane: Lane
    observed_at: datetime
    policy_effect: str
    policy_version: str


class ModelSelectionError(Exception):
    """Base class later HTTP composition maps without parsing prose."""

    #: A STATIC, non-customer explanation a run TERMINAL may carry verbatim.
    #:
    #: Opt-in by construction: only text a raiser explicitly marks safe is ever
    #: propagated, so no exception message can leak customer content into a
    #: terminal or an audit row.  ``None`` (the default, and every exception
    #: that predates this) keeps the terminal exactly as opaque as before.
    safe_reason: str | None = None


class UnknownModelSelection(ModelSelectionError):
    """The requested value is not a boot-registry alias."""


class ModelSelectionUnavailable(ModelSelectionError):
    """A known alias is incompatible with the task or lacks current evidence.

    ``model`` carries the SERVER-registered alias whenever one was resolved, so
    the HTTP edge can name what failed instead of refusing anonymously.  It is
    never client-supplied text: unregistered input raises
    :class:`UnknownModelSelection`, which stays deliberately unnamed.
    """

    def __init__(
        self, *args: object, model: str | None = None, safe_reason: str | None = None
    ) -> None:
        super().__init__(*args)
        self.model = model
        self.safe_reason = safe_reason


class ModelSelectionPolicyRefused(ModelSelectionError):
    """The PDP or effective AIR_GAP posture refused a known choice."""


class ModelSelectionEvidenceError(ModelSelectionError):
    """The authority could not establish a truthful availability catalog."""


def _aware_utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _is_current(value: datetime | None, *, now: datetime, max_age_s: float) -> bool:
    if value is None:
        return False
    observed = _aware_utc(value)
    current = _aware_utc(now)
    if observed is None or current is None:
        return False
    age_s = (current - observed).total_seconds()
    return -OBSERVATION_FUTURE_SKEW_S <= age_s <= max_age_s


def _frontier_record_is_current(
    record: AuditRecord | None,
    model: ModelDef,
    *,
    now: datetime,
    max_age_s: float = FRONTIER_OBSERVATION_MAX_AGE_S,
) -> bool:
    """THE frontier-evidence rule, in one place.

    Extracted verbatim from :meth:`ModelSelectionService._frontier_choices` so
    the warm path and the catalog path cannot drift into two different notions
    of "this lane is live" — a warmer that used a laxer rule would warm a lane
    the catalog then refused, and a stricter one would warm forever.

    ``max_age_s`` may only ever be TIGHTENED below the default, and only by the
    background refresher's cold-model filter
    (:data:`FRONTIER_EVIDENCE_CURRENCY_HORIZON_S`): a proactive warm of a
    record that would expire before the next tick.  The catalog verdict —
    :meth:`ModelSelectionService._frontier_choices` — always uses the default
    full window, so a tighter warm horizon can never darken a lane.
    """
    return (
        record is not None
        and record.outcome == AuditOutcome.success.value
        and _provenance_str(record, "lane") == model.lane.value
        and _is_current(record.occurred_at, now=now, max_age_s=max_age_s)
    )


def _provenance_str(record: AuditRecord, key: str) -> str | None:
    provenance = record.payload.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    raw = provenance.get(key)
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    return raw if isinstance(raw, str) else None


class ModelSelectionService:
    """One governed availability + authorization mechanism for Ask/workflows."""

    def __init__(
        self,
        *,
        gateway_settings: GatewaySettings,
        serving_status: ServingStatusRead,
        audit_engine: sa.Engine,
        pdp: PolicyDecisionPoint,
        sink: AuditSink,
        config_hash: str,
        air_gap_provider: Callable[[], bool],
        lifecycle_availability: Callable[[str], tuple[bool, str | None]] | None = None,
        route_provider: Callable[[], Mapping[str, TaskRoute]] | None = None,
        frontier_warm: FrontierWarm | None = None,
        chain_verifier: AnchoredChainVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = gateway_settings
        self._serving = serving_status
        self._audit_engine = audit_engine
        self._pdp = pdp
        self._sink = sink
        self._config_hash = config_hash
        self._air_gap_provider = air_gap_provider
        self._clock = clock
        self._lifecycle_availability = lifecycle_availability
        self._route_provider = route_provider
        #: ``None`` (the default, and every composition that has not staged a
        #: frontier lane) ⇒ this service issues no provider call of any kind.
        self._frontier_warm = frontier_warm
        #: THE process's chain-integrity trust root
        #: (:class:`ne.audit.AnchoredChainVerifier`).  Every evidence read's
        #: verification goes through it: on the request path it proves the
        #: chain still extends a head a full ``verify_chain`` blessed
        #: (O(extension)), and :meth:`run_chain_anchor_refresher` re-establishes
        #: that blessing in the background (O(chain)).  Injectable so a test can
        #: pin the clock or the age bound; the default is one verifier over this
        #: service's own audit engine.  Constructing a SECOND verifier over the
        #: same engine would carry a second, independently aged anchor and a
        #: second full verify for no gain — hence one, held here.
        self._chain_verifier = chain_verifier or AnchoredChainVerifier(audit_engine)
        self._by_name = {model.name: model for model in gateway_settings.models}
        #: ``{model-name-set: (monotonic_read_at, baseline_head_seq,
        #: baseline_head_hash, records)}``
        #: — recent frontier evidence reads, KEYED BY THE MODEL SET each read
        #: was taken for.  A read for ``[frontier-default]`` holds no verdict
        #: about ``frontier-best``, so returning it verbatim to a caller asking
        #: about ``[frontier-default, frontier-best]`` (as an unkeyed cache
        #: did) reported the second model dark for a whole TTL even though its
        #: evidence sat on the chain.  The converse IS sound and is used: a
        #: fresh read for a SUPERSET already contains every row a subset
        #: caller needs (see :meth:`_read_frontier_records`).  Distinct keys
        #: are bounded by the boot registry's frontier subsets, so this cannot
        #: grow unboundedly.  ``baseline_head_seq``/``baseline_head_hash``
        #: are the VERIFIED chain head this snapshot is current as of — read
        #: BEFORE the read's replays began, then advanced on every reuse by
        #: the incremental extension proof (:func:`ne.audit.verified_extension`)
        #: that folds later observation rows (another process's warm, or
        #: normal frontier traffic — error OR success) into the snapshot, so
        #: a cached SUCCESS is never reused after a later row supersedes it
        #: AND ordinary foreign traffic never forces the O(chain)
        #: replay+verify back onto an admission.
        #: ``_frontier_cache_lock`` guards the DICTIONARY only — lookup and
        #: whole-entry replacement — never a chain read: both the extension
        #: proof and the full replay+verify run outside it, so one caller's
        #: multi-second miss cannot queue a concurrent caller's ~ms cache hit
        #: behind it (see :meth:`_read_frontier_records` for why the
        #: resulting races are benign).
        #: Never populated (and the chain never read) in a composition without
        #: frontier models in play.
        self._frontier_cache: dict[
            frozenset[str], tuple[float, int, str, dict[str, AuditRecord]]
        ] = {}
        self._frontier_cache_lock = asyncio.Lock()
        #: Newest ``occurred_at`` ever seen per frontier model, across ALL
        #: reads.  This is bookkeeping for the refresher's expiry-anchored
        #: schedule (:meth:`_frontier_refresh_target`) — never an evidence
        #: source: every catalog verdict still comes from an actual chain read.
        self._frontier_observed_at: dict[str, datetime] = {}

    async def refresh_frontier_evidence(
        self,
        task: str,
        *,
        currency_horizon_s: float = FRONTIER_EVIDENCE_CURRENCY_HORIZON_S,
    ) -> bool:
        """Re-measure frontier liveness OFF the critical path (NE-SELECT-LATENCY).

        A composition root may call this on a timer (interval <
        :data:`FRONTIER_OBSERVATION_MAX_AGE_S`) so that an admitted turn finds
        CURRENT evidence already on the chain and never pays a warm — or an
        O(chain) replay + full-chain verify — between its own hops.

        ``currency_horizon_s`` makes the refresh PROACTIVE: a record older than
        this is re-warmed even while still inside the full window, because a
        record that merely satisfies the window NOW may not satisfy it for the
        turns admitted before the caller's next tick.  The default is
        :data:`FRONTIER_EVIDENCE_CURRENCY_HORIZON_S` (window minus interval),
        which is exactly the age a record must not exceed at one tick if it is
        to still satisfy the window at the next.  This changes WHEN the
        governed warm happens, never whether currency is required: admission
        still judges evidence against the full window.

        This is the SAME governed warm :meth:`authorize` would have issued; it
        is merely issued earlier, by a background caller, instead of inside a
        user's turn.  It weakens no rule: the 60s currency window, the lane
        pinning, the egress guard and the audit append are all unchanged.

        Returns ``True`` when the frontier lane holds current evidence after the
        refresh.  Never raises for an unavailable lane — a background refresher
        must not take down its caller — but DOES propagate a malformed/invalid
        audit chain, which is a governance fault rather than a liveness fact.
        """
        if self._frontier_warm is None:
            return False
        # Off-thread for the same reason as the loop's route read above: the
        # posture register is a bounded synchronous row read on store-backed
        # compositions, and this method runs on the refresher's (cold-boot)
        # path. The admission/display paths keep their existing inline reads —
        # pre-existing behavior this change does not touch.
        air_gap = await anyio.to_thread.run_sync(self._effective_air_gap)
        try:
            catalog = await self._choices(
                task,
                air_gap=air_gap,
                warm_frontier=True,
                currency_horizon_s=currency_horizon_s,
            )
        except ModelSelectionEvidenceError:
            raise
        except Exception:
            return False
        return any(choice.lane is Lane.FRONTIER_API for choice in catalog.choices)

    def _frontier_refresh_task(self) -> str | None:
        """One task whose route primary is on the frontier lane, or ``None``.

        Warming is per-MODEL, not per-task: ``_choices`` warms every cold
        frontier model it finds, and :func:`_frontier_record_is_current` never
        looks at the task.  So ONE frontier-routed task refreshes the evidence
        for all of them, and picking a second would only double the outbound
        provider calls.  Sorted, so the choice is deterministic across boots.

        Reads the route register ONCE and checks primaries against the
        in-memory model map — the earlier shape re-read the register per task
        through a per-task helper, 1+N bounded synchronous row reads per pass
        for no verdict difference.
        """
        try:
            routes = self._routes()
        except Exception:
            return None
        for task in sorted(routes):
            primary = self._by_name.get(routes[task].model)
            if primary is not None and primary.lane is Lane.FRONTIER_API:
                return task
        return None

    def _frontier_refresh_target(self) -> datetime | None:
        """When the next background warm is DUE, from the evidence itself.

        The wake-up is anchored to ``occurred_at`` of the oldest still-current
        frontier record this process has read — NOT counted as a fixed interval
        from the end of the previous loop iteration.  The difference is the
        whole fix (NE-SELECT-LATENCY, round 2): an iteration's own cost — one
        O(chain) evidence read (V, measured 10-27s at 142k rows and growing)
        plus the provider warm (W) plus the forced post-warm re-read (V again)
        — used to be silently ADDED to the sleep, so consecutive warms landed
        ``interval + 2V + W`` apart and evidence went stale past the 60s window
        whenever ``2V + W > interval`` (V ≳ 12.8s, which the live box exceeds).

        Anchored at ``occurred_at + interval`` the next warm begins when the
        evidence is exactly ``interval`` old, so consecutive observations land
        ``interval + W`` apart however slow the reads are (the pre-warm read is
        a cache hit at that moment, because the cache TTL covers the whole
        observation period and the previous iteration's forced re-read
        refilled it ``V`` seconds after ``occurred_at``).  Evidence currency
        is therefore independent of
        V for any ``V <= window - interval``; past that the anchor has already
        passed when the iteration ends and the loop runs back-to-back cycles,
        which keeps observations ``V + W`` apart — still inside the window for
        any ``V + W <= window``.  Beyond THAT (a single read outlasting the
        entire window minus the warm) no serial scheduler can keep evidence
        current without weakening the window itself, and behaviour degrades to
        the pre-refresher floor: one inline re-probe per admission.

        ``None`` means this process has never read current frontier evidence —
        the caller falls back to the base interval.  Records already past the
        full window cannot anchor anything (they cannot be kept current, only
        re-earned) and are ignored, which also keeps a permanently-dark model
        from pinning the schedule to the past and spinning the loop.
        """
        now = self._clock()
        current = [
            observed
            for observed in self._frontier_observed_at.values()
            if _is_current(observed, now=now, max_age_s=FRONTIER_OBSERVATION_MAX_AGE_S)
        ]
        if not current:
            return None
        return min(current) + timedelta(
            seconds=FRONTIER_EVIDENCE_REFRESH_INTERVAL_S + _FRONTIER_REFRESH_ANCHOR_SLACK_S
        )

    def _frontier_retry_delay_s(self) -> float:
        """Seconds until the oldest still-current observation exits the window.

        Pacing for a refresh that earned no new evidence (a warm refused
        before transport appends no row, so the anchor never moves): the
        retry is due when the evidence the schedule is anchored to actually
        EXPIRES — the interval the evidence's own expiry implies — not one
        blind base interval later, which overshoots the expiry and (via the
        no-anchor branch's second sleep) lands the retry a whole interval
        after currency was lost.  The anchor slack puts the wake just past
        the expiry, for the same reason the refresh target carries it; the
        base interval bounds the sleep exactly as the anchored branch is
        bounded, so a dead lane still cannot spin the provider.  Falls back
        to the base interval when nothing is current to anchor a retry.
        """
        now = self._clock()
        expiries = [
            observed + timedelta(seconds=FRONTIER_OBSERVATION_MAX_AGE_S)
            for observed in self._frontier_observed_at.values()
            if _is_current(observed, now=now, max_age_s=FRONTIER_OBSERVATION_MAX_AGE_S)
        ]
        if not expiries:
            return FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
        remaining_s = (min(expiries) - now).total_seconds() + _FRONTIER_REFRESH_ANCHOR_SLACK_S
        return min(max(remaining_s, 0.0), FRONTIER_EVIDENCE_REFRESH_INTERVAL_S)

    async def run_frontier_evidence_refresher(self) -> None:
        """Keep frontier evidence inside its window, OFF every user turn.

        This is an OPTIMISATION and never a source of authority.  It issues the
        same governed warm :meth:`authorize` would have issued, just earlier and
        from a background caller, so it can only ever produce REAL measurements.
        It cannot assert liveness it did not measure, and it relaxes nothing:
        :data:`FRONTIER_OBSERVATION_MAX_AGE_S` is untouched.

        Scheduling is EXPIRY-ANCHORED (see :meth:`_frontier_refresh_target`):
        each wake-up is computed from the evidence's own ``occurred_at``, so
        the multi-second chain reads an iteration pays are absorbed by the
        schedule instead of being silently appended to it.

        The FIRST pass is EAGER: on a cold process no observation exists to
        anchor a schedule, and treating that like a paced retry (one base
        interval of sleep before the first warm) is what made every admission
        in the first ~interval-plus-read seconds after a restart pay the
        inline warm and the full O(chain) read — including the deploy's
        post-recreate acceptance.  The eager pass begins the first governed
        warm as soon as this coroutine first runs.  What that does and does
        not guarantee: evidence becomes current as early as the first
        refresh CAN complete (its own O(chain) read + warm + forced re-read),
        not instantly — an admission that arrives before that refresh lands
        still finds a cold cache and pays its own inline path.  A caller that
        must not race the cold window (the deploy) should gate on
        :meth:`frontier_evidence_readiness` rather than assume boot ⇒ warm.

        It therefore FAILS SAFE by construction.  If this loop dies, stalls, or
        falls behind, evidence simply ages out and behaviour degrades to exactly
        today's: a stale row, ONE inline re-probe in :meth:`authorize`, and then
        a real refusal.  Every iteration's failure is logged and swallowed so a
        transient provider or chain fault cannot end the loop.
        """
        if self._frontier_warm is None:
            return
        retry_due = False
        cold_boot = True
        while True:
            target = self._frontier_refresh_target()
            if target is not None:
                cold_boot = False
                remaining_s = (target - self._clock()).total_seconds()
                if remaining_s > 0:
                    # Capped at the base interval so the anchor is re-read
                    # periodically even under clock skew; re-anchored after
                    # every sleep because an admission's own warm may have
                    # moved the expiry while we slept.
                    await asyncio.sleep(min(remaining_s, FRONTIER_EVIDENCE_REFRESH_INTERVAL_S))
                    retry_due = False
                    continue
            elif cold_boot:
                # COLD START (the #958 residual, and what failed every deploy's
                # post-recreate acceptance): ``_frontier_observed_at`` is empty
                # on the first pass of a fresh process, so ``target`` is
                # ``None`` — but that is "never measured", not "recently
                # retried".  Sleeping the base interval here parked the first
                # proactive warm a whole interval after boot, so every
                # admission inside that window (the deploy's acceptance, the
                # owner's first request after a restart) paid the inline warm
                # plus the full O(chain) read.  Warm NOW instead.  The zero
                # sleep still SUSPENDS — the structural invariant
                # (``test_every_pass_through_the_refresher_loop_suspends``)
                # is that every path through this body yields the event loop,
                # and ``sleep(0)`` is a real yield.  This branch runs at most
                # once: ``cold_boot`` clears below, so a first warm that earns
                # nothing (dead lane) falls to the paced branches and can
                # never spin the provider.
                await asyncio.sleep(0)
            elif not retry_due:
                # ``retry_due`` marks a pass whose pacing sleep already
                # happened: the previous iteration's refresh earned no new
                # evidence and slept until the anchoring row's OWN expiry
                # (see below).  The anchor has now aged out, so ``target``
                # is ``None`` — but adding the blind base-interval sleep
                # here on top would land the retry a whole interval late
                # (t≈90s instead of t≈60s for a warm refused at t≈30s),
                # reopening the stale window this refresher exists to close.
                await asyncio.sleep(FRONTIER_EVIDENCE_REFRESH_INTERVAL_S)
            cold_boot = False
            retry_due = False
            # Off-thread: the route register is a bounded SYNCHRONOUS row read
            # on store-backed compositions. Inline it would run on the event
            # loop's thread — and the eager pass runs at cold boot, exactly
            # when a contended pool or slow Postgres would turn that read into
            # a loop-wide stall (/healthz included), the same freeze family as
            # the ~29s await-free pass this loop already had once.
            task = await anyio.to_thread.run_sync(self._frontier_refresh_task)
            if task is None:
                # No task currently routes to a frontier primary — the demo's
                # air-gap prerequisite (every frontier-routed task repointed
                # before the Tier-1 engage) parks the loop in exactly this
                # state while catalog reads keep the anchor armed.  EVERY path
                # through this loop body must suspend: when the anchor is
                # already in the past the branch above falls through without
                # sleeping, and reaching ``continue`` without an await here
                # means the coroutine never yields the event loop back — not a
                # busy task, a stalled API — until the evidence ages out.
                # Pace at the base interval, as the pre-anchor loop always did.
                await asyncio.sleep(FRONTIER_EVIDENCE_REFRESH_INTERVAL_S)
                continue
            try:
                await self.refresh_frontier_evidence(
                    task, currency_horizon_s=FRONTIER_EVIDENCE_CURRENCY_HORIZON_S
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never fatal: the next authorize re-probes and, if the lane is
                # genuinely down, refuses by name exactly as it does today.
                _LOG.warning(
                    "frontier evidence refresh failed; the lane will be "
                    "re-probed on the next authorize",
                    exc_info=True,
                )
            if target is not None and self._frontier_refresh_target() == target:
                # The refresh earned no new evidence (dead lane, warm refused
                # before transport — nothing appended, so the anchor never
                # moved): retry on the interval the evidence's OWN expiry
                # implies, never a blind base interval.  A full-interval sleep
                # here overshot the window — for a warm refused at t≈30s the
                # anchor row was ~60.05s old on wake, ``_frontier_refresh_
                # target()`` returned ``None``, and the no-anchor branch slept
                # ANOTHER interval, landing the retry at t≈90s instead of
                # t≈60s and reopening ~30s in which admissions pay the inline
                # warm and the full-chain read.  The sleep is still bounded by
                # the base interval, so a dead lane cannot spin the provider;
                # ``retry_due`` hands the pacing already paid here to the next
                # pass.
                await asyncio.sleep(self._frontier_retry_delay_s())
                retry_due = True

    def frontier_evidence_readiness(self) -> FrontierReadiness:
        """Would an admission RIGHT NOW find frontier evidence already earned?

        An OPS SIGNAL — the observable the deploy's acceptance gate polls
        before it spends its one canonical run admission — and NEVER an
        evidence source: every admission still judges the real, chain-verified
        records against :data:`FRONTIER_OBSERVATION_MAX_AGE_S` exactly as
        before.  This method issues no provider call and no audit-chain
        read/replay.  It is NOT free of I/O: deciding "is any task routed at a
        frontier primary?" and "is the air gap engaged?" each read their
        governed register (one bounded synchronous row read apiece on the
        store-backed compositions), so the HTTP route runs this method off the
        event-loop thread (``anyio.to_thread.run_sync``) — polling it cannot
        stall the loop, and it takes no lock of this service's.

        * ``"not-applicable"`` — no state exists for an admission to wait on:
          no warmer is composed, the effective air gap is engaged (frontier is
          refused by posture, never warmed), no frontier model is registered,
          or no task routes to a frontier primary (the background refresher
          never runs a warm in that composition, so waiting cannot change the
          answer).
        * ``"unknown"`` — the answer could not be DETERMINED (the air-gap or
          route provider raised or returned garbage).  Deliberately distinct
          from ``"not-applicable"``: "I could not read the register" must
          never render as "there is nothing to wait for", or a database fault
          green-lights the gate instantly.  A caller treats it as not-ready
          and keeps polling inside its own bound.  Residual, stated honestly:
          a store-backed provider that FAILS CLOSED without raising (the
          egress provider returns the most-restrictive posture on a store
          fault; the route provider returns the boot routes) is
          indistinguishable from a real read here, so those faults surface as
          ``"not-applicable"``/``"warming"`` per the fail-closed value rather
          than ``"unknown"``.
        * ``"ready"`` — every registered frontier model has an observation
          this process has read from the chain that is still inside the
          currency window.  An admission at this moment pays no inline warm
          and no full chain read (the records cache was primed by the same
          read that noted the observation).
        * ``"warming"`` — otherwise: the first refresh (or a re-earn after
          expiry) has not landed yet.  A cold-cache admission would pay the
          inline warm plus the O(chain) verify — the multi-tens-of-seconds
          stall the caller is gating against.

        A failed warm ATTEMPT is audited as an ``observed[error]`` row and
        advances the observation like any measurement, so a genuinely dark
        lane still reaches ``"ready"`` — and the admission then refuses BY
        NAME, fast, instead of timing out.  Only a warm refused before
        transport (nothing appended) leaves this ``"warming"``; the caller's
        wait must therefore be bounded, after which it proceeds and lets the
        admission tell the truth.
        """
        if self._frontier_warm is None:
            return "not-applicable"
        try:
            air_gap = self._air_gap_provider()
        except Exception:
            return "unknown"
        if not isinstance(air_gap, bool):
            return "unknown"
        if air_gap:
            return "not-applicable"
        frontier = [model for model in self._settings.models if model.lane is Lane.FRONTIER_API]
        if not frontier:
            return "not-applicable"
        # One route read, reused for every task — NOT ``_frontier_refresh_task``,
        # which re-reads the register per task AND collapses a read failure
        # into "no frontier task" (the exact masking this signal must refuse).
        try:
            routes = self._routes()
        except Exception:
            return "unknown"

        def _primary_is_frontier(route: TaskRoute) -> bool:
            primary = self._by_name.get(route.model)
            return primary is not None and primary.lane is Lane.FRONTIER_API

        if not any(_primary_is_frontier(route) for route in routes.values()):
            return "not-applicable"
        now = self._clock()
        ready = all(
            _is_current(
                self._frontier_observed_at.get(model.name),
                now=now,
                max_age_s=FRONTIER_OBSERVATION_MAX_AGE_S,
            )
            for model in frontier
        )
        return "ready" if ready else "warming"

    def _routes(self) -> Mapping[str, TaskRoute]:
        if self._route_provider is None:
            return self._settings.tasks
        try:
            routes = self._route_provider()
        except Exception as exc:
            raise ModelSelectionEvidenceError("model route authority is unavailable") from exc
        return routes

    def _effective_air_gap(self) -> bool:
        try:
            value = self._air_gap_provider()
            return value if isinstance(value, bool) else True
        except Exception:
            # Failure has the restrictive meaning: frontier is unavailable.  A
            # private model still needs its own current successful observation.
            return True

    async def configured_choices(
        self, task: str, *, snapshot_tokens: int | None = None
    ) -> ModelChoiceCatalog:
        """List the models CONFIGURED for ``task``, making no liveness claim.

        This is the display catalog behind the console run-model picker.  It
        performs no serving probe and no audit-chain replay/verification, so it
        costs a routing-table read instead of the multi-second evidence gather
        :meth:`choices` requires.

        The governance trade is deliberate and the honesty is RELOCATED, not
        dropped.  This list may name a model that is not currently serving;
        :meth:`authorize`, which every run must pass, still demands current
        evidence and refuses BY NAME when it is absent, so an unavailable pick
        fails loudly at submit rather than disappearing from a dropdown.

        Policy is NOT relaxed here.  AIR_GAP posture and the boot task/budget
        rules still remove a model, because those are configuration facts about
        what may be selected — not measurements of what is alive.  Lifecycle
        readiness IS a measurement and is therefore left to :meth:`authorize`.
        """
        return self.configured_catalog(task, snapshot_tokens=snapshot_tokens)

    def configured_catalog(
        self, task: str, *, snapshot_tokens: int | None = None
    ) -> ModelChoiceCatalog:
        """The SYNCHRONOUS body of :meth:`configured_choices`.

        Split out, not duplicated: this computation never awaited anything (it
        reads the boot settings, the live route register and the posture
        provider — all sync). ⚠️ It is a DISPLAY catalog and must not back a
        budget resolver: it deliberately makes no liveness claim, and the
        NE-CTX-IN-FORCE round-4 finding was exactly a resolver reading THIS
        while the attempt loop read the evidence-filtered :meth:`choices` —
        the two disagree the moment serving/lifecycle evidence removes a
        model. :class:`ne.contracts.models.TurnContextBudget` is async for
        that reason; the Run-path resolver awaits :meth:`choices`.
        """
        if task not in self._settings.tasks:
            return ModelChoiceCatalog(task=task, choices=(), effective_default=None)
        air_gap = self._effective_air_gap()
        choices = tuple(
            ModelChoice(model=model.name, provider=model.provider, lane=model.lane)
            for model in self._settings.models
            if refusal_for_selection(
                self._settings,
                task,
                model.name,
                air_gap=air_gap,
                snapshot_tokens=snapshot_tokens,
            )
            is None
        )
        route = self._routes().get(task)
        if route is None:
            raise ModelSelectionEvidenceError("model route authority is malformed")
        listed = {choice.model for choice in choices}
        effective_default = next(
            (name for name in (route.model, *route.fallbacks) if name in listed),
            None,
        )
        return ModelChoiceCatalog(task=task, choices=choices, effective_default=effective_default)

    async def choices(self, task: str, *, snapshot_tokens: int | None = None) -> ModelChoiceCatalog:
        """Return only models currently evidenced and safe for ``task``."""
        catalog, _air_gap = await self.choices_with_air_gap(task, snapshot_tokens=snapshot_tokens)
        return catalog

    async def choices_with_air_gap(
        self, task: str, *, snapshot_tokens: int | None = None
    ) -> tuple[ModelChoiceCatalog, bool]:
        """Return the catalog together with the EXACT posture it was built from.

        A second caller that must send its own PDP request about these choices
        cannot re-read the posture provider: between the two reads the effective
        AIR_GAP can flip, and the request would then describe a catalog that was
        never computed.  Reading both out of one call is the only way a preview
        can be honest about which posture produced it.
        """
        air_gap = self._effective_air_gap()
        return await self._choices(task, air_gap=air_gap, snapshot_tokens=snapshot_tokens), air_gap

    async def _choices(
        self,
        task: str,
        *,
        air_gap: bool,
        force_refresh_private: bool = False,
        warm_frontier: bool | None = None,
        force_warm: bool = False,
        snapshot_tokens: int | None = None,
        currency_horizon_s: float = FRONTIER_OBSERVATION_MAX_AGE_S,
    ) -> ModelChoiceCatalog:
        """``warm_frontier``: ``True`` forces the governed warm (authorization
        reads), ``False`` suppresses it (``authorize_auto``'s second pass, which
        already warmed), ``None`` — the display default — warms only when this
        task's ROUTE PRIMARY is itself on the frontier lane.

        ``currency_horizon_s`` tightens ONLY the cold-model filter that decides
        which frontier models to warm — the background refresher passes
        :data:`FRONTIER_EVIDENCE_CURRENCY_HORIZON_S` so it warms records that
        would expire before its next tick (proactive, NE-SELECT-LATENCY).  The
        catalog verdict below always judges currency against the full
        :data:`FRONTIER_OBSERVATION_MAX_AGE_S` window regardless."""
        # An unknown task cannot be created by runtime selection and causes no
        # liveness probe or audit scan merely to rediscover that boot fact.
        if task not in self._settings.tasks:
            return ModelChoiceCatalog(task=task, choices=(), effective_default=None)

        eligible = [
            model
            for model in self._settings.models
            if refusal_for_selection(
                self._settings,
                task,
                model.name,
                air_gap=air_gap,
                snapshot_tokens=snapshot_tokens,
            )
            is None
        ]
        if self._lifecycle_availability is not None:
            # Off-thread, ONE hop for the whole list: each availability read
            # is a bounded SYNCHRONOUS lifecycle-state row read (plus a
            # posture read), and this method runs on the refresher's
            # cold-boot path as well as every request path — inline it
            # blocked the event loop for the read's whole duration.
            lifecycle_availability = self._lifecycle_availability
            candidates = eligible

            def _lifecycle_eligible() -> list[ModelDef]:
                return [model for model in candidates if lifecycle_availability(model.name)[0]]

            eligible = await anyio.to_thread.run_sync(_lifecycle_eligible)
        private = [model for model in eligible if model.lane is Lane.PRIVATE_VLLM]
        frontier = [model for model in eligible if model.lane is Lane.FRONTIER_API]

        # ONE register read for this whole call, off the event-loop thread
        # (same reason as above; the store-backed provider is a synchronous
        # row read).  The display-warm decision and the effective-default
        # walk at the end both reuse this snapshot — the earlier shape read
        # the register here AND a second time inside the per-task
        # ``_route_primary_is_frontier`` helper on display reads, and both
        # reads ran inline on the loop thread.  A malformed route authority
        # therefore now surfaces BEFORE any warm or probe is issued, with
        # the same error it always raised.
        routes = await anyio.to_thread.run_sync(self._routes)
        route = routes.get(task)
        if route is None:
            raise ModelSelectionEvidenceError("model route authority is malformed")

        frontier_records: dict[str, AuditRecord] = {}
        if frontier:
            frontier_records = await self._read_frontier_records(frontier)
            # NE-FRONTIER-LIVE: over a deliberately staged frontier lane, issue
            # the one governed call that produces the evidence this rule
            # requires (see ne.api.frontier_warm). Two entry conditions, both
            # narrow:
            #
            #  * `warm_frontier` — an AUTHORIZATION read (authorize /
            #    authorize_auto), i.e. a run is actually being admitted;
            #  * the task's own ROUTE PRIMARY is on the frontier lane — the
            #    display read case. A catalog that cannot offer the primary the
            #    operator ROUTED this task to does not merely omit a row: the
            #    picker then silently substitutes a different model for the
            #    configured route, which is the same silent-degrade defect one
            #    layer up. A task routed at a private primary never warms on
            #    display no matter what else is registered.
            #
            # Never when the warmer is not injected (the default everywhere),
            # and never under AIR_GAP — `eligible` above has already dropped
            # every frontier model in that posture, so `frontier` is empty and
            # this branch cannot be reached. The evidence RULE below is
            # unchanged — newest observation wins.  Note what that means for a
            # FAILED warm, per the two production shapes: refused before
            # transport, nothing is appended and the lane is left exactly as
            # evidenced as it was; but a warm whose provider ATTEMPT fails is
            # audited as a newer `observed[error]` row, which the forced
            # re-read then selects — superseding even a still-in-window
            # success.  Fail-closed and by name, never a silent success
            # against a lane whose newest real measurement is a failure.
            route_primary = self._by_name.get(route.model)
            should_warm = (
                (route_primary is not None and route_primary.lane is Lane.FRONTIER_API)
                if warm_frontier is None
                else warm_frontier
            )
            if should_warm and self._frontier_warm is not None:
                cold = (
                    # NE-SELECT-REPROBE: a forced re-measure warms every
                    # frontier model regardless of what the last read said —
                    # the caller has already seen that read come back without
                    # the row it needs.
                    list(frontier)
                    if force_warm
                    else [
                        model
                        for model in frontier
                        if not _frontier_record_is_current(
                            frontier_records.get(model.name),
                            model,
                            now=self._clock(),
                            max_age_s=currency_horizon_s,
                        )
                    ]
                )
                if cold:
                    await self._frontier_warm(cold, task=task)
                    # force: the warm just appended the row this read must see.
                    frontier_records = await self._read_frontier_records(frontier, force=True)

        # Frontier replay/full-chain verification can be materially slower than
        # a private probe. Acquire it first, then obtain private evidence as the
        # last I/O immediately before the one selection clock. Authorization
        # paths force a real probe; display/preflight reads may use the cache.
        try:
            private_rows = (
                await self._serving.observe(private, force_refresh=force_refresh_private)
                if private
                else ()
            )
        except Exception as exc:
            raise ModelSelectionEvidenceError("private serving evidence is unavailable") from exc

        try:
            now = self._clock()
            if _aware_utc(now) is None:
                raise ModelSelectionEvidenceError("selection clock is not timezone-aware")
            private_choices = self._private_choices(private, private_rows, now=now)
        except ModelSelectionEvidenceError:
            raise
        except Exception as exc:
            raise ModelSelectionEvidenceError("private serving evidence is malformed") from exc
        frontier_choices = self._frontier_choices(frontier, frontier_records, now=now)

        by_name = {choice.model: choice for choice in (*private_choices, *frontier_choices)}
        ordered = tuple(
            by_name[model.name] for model in self._settings.models if model.name in by_name
        )
        available_names = {choice.model for choice in ordered}
        effective_default = next(
            (name for name in (route.model, *route.fallbacks) if name in available_names),
            None,
        )
        return ModelChoiceCatalog(
            task=task,
            choices=ordered,
            effective_default=effective_default,
        )

    async def _read_frontier_records(
        self, frontier: Sequence[ModelDef], *, force: bool = False
    ) -> dict[str, AuditRecord]:
        """Read the newest frontier evidence rows, reusing a recent read.

        A read may be reused only by a caller whose model set the read COVERS:
        a read for ``[frontier-default]`` proves nothing about
        ``frontier-best``, and handing it over anyway (the unkeyed behaviour)
        reported the other model dark for a whole TTL while its evidence sat
        on the chain.  The converse is sound and is used: a fresh read taken
        for ``[frontier-default, frontier-best]`` already contains every row a
        caller asking only about ``[frontier-default]`` needs (each model's
        newest row is computed independently, per resource), so a SUBSET
        caller reuses a covering read instead of paying the inline O(chain)
        replay+verify — the recurring miss otherwise, because every forced
        post-warm re-read clears all entries and refills only its own set.

        Reuse is bounded by TWO conditions, both required:

        * the read is younger than :data:`FRONTIER_RECORDS_CACHE_TTL_S`; and
        * :func:`ne.audit.verified_extension` PROVES the chain still extends
          the read's verified baseline head.  A cached read is a snapshot:
          rows appended after its baseline — another process's warm, normal
          frontier traffic failing, or (the live-demo shape) every turn's
          own SUCCESSFUL frontier calls — supersede it on the chain.  The
          first cut of this bound forced the full O(chain) replay+verify on
          ANY newer observation row, success included, which put the
          multi-second inline read back on MOST admissions of an actively
          used deployment — the very cost this cache exists to remove.
          Instead, the PROVEN extension rows are FOLDED into the snapshot
          (:meth:`_fold_observations`): for each model in the cached entry's
          key, a matching newer observation row — same filter as the full
          read, :meth:`_observation_of` — replaces the cached row by highest
          ``seq``, which equals a fresh full read at the new head.  A newer
          ``observed[error]`` therefore still darkens the lane, and a
          superseded success is never authorized, with no full read at all.

          Integrity is NOT relaxed to buy that.  The extension is
          chain-verified — density, per-row hash recomputation, ``prev_hash``
          linkage anchored at the baseline hash this cache only ever stores
          off a read whose own chain proof passed (normally the ANCHORED
          proof — see :meth:`_frontier_records` — falling back to a full
          ``verify_chain``), column/payload cross-check, and a tail that
          must land exactly on the head — at
          O(extension) cost, bounded by one reuse window of appends (an
          indexed ``seq > baseline`` scan; never a replay, never O(chain)).
          Every folded field derives from the hash-covered payload; no
          denormalized column is trusted for content or even for selection
          (the extension is selected by ``seq`` alone).  An extension that
          cannot be proven falls through to the full verified read below,
          whose ``verify_chain`` names the break.  On success the entry's
          baseline advances to the proven head — but ``read_at`` does NOT,
          so the TTL still bounds every entry's lifetime and a full
          replay+verify still recurs at least once per window.

        The cache lock covers only the dictionary — the covering lookup and
        the atomic whole-entry write — NEVER a chain read.  Holding it across
        the miss's ``replay`` + chain proof (V was 10-27s on the demo box when
        that proof was an unbounded ``verify_chain``; it is now the anchored
        proof, but the miss still reads and the lock still must not cover it)
        made every concurrent admission queue for that whole window instead
        of taking its ~ms cache hit.  Unlocked racing is safe because
        entries are immutable tuples replaced whole under the lock: every
        reader sees a self-consistent ``(read_at, baseline_seq,
        baseline_hash, records)`` snapshot whose records match its own
        baseline.  Two callers racing a miss each pay the full verified
        read and the LAST WRITER WINS — either entry is sound because each
        was verified against its own baseline.  A fold writes back only if
        the entry it proved from is still the cached one (identity-checked
        under the lock), so it can neither rewind a baseline a concurrent
        fold or read advanced nor resurrect an entry a failed read's
        ``clear()`` invalidated; a losing fold still RETURNS its records,
        which the extension proof made as authoritative as a full read at
        its head.

        ``force=True`` bypasses the cache and, because it is only ever used
        immediately after a warm appended a row that EVERY cached read
        predates, invalidates all of them before refilling its own entry.
        See :data:`FRONTIER_RECORDS_CACHE_TTL_S` for why reuse cannot extend
        a liveness claim past the window.
        """
        key = frozenset(model.name for model in frontier)
        if not force:
            async with self._frontier_cache_lock:
                covering = self._covering_cache_entry(key)
            if covering is not None:
                cached_key, entry = covering
                read_at, baseline_seq, baseline_hash, cached_records = entry
                try:
                    extension = await anyio.to_thread.run_sync(
                        functools.partial(
                            verified_extension,
                            self._audit_engine,
                            baseline_seq=baseline_seq,
                            baseline_hash=baseline_hash,
                        )
                    )
                except Exception:
                    # A failed proof means "cannot show the read still
                    # stands" — fall through to the fresh, verified read,
                    # whose own failure handling names the fault.
                    extension = None
                if extension is not None:
                    head_seq, head_hash, new_rows = extension
                    if new_rows:
                        cached_records = self._fold_observations(
                            cached_key, cached_records, new_rows
                        )
                        # The folded rows are as authoritative as a full
                        # read's (chain-verified, newest per model), so
                        # the refresher's scheduling anchor advances here
                        # too.  Without this the cold filter sees a young
                        # folded row and skips warming while the scheduler
                        # stays pinned to the OLD timestamp — and once
                        # that anchor expires the loop can wait until the
                        # folded success is itself stale, sending
                        # admissions back through the inline warm plus the
                        # full-chain read.
                        self._note_observed(cached_records)
                    async with self._frontier_cache_lock:
                        # Write back ONLY if the entry this proof started
                        # from is still the one in the cache.  Entries are
                        # immutable tuples replaced whole, so an identity
                        # mismatch means a concurrent writer superseded it
                        # while the proof ran unlocked — a fold that proved
                        # a later head, a fresh full read, or a forced
                        # post-warm refill/clear — and writing back would
                        # rewind that newer baseline or resurrect an entry
                        # a failed read just invalidated.  The fold itself
                        # is chain-proven either way, so it is returned
                        # regardless; only the cache write is conditional.
                        if self._frontier_cache.get(cached_key) is entry:
                            self._frontier_cache[cached_key] = (
                                read_at,
                                head_seq,
                                head_hash,
                                cached_records,
                            )
                    return {
                        model.name: cached_records[model.name]
                        for model in frontier
                        if model.name in cached_records
                    }
        try:
            baseline_seq, baseline_hash, records = await anyio.to_thread.run_sync(
                functools.partial(self._frontier_records_with_baseline, frontier)
            )
        except ModelSelectionEvidenceError:
            async with self._frontier_cache_lock:
                self._frontier_cache.clear()
            raise
        except Exception as exc:
            async with self._frontier_cache_lock:
                self._frontier_cache.clear()
            raise ModelSelectionEvidenceError("frontier serving evidence is unavailable") from exc
        async with self._frontier_cache_lock:
            if force:
                # The warm just appended rows every cached read predates.
                self._frontier_cache.clear()
            self._frontier_cache[key] = (
                asyncio.get_running_loop().time(),
                baseline_seq,
                baseline_hash,
                records,
            )
        self._note_observed(records)
        return records

    def _note_observed(self, records: Mapping[str, AuditRecord]) -> None:
        """Advance the refresher's scheduling anchor from authoritative rows.

        Called with every mapping of newest-per-model evidence rows this
        service ever holds — a full read's records AND a cached snapshot
        advanced by a chain-verified fold — so the expiry-anchored schedule
        (:meth:`_frontier_refresh_target`) always plans from the newest
        observation actually on the chain, whichever path proved it.
        Monotone per model: an older row never rewinds the anchor.
        """
        for name, record in records.items():
            observed = _aware_utc(record.occurred_at)
            if observed is None:
                continue
            previous = self._frontier_observed_at.get(name)
            if previous is None or observed > previous:
                self._frontier_observed_at[name] = observed

    def _covering_cache_entry(
        self, key: frozenset[str]
    ) -> tuple[frozenset[str], tuple[float, int, str, dict[str, AuditRecord]]] | None:
        """The freshest TTL-fresh cached read whose model set covers ``key``.

        Exact-key entries are covering entries (``key <= key``); when several
        entries cover, the most recently read wins.  Returns the cached key
        with its whole entry — the caller still owes the extension proof
        before reuse, and folds/advances the entry IN PLACE under that key.
        Callers hold ``_frontier_cache_lock``.
        """
        now = asyncio.get_running_loop().time()
        best: tuple[frozenset[str], tuple[float, int, str, dict[str, AuditRecord]]] | None = None
        for cached_key, entry in self._frontier_cache.items():
            if not key <= cached_key:
                continue
            read_at = entry[0]
            if (now - read_at) >= FRONTIER_RECORDS_CACHE_TTL_S:
                continue
            if best is None or read_at > best[1][0]:
                best = (cached_key, entry)
        return best

    def _frontier_records_with_baseline(
        self, models: Sequence[ModelDef]
    ) -> tuple[int, str, dict[str, AuditRecord]]:
        """One verified evidence read plus the head it is current as of.

        The head is read FIRST: an observation row appended while the replays
        below run then has ``seq > baseline``, so the next reuse's extension
        proof folds it in (or, unprovable, forces a fresh read) rather than
        trusting a snapshot that may already be superseded — the race
        resolves to one bounded extension scan, never a stale verdict.

        The head HASH is the baseline every later cache-reuse extension proof
        chains from, and it is only ever cached off THIS read, whose own
        posterior chain proof (see :meth:`_frontier_records`) must pass for the
        result to be cached at all.  That proof is the anchored one, so the
        baseline inherits exactly its scope — records after the process's
        verified anchor walked here, records at or before it walked by the
        background full verify within :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S`.
        A baseline no chain proof blessed cannot enter the cache; an
        inconsistent one (head row disagreeing with its chain row) can only
        ever FAIL a later linkage proof, forcing the full read — fail-closed,
        never fail-open.
        """
        with self._audit_engine.connect() as conn:
            baseline_seq, baseline_hash = read_head(conn)
        return baseline_seq, baseline_hash, self._frontier_records(models)

    def _observation_of(self, record: AuditRecord, model: ModelDef) -> bool:
        """THE gateway-observation filter, shared by the full read and the fold.

        One predicate so the two paths cannot drift: a row is evidence about
        ``model`` iff it is a gateway transport observation of that exact
        alias under THIS boot's config hash.  Every field tested here derives
        from the hash-covered payload (``record.resource`` included — records
        are built payload-authoritatively), so the fold path trusts no
        denormalized filter column at all; the full read's :func:`replay`
        pre-filters on the ``resource`` COLUMN, and its posterior
        ``verify_chain`` cross-checks that column against the payload.
        """
        return (
            record.action == MODEL_OBSERVATION_ACTION
            and record.resource == model.litellm_model
            and record.actor == _GATEWAY_OBSERVATION_ACTOR
            and record.config_hash == self._config_hash
            and _provenance_str(record, "model_name") == model.name
            and _provenance_str(record, "operation") in _GATEWAY_OBSERVATION_OPERATIONS
        )

    def _fold_observations(
        self,
        cached_key: frozenset[str],
        cached_records: dict[str, AuditRecord],
        extension: Sequence[AuditRecord],
    ) -> dict[str, AuditRecord]:
        """A cached snapshot advanced by a PROVEN chain extension.

        Equals a fresh :meth:`_frontier_records` read at the extension's
        head, for every model in the cached entry's key: that read is, per
        model, "the max-``seq`` matching row over the whole chain"; the
        cached row is that maximum as of the baseline; and every extension
        row outranks every cached row by ``seq`` — so ``max(cached, newest
        matching extension row)`` IS the whole-chain maximum.  Folded per
        CACHED KEY, not per caller subset, so a superset entry stays
        coherent for every later caller it covers.  Newest by ``seq``
        (append order), never by ``occurred_at``: an older-stamped but
        later-appended row wins, exactly as it does in a full read.
        """
        newest: dict[str, AuditRecord] = dict(cached_records)
        for name in cached_key:
            model = self._by_name.get(name)
            if model is None:
                continue
            candidates = [record for record in extension if self._observation_of(record, model)]
            if candidates:
                newest[name] = max(candidates, key=lambda record: record.seq)
        return newest

    def configured_primary(self, task: str) -> str:
        """Return the configured primary, never an availability-based fallback."""
        try:
            route = self._routes()[task]
            return route.model
        except KeyError as exc:
            raise UnknownModelSelection("task is not registered") from exc

    def input_budget(self, model: str) -> int:
        """Return the boot-frozen deterministic input budget for one alias."""
        try:
            definition = self._by_name[model]
        except KeyError as exc:
            raise UnknownModelSelection("model is not registered") from exc
        return self._settings.context_budget_for(definition).input_budget

    def context_budget(self, model: str) -> ContextBudget:
        try:
            definition = self._by_name[model]
        except KeyError as exc:
            raise UnknownModelSelection("model is not registered") from exc
        return self._settings.context_budget_for(definition)

    async def authorize_auto(
        self,
        *,
        task: str,
        principal: Principal,
        trace_id: str,
    ) -> AuthorizedModelSelection:
        """Authorize the currently effective automatic primary without pinning a request."""
        air_gap = self._effective_air_gap()
        catalog = await self._choices(
            task,
            air_gap=air_gap,
            force_refresh_private=True,
            warm_frontier=True,
        )
        model = catalog.effective_default
        # NE-FRONTIER-LIVE: name what the automatic policy PASSED OVER, on the
        # chain, before it reports what it settled on. Without this a route
        # whose primary is a frontier model reads identically whether the
        # frontier lane answered or the air gap killed it and a private
        # fallback quietly served the run — the silent degrade the swap beat
        # exists to make impossible. One `denied` record per skipped candidate,
        # names + reason only.
        await self._emit_auto_skips(
            task=task,
            catalog=catalog,
            principal=principal,
            trace_id=trace_id,
            air_gap=air_gap,
        )
        if model is None:
            raise ModelSelectionUnavailable(
                "automatic model policy has no available candidate",
                safe_reason="automatic model policy has no available candidate",
            )
        return await self.authorize(
            task=task,
            requested_model=model,
            principal=principal,
            trace_id=trace_id,
            warm_frontier=False,
        )

    async def _emit_auto_skips(
        self,
        *,
        task: str,
        catalog: ModelChoiceCatalog,
        principal: Principal,
        trace_id: str,
        air_gap: bool,
    ) -> None:
        """Record every candidate the automatic policy skipped, and why.

        Walks the route in order and stops at the candidate that was chosen
        (``catalog.effective_default``) — candidates AFTER it were never
        considered and recording them would overstate what happened. When
        nothing was available every candidate is recorded, which is exactly
        the evidence a run that refused outright should leave behind.
        """
        route = self._routes().get(task)
        if route is None:
            return
        chosen = catalog.effective_default
        available = {choice.model for choice in catalog.choices}
        for name in (route.model, *route.fallbacks):
            if name == chosen:
                return
            target = self._by_name.get(name)
            if target is None or name in available:
                continue
            refusal = refusal_for_selection(self._settings, task, name, air_gap=air_gap)
            reason = (
                "air-gap"
                if air_gap and target.lane is Lane.FRONTIER_API
                else ("task-incompatible" if refusal is not None else "unavailable")
            )
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.denied,
                reason=reason,
                policy_effect="not-evaluated",
                policy_version="unavailable",
            )

    @staticmethod
    def _private_choices(
        models: Sequence[ModelDef],
        rows: Sequence[ServingObservation],
        *,
        now: datetime,
    ) -> tuple[ModelChoice, ...]:
        choices: list[ModelChoice] = []
        for model in models:
            matches = [
                row for row in rows if row.model_id == model.name and row.lane == model.lane.value
            ]
            # Missing OR ambiguous is no evidence.  last_ok_at is deliberately
            # ignored: history never overrides the current row's state.
            if len(matches) != 1:
                continue
            row = matches[0]
            if (
                row.state != "ok"
                or row.serves_configured_model is not True
                or not _is_current(
                    row.checked_at,
                    now=now,
                    max_age_s=PRIVATE_OBSERVATION_MAX_AGE_S,
                )
            ):
                continue
            assert row.checked_at is not None  # _is_current proved this.
            choices.append(
                ModelChoice(
                    model=model.name,
                    provider=model.provider,
                    lane=model.lane,
                    evidence="private-serving-probe",
                    observed_at=row.checked_at,
                )
            )
        return tuple(choices)

    def _frontier_records(self, models: Sequence[ModelDef]) -> dict[str, AuditRecord]:
        """The newest evidence row per model, under a POSTERIOR chain proof.

        The posterior verify is deliberate and stays: every row the replays
        above just used was present when the verdict formed, so a concurrent
        later append may extend the valid chain but an earlier read can never
        be blessed by a proof that completed before that row existed.  What
        changed (NE-SELECT-LATENCY) is only WHICH proof runs here, never that
        one runs, and never its ordering.

        The proof is :meth:`ne.audit.AnchoredChainVerifier.verify`.  On the
        request path that is normally an ANCHORED proof: the chain is shown to
        be a valid, dense, unbroken extension of a head that a real full
        ``verify_chain`` blessed at most
        :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S` ago, with no record past that
        head — one indexed ``seq > anchor`` range scan instead of a
        re-hash of every record in the ledger.  That is a SMALLER claim than
        the unbounded ``verify_chain`` this used to call, and the smaller claim
        is stated exactly, not glossed:

        * covered by THIS call — every record after the anchor whose tamper
          leaves that segment internally inconsistent (a stale
          ``record_hash``, an edited ``record_hash``, a gap, a reorder, a
          broken link, a swapped denormalized column, a truncated or forged
          tail, a record inserted past the head), which is precisely what
          ``verify_chain`` catches over the same segment; plus any tamper of
          the prefix that was propagated forward to keep the ledger
          self-consistent, since propagating it moves the anchor successor's
          ``prev_hash`` off the remembered anchor hash — a class
          ``verify_chain`` does NOT catch;
        * NOT covered by THIS call — an UNPROPAGATED tamper of a record at or
          before the anchor (an edited payload with a stale ``record_hash``, an
          edited ``record_hash``, a swapped column, a deleted prefix record).
          This is the ONE class the anchored proof gives up relative to
          ``verify_chain``.  It is caught actively by the background full
          verify (:meth:`run_chain_anchor_refresher`), and independently of
          whether that loop runs at all by
          :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S` — which is a STALENESS
          CEILING ON REUSE, not a time-to-detection: the age test lives inside
          ``AnchoredChainVerifier.verify``, so past that age no evidence read
          may be served off the anchor and each one runs the full verify
          inline, surfacing the tamper on the first such read.  Elapsed time
          with no reads detects nothing;
        * NOT covered by THIS CALL OR BY ``verify_chain`` — any COHERENT
          REPLACEMENT OR TRUNCATION of the suffix above the anchor.  Either
          rewrite record k > anchor (recompute its ``record_hash`` and
          denormalized columns, relink every ``prev_hash`` up to the head,
          advance ``ne_audit_head``), or delete every row above some
          ``t >= anchor`` and rewind ``ne_audit_head`` to row ``t``
          coherently.  In both the walk reads a self-consistent suffix still
          linked onto the remembered anchor hash, so it is satisfied — and an
          unbounded ``verify_chain`` on this same request would be satisfied
          too.  Nothing was traded away here; this is the inherent blind spot
          of a forward hash chain with no external anchor (see
          :mod:`ne.audit.verify`'s "inherent limitation"), and
          :mod:`ne.audit.checkpoint`'s signed WORM heads are its control.
          Note the truncation boundary is STRICT: rewinding the head to
          ``t < anchor`` IS refused, and that refusal is a class
          ``verify_chain`` cannot see.

        The rows the replays read are covered either way: a row newer than the
        anchor is walked by this very call, and a row at or before it was
        walked by the blessing full verify.  When no usable anchor exists —
        cold process, stale blessing, or a chain that does not provably extend
        it — this falls back to the inline full ``verify_chain`` at today's
        O(chain) cost, LOUDLY (see :class:`ne.audit.AnchoredChainVerifier`).
        There is no configuration in which a missing or stale anchor yields
        "verified".
        """
        newest: dict[str, AuditRecord] = {}
        for model in models:
            result = replay(self._audit_engine, resource=model.litellm_model)
            candidates = [record for record in result.events if self._observation_of(record, model)]
            if candidates:
                newest[model.name] = max(candidates, key=lambda record: record.seq)
        verification = self._chain_verifier.verify()
        if not verification.valid:
            raise ModelSelectionEvidenceError("frontier audit chain is invalid")
        return newest

    async def run_chain_anchor_refresher(self) -> None:
        """Re-establish FULL-chain integrity on a timer, off every user turn.

        This is the other half of the anchored-verification trade and it is NOT
        optional in the way the frontier evidence refresher is: the frontier
        refresher only moves WHEN a measurement is taken, whereas this loop is
        the only place the full ledger is ever walked once the request path is
        anchored.  Without it the anchor ages past
        :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S` and the request path degrades —
        correctly, loudly, and slowly — back to an inline full ``verify_chain``
        on every evidence read.  That degradation is the designed failure mode:
        losing this loop costs LATENCY, never integrity.

        A failed full verify clears the anchor inside the verifier and is
        logged at ERROR there; this loop additionally logs the break at ERROR
        with the ledger's own ``detail`` string, because a chain break is an
        operational event and not merely a slow path.  The loop does not exit
        on it — the next tick re-runs the verify, so a break that is repaired
        (a restored replica, a resolved replication lag) re-blesses the anchor
        without a restart.

        ⚠ **The same is true of a tick that RAISES rather than returning
        invalid** (a dropped connection, a statement timeout, a failover): the
        verifier clears the anchor there too, so this loop's ERROR can honestly
        say evidence reads now fall back to an inline full verify — they really
        do.  That fallback is the expensive one, and while the fault persists
        those inline walks will raise as well; the loop keeps ticking, and the
        FIRST full walk that completes — whichever it is, a later tick or one of
        those inline request-path walks — re-blesses the anchor and reads go
        fast again.
        See :meth:`ne.audit.AnchoredChainVerifier._full_verify` for why
        fail-closed was chosen over letting reads coast on the old blessing to
        the 900 s ceiling.

        ⚠️ **WHERE THIS IS ACTUALLY WIRED, stated because the "typical" number
        depends on it.**  Only the API process runs this loop
        (``ne.api.app._build_app`` passes it to the lifespan as
        ``chain_anchor_refresher``).  The WORKER builds its own
        :class:`ModelSelectionService`
        (``ne.worker.run_execution.build_profile_bound_run_execution_adapter``)
        whose verifier has NO background loop, so in that process the anchor is
        only ever blessed by the inline fallback and there is no ACTIVE
        detection at all — only :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S` as a
        staleness ceiling on reuse, taking effect on that process's next
        verification call after 900 s, whenever that happens to be.  (The
        300 s "typical" is a real cadence only where this loop runs, because
        the loop is itself a caller; elapsed time detects nothing on its own.)
        **That is a latency property, not an integrity one** — the ceiling is
        enforced in :meth:`ne.audit.AnchoredChainVerifier.verify` itself, so
        the worker's request path still refuses a stale anchor and pays the
        full walk before answering.  Wiring a second refresher there is DELIBERATELY
        NOT DONE here: it would put a second, independent O(chain) walk (9.77 s
        measured on the pilot's Postgres) on the same ledger every 300 s from a
        second process, and the worker's verifier serves only the per-call
        continuity recheck of already-admitted runs.  That trade deserves a
        measurement and its own card, not a drive-by in a docstring-scope fix.
        """
        interval_s = self._chain_verifier.full_verify_interval_s
        while True:
            try:
                result = await anyio.to_thread.run_sync(self._chain_verifier.full_verify)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.error(
                    "background full audit-chain verification RAISED; the verified "
                    "anchor has been CLEARED, so every evidence read now runs an "
                    "inline full verify until one of them COMPLETES and re-blesses "
                    "the anchor (or a later tick does) — expect slow reads, and "
                    "reads that raise too while the fault persists",
                    exc_info=True,
                )
            else:
                if not result.valid:
                    _LOG.error(
                        "background full audit-chain verification reports the chain "
                        "INVALID: %s — the verified anchor is cleared and every "
                        "evidence read now runs the full chain inline and refuses",
                        result.detail,
                    )
            await asyncio.sleep(interval_s)

    @staticmethod
    def _frontier_choices(
        models: Sequence[ModelDef],
        records: Mapping[str, AuditRecord],
        *,
        now: datetime,
    ) -> tuple[ModelChoice, ...]:
        choices: list[ModelChoice] = []
        for model in models:
            record = records.get(model.name)
            if not _frontier_record_is_current(record, model, now=now):
                continue
            assert record is not None  # _frontier_record_is_current proved this.
            choices.append(
                ModelChoice(
                    model=model.name,
                    provider=model.provider,
                    lane=model.lane,
                    evidence="frontier-provider-attempt",
                    observed_at=record.occurred_at,
                )
            )
        return tuple(choices)

    async def authorize(
        self,
        *,
        task: str,
        requested_model: str,
        principal: Principal,
        trace_id: str,
        warm_frontier: bool = True,
        force_refresh_private: bool = True,
        snapshot_tokens: int | None = None,
    ) -> AuthorizedModelSelection:
        """Authorize and record one explicit model choice, or withhold it.

        ``snapshot_tokens`` (NE-CTX-IN-FORCE round 7) is the CURRENT
        conversation snapshot's estimated size, passed only by surfaces that
        hold a conversation (the explicit switch on the chat POST). SPEC
        PRE-DEMO-014: a target whose window cannot safely accept it is
        absent/disabled — refused here BY NAME, before any provider call —
        because no governed compaction operation exists and silent truncation
        is forbidden. ``None`` ⇒ no conversation in hand ⇒ no snapshot check.

        ``warm_frontier`` is ``False`` only for :meth:`authorize_auto`, which
        has already warmed while building the catalog it chose from — leaving
        it ``True`` there would mean TWO outbound provider calls per admitted
        run instead of one (the warmer's own debounce would usually, but not
        always, absorb the second).

        ``force_refresh_private`` is ``False`` only for the worker's per-call
        Run continuity recheck (NE-CONTINUITY-PROBE-REUSE): admission and every
        other authorization path keep the forced probe, so a prior UI or
        preflight read can never lend an admission decision expired evidence.
        The continuity recheck sits BETWEEN the model calls of one already
        admitted run; it may reuse this service's own bounded serving cache,
        because the model call that follows immediately is itself the
        definitive liveness test and fails loudly into the recorded fallback
        chain. A stale-or-missing cache entry still triggers a REAL probe —
        this flag never turns evidence off, it only stops re-probing a lane
        that was measured moments ago. Policy (PDP), air-gap,
        task-compatibility and every audit record are unchanged either way.
        """
        target = self._by_name.get(requested_model)
        if target is None:
            await self._emit_unknown(principal=principal, trace_id=trace_id)
            raise UnknownModelSelection("requested model is not registered")

        air_gap = self._effective_air_gap()
        try:
            decision = await self._pdp.decide(
                PolicyRequest(
                    principal=principal,
                    action=MODEL_SELECTION_ACTION,
                    resource=f"model:{target.name}",
                    context={
                        "task": task,
                        "lane": target.lane.value,
                        "air_gap": air_gap,
                    },
                )
            )
        except Exception as exc:
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.error,
                reason="policy-error",
                policy_effect="error",
                policy_version="unavailable",
            )
            raise ModelSelectionEvidenceError("model selection policy is unavailable") from exc

        if decision.effect is not DecisionEffect.ALLOW:
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.denied,
                reason="policy-refused",
                decision=decision,
            )
            raise ModelSelectionPolicyRefused("model selection was not allowed by policy")

        refusal = refusal_for_selection(
            self._settings,
            task,
            target.name,
            air_gap=air_gap,
            snapshot_tokens=snapshot_tokens,
        )
        if refusal is not None:
            is_air_gap_refusal = air_gap and target.lane is Lane.FRONTIER_API
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.denied,
                reason="air-gap" if is_air_gap_refusal else "task-incompatible",
                decision=decision,
            )
            if is_air_gap_refusal:
                raise ModelSelectionPolicyRefused(
                    "frontier model selection is unavailable under AIR_GAP"
                )
            raise ModelSelectionUnavailable(
                "model selection is incompatible with this task",
                model=target.name,
                safe_reason="model selection is incompatible with this task",
            )

        try:
            catalog = await self._choices(
                task,
                air_gap=air_gap,
                force_refresh_private=force_refresh_private,
                warm_frontier=warm_frontier,
            )
        except ModelSelectionEvidenceError:
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.error,
                reason="evidence-error",
                decision=decision,
            )
            raise
        # NE-SELECT-REPROBE gate: only a FRONTIER target can be rescued by a
        # warm.  A private model's evidence comes from its own serving probe
        # (already forced above), so a miss there is a real absence.
        force_warm_on_miss = target.lane is Lane.FRONTIER_API
        choice = next((item for item in catalog.choices if item.model == target.name), None)
        if choice is None and force_warm_on_miss and self._frontier_warm is not None:
            # NE-SELECT-REPROBE.  Policy ALLOWED this model (checked above) and
            # the operator staged this lane, yet no current row came back.  On a
            # multi-hop turn whose hops are slower than
            # FRONTIER_OBSERVATION_MAX_AGE_S that means the window LAPSED
            # between hops — not that the provider is down.  Re-measure ONCE,
            # inline, through the same governed warm.  This invents nothing: it
            # is a real outbound call that either produces a real success row or
            # leaves the lane exactly as unavailable as it already was.
            try:
                catalog = await self._choices(
                    task,
                    air_gap=air_gap,
                    force_refresh_private=True,
                    warm_frontier=True,
                    force_warm=True,
                )
            except ModelSelectionEvidenceError:
                await self._emit_known(
                    target=target,
                    task=task,
                    principal=principal,
                    trace_id=trace_id,
                    outcome=AuditOutcome.error,
                    reason="evidence-error",
                    decision=decision,
                )
                raise
            choice = next((item for item in catalog.choices if item.model == target.name), None)
        if choice is None:
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.denied,
                reason="unavailable",
                decision=decision,
            )
            raise ModelSelectionUnavailable(
                "model has no current serving evidence",
                model=target.name,
                safe_reason="model has no current serving evidence",
            )

        observed_at = choice.observed_at
        if observed_at is None:
            # Defensive: only the MEASURED catalog reaches this line, and it
            # always carries an observation time.  Never invent one — a run must
            # not be able to claim an availability instant that was not measured.
            await self._emit_known(
                target=target,
                task=task,
                principal=principal,
                trace_id=trace_id,
                outcome=AuditOutcome.error,
                reason="evidence-error",
                decision=decision,
            )
            raise ModelSelectionEvidenceError("model selection evidence is malformed")

        # Append BEFORE returning: an unrecorded selection never reaches a run.
        await self._emit_known(
            target=target,
            task=task,
            principal=principal,
            trace_id=trace_id,
            outcome=AuditOutcome.success,
            reason="authorized",
            decision=decision,
        )
        return AuthorizedModelSelection(
            model=target.name,
            lane=target.lane,
            observed_at=observed_at,
            policy_effect=decision.effect.value,
            policy_version=decision.policy_version,
        )

    async def _emit_unknown(self, *, principal: Principal, trace_id: str) -> None:
        await self._append_selection_event(
            new_audit_event(
                action=MODEL_SELECTION_EVIDENCE_ACTION,
                resource="model:unregistered",
                outcome=AuditOutcome.denied,
                actor=principal.id,
                trace_id=trace_id,
                config_hash=self._config_hash,
                detail=json.dumps(
                    {
                        "reason": "unregistered-model",
                        "pdp_effect": "not-evaluated",
                        "policy_version": "unavailable",
                    },
                    sort_keys=True,
                ),
                provenance={
                    "source": "ne-api",
                    "component": "model-selection",
                    "reason": "unregistered-model",
                    "pdp_effect": "not-evaluated",
                    "policy_version": "unavailable",
                },
            )
        )

    async def _emit_known(
        self,
        *,
        target: ModelDef,
        task: str,
        principal: Principal,
        trace_id: str,
        outcome: AuditOutcome,
        reason: str,
        decision: PolicyDecision | None = None,
        policy_effect: str | None = None,
        policy_version: str | None = None,
    ) -> None:
        if decision is not None:
            effect = decision.effect.value
            version = decision.policy_version
        elif policy_effect is not None and policy_version is not None:
            effect = policy_effect
            version = policy_version
        else:  # defensive: an evidence row must never invent a PDP verdict
            raise ValueError("model selection evidence requires a policy verdict")
        detail: dict[str, str] = {
            "task": task,
            "requested_model": target.name,
            "lane": target.lane.value,
            "reason": reason,
            "pdp_effect": effect,
            "policy_version": version,
        }
        await self._append_selection_event(
            new_audit_event(
                action=MODEL_SELECTION_EVIDENCE_ACTION,
                resource=f"model:{target.name}",
                outcome=outcome,
                actor=principal.id,
                trace_id=trace_id,
                config_hash=self._config_hash,
                detail=json.dumps(detail, sort_keys=True),
                provenance={
                    "source": "ne-api",
                    "component": "model-selection",
                    **detail,
                },
            )
        )

    async def _append_selection_event(self, event: AuditEventLike) -> None:
        try:
            await self._sink.append(event)
        except Exception as exc:
            # A caller can map every authority failure without learning an
            # adapter/DB error string.  Crucially, no successful selection was
            # returned before this append completed.
            raise ModelSelectionEvidenceError(
                "model selection evidence could not be recorded"
            ) from exc
