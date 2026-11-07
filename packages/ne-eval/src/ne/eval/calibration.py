# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Judge-vs-human calibration artifacts (SPEC §5 NE-024 judge policy).

A :class:`CalibrationArtifact` records, for a given (judge, rubric-version), how a
run of the judge over human-labeled rows agreed with the humans (accuracy +
Cohen's kappa, computed natively for binary labels — NO scipy). The artifact
binds to the rubric via ``rubric_hash`` = the rubric's ``content_hash``.

:func:`check_calibration` is the SPEC's "calibration artifact re-run on judge/
rubric change" enforcement, fail-closed and with NO v1 override: before any
scoring runs, EACH judge-scored rubric must have an artifact whose ``rubric_id``,
``judge_model_id``, and ``rubric_hash`` all match the live judge + rubric. A
missing artifact raises :class:`CalibrationMissingError`; an artifact for a
different judge, or one whose ``rubric_hash`` no longer matches (the rubric was
edited without recalibrating), raises :class:`CalibrationStaleError`.

Human-labels input format (JSONL, one row per line)::

    {"item_id": "...",
     "variables": {<rubric placeholder>: <value>, ...},
     "human_verdict": 0 | 1,
     "verdict_path": "verdict"}   # optional dotted path into the judge JSON

``verdict_path`` (default ``"verdict"``) navigates the judge's parsed output
(``model_dump``) to the scalar 0/1 verdict — supporting list indices, e.g.
``"verdicts.0.verdict"``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ne.eval.errors import CalibrationMissingError, CalibrationStaleError, EvalStoreError
from ne.eval.judge import Judge, out_model_for
from ne.eval.rubrics import Rubric

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


class CalibrationItem(BaseModel):
    """One human/judge verdict pair over a labeled row."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    human_verdict: int
    judge_verdict: int
    reason: str | None = None


class CalibrationArtifact(BaseModel):
    """A persisted judge-vs-human calibration result, bound to a rubric hash."""

    model_config = ConfigDict(frozen=True)

    judge_model_id: str
    judge_family: str
    rubric_id: str
    rubric_version: str
    rubric_hash: str
    labels_hash: str
    items: tuple[CalibrationItem, ...]
    accuracy: float
    cohen_kappa: float
    created_at: datetime


def cohen_kappa(human: Sequence[int], judge: Sequence[int]) -> float:
    """Cohen's kappa for two binary (0/1) label sequences (native, no scipy)."""
    n = len(human)
    if n == 0 or n != len(judge):
        return 0.0
    agree = sum(1 for h, j in zip(human, judge, strict=True) if h == j)
    po = agree / n
    p_yes_h = sum(human) / n
    p_yes_j = sum(judge) / n
    pe = p_yes_h * p_yes_j + (1 - p_yes_h) * (1 - p_yes_j)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def _navigate(data: Any, path: str) -> Any:
    """Navigate a dotted ``path`` into nested dicts/lists (int segments = indices)."""
    cur = data
    for seg in path.split("."):
        if isinstance(cur, list | tuple):
            cur = cur[int(seg)]
        elif isinstance(cur, dict):
            cur = cur[seg]
        else:
            raise EvalStoreError(f"verdict_path {path!r} does not resolve in judge output")
    return cur


def _sanitize(model_id: str) -> str:
    return _SANITIZE_RE.sub("_", model_id)


def _artifact_path(calibration_dir: str | Path, artifact: CalibrationArtifact) -> Path:
    judge = _sanitize(artifact.judge_model_id)
    name = f"{artifact.rubric_id}@{artifact.rubric_version}--{judge}.json"
    return Path(calibration_dir) / name


def run_calibration(judge: Judge, rubric: Rubric, labeled_path: str | Path) -> CalibrationArtifact:
    """Run ``judge`` over the labeled rows and return a :class:`CalibrationArtifact`."""
    p = Path(labeled_path)
    raw = p.read_bytes()
    labels_hash = hashlib.sha256(raw).hexdigest()
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise EvalStoreError(f"{p}:{lineno}: invalid calibration label JSON: {exc}") from exc

    out_model = out_model_for(rubric.id)

    async def _run() -> list[CalibrationItem]:
        items: list[CalibrationItem] = []
        for row in rows:
            variables = {k: str(v) for k, v in dict(row["variables"]).items()}
            human = int(row["human_verdict"])
            verdict_path = row.get("verdict_path", "verdict")
            result = await judge.judge(rubric, variables, out_model)
            judge_verdict = int(_navigate(result.model_dump(), verdict_path))
            items.append(
                CalibrationItem(
                    item_id=str(row["item_id"]),
                    human_verdict=human,
                    judge_verdict=judge_verdict,
                    reason=row.get("reason"),
                )
            )
        return items

    items = asyncio.run(_run())
    humans = [it.human_verdict for it in items]
    judges = [it.judge_verdict for it in items]
    accuracy = (
        sum(1 for h, j in zip(humans, judges, strict=True) if h == j) / len(items) if items else 0.0
    )
    return CalibrationArtifact(
        judge_model_id=judge.judge_model_id,
        judge_family=judge.judge_family,
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        rubric_hash=rubric.content_hash,
        labels_hash=labels_hash,
        items=tuple(items),
        accuracy=accuracy,
        cohen_kappa=cohen_kappa(humans, judges),
        created_at=datetime.now(UTC),
    )


def save_artifact(artifact: CalibrationArtifact, calibration_dir: str | Path) -> Path:
    """Persist ``artifact`` as JSON; return the written path."""
    directory = Path(calibration_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(directory, artifact)
    path.write_text(
        json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_artifacts(calibration_dir: str | Path) -> list[CalibrationArtifact]:
    """Load every ``*.json`` calibration artifact from ``calibration_dir``."""
    directory = Path(calibration_dir)
    if not directory.is_dir():
        return []
    artifacts: list[CalibrationArtifact] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            artifacts.append(CalibrationArtifact.model_validate(payload))
        except (json.JSONDecodeError, ValueError) as exc:
            raise EvalStoreError(f"invalid calibration artifact {path}: {exc}") from exc
    return artifacts


def check_calibration(calibration_dir: str | Path, judge: Judge, rubrics: Sequence[Rubric]) -> None:
    """Fail-closed: require a matching calibration artifact for every rubric.

    For each rubric there must exist an artifact with matching ``rubric_id`` AND
    ``judge_model_id`` AND ``rubric_hash == rubric.content_hash``. A missing
    artifact raises :class:`CalibrationMissingError`; a different-judge or
    hash-mismatched artifact raises :class:`CalibrationStaleError`.
    """
    artifacts = load_artifacts(calibration_dir)
    for rubric in rubrics:
        for_rubric = [a for a in artifacts if a.rubric_id == rubric.id]
        if not for_rubric:
            raise CalibrationMissingError(
                f"no calibration artifact for rubric {rubric.id!r} in {calibration_dir}"
            )
        for_judge = [a for a in for_rubric if a.judge_model_id == judge.judge_model_id]
        if not for_judge:
            raise CalibrationStaleError(
                f"rubric {rubric.id!r} has calibration artifacts but none for judge "
                f"{judge.judge_model_id!r}; re-run calibration for this judge"
            )
        if not any(a.rubric_hash == rubric.content_hash for a in for_judge):
            raise CalibrationStaleError(
                f"rubric {rubric.id!r} content hash {rubric.content_hash[:12]}... does not "
                f"match any calibration artifact for judge {judge.judge_model_id!r}; "
                "the rubric was edited — re-run calibration"
            )
