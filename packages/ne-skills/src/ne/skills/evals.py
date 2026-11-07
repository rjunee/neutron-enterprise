# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``SkillTriggerEvalSuite`` — the deterministic BIRTH eval + the fail-closed gate.

The moat vs libraries that rot at scale (NE-SKILL-2's whole point): a forged pack
is surfaced ONLY if it can ROUTE to itself through the REAL discovery seam, and
apply/approve REFUSE any candidate whose payload lacks a passing, store-anchored
eval ref bound to the pack's ``routing_hash``.

The suite implements the frozen :class:`ne.contracts.evalobs.EvalSuite` Protocol
and runs entirely IN-PROCESS through ``ne.skills.tools.score_pack`` /
``search_packs`` — the SAME scorer ``read.skills.search`` uses (SPEC §3.1
keyword-default degradation): no judge, no embeddings, no egress (C2/C3). The
optional ``GatewayJudge`` procedure-quality tier is explicitly NOT built here
(plan §4.4 marks it off-by-default).

Four metrics, all thresholded at 1.0 (absent metric ⇒ FAIL, fail-closed):

- ``trigger_recall`` — each of the pack's ``triggers`` must rank the pack in the
  top-k against ``library + candidate``.
- ``near_miss_precision`` — a deterministic near-miss set (every OTHER pack's
  triggers, which must NOT rank the candidate top-1, plus 8 stopword-free generic
  office decoys, which must NOT score a full trigger-substring hijack ≥ 1.0)
  must all pass.
- ``utility_step_coverage`` (§4.7 utility band) — every distinct birthing-snapshot
  action must have at least one token echoed in the pack body / requires_tools (a
  skill that omits the steps that made the workflow succeed is not worth encoding).
- ``utility_tools_grounded`` — the pack's ``requires_tools`` must be grounded in
  the snapshot's observed actions.

ANTI-VACUOUS guard (the NE-CKC6 lesson): empty ``triggers`` OR an empty near-miss
set ⇒ ``passed=False`` with FAIL statuses regardless of the other metrics — the
suite can NEVER pass by testing nothing.

The eval binds to the label-EXCLUDED ``routing_hash`` (D-12): relabel-at-apply
mutates ``content_hash`` but never ``routing_hash``, so a passing eval stays valid
across the steward's label assignment.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from ne.contracts.evalobs import EvalRunResult
from ne.contracts.skills import SkillPack, WorkflowSnapshot
from ne.eval.store import EvalRunRecord, EvalRunStore
from ne.eval.thresholds import MetricStatus, ThresholdSpec, evaluate_thresholds, gate_passed
from ne.skills.errors import SkillEvalGateError
from ne.skills.tools import score_pack, search_packs

SKILL_TRIGGER_SUITE_NAME = "skill-trigger"

#: decoys-v1 — 8 STOPWORD-FREE generic office queries. They MUST NOT contain
#: articles/stopwords (the/a/of/is/this): any shared stopword token would score
#: > 0 against every pack and false-fail the near-miss precision (planner-verified
#: against ``score_pack``). The near-miss criterion is a FULL trigger-substring
#: hijack (score >= 1.0), NOT top-1 — a top-1 criterion false-fails every pack on
#: a small/empty library where any shared token retrieves the pack top-1.
_NEAR_MISS_DECOYS: tuple[str, ...] = (
    "reset password",
    "expense report help",
    "book meeting room",
    "weather today",
    "employee onboarding paperwork",
    "printer not working",
    "order new laptop",
    "cancel subscription",
)

SKILL_EVAL_THRESHOLDS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec(metric="trigger_recall", threshold=1.0, tolerance=0.0),
    ThresholdSpec(metric="near_miss_precision", threshold=1.0, tolerance=0.0),
    ThresholdSpec(metric="utility_step_coverage", threshold=1.0, tolerance=0.0),
    ThresholdSpec(metric="utility_tools_grounded", threshold=1.0, tolerance=0.0),
)

_COMPUTED_KEYS = ("content_hash", "routing_hash")


def _tokens(text: str) -> list[str]:
    """The SAME tokenizer ``ne.skills.tools`` uses (keyword-default parity)."""
    return re.findall(r"[a-z0-9]+", text.lower())


class SkillTriggerEvalSuite:
    """A per-candidate routing+utility eval through the real ``read.skills.search`` seam."""

    def __init__(
        self,
        candidate: SkillPack,
        library: Sequence[SkillPack],
        snapshot: WorkflowSnapshot,
        *,
        store: EvalRunStore,
        top_k: int = 8,
        decoys: tuple[str, ...] = _NEAR_MISS_DECOYS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidate = candidate
        self._library = tuple(library)
        self._snapshot = snapshot
        self._store = store
        self._top_k = top_k
        self._decoys = tuple(decoys)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def name(self) -> str:
        return SKILL_TRIGGER_SUITE_NAME

    async def run(self, *, config_hash: str) -> EvalRunResult:
        """Run + record, projecting the stored record to the frozen ``EvalRunResult``."""
        record = await self.run_recorded(config_hash=config_hash)
        return EvalRunResult(
            suite=record.suite,
            config_hash=record.config_hash,
            metrics=dict(record.metrics),
            thresholds={spec.metric: spec.threshold for spec in SKILL_EVAL_THRESHOLDS},
            passed=record.passed,
        )

    async def run_recorded(self, *, config_hash: str) -> EvalRunRecord:
        """Compute the metrics, append the append-only run row, return the record."""
        return self.run_recorded_sync(config_hash=config_hash)

    def run_recorded_sync(self, *, config_hash: str) -> EvalRunRecord:
        """The SYNCHRONOUS body of :meth:`run_recorded` (contains zero awaits).

        The NE-SKILL-3 edit-before-approve seam runs a FRESH eval on the edited
        pack INSIDE the apply request path (already on a threadpool hop), so it
        needs the deterministic body without an event loop. ``run_recorded``
        delegates here so the forge's async call site is unchanged.
        """
        metrics, statuses, passed = self._evaluate()
        record = EvalRunRecord(
            run_id=uuid.uuid4().hex,
            suite=SKILL_TRIGGER_SUITE_NAME,
            config_hash=config_hash,
            golden_set_hash=self._golden_set_hash(),
            judge_model_id=None,
            judge_family=None,
            rubric_versions=self._rubric_versions(),
            metrics=metrics,
            thresholds={spec.metric: spec.threshold for spec in SKILL_EVAL_THRESHOLDS},
            statuses={metric: status.value for metric, status in statuses.items()},
            judge_error_rate=0.0,
            passed=passed,
            created_at=self._clock(),
        )
        return self._store.append(record)

    # -- metrics ----------------------------------------------------------------

    def _evaluate(self) -> tuple[dict[str, float], dict[str, MetricStatus], bool]:
        library_plus = (*self._library, self._candidate)
        metrics = {
            "trigger_recall": self._trigger_recall(library_plus),
            "near_miss_precision": self._near_miss_precision(library_plus),
            "utility_step_coverage": self._utility_step_coverage(),
            "utility_tools_grounded": self._utility_tools_grounded(),
        }
        vacuous = (not self._candidate.triggers) or self._near_miss_empty()
        if vacuous:
            # NE-CKC6: never pass by testing nothing.
            statuses = {spec.metric: MetricStatus.FAIL for spec in SKILL_EVAL_THRESHOLDS}
            return metrics, statuses, False
        statuses = evaluate_thresholds(metrics, SKILL_EVAL_THRESHOLDS)
        return metrics, statuses, gate_passed(statuses)

    def _trigger_recall(self, library_plus: tuple[SkillPack, ...]) -> float:
        triggers = self._candidate.triggers
        if not triggers:
            return 0.0
        hits = 0
        for trigger in triggers:
            results = search_packs(trigger, library_plus, self._top_k)
            if any(p.name == self._candidate.name for p in results):
                hits += 1
        return hits / len(triggers)

    def _near_miss_checks(self, library_plus: tuple[SkillPack, ...]) -> list[bool]:
        checks: list[bool] = []
        # Every OTHER pack's triggers must NOT rank the candidate top-1.
        for pack in self._library:
            for trigger in pack.triggers:
                results = search_packs(trigger, library_plus, self._top_k)
                top1 = results[0].name if results else None
                checks.append(top1 != self._candidate.name)
        # Generic decoys must NOT score a full trigger-substring hijack (>= 1.0).
        for query in self._decoys:
            checks.append(score_pack(query, self._candidate) < 1.0)
        return checks

    def _near_miss_empty(self) -> bool:
        return not self._near_miss_checks((*self._library, self._candidate))

    def _near_miss_precision(self, library_plus: tuple[SkillPack, ...]) -> float:
        checks = self._near_miss_checks(library_plus)
        if not checks:
            return 0.0
        return sum(1 for ok in checks if ok) / len(checks)

    def _utility_step_coverage(self) -> float:
        actions = {step.action for step in self._snapshot.steps}
        if not actions:
            return 1.0
        vocab = set(_tokens(self._candidate.body)) | set(
            _tokens(" ".join(self._candidate.requires_tools))
        )
        covered = sum(1 for action in actions if any(tok in vocab for tok in _tokens(action)))
        return covered / len(actions)

    def _utility_tools_grounded(self) -> float:
        required = self._candidate.requires_tools
        if not required:
            return 1.0
        actions = {step.action for step in self._snapshot.steps}
        grounded = sum(1 for tool in required if tool in actions)
        return grounded / len(required)

    # -- record identity --------------------------------------------------------

    def _golden_set_hash(self) -> str:
        payload = {
            "routing_hash": self._candidate.routing_hash,
            "library": sorted(p.routing_hash for p in self._library),
            "top_k": self._top_k,
            "decoys": "decoys-v1",
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _rubric_versions(self) -> dict[str, object]:
        return {
            "routing_hash": self._candidate.routing_hash,
            "decoys": "decoys-v1",
            "top_k": self._top_k,
            "library_size": len(self._library),
        }


# -- payload ref helpers + the fail-closed gate --------------------------------


def build_eval_ref(record: EvalRunRecord) -> dict[str, object]:
    """Project a stored run into the proposal-payload ``eval_run`` ref (hashes/floats)."""
    return {
        "suite": record.suite,
        "run_id": record.run_id,
        "config_hash": record.config_hash,
        "routing_hash": record.rubric_versions["routing_hash"],
        "passed": record.passed,
        "metrics": dict(record.metrics),
        "recorded_at": record.created_at.isoformat(),
    }


def verify_skill_eval_ref(
    eval_ref: object, *, expected_routing_hash: str, engine: sa.Engine
) -> None:
    """Fail closed unless ``eval_ref`` is a passing, store-anchored, routing-bound run.

    The STORED row is the tamper-resistant anchor: a forged payload ref pointing at
    no stored run — or a stored run that did not pass / has a mismatched
    ``routing_hash`` — is refused. Raises :class:`SkillEvalGateError` (a
    ``SkillApplyGateError`` subclass ⇒ existing route mapping gives 409).
    """
    if not isinstance(eval_ref, Mapping):
        raise SkillEvalGateError(
            "skill candidate has no passing eval-run ref (fail-closed birth gate)"
        )
    required = ("suite", "run_id", "config_hash", "routing_hash", "passed")
    missing = [key for key in required if key not in eval_ref]
    if missing:
        raise SkillEvalGateError(f"eval ref missing keys: {sorted(missing)}")
    if eval_ref.get("passed") is not True:
        raise SkillEvalGateError("eval ref is not a passing run")
    if eval_ref.get("suite") != SKILL_TRIGGER_SUITE_NAME:
        raise SkillEvalGateError(f"eval ref suite mismatch: {eval_ref.get('suite')!r}")
    if eval_ref.get("routing_hash") != expected_routing_hash:
        raise SkillEvalGateError(
            "eval ref routing_hash does not match the pack (a stale/edited eval)"
        )
    run_id = eval_ref.get("run_id")
    record = EvalRunStore(engine).get_run(str(run_id))
    if record is None:
        raise SkillEvalGateError("eval ref names no stored run (forged/absent)")
    if not record.passed:
        raise SkillEvalGateError("the stored eval run did not pass")
    if record.suite != SKILL_TRIGGER_SUITE_NAME:
        raise SkillEvalGateError("the stored eval run is the wrong suite")
    if record.rubric_versions.get("routing_hash") != expected_routing_hash:
        raise SkillEvalGateError("the stored eval run routing_hash does not match the pack")


def verify_skill_proposal_payload(payload: Mapping[str, object], *, engine: sa.Engine) -> None:
    """Reconstruct the pack, recompute ``routing_hash``, then run the eval gate.

    Mirrors ``apply.py`` step 4's pack reconstruction (strip the computed hash
    keys). Any malformation raises the SINGLE :class:`SkillEvalGateError` type, so
    the steward approve route maps it to 409 with no new error-table entries.
    """
    if not isinstance(payload, Mapping):
        raise SkillEvalGateError("proposal payload is not a mapping")
    raw_pack = payload.get("pack")
    if not isinstance(raw_pack, Mapping):
        raise SkillEvalGateError("proposal payload has no pack")
    pack_fields = {k: v for k, v in raw_pack.items() if k not in _COMPUTED_KEYS}
    try:
        pack = SkillPack.model_validate(pack_fields)
    except Exception as exc:
        raise SkillEvalGateError(f"payload pack invalid: {type(exc).__name__}") from exc
    verify_skill_eval_ref(
        payload.get("eval_run"), expected_routing_hash=pack.routing_hash, engine=engine
    )


__all__ = [
    "SKILL_EVAL_THRESHOLDS",
    "SKILL_TRIGGER_SUITE_NAME",
    "SkillTriggerEvalSuite",
    "build_eval_ref",
    "verify_skill_eval_ref",
    "verify_skill_proposal_payload",
]
