# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Decline suppression — the FIRST consumer of the NE-066 rejection signal.

**This is a BUILD, not a query (design §5.2, verification-corrected).** The
rejection SIGNAL already exists: every ``ProposalReviewService.reject`` atomically
appends a ``thumbs_down`` :class:`~ne.schema.CorrectionSignal` whose provenance
carries ``rejected_proposal_id`` / ``proposal_lane`` / ``rejection_code`` /
``review_id`` (review.py:598-620). But NOTHING consumes it yet — ``review.py``
records that acting on captured feedback is later work. So this module WRITES the
fold; it does not reuse any existing suppression logic (there is none).

The fold: read skills-lane ``thumbs_down`` signals, join
``rejected_proposal_id → proposal.cluster_id`` (= ``{signature}:r{epoch}``, the
content address), and per signature decide:

- **suppressed** for ``window_days`` (default 90) after the latest rejection,
  UNLESS the signature's recurrence has GROWN by ≥ ``growth_threshold`` (default 3)
  sightings since that rejection (a genuinely-recurring workflow earns a fresh
  look) OR the window has expired.
- a re-proposal (either the growth path or the window-expiry path) uses
  ``next_epoch = max_rejected_epoch + 1``, minting a FRESH content address
  (``{signature}:r{n+1}``) — the old ``:r{n}`` proposal_id row already exists and a
  same-epoch re-append would be an idempotent no-op that folds straight back to
  ``rejected``.

Malformed / epoch-less cluster_ids are TOLERATED and skipped (a non-forge lane
proposal, or a hand-authored cluster_id, never suppresses a forge signature).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.flywheel.lanes import FixLane
from ne.flywheel.proposals import SqlAlchemyProposalStore
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore, StoredCorrectionSignal
from ne.schema import CorrectionKind
from ne.skills import worthiness

#: Documented pilot-scale cap on the rejection-signal fold (R10 precedent).
_SIGNAL_LIMIT = 500


class SuppressionDecision(BaseModel):
    """The per-signature decline verdict at forge time (frozen, counts/ids only)."""

    model_config = ConfigDict(frozen=True)

    signature: str
    suppressed: bool
    next_epoch: int
    rejected_at: datetime


def _parse_cluster(cluster_id: str) -> tuple[str, int] | None:
    """Parse ``{signature}:r{epoch}`` → ``(signature, epoch)``; tolerate malformed."""
    parts = cluster_id.rsplit(":r", 1)
    if len(parts) != 2 or not parts[0]:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def suppression_decisions(
    engine: sa.Engine,
    proposal_store: SqlAlchemyProposalStore,
    *,
    now: datetime,
    window_days: int = 90,
    growth_threshold: int = 3,
    signal_limit: int = _SIGNAL_LIMIT,
    page_growth: Mapping[str, int] | None = None,
) -> dict[str, SuppressionDecision]:
    """Fold skills-lane rejections into ``{signature: SuppressionDecision}``.

    A signature appears IFF a skills-lane ``thumbs_down`` names a proposal whose
    ``cluster_id`` parses into ``{signature}:r{epoch}``. ``rejected_at`` is the
    LATEST such rejection; ``next_epoch`` is ``max(parsed epoch) + 1``.

    ``page_growth`` carries the current forge page's floor-passing sighting counts
    per signature. The birth-gate sightings for THIS page are recorded only in the
    forge's final transaction, so :func:`worthiness.growth_since` (a committed-rows
    fold) cannot see them yet. Without folding them in, the ≥``growth_threshold``
    override would undercount whenever the recurrence that earns a rejected skill a
    fresh look FIRST arrives in the same page — the signature would be marked
    suppressed, dropped, and the cursor would advance past those sightings, so the
    override could silently never fire (Codex P1). Every page sighting is seen at
    ``now >= rejected_at`` and rides the same-transaction cursor advance (a
    rolled-back run commits neither), so counting them here neither double-counts
    committed rows nor counts sightings older than the rejection.
    """
    page_growth = page_growth or {}
    window = timedelta(days=window_days)
    now = _as_utc(now)
    # Read ALL thumbs_down rejections, PAGED — never a single oldest-first capped
    # read. `read_signals` is cursor-ordered id-ASC and `thumbs_down` is a store-wide
    # correction kind (every surface writes it), so a fixed `limit=500` returned the
    # OLDEST 500 across all surfaces: once the store held >500, newer skill
    # rejections fell outside the page and suppression silently stopped applying to
    # them — the anti-pollution moat failing exactly at scale (Codex P2). `signal_limit`
    # is now the PAGE size, not a ceiling.
    #
    # NOT windowed by `since`: a rejection OUTSIDE the 90d window is still needed to
    # compute `next_epoch` for the bump-on-expiry (and bump-on-growth) re-proposal —
    # dropping it would recycle the old epoch, collide the content-addressed
    # proposal_id, and make the re-proposal a silent idempotent no-op. So the fold
    # reads the full thumbs_down history; the window is applied downstream to decide
    # suppress-vs-expired, not to bound the read. (Cost: a full thumbs_down scan per
    # forge run — acceptable for a periodic background job; a surface-scoped or
    # newest-per-signature query is the optimization if it ever bottlenecks.)
    store = SqlAlchemyCorrectionSignalStore(engine)
    signals: list[StoredCorrectionSignal] = []
    after_id = 0
    while True:
        page = store.read_signals(
            kinds=[CorrectionKind.thumbs_down.value],
            after_id=after_id,
            limit=signal_limit,
        )
        signals.extend(page)
        if len(page) < signal_limit:
            break
        after_id = page[-1].id  # id-ASC, so the last row is the page's max id

    # signature -> (latest rejected_at, max epoch)
    folded: dict[str, tuple[datetime, int]] = {}
    for signal in signals:
        provenance = signal.payload.get("provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("proposal_lane") != FixLane.SKILLS.value:
            continue
        rejected_proposal_id = provenance.get("rejected_proposal_id")
        if not isinstance(rejected_proposal_id, str) or not rejected_proposal_id:
            continue
        proposal = proposal_store.read_proposal(rejected_proposal_id)
        if proposal is None:
            continue
        parsed = _parse_cluster(proposal.cluster_id)
        if parsed is None:
            continue
        signature, epoch = parsed
        occurred_at = _as_utc(signal.occurred_at)
        existing = folded.get(signature)
        if existing is None:
            folded[signature] = (occurred_at, epoch)
        else:
            prev_at, prev_epoch = existing
            folded[signature] = (max(prev_at, occurred_at), max(prev_epoch, epoch))

    decisions: dict[str, SuppressionDecision] = {}
    for signature, (rejected_at, max_epoch) in folded.items():
        growth = worthiness.growth_since(engine, signature, after=rejected_at)
        growth += page_growth.get(signature, 0)
        suppressed = (now - rejected_at < window) and (growth < growth_threshold)
        decisions[signature] = SuppressionDecision(
            signature=signature,
            suppressed=suppressed,
            next_epoch=max_epoch + 1,
            rejected_at=rejected_at,
        )
    return decisions


__all__ = ["SuppressionDecision", "suppression_decisions"]
