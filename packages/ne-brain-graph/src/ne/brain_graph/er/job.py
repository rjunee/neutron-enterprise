# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``SplinkResolutionJob`` — statistical ER candidates → review-gated proposals.

The job's SOLE write surface is the NE-065 proposal store (T-ER-1, structural):
its constructor accepts a SCAN-ONLY store (``ErScanStore`` — no upsert/delete is
reachable) and a :class:`~ne.brain_graph.er._splink.SplinkSidecar`; it emits
``ENTITY_MERGE_CANDIDATE`` proposals on the ``ENTITY_RESOLUTION`` lane and NEVER
mutates the graph. The ``proposal_id`` is content-addressed over the UNORDERED key
pair, so re-runs are idempotent via the store's SAVEPOINT-idempotent append.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa

from ne.brain_graph.config import BrainGraphSettings, ErClassConfig
from ne.brain_graph.er.export import ErScanStore, export_frames
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
)

__all__ = ["ErSidecar", "SplinkResolutionJob"]


class ErSidecar(Protocol):
    """The sidecar surface the job needs (resolve a payload to candidate pairs)."""

    def resolve(self, payload: dict[str, Any]) -> list[dict[str, Any]]: ...


def _pair_id(entity_class: str, left_key: str, right_key: str) -> tuple[str, str]:
    """Deterministic (proposal_id, sha16) over the UNORDERED pair (idempotent)."""
    pair = sorted([left_key, right_key])
    digest = hashlib.sha256(
        json.dumps(["er", entity_class, pair], sort_keys=True).encode()
    ).hexdigest()
    return f"sha256:{digest}", digest[:16]


class SplinkResolutionJob:
    """Export → sidecar resolve → filter → append ENTITY_MERGE_CANDIDATE proposals."""

    def __init__(
        self,
        export_store: ErScanStore,
        proposal_store_engine: sa.Engine,
        settings: BrainGraphSettings,
        sidecar: ErSidecar,
        *,
        config_hash: str,
        clock: Callable[[], datetime],
    ) -> None:
        # NOTE: ``export_store`` is typed SCAN-ONLY — the job structurally cannot
        # reach a graph write (T-ER-1). The engine is the ONLY write surface.
        self._store = export_store
        self._engine = proposal_store_engine
        self._settings = settings
        self._sidecar = sidecar
        self._config_hash = config_hash
        self._clock = clock

    async def run(self, *, entity_class: str | None = None) -> list[str]:
        """Run ER over the configured classes; return the appended proposal ids."""
        ensure_proposal_schema(self._engine)
        proposal_store = SqlAlchemyProposalStore(self._engine)
        classes = (
            {entity_class: self._settings.er_classes[entity_class]}
            if entity_class is not None
            else dict(self._settings.er_classes)
        )
        appended: list[str] = []
        for cls_name, cfg in classes.items():
            for proposal in await self._resolve_class(cls_name, cfg):
                with self._engine.begin() as conn:
                    proposal_store.append(conn, proposal)
                appended.append(proposal.proposal_id)
        return appended

    async def _resolve_class(self, entity_class: str, cfg: ErClassConfig) -> list[FixProposal]:
        fields = (*cfg.compare_fields, *cfg.blocking_fields)
        frames = await export_frames(self._store, entity_class, fields)
        by_key = {f["key"]: f for f in frames}
        payload = {
            "entity_class": entity_class,
            "records": frames,
            "compare_fields": list(cfg.compare_fields),
            "blocking_fields": list(cfg.blocking_fields),
            "threshold": self._settings.er_proposal_threshold,
            "seed": 42,
        }
        candidates = self._sidecar.resolve(payload)
        proposals: list[FixProposal] = []
        seen: set[str] = set()
        for cand in candidates:
            prob = float(cand["match_probability"])
            if prob < self._settings.er_proposal_threshold:
                continue
            left_key = str(cand["left_key"])
            right_key = str(cand["right_key"])
            proposal_id, sha16 = _pair_id(entity_class, left_key, right_key)
            if proposal_id in seen:
                continue
            seen.add(proposal_id)
            proposals.append(
                self._build_proposal(entity_class, cfg, cand, by_key, proposal_id, sha16, prob)
            )
        return proposals

    def _build_proposal(
        self,
        entity_class: str,
        cfg: ErClassConfig,
        cand: dict[str, Any],
        by_key: dict[str, dict[str, Any]],
        proposal_id: str,
        sha16: str,
        prob: float,
    ) -> FixProposal:
        left_key = str(cand["left_key"])
        right_key = str(cand["right_key"])
        left_display = str(by_key.get(left_key, {}).get("display", ""))
        right_display = str(by_key.get(right_key, {}).get("display", ""))
        priority = ProposalPriority.HIGH if prob >= 0.99 else ProposalPriority.MEDIUM
        summary = (
            f"entity-merge candidate ({entity_class}): "
            f"{left_display or left_key} == {right_display or right_key} (p={prob:.4f})"
        )
        payload = {
            "left": {
                "entity_class": entity_class,
                "entity_key": left_key,
                "display": left_display,
            },
            "right": {
                "entity_class": entity_class,
                "entity_key": right_key,
                "display": right_display,
            },
            "match_probability": prob,
            "match_weight": float(cand.get("match_weight", 0.0)),
            "blocking_rule": str(cand.get("blocking_rule", "")),
            "threshold": self._settings.er_proposal_threshold,
        }
        return FixProposal(
            proposal_id=proposal_id,
            kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
            lane=FixLane.ENTITY_RESOLUTION,
            cluster_id=f"er:{entity_class}:{sha16}",
            evidence_event_ids=(),
            evidence_signal_ids=(),
            summary=summary,
            confidence=prob,
            priority=priority,
            config_hash=self._config_hash,
            created_at=self._clock(),
            payload=payload,
        )
