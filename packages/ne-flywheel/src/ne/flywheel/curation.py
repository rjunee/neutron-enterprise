# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CurationAgent — the deterministic, governed flywheel curation engine (NE-065).

One :meth:`CurationAgent.run_once` is a scheduled curation run: it cursor-reads a
batch of NE-064 correction signals, clusters them deterministically
(:mod:`ne.flywheel.clustering`), routes each cluster to exactly one of the four
fix lanes (:mod:`ne.flywheel.lanes`), and appends a TYPED
:class:`~ne.flywheel.proposals.FixProposal` per cluster (plus a
``golden_set_candidate`` proposal for every retrieval-lane cluster) into the
append-only proposal store. All of that — the proposals, the cursor advance, the
ne-events outbox event, and the tamper-evident audit-chain record — commits in
ONE transaction.

The four invariants this engine upholds:

* **Proposals-only.** The engine's COMPLETE business-write surface is proposal
  rows + its own cursor + its own audit event. It NEVER touches content,
  retrieval config, schema, or policy — those applications are gated by NE-066's
  propose-then-approve human review. Every proposal ships ``status="proposed"``.
* **Deterministic.** No LLM participates in a clustering or routing decision.
  Proposal *text* sits behind the :class:`SummaryComposer` seam whose default is
  a pure template; a scripted composer replaces it in tests. CI never depends on
  live-LLM nondeterminism.
* **Audited.** Every run — including a zero-signal run — dual-appends the SAME
  ``curation.run`` :class:`~ne.schema.AuditEvent` to BOTH the ne-events outbox
  and the hash-chained ``ne_audit_log`` in the SAME transaction as the proposals
  (plan §1.4 event-spine rule). Fail-closed: any dual-append failure raises
  :class:`~ne.flywheel.errors.CurationAuditError` and the WHOLE run rolls back —
  no proposals, no cursor advance, nothing without its audit record (C6).
* **C2 in-VPC.** Customer signal data never leaves the injected engine — the
  only I/O is SQLAlchemy; no gateway/httpx import anywhere in this module.

Same-engine requirement: :func:`ensure_curation_schema` creates the proposal,
cursor, correction-signal, outbox, AND audit tables on ONE engine, mirroring
``ne.runtime.scheduler.ensure_scheduler_schema`` — the composition root MUST
hand the SAME engine to the signal store, the proposal store, and the audit
sink, or the same-transaction dual-append would not hold.

Multi-worker: the cursor upsert is single-writer-safe (compare-by-rowcount, like
the NE-034 scheduler). Real multi-worker double-curation safety under concurrent
Postgres workers is LIVE-PARKED to NE-070; there is deliberately no DBOS import
here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
from ne.contracts import get_config_hash
from ne.events import append as outbox_append
from ne.events import ensure_outbox_schema, new_audit_event
from ne.flywheel.clustering import (
    SignalCluster,
    cluster_signals,
    correction_candidates,
)
from ne.flywheel.errors import CurationAuditError
from ne.flywheel.lanes import (
    FixLane,
    confidence_for,
    priority_for,
    route_cluster,
)
from ne.flywheel.proposals import (
    FixProposal,
    GoldenSetCandidate,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
)
from ne.flywheel.store import (
    SqlAlchemyCorrectionSignalStore,
    ensure_flywheel_schema,
)
from ne.schema import AuditEvent, AuditOutcome, CorrectionKind

_CURATION_ACTOR = "ne-flywheel.curation"
_PROVENANCE_SOURCE = {"source": "ne-flywheel", "component": "curation"}


class SummaryComposer(Protocol):
    """The LLM seam: turns a cluster + its lane into human-readable proposal text.

    The default (:class:`TemplateSummaryComposer`) is a deterministic template —
    NO gateway import lives here. A gateway-backed composer that enriches the
    text with a model is future NE-066+ work; tests inject a scripted composer.
    """

    def compose(self, cluster: SignalCluster, lane: FixLane) -> str: ...


class TemplateSummaryComposer:
    """Deterministic, LLM-free proposal-text composer (the default seam impl)."""

    def compose(self, cluster: SignalCluster, lane: FixLane) -> str:
        n = len(cluster.signal_ids)
        kinds_csv = ", ".join(cluster.kinds)
        refs_csv = ", ".join(cluster.refs)
        if lane is FixLane.RETRIEVAL:
            return (
                f"[retrieval] {n} signal(s) ({kinds_csv}) about {refs_csv}: a "
                "correct source likely exists but was not retrieved/ranked; "
                "propose a retrieval-config review."
            )
        if lane is FixLane.SCHEMA:
            return (
                f"[schema] {n} signal(s) ({kinds_csv}) about {refs_csv}: a "
                "data-contract/extraction gap is implicated; propose a schema "
                "change."
            )
        if lane is FixLane.POLICY:
            return (
                f"[policy] {n} signal(s) ({kinds_csv}) about {refs_csv}: a "
                "governance/guardrail/ACL denial recurred; propose an OPA "
                "policy diff."
            )
        return (
            f"[content] {n} signal(s) ({kinds_csv}) about {refs_csv}: the "
            "produced content appears wrong; propose a source-document fix."
        )


curation_metadata = sa.MetaData()

ne_curation_cursor = sa.Table(
    "ne_curation_cursor",
    curation_metadata,
    sa.Column("name", sa.String, primary_key=True),
    sa.Column(
        "last_signal_id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=False,
    ),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_curation_schema(engine: sa.Engine) -> None:
    """Create the curation cursor + proposals + signals + outbox + audit schema.

    Full closure on ONE engine, mirroring
    ``ne.runtime.scheduler.ensure_scheduler_schema``: the cursor rows, the
    proposal rows, the correction-signal table read from, the ne-events outbox,
    and the tamper-evident ``ne_audit_log``/``ne_audit_head`` all ride the same
    engine so each curation run and its dual-appended audit event commit in ONE
    transaction. Idempotent. The composition root MUST hand this SAME engine to
    the signal store, the proposal store, and the ``PostgresAuditSink``.
    """
    curation_metadata.create_all(engine)
    ensure_proposal_schema(engine)
    ensure_flywheel_schema(engine)
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)


class CurationOutcome(BaseModel):
    """The counts-only result of one :meth:`CurationAgent.run_once` (audit-safe)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    cursor_start: int
    cursor_end: int
    signals_read: int
    clusters: int
    proposal_ids: tuple[str, ...]
    golden_candidates: int
    lane_counts: dict[str, int]


def _proposal_id(kind: ProposalKind, lane: FixLane, cluster_id: str) -> str:
    """Deterministic, content-addressed proposal id (re-runs are idempotent)."""
    return hashlib.sha256(f"{kind}:{lane}:{cluster_id}".encode()).hexdigest()


class CurationAgent:
    """The scheduled, governed, deterministic curation engine (NE-065)."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        signal_store: SqlAlchemyCorrectionSignalStore | None = None,
        proposal_store: SqlAlchemyProposalStore | None = None,
        composer: SummaryComposer | None = None,
        batch_limit: int = 500,
        cursor_name: str = "curation",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        self._signal_store = signal_store or SqlAlchemyCorrectionSignalStore(engine)
        self._proposal_store = proposal_store or SqlAlchemyProposalStore(engine)
        self._composer: SummaryComposer = composer or TemplateSummaryComposer()
        self._batch_limit = batch_limit
        self._cursor_name = cursor_name
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def proposal_store(self) -> SqlAlchemyProposalStore:
        return self._proposal_store

    async def run_once(self) -> CurationOutcome:
        """Run ONE curation pass; return its counts-only outcome.

        Async by protocol shape only — the body is DIRECT sync SQLAlchemy on
        short-lived transactions (the ``capture.py`` convention); there is no
        network I/O. The config_hash is resolved up front so
        ``ConfigHashNotFrozenError`` propagates BEFORE any write. Every proposal,
        the cursor advance, and the dual-appended ``curation.run`` event commit
        in ONE transaction; a dual-append failure raises
        :class:`CurationAuditError` and rolls the whole run back (C6 fail
        closed). A zero-signal run still emits its audit event with the cursor
        unchanged — a scheduled governance-relevant run happened.
        """
        run_id = str(uuid.uuid4())
        now = self._clock()
        config_hash = get_config_hash()

        cursor_start = self._read_cursor()
        signals = self._signal_store.read_signals(after_id=cursor_start, limit=self._batch_limit)
        cursor_end = max((s.id for s in signals), default=cursor_start)

        candidates = correction_candidates(signals)
        clusters = cluster_signals(candidates)

        proposals: list[FixProposal] = []
        lane_counts: dict[str, int] = {}
        golden_candidates = 0
        for cluster in clusters:
            lane = route_cluster(cluster)
            lane_counts[lane.value] = lane_counts.get(lane.value, 0) + 1
            proposals.append(self._build_fix_proposal(cluster, lane, config_hash, now))
            if lane is FixLane.RETRIEVAL:
                proposals.append(self._build_golden_proposal(cluster, config_hash, now))
                golden_candidates += 1

        proposal_ids = tuple(p.proposal_id for p in proposals)

        # Build the audit event OUTSIDE the transaction. Counts + lane names ONLY
        # in the detail — never signal text/refs content (C12).
        event = new_audit_event(
            action="curation.run",
            resource="flywheel:curation",
            outcome=AuditOutcome.success,
            actor=_CURATION_ACTOR,
            detail=json.dumps(
                {
                    "run_id": run_id,
                    "cursor_start": cursor_start,
                    "cursor_end": cursor_end,
                    "signals_read": len(signals),
                    "clusters": len(clusters),
                    "proposals": len(proposal_ids),
                    "golden_candidates": golden_candidates,
                    "lane_counts": lane_counts,
                }
            ),
            provenance=dict(_PROVENANCE_SOURCE),
        )

        with self._engine.begin() as conn:
            for proposal in proposals:
                self._proposal_store.append(conn, proposal)
            self._upsert_cursor(conn, cursor_end, now)
            outbox_append(conn, event)
            self._chain_append(conn, event)

        return CurationOutcome(
            run_id=run_id,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            signals_read=len(signals),
            clusters=len(clusters),
            proposal_ids=proposal_ids,
            golden_candidates=golden_candidates,
            lane_counts=lane_counts,
        )

    # -- internals ----------------------------------------------------------

    def _read_cursor(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_curation_cursor.c.last_signal_id).where(
                    ne_curation_cursor.c.name == self._cursor_name
                )
            ).first()
        return int(row.last_signal_id) if row is not None else 0

    def _upsert_cursor(self, conn: sa.Connection, last_id: int, now: datetime) -> None:
        """CAS-style cursor upsert (single-writer-safe; multi-worker -> NE-070)."""
        result = conn.execute(
            ne_curation_cursor.update()
            .where(ne_curation_cursor.c.name == self._cursor_name)
            .values(last_signal_id=last_id, updated_at=now)
        )
        if result.rowcount == 0:
            conn.execute(
                ne_curation_cursor.insert().values(
                    name=self._cursor_name,
                    last_signal_id=last_id,
                    updated_at=now,
                )
            )

    def _chain_append(self, conn: sa.Connection, event: AuditEvent) -> None:
        """Append ``event`` to the hash chain; fail closed on any failure (C6).

        A chain-append failure raises :class:`CurationAuditError` so the whole
        curation transaction (proposals + cursor + outbox event) rolls back —
        nothing persists without its tamper-evident audit record.
        """
        try:
            append_record(conn, event)
        except Exception as exc:
            raise CurationAuditError(
                "audit-chain append failed for curation.run; the whole curation "
                "run rolls back (C6 fail closed)"
            ) from exc

    def _build_fix_proposal(
        self, cluster: SignalCluster, lane: FixLane, config_hash: str, now: datetime
    ) -> FixProposal:
        return FixProposal(
            proposal_id=_proposal_id(ProposalKind.FIX, lane, cluster.cluster_id),
            kind=ProposalKind.FIX,
            lane=lane,
            cluster_id=cluster.cluster_id,
            evidence_event_ids=cluster.event_ids,
            evidence_signal_ids=cluster.signal_ids,
            summary=self._composer.compose(cluster, lane),
            confidence=confidence_for(cluster),
            priority=priority_for(cluster, lane),
            config_hash=config_hash,
            created_at=now,
            payload={},
        )

    def _build_golden_proposal(
        self, cluster: SignalCluster, config_hash: str, now: datetime
    ) -> FixProposal:
        proposal_id = _proposal_id(
            ProposalKind.GOLDEN_SET_CANDIDATE, FixLane.RETRIEVAL, cluster.cluster_id
        )
        candidate = self._build_candidate(cluster, proposal_id)
        summary = (
            f"[golden-set-candidate] retrieval failure about "
            f"{candidate.question_ref}: a correct source existed but was not "
            "surfaced — candidate for the NE-024 eval golden set (steward "
            "resolves the Q + expected source at NE-066 review)."
        )
        return FixProposal(
            proposal_id=proposal_id,
            kind=ProposalKind.GOLDEN_SET_CANDIDATE,
            lane=FixLane.RETRIEVAL,
            cluster_id=cluster.cluster_id,
            evidence_event_ids=cluster.event_ids,
            evidence_signal_ids=cluster.signal_ids,
            summary=summary,
            confidence=confidence_for(cluster),
            priority=priority_for(cluster, FixLane.RETRIEVAL),
            config_hash=config_hash,
            created_at=now,
            payload=candidate.model_dump(mode="json"),
        )

    def _build_candidate(self, cluster: SignalCluster, proposal_id: str) -> GoldenSetCandidate:
        """Build the ref-based golden-set candidate (C12: no raw text persists).

        ``question_ref``: a ``re_query`` member's ``original_ref`` (its
        subject_ref) if present; else the first ``wrong_source`` member's
        provenance ``answer_ref``; else the cluster's first ref.
        ``wrong_source_refs``: the subject_refs of the wrong_source members.
        ``expected_source_refs`` is EMPTY in v1 by design.
        """
        requery = next(
            (s for s in cluster.signals if s.signal_kind == CorrectionKind.re_query.value),
            None,
        )
        wrong = [s for s in cluster.signals if s.signal_kind == CorrectionKind.wrong_source.value]
        question_ref = ""
        if requery is not None:
            question_ref = requery.subject_ref
        elif wrong:
            for s in wrong:
                provenance: Any = s.payload.get("provenance")
                if isinstance(provenance, Mapping):
                    answer_ref = provenance.get("answer_ref")
                    if isinstance(answer_ref, str) and answer_ref.strip():
                        question_ref = answer_ref.strip()
                        break
        if not question_ref:
            question_ref = cluster.refs[0] if cluster.refs else ""

        return GoldenSetCandidate(
            candidate_id=proposal_id,
            question_ref=question_ref,
            wrong_source_refs=tuple(sorted({s.subject_ref for s in wrong})),
            expected_source_refs=(),
            evidence_event_ids=cluster.event_ids,
            notes=(
                "Steward resolves the actual question + expected source at "
                "NE-066 review (C12: raw query/doc text is not persisted)."
            ),
        )


__all__ = [
    "CurationAgent",
    "CurationOutcome",
    "SummaryComposer",
    "TemplateSummaryComposer",
    "ensure_curation_schema",
    "ne_curation_cursor",
]
