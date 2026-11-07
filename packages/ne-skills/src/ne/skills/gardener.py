# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Skill Gardener — the deterministic, model-free monthly rot sweep.

ONE run produces ONE persisted digest card. The sweep, in order:

1. attribute best-effort outcomes (when an audit engine is wired) then FOLD the
   invocation telemetry into the ``ne_skill_usage`` rollup (R-26: the fold reads
   ``ne_skill_invocations`` directly — it never drains ``collect_invocations``);
2. retirement candidates (unfired-90d ∧ <3-lifetime-fires, clock-injected);
3. failure flags (≥50% fail over ≥4 known-outcome fires — REVIEW, never
   auto-retire);
4. near-dup sweep REUSING :func:`ne.skills.near_dup.find_near_dup` (never
   re-implemented) pairwise over the active library — every ``duplicate`` pair
   also makes the LOWER-ranked pack a ``superseded`` retirement candidate (the
   §6.5 superseded-retirement enhancement, INCLUDED);
5. trigger-overlap lint across the active packs;
6. ``requires_tools`` naming unregistered tools (skipped when the tool catalogue
   is not supplied);
7. bundled-origin hash drift (a registered bundled/engagement pack whose bundle
   file now differs);
8. STALE (>14d open) skill proposals — FLAGGED ONLY.

D-7 DECISION (stated, not inherited): the Gardener NEVER rejects a proposal. An
auto-reject would be the platform's FIRST non-human review-log transition (the
``review.py`` actor field would carry an agent principal) — a C6-posture change
buried in a maintenance job. Stale proposals are FLAGGED; a human batch-taps the
reject via the existing steward reject route (the console loops one tap per item).

C12: the tool RESULT is a counts-only :class:`GardenerOutcome`; the names and
bodies live only in the persisted :class:`GardenerReport`, read by the
steward-gated digest route. The Gardener is deterministic and MODEL-FREE (stdlib
+ SQLAlchemy only) — zero egress, zero gateway calls (C2/C3).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.contracts.skills import SkillPack, SkillSource
from ne.skills.active_set import _tier
from ne.skills.config import SkillsSettings
from ne.skills.near_dup import NearDupConfig, find_near_dup
from ne.skills.packio import pack_from_dir
from ne.skills.registry import SqlAlchemySkillRegistry, ne_skill_usage
from ne.skills.retirement import (
    FailureFlag,
    RetirementCandidate,
    failure_flags,
    retirement_candidates,
)
from ne.skills.standard import normalize_trigger
from ne.skills.telemetry import attribute_outcomes, fold_invocations

_SKILL_PROPOSAL_KINDS = ("skill_candidate", "skill_update_candidate")


def _now_utc() -> datetime:
    return datetime.now(UTC)


class TriggerOverlap(BaseModel):
    model_config = ConfigDict(frozen=True)

    phrase: str
    names: tuple[str, ...]


class NearDupPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    a: str
    b: str
    method: str
    score: float


class UnknownToolsFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    unknown: tuple[str, ...]


class HashDriftFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str


class StaleProposalFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    kind: str
    name: str
    age_days: int


class GardenerReport(BaseModel):
    """The full persisted report (names + detail) the digest route reads."""

    model_config = ConfigDict(frozen=True)

    ran_at: datetime
    retirement_candidates: tuple[RetirementCandidate, ...] = ()
    failure_flags: tuple[FailureFlag, ...] = ()
    trigger_overlaps: tuple[TriggerOverlap, ...] = ()
    near_dups: tuple[NearDupPair, ...] = ()
    unknown_requires_tools: tuple[UnknownToolsFlag, ...] = ()
    bundle_hash_drift: tuple[HashDriftFlag, ...] = ()
    stale_proposals: tuple[StaleProposalFlag, ...] = ()


class GardenerOutcome(BaseModel):
    """The counts-only tool result (C12 — no names, no bodies)."""

    model_config = ConfigDict(frozen=True)

    rows_folded: int
    retirement_candidates: int
    failure_flags: int
    trigger_overlaps: int
    near_dups: int
    unknown_requires_tools: int
    bundle_hash_drift: int
    stale_proposals: int


class SkillGardener:
    """The monthly rot sweep. One :meth:`run_once` = one persisted digest card."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        settings: SkillsSettings,
        audit_engine: sa.Engine | None = None,
        known_tools: frozenset[str] | None = None,
        near_dup_config: NearDupConfig | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._audit_engine = audit_engine
        self._known_tools = known_tools
        self._near_dup_config = near_dup_config or NearDupConfig()
        self._clock = clock
        self._registry = SqlAlchemySkillRegistry(engine)

    def run_once(self) -> GardenerOutcome:
        now = self._clock()

        # (1) attribute best-effort outcomes, then fold telemetry into the rollup.
        attribute_outcomes(self._engine, self._audit_engine)
        fold = fold_invocations(self._engine)

        # (2) + (3) retirement candidates + failure flags (over the fresh rollup).
        unfired = retirement_candidates(
            self._engine,
            now=now,
            days=self._settings.retirement_days,
            min_fires=self._settings.retirement_min_fires,
        )
        flags = failure_flags(
            self._engine,
            min_fires=self._settings.failure_flag_min_fires,
            ratio=self._settings.failure_flag_ratio,
        )

        library, _tampered = self._registry.verified_current_packs()

        # (4) near-dup sweep + superseded-retirement.
        near_dups, superseded = self._near_dup_sweep(library)

        # merge unfired + superseded retirement candidates (dedupe by name).
        seen: set[str] = set()
        retire: list[RetirementCandidate] = []
        for cand in (*unfired, *superseded):
            if cand.name in seen:
                continue
            seen.add(cand.name)
            retire.append(cand)

        # (5) trigger-overlap lint across the active packs.
        overlaps = self._trigger_overlaps(library)

        # (6) unknown requires_tools.
        unknown_tools = self._unknown_requires_tools(library)

        # (7) bundled-origin hash drift.
        drift = self._bundle_hash_drift(library)

        # (8) stale skill proposals (flag only — D-7).
        stale = self._stale_proposals(now)

        report = GardenerReport(
            ran_at=now,
            retirement_candidates=tuple(retire),
            failure_flags=flags,
            trigger_overlaps=overlaps,
            near_dups=near_dups,
            unknown_requires_tools=unknown_tools,
            bundle_hash_drift=drift,
            stale_proposals=stale,
        )
        self._registry.append_gardener_run(report.model_dump(mode="json"))

        return GardenerOutcome(
            rows_folded=fold.rows_folded,
            retirement_candidates=len(report.retirement_candidates),
            failure_flags=len(report.failure_flags),
            trigger_overlaps=len(report.trigger_overlaps),
            near_dups=len(report.near_dups),
            unknown_requires_tools=len(report.unknown_requires_tools),
            bundle_hash_drift=len(report.bundle_hash_drift),
            stale_proposals=len(report.stale_proposals),
        )

    # -- sweep sections --------------------------------------------------------

    def _rank_key(self, pack: SkillPack, fired: dict[str, int]) -> tuple[int, int, str]:
        """The active-set rank key (source tier, then fired desc, then name).

        The pack with the LARGER key is LOWER-ranked (retired on a superseded
        collision). Reuses the active-set ``_tier`` helper so the sweep and the
        ranker agree."""
        return (_tier(pack.source), -fired.get(pack.name, 0), pack.name)

    def _fired_counts(self) -> dict[str, int]:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.select(ne_skill_usage.c.skill_name, ne_skill_usage.c.fired_count)
            ).all()
        return {str(name): int(count) for name, count in rows}

    def _near_dup_sweep(
        self, library: tuple[SkillPack, ...]
    ) -> tuple[tuple[NearDupPair, ...], tuple[RetirementCandidate, ...]]:
        """Pairwise near-dup detection; every ``duplicate`` pair also supersedes the
        lower-ranked pack. O(n²) monthly at pilot library scale — acceptable (noted
        for the deferred soft-cap follow-up NE-SKILL-ROT-CAP)."""
        fired = self._fired_counts()
        registered_at = self._registry.registered_at_map()
        pairs: list[NearDupPair] = []
        superseded: dict[str, RetirementCandidate] = {}
        for i in range(len(library)):
            for j in range(i + 1, len(library)):
                a, b = library[i], library[j]
                hit = find_near_dup(a, (b,), config=self._near_dup_config)
                if hit is None or hit.verdict != "duplicate":
                    continue
                pairs.append(NearDupPair(a=a.name, b=b.name, method=hit.method, score=hit.score))
                loser = a if self._rank_key(a, fired) > self._rank_key(b, fired) else b
                superseded[loser.name] = RetirementCandidate(
                    name=loser.name,
                    fired_count=fired.get(loser.name, 0),
                    last_fired_at=None,
                    registered_at=registered_at.get(loser.name),
                    reason="superseded",
                )
        return tuple(pairs), tuple(superseded.values())

    def _trigger_overlaps(self, library: tuple[SkillPack, ...]) -> tuple[TriggerOverlap, ...]:
        """Every normalized trigger phrase claimed by ≥2 active packs."""
        by_phrase: dict[str, set[str]] = {}
        for pack in library:
            for trigger in pack.triggers:
                if not trigger.strip():
                    continue
                by_phrase.setdefault(normalize_trigger(trigger), set()).add(pack.name)
        overlaps = [
            TriggerOverlap(phrase=phrase, names=tuple(sorted(names)))
            for phrase, names in sorted(by_phrase.items())
            if len(names) >= 2
        ]
        return tuple(overlaps)

    def _unknown_requires_tools(
        self, library: tuple[SkillPack, ...]
    ) -> tuple[UnknownToolsFlag, ...]:
        """Active packs declaring a ``requires_tools`` entry not in the catalogue.

        Skipped entirely when the tool catalogue is not supplied (``known_tools is
        None``) — a missing catalogue is not evidence of an unknown tool."""
        if self._known_tools is None:
            return ()
        flags: list[UnknownToolsFlag] = []
        for pack in library:
            unknown = tuple(t for t in pack.requires_tools if t not in self._known_tools)
            if unknown:
                flags.append(UnknownToolsFlag(name=pack.name, unknown=unknown))
        return tuple(flags)

    def _bundle_hash_drift(self, library: tuple[SkillPack, ...]) -> tuple[HashDriftFlag, ...]:
        """Registered bundled/engagement packs whose bundle file now differs.

        Version-INDEPENDENT content probe (mirrors ``reconcile._reconcile_one``):
        the bundle pack is compared AS IF it carried the registry pack's version,
        so a version-field bookkeeping difference alone is not drift. An invalid /
        absent bundle file is not reported here (reconcile handles quarantine)."""
        from ne.contracts.skills import AuthoringStandard

        if not self._settings.bundle_dir:
            return ()
        from pathlib import Path

        root = Path(self._settings.bundle_dir)
        if not root.is_dir():
            return ()
        standard = AuthoringStandard()
        flags: list[HashDriftFlag] = []
        for pack in library:
            if pack.source not in (SkillSource.BUNDLED, SkillSource.ENGAGEMENT):
                continue
            sub = root / pack.name
            if not (sub / "SKILL.md").is_file():
                continue
            result = pack_from_dir(sub, standard=standard)
            if isinstance(result, tuple):
                continue  # invalid file — reconcile's quarantine domain, not drift
            bundle_pack = result
            probe = bundle_pack
            if bundle_pack.version != pack.version:
                probe = bundle_pack.model_copy(update={"version": pack.version})
            if probe.content_hash != pack.content_hash:
                flags.append(HashDriftFlag(name=pack.name))
        return tuple(flags)

    def _stale_proposals(self, now: datetime) -> tuple[StaleProposalFlag, ...]:
        """Open (no review row) skill proposals older than the stale threshold.

        FLAG ONLY (D-7): the Gardener never rejects. ne-skills already imports
        ne.flywheel (the forge.py precedent), and the skills engine IS the flywheel
        signal DB (same-engine requirement), so this reads both tables directly."""
        from ne.flywheel.proposals import SqlAlchemyProposalStore
        from ne.flywheel.review import ne_proposal_reviews

        store = SqlAlchemyProposalStore(self._engine)
        threshold = now - timedelta(days=self._settings.stale_proposal_days)

        with self._engine.begin() as conn:
            reviewed = {
                str(pid)
                for (pid,) in conn.execute(
                    sa.select(ne_proposal_reviews.c.proposal_id).distinct()
                ).all()
            }

        flags: list[StaleProposalFlag] = []
        for kind in _SKILL_PROPOSAL_KINDS:
            for proposal in store.read_proposals(kind=kind, limit=500):
                if proposal.proposal_id in reviewed:
                    continue  # proposed ⇔ no review rows (the append-only fold)
                if proposal.created_at >= threshold:
                    continue
                pack = proposal.payload.get("pack") if isinstance(proposal.payload, dict) else None
                name = str(pack.get("name", "")) if isinstance(pack, dict) else ""
                age_days = (now - proposal.created_at).days
                flags.append(
                    StaleProposalFlag(
                        proposal_id=proposal.proposal_id,
                        kind=kind,
                        name=name,
                        age_days=age_days,
                    )
                )
        return tuple(flags)


__all__ = [
    "GardenerOutcome",
    "GardenerReport",
    "HashDriftFlag",
    "NearDupPair",
    "SkillGardener",
    "StaleProposalFlag",
    "TriggerOverlap",
    "UnknownToolsFlag",
]
