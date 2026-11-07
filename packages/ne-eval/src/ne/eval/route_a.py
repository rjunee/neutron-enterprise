# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cheap production Route-A evaluation over the composed knowledge reader.

Route A deliberately evaluates the live retrieval pipeline and the deterministic
extractive floor.  It does not call, or claim to measure, the deployment's
generative model.  The caller must supply the boot-frozen ``config_hash`` and a
real :class:`EvalRunStore`; successful execution therefore always leaves one
append-only, deployment-local run row.
"""

from __future__ import annotations

from ne.contracts.evalobs import EvalRunResult
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Principal
from ne.eval.baseline import KnowledgeReadEvalTarget, LexicalJudge
from ne.eval.config import EvalSettings
from ne.eval.golden import GoldenSet
from ne.eval.store import EvalRunStore
from ne.eval.suite import RagEvalSuite


async def run_route_a(
    *,
    golden: GoldenSet,
    knowledge_read: KnowledgeRead,
    principal: Principal,
    settings: EvalSettings,
    store: EvalRunStore,
    config_hash: str,
    suite_name: str,
) -> EvalRunResult:
    """Run and persist one deterministic, principal-scoped Route-A measurement."""

    target = KnowledgeReadEvalTarget(knowledge_read, principal, top_k=settings.top_k)
    suite = RagEvalSuite(
        name=suite_name,
        golden=golden,
        target=target,
        judge=LexicalJudge(),
        thresholds=settings.thresholds,
        metrics=settings.metrics,
        evaluated_family="extractive-baseline",
        calibration_dir=str(settings.calibration_dir),
        store=store,
        top_k=settings.top_k,
        max_judge_error_rate=settings.max_judge_error_rate,
    )
    return await suite.run(config_hash=config_hash)


__all__ = ["run_route_a"]
