# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``SkillForge`` — one governed forge run with the NE-SKILL-2 BIRTH gate.

Extends the NE-SKILL-1b forge (snapshots → author → propose) with the birth gate:

- **Worthiness ranking** (``worthiness.py``): the FLOOR stays (``succeeded ∧ ≥2
  distinct actions``); over the floor, candidates are RANKED (repetition-30d →
  distinct principals → step count → recency) and only the top ``allowance`` are
  authored per run. Worthiness RANKS, it does NOT drop — excess persists in the
  ranked ``ne_skill_forge_queue`` and is authored on a later run.
- **Rate cap**: ≤2 OPEN skill proposals + ~1 new/day. Excess queues silently.
- **Decline suppression** (``suppression.py``): a rejected signature is not
  re-proposed for 90d unless its recurrence grows ≥3 (the first consumer of the
  NE-066 ``thumbs_down`` rejection signal); a re-proposal bumps the epoch.
- **Eval gate** (``evals.py``): every authored pack must PASS the deterministic
  ``SkillTriggerEvalSuite`` (routing through the real ``read.skills.search``
  scorer) before it is proposed; the passing run's ref (bound to the pack's
  label-EXCLUDED ``routing_hash``, D-12) is attached to the payload and the apply
  seam ENFORCES it fail-closed.

ONE-transaction discipline (C6/C10) is preserved: the proposals, the new sightings
+ queue writes, the cursor advance, the outbox event, and the dual-appended
``skill.forge_run`` audit record ALL commit in ONE transaction; a chain-append
failure raises :class:`SkillForgeAuditError` and rolls the WHOLE run back. Eval
rows are append-only in their OWN transaction (``EvalRunStore`` owns its
``engine.begin``) BEFORE the proposal commit — an eval row without a matching
proposal is harmless (append-only, replayable), so this is accepted and documented.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
from ne.contracts import get_config_hash
from ne.contracts.knowledge import Embedder
from ne.contracts.skills import AuthoringStandard, SkillPack, WorkflowSnapshot
from ne.eval.store import EvalRunRecord, EvalRunStore, ensure_eval_schema
from ne.events import append as outbox_append
from ne.events import ensure_outbox_schema, new_audit_event
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
)
from ne.flywheel.review import ensure_review_schema
from ne.flywheel.store import ensure_flywheel_schema
from ne.schema import AuditEvent, AuditOutcome
from ne.skills import worthiness
from ne.skills.errors import SkillAuthoringParseError, SkillForgeAuditError
from ne.skills.evals import SkillTriggerEvalSuite, build_eval_ref
from ne.skills.near_dup import (
    NearDupConfig,
    NearDupHit,
    find_near_dup,
    find_near_dup_embedded,
)
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.signature import workflow_signature
from ne.skills.standard import validate_pack_fields
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.skills.suppression import suppression_decisions
from ne.skills.worthiness import (
    DAILY_WINDOW,
    DEFAULT_OPEN_CAP,
    REPETITION_WINDOW_DAYS,
    CandidateStat,
    SightingEntry,
)

_FORGE_ACTOR = "ne-skills.forge"
_PROVENANCE_SOURCE = {"source": "ne-skills", "component": "forge"}

forge_metadata = sa.MetaData()

ne_skill_forge_cursor = sa.Table(
    "ne_skill_forge_cursor",
    forge_metadata,
    sa.Column("name", sa.String, primary_key=True),
    sa.Column("cursor", sa.String, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_forge_schema(engine: sa.Engine) -> None:
    """Create the forge cursor + the birth/proposal/review/eval/audit closure.

    The full-closure pattern (mirrors ``ensure_curation_schema``): the cursor
    rows, the sightings + queue tables, the proposal rows, the review log, the
    eval-run store, the ne-events outbox, and the tamper-evident audit chain all
    ride the same engine so a forge run and its dual-appended audit event commit
    in ONE transaction. Every call is idempotent, so ne-worker's single
    ``ensure_forge_schema`` at composition stays sufficient.
    """
    forge_metadata.create_all(engine)
    worthiness.ensure_birth_schema(engine)
    ensure_proposal_schema(engine)
    ensure_flywheel_schema(engine)
    ensure_review_schema(engine)
    ensure_eval_schema(engine)
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)


class ForgeOutcome(BaseModel):
    """The counts-only result of one :meth:`SkillForge.run_once` (audit-safe)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    cursor_start: str
    cursor_end: str
    snapshots_read: int
    floor_passed: int
    authored: int
    rejected_by_standard: int
    authoring_failed: int
    suppressed: int
    queued: int
    eval_failed: int
    proposal_ids: tuple[str, ...]


def _proposal_id(kind: ProposalKind, lane: FixLane, cluster_id: str) -> str:
    """Deterministic, content-addressed proposal id (re-runs are idempotent)."""
    return hashlib.sha256(f"{kind}:{lane}:{cluster_id}".encode()).hexdigest()


def _near_dup_payload(hit: NearDupHit) -> dict[str, object]:
    """Project a :class:`NearDupHit` into the proposal payload ``near_dup`` block."""
    return {
        "target": hit.name,
        "target_version": hit.version,
        "method": hit.method,
        "score": hit.score,
        "verdict": hit.verdict,
    }


class SkillForge:
    """The scheduled, governed forge engine with the NE-SKILL-2 birth gate."""

    def __init__(
        self,
        engine: sa.Engine,
        substrate: PydanticAiSkillSubstrate,
        *,
        standard: AuthoringStandard | None = None,
        proposal_store: SqlAlchemyProposalStore | None = None,
        registry: SqlAlchemySkillRegistry | None = None,
        eval_store: EvalRunStore | None = None,
        batch_limit: int = 50,
        cursor_name: str = "skill-forge",
        search_top_k: int = 8,
        open_cap: int = DEFAULT_OPEN_CAP,
        daily_window: timedelta = DAILY_WINDOW,
        suppression_window_days: int = 90,
        suppression_growth: int = 3,
        near_dup_config: NearDupConfig | None = None,
        embedder: Embedder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        self._substrate = substrate
        self._standard = standard or AuthoringStandard()
        self._near_dup_config = near_dup_config or NearDupConfig()
        self._embedder = embedder
        self._proposal_store = proposal_store or SqlAlchemyProposalStore(engine)
        self._registry = registry or SqlAlchemySkillRegistry(engine)
        self._eval_store = eval_store or EvalRunStore(engine)
        self._batch_limit = batch_limit
        self._cursor_name = cursor_name
        self._search_top_k = search_top_k
        self._open_cap = open_cap
        self._daily_window = daily_window
        self._suppression_window_days = suppression_window_days
        self._suppression_growth = suppression_growth
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def proposal_store(self) -> SqlAlchemyProposalStore:
        return self._proposal_store

    async def run_once(self) -> ForgeOutcome:
        """Run ONE forge pass with the birth gate; return its counts-only outcome."""
        run_id = str(uuid.uuid4())
        now = self._clock()
        config_hash = get_config_hash()

        cursor_start = self._read_cursor()
        page = await self._substrate.snapshots(cursor=cursor_start, limit=self._batch_limit)
        cursor_end = page.cursor

        # (1) floor-pass the page; record a sighting for EVERY floor-passing snapshot
        #     (per snapshot, not per unique signature — recurrence needs every hit).
        sighting_entries: list[SightingEntry] = []
        page_candidates: dict[str, WorkflowSnapshot] = {}
        page_occurrences: dict[str, int] = {}
        page_principals: dict[str, set[str]] = {}
        for snap in page.items:
            distinct = len({s.action for s in snap.steps})
            if not (snap.succeeded and distinct >= 2):
                continue
            sig = workflow_signature(snap)
            sighting_entries.append(
                SightingEntry(
                    signature=sig,
                    snapshot_id=snap.snapshot_id,
                    principal_id=snap.principal_id,
                    session_id=snap.session_id,
                    department=snap.department or "",
                    step_count=len(snap.steps),
                    distinct_actions=distinct,
                    finished_at=snap.finished_at,
                )
            )
            page_occurrences[sig] = page_occurrences.get(sig, 0) + 1
            page_principals.setdefault(sig, set()).add(snap.principal_id)
            prev = page_candidates.get(sig)
            if prev is None or snap.finished_at >= prev.finished_at:
                page_candidates[sig] = snap
        floor_passed = len(page_candidates)

        # (2) merge the persisted ranked queue with the page (page/freshest wins).
        candidates: dict[str, WorkflowSnapshot] = {}
        for sig, snap in worthiness.load_queue(self._engine):
            candidates[sig] = snap
        candidates.update(page_candidates)

        # (3) decline suppression + (4) already-proposed detection.
        #     page_occurrences folds THIS page's not-yet-committed sightings into
        #     the recurrence-growth override so a signature whose ≥growth recurrence
        #     first lands in this page is not wrongly suppressed-and-dropped before
        #     those sightings commit (Codex P1).
        decisions = suppression_decisions(
            self._engine,
            self._proposal_store,
            now=now,
            window_days=self._suppression_window_days,
            growth_threshold=self._suppression_growth,
            page_growth=page_occurrences,
        )

        def _next_epoch(sig: str) -> int:
            decision = decisions.get(sig)
            return decision.next_epoch if decision is not None else 0

        # Fold BOTH skill kinds: an OPEN update candidate for a signature must
        # also block a re-propose on a later run (the same cluster_id scheme).
        existing_clusters = {
            p.cluster_id
            for p in self._proposal_store.read_proposals(
                kind=ProposalKind.SKILL_CANDIDATE.value, limit=500
            )
        }
        existing_clusters |= {
            p.cluster_id
            for p in self._proposal_store.read_proposals(
                kind=ProposalKind.SKILL_UPDATE_CANDIDATE.value, limit=500
            )
        }

        suppressed_sigs: set[str] = set()
        already_sigs: set[str] = set()
        active_sigs: list[str] = []
        for sig in candidates:
            decision = decisions.get(sig)
            if decision is not None and decision.suppressed:
                suppressed_sigs.add(sig)
                continue
            if f"{sig}:r{_next_epoch(sig)}" in existing_clusters:
                already_sigs.add(sig)
                continue
            active_sigs.append(sig)

        # (5) stats + deterministic rank.
        since = now - timedelta(days=REPETITION_WINDOW_DAYS)
        stored_rep = worthiness.repetition_counts(self._engine, active_sigs, since=since)
        stored_dp = worthiness.distinct_principal_counts(self._engine, active_sigs, since=since)
        stats = [
            CandidateStat(
                signature=sig,
                repetition_30d=stored_rep.get(sig, 0) + page_occurrences.get(sig, 0),
                distinct_principals_30d=(
                    stored_dp.get(sig, 0) + len(page_principals.get(sig, set()))
                ),
                step_count=len(candidates[sig].steps),
                finished_at=candidates[sig].finished_at,
            )
            for sig in active_sigs
        ]
        ranked = worthiness.rank_candidates(stats)

        # (6) rate cap: at most one new proposal per run, ≤ open_cap open at once.
        open_count = worthiness.open_skill_proposal_count(self._engine, self._proposal_store)
        last_at = worthiness.last_skill_proposal_at(self._engine)
        daily_allow = 0 if (last_at is not None and now - last_at < self._daily_window) else 1
        allowance = max(0, min(self._open_cap - open_count, daily_allow))

        to_author = ranked[:allowance]
        excess = ranked[allowance:]

        # (7) author the top `allowance`; near-dup detect; run the eval gate;
        #     build proposals. The verified registry is read ONCE (it is not
        #     written mid-run) and reused for BOTH the near-dup detector and the
        #     eval suite.
        library, _tampered = self._registry.verified_current_packs()
        authored = 0
        rejected_by_standard = 0
        authoring_failed = 0
        eval_failed = 0
        proposals: list[FixProposal] = []
        requeue: set[str] = set()
        drop_sigs: set[str] = set(suppressed_sigs) | set(already_sigs)

        for rank, stat in enumerate(to_author):
            sig = stat.signature
            snapshot = candidates[sig]
            try:
                draft = await self._substrate.author(snapshot, standard=self._standard)
            except SkillAuthoringParseError:
                authoring_failed += 1
                requeue.add(sig)  # transient — re-queue for a later run
                continue
            except Exception:
                authoring_failed += 1
                requeue.add(sig)
                continue
            stamped = self._stamp_and_validate(draft, snapshot, config_hash, now)
            if stamped is None:
                rejected_by_standard += 1
                drop_sigs.add(sig)  # standard violation — drop from the queue (1b behaviour)
                continue

            # NE-SKILL-3: near-dup across the WHOLE verified registry.
            hit = await self._detect_near_dup(stamped, library)

            if hit is not None and hit.verdict == "duplicate":
                existing = self._registry.get_current(hit.name)
                if existing is not None:
                    result = await self._author_update(
                        snapshot,
                        existing,
                        hit,
                        sig,
                        stat,
                        rank,
                        library,
                        config_hash,
                        now,
                        next_epoch=_next_epoch(sig),
                    )
                    if isinstance(result, str):
                        if result == "authoring_failed":
                            authoring_failed += 1
                            requeue.add(sig)
                        elif result == "rejected_by_standard":
                            rejected_by_standard += 1
                            drop_sigs.add(sig)
                        else:  # "eval_failed"
                            eval_failed += 1
                            drop_sigs.add(sig)
                        continue
                    proposals.append(result)
                    authored += 1
                    drop_sigs.add(sig)
                    continue
                # existing pack vanished/tampered between the detect and the
                # get — fall through and propose it as a fresh candidate.

            # Fresh (or overlap-warn) candidate: eval over the FULL library.
            record = await self._run_eval(stamped, library, snapshot, config_hash)
            if not record.passed:
                eval_failed += 1
                drop_sigs.add(sig)  # a deterministic re-run fails identically — drop
                continue
            near_dup_payload = None
            if hit is not None and hit.verdict == "overlap":
                near_dup_payload = _near_dup_payload(hit)
            proposals.append(
                self._build_proposal(
                    stamped,
                    snapshot,
                    sig,
                    stat,
                    kind=ProposalKind.SKILL_CANDIDATE,
                    next_epoch=_next_epoch(sig),
                    rank=rank,
                    eval_ref=build_eval_ref(record),
                    config_hash=config_hash,
                    now=now,
                    near_dup=near_dup_payload,
                )
            )
            authored += 1
            drop_sigs.add(sig)  # authored — leave the queue

        # (8) the queue delta: excess ranked-but-unauthored + transient re-queues.
        queue_sigs = {stat.signature for stat in excess} | requeue
        proposal_ids = tuple(p.proposal_id for p in proposals)

        event = new_audit_event(
            action="skill.forge_run",
            resource="skills:forge",
            outcome=AuditOutcome.success,
            actor=_FORGE_ACTOR,
            detail=json.dumps(
                {
                    "run_id": run_id,
                    "cursor_start": cursor_start,
                    "cursor_end": cursor_end,
                    "snapshots_read": len(page.items),
                    "floor_passed": floor_passed,
                    "authored": authored,
                    "rejected_by_standard": rejected_by_standard,
                    "authoring_failed": authoring_failed,
                    "suppressed": len(suppressed_sigs),
                    "queued": len(queue_sigs),
                    "eval_failed": eval_failed,
                    "proposals": len(proposal_ids),
                }
            ),
            provenance=dict(_PROVENANCE_SOURCE),
        )

        with self._engine.begin() as conn:
            for proposal in proposals:
                self._proposal_store.append(conn, proposal)
            worthiness.record_sightings(conn, sighting_entries, now)
            for sig in queue_sigs:
                worthiness.upsert_queue(conn, sig, candidates[sig].model_dump(mode="json"), now)
            worthiness.delete_queued(conn, drop_sigs)
            self._upsert_cursor(conn, cursor_end, now)
            outbox_append(conn, event)
            self._chain_append(conn, event)

        return ForgeOutcome(
            run_id=run_id,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            snapshots_read=len(page.items),
            floor_passed=floor_passed,
            authored=authored,
            rejected_by_standard=rejected_by_standard,
            authoring_failed=authoring_failed,
            suppressed=len(suppressed_sigs),
            queued=len(queue_sigs),
            eval_failed=eval_failed,
            proposal_ids=proposal_ids,
        )

    # -- internals ----------------------------------------------------------

    def _stamp_and_validate(
        self,
        draft: SkillPack,
        snapshot: WorkflowSnapshot,
        config_hash: str,
        now: datetime,
    ) -> SkillPack | None:
        """Validate the draft (incl. the R-23 lint); stamp provenance or drop it."""
        fields = {
            "name": draft.name,
            "version": draft.version,
            "description": draft.description,
            "triggers": list(draft.triggers),
            "source": draft.source.value,
            "body": draft.body,
            "requires_tools": list(draft.requires_tools),
        }
        if validate_pack_fields(fields, self._standard):
            return None
        return draft.model_copy(
            update={
                "provenance": {
                    "snapshot_id": snapshot.snapshot_id,
                    "session_id": snapshot.session_id,
                    "principal_id": snapshot.principal_id,
                    "department": snapshot.department or "",
                    "config_hash": config_hash,
                    "forged_at": now.isoformat(),
                }
            }
        )

    async def _detect_near_dup(
        self, pack: SkillPack, library: tuple[SkillPack, ...]
    ) -> NearDupHit | None:
        """Run the embedder detector when the slot is wired, else the keyword one."""
        if self._embedder is not None:
            return await find_near_dup_embedded(
                pack, library, embedder=self._embedder, config=self._near_dup_config
            )
        return find_near_dup(pack, library, config=self._near_dup_config)

    async def _run_eval(
        self,
        pack: SkillPack,
        eval_library: tuple[SkillPack, ...],
        snapshot: WorkflowSnapshot,
        config_hash: str,
    ) -> EvalRunRecord:
        """Run the BIRTH eval on ``pack`` (its own append-only txn) and return the record."""
        suite = SkillTriggerEvalSuite(
            pack, eval_library, snapshot, store=self._eval_store, top_k=self._search_top_k
        )
        return await suite.run_recorded(config_hash=config_hash)

    async def _author_update(
        self,
        snapshot: WorkflowSnapshot,
        existing: SkillPack,
        hit: NearDupHit,
        signature: str,
        stat: CandidateStat,
        rank: int,
        library: tuple[SkillPack, ...],
        config_hash: str,
        now: datetime,
        *,
        next_epoch: int,
    ) -> FixProposal | str:
        """Author + eval a SKILL_UPDATE_CANDIDATE for ``existing`` at ``version+1``.

        Returns the built proposal, or a status string (``"authoring_failed"`` /
        ``"rejected_by_standard"`` / ``"eval_failed"``) mirroring the fresh path's
        failure semantics. ``parse_author_output`` already forces
        ``name=existing.name`` and ``version=existing.version + 1``, so no ``-2``
        sibling is ever mintable here.
        """
        try:
            update_draft = await self._substrate.author(
                snapshot, standard=self._standard, existing=existing
            )
        except SkillAuthoringParseError:
            return "authoring_failed"
        except Exception:
            return "authoring_failed"
        update_stamped = self._stamp_and_validate(update_draft, snapshot, config_hash, now)
        if update_stamped is None:
            return "rejected_by_standard"
        # The eval library EXCLUDES the same-name pack being updated: otherwise the
        # near_miss_precision top-1 check false-fails against the very pack whose
        # triggers this update shares (evals.py compares by name).
        eval_library = tuple(p for p in library if p.name != existing.name)
        record = await self._run_eval(update_stamped, eval_library, snapshot, config_hash)
        if not record.passed:
            return "eval_failed"
        return self._build_proposal(
            update_stamped,
            snapshot,
            signature,
            stat,
            kind=ProposalKind.SKILL_UPDATE_CANDIDATE,
            next_epoch=next_epoch,
            rank=rank,
            eval_ref=build_eval_ref(record),
            config_hash=config_hash,
            now=now,
            near_dup=_near_dup_payload(hit),
        )

    def _build_proposal(
        self,
        pack: SkillPack,
        snapshot: WorkflowSnapshot,
        signature: str,
        stat: CandidateStat,
        *,
        kind: ProposalKind,
        next_epoch: int,
        rank: int,
        eval_ref: dict[str, object],
        config_hash: str,
        now: datetime,
        near_dup: dict[str, object] | None,
    ) -> FixProposal:
        distinct = len({s.action for s in snapshot.steps})
        cluster_id = f"{signature}:r{next_epoch}"
        proposal_id = _proposal_id(kind, FixLane.SKILLS, cluster_id)
        suggested = [f"grp:{snapshot.department}"] if snapshot.department else []
        if kind == ProposalKind.SKILL_UPDATE_CANDIDATE:
            summary = (
                f"[skill-update] {pack.name!r} looks like an improvement to the "
                f"existing skill — proposing v{pack.version}"
            )
        else:
            summary = (
                f"[skill-candidate] forged skill {pack.name!r} from a "
                f"{len(snapshot.steps)}-step workflow ({distinct} distinct actions)"
            )
        return FixProposal(
            proposal_id=proposal_id,
            kind=kind,
            lane=FixLane.SKILLS,
            cluster_id=cluster_id,
            evidence_event_ids=(snapshot.snapshot_id,),
            evidence_signal_ids=(),
            summary=summary,
            confidence=0.5,
            priority=ProposalPriority.LOW,
            config_hash=config_hash,
            created_at=now,
            payload={
                "pack": pack.model_dump(mode="json"),
                "signature": signature,
                "worthiness": {
                    "floor": "v1",
                    "succeeded": True,
                    "distinct_actions": distinct,
                    "repetition_30d": stat.repetition_30d,
                    "distinct_principals_30d": stat.distinct_principals_30d,
                    "step_count": stat.step_count,
                    "rank": rank,
                },
                "suggested_acl_labels": suggested,
                "near_dup": near_dup,
                "eval_run": eval_ref,
                # The names-only snapshot powers the apply-time FRESH eval when a
                # steward edits the pack (NE-SKILL-3 edit-before-approve). C12-safe:
                # a WorkflowSnapshot has no args field by frozen-contract construction.
                "snapshot": snapshot.model_dump(mode="json"),
            },
        )

    def _read_cursor(self) -> str:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_skill_forge_cursor.c.cursor).where(
                    ne_skill_forge_cursor.c.name == self._cursor_name
                )
            ).first()
        return str(row.cursor) if row is not None else "0"

    def _upsert_cursor(self, conn: sa.Connection, cursor: str, now: datetime) -> None:
        """Single-row CAS-style cursor upsert (multi-worker LIVE-PARKED)."""
        result = conn.execute(
            ne_skill_forge_cursor.update()
            .where(ne_skill_forge_cursor.c.name == self._cursor_name)
            .values(cursor=cursor, updated_at=now)
        )
        if result.rowcount == 0:
            conn.execute(
                ne_skill_forge_cursor.insert().values(
                    name=self._cursor_name, cursor=cursor, updated_at=now
                )
            )

    def _chain_append(self, conn: sa.Connection, event: AuditEvent) -> None:
        try:
            append_record(conn, event)
        except Exception as exc:
            raise SkillForgeAuditError(
                "audit-chain append failed for skill.forge_run; the whole forge "
                "run rolls back (C6 fail closed)"
            ) from exc


__all__ = [
    "ForgeOutcome",
    "SkillForge",
    "ensure_forge_schema",
    "ne_skill_forge_cursor",
]
