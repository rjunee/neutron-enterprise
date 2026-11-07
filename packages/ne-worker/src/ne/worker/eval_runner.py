# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""One-shot worker-hosted Route-A evaluation writer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ne.api.platform import Platform
from ne.audit import create_engine_with_timeouts
from ne.contracts.evalobs import EvalRunResult
from ne.eval.config import EvalSettings
from ne.eval.errors import EvalConfigError
from ne.eval.fabrication import (
    FabricationCandidate,
    FabricationScore,
    score_subject_attribution,
)
from ne.eval.golden import load_golden_set
from ne.eval.route_a import run_route_a
from ne.eval.store import EvalRunRecord, EvalRunStore, ensure_eval_schema
from ne.permissions.resolver import StorePrincipalResolver


async def run_route_a_once(
    *,
    platform: Platform,
    database_url: str,
    settings: EvalSettings,
    principal_id: str,
    suite_name: str,
    golden_path: str | Path | None = None,
) -> EvalRunResult:
    """Measure the composed reader and append exactly one deployment-local row."""

    path = Path(golden_path) if golden_path is not None else settings.golden_set
    if path is None:
        raise EvalConfigError("Route A requires --golden or NE_EVAL_GOLDEN_SET")
    if not database_url:
        raise EvalConfigError("Route A requires NE_API_EVAL_DATABASE_URL")
    principal = await StorePrincipalResolver(platform.expander).resolve(principal_id)
    engine = create_engine_with_timeouts(database_url)
    try:
        ensure_eval_schema(engine)
        return await run_route_a(
            golden=load_golden_set(path),
            knowledge_read=platform.retriever,
            principal=principal,
            settings=settings,
            store=EvalRunStore(engine),
            config_hash=platform.config_hash,
            suite_name=suite_name,
        )
    finally:
        engine.dispose()


def persist_fabrication_twin(
    *,
    platform: Platform,
    database_url: str,
    candidate: FabricationCandidate,
    served_rows: Sequence[Mapping[str, object]],
    subject_field: str,
    allowed_subjects: Sequence[str],
    required_fields: Sequence[str],
    case_hash: str,
    suite_name: str = "meridian-b3-fabrication",
) -> FabricationScore:
    """Score an already-served candidate and persist its config-bound twin row."""

    if not database_url:
        raise EvalConfigError("fabrication scoring requires NE_API_EVAL_DATABASE_URL")
    score = score_subject_attribution(
        candidate,
        served_rows=served_rows,
        subject_field=subject_field,
        allowed_subjects=allowed_subjects,
        required_fields=required_fields,
    )
    engine = create_engine_with_timeouts(database_url)
    try:
        ensure_eval_schema(engine)
        EvalRunStore(engine).append(
            EvalRunRecord(
                run_id=uuid4().hex,
                suite=suite_name,
                config_hash=platform.config_hash,
                golden_set_hash=case_hash,
                judge_model_id=None,
                judge_family=None,
                rubric_versions={
                    "subject-attributed-fabrication": {"version": "1", "deterministic": True}
                },
                metrics={"subject_attributed_fabrication_pass_rate": score.score},
                thresholds={"subject_attributed_fabrication_pass_rate": 1.0},
                statuses={
                    "subject_attributed_fabrication_pass_rate": ("pass" if score.passed else "fail")
                },
                judge_error_rate=0.0,
                passed=score.passed,
                created_at=datetime.now(UTC),
            )
        )
    finally:
        engine.dispose()
    return score


__all__ = ["persist_fabrication_twin", "run_route_a_once"]
