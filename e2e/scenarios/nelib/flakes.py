# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Retry-with-attribution: the suite's flake policy.

The demo push measured three KNOWN, RATE-BOUNDED failure modes that are not
regressions (ASK_SCENARIOS.md, POST_DEMO.md #18/#19, DEMO_RUNBOOK hard rules):

* ``claim-binding-no-span`` — ``AgentOutputContractError: no span of the final
  answer is verbatim in any source``; ~1 in 11, clears on re-run, writes
  nothing to the thread.
* ``model-admission-timeout`` — the ne-api → OPA ``pdp.decide`` call times out
  (5s client timeout), model selection fails CLOSED; only ever observed on
  ``private-default``. The submission (or the run) fails with nothing written.
* ``frontier-reply-schema-rejection`` — the frontier reply is prose where JSON
  was required (``json_invalid``); S1 measured ~1 in 3 warm. It returns NO
  answer, never a wrong one.

The policy, and why it is not a bare retry:

1. A failure is retried ONLY when it matches a known signature. An unmatched
   failure fails the scenario immediately — retrying it would hide a real
   break.
2. Every attempt (flake or not) is recorded in the session flake ledger, which
   is printed at the end of the run and written next to the transcripts. A
   green suite still tells you how many known flakes it absorbed.
3. Exhausting the attempt budget on the SAME signature fails the scenario as a
   suspected RATE REGRESSION, naming the known rate — three consecutive
   1-in-11 draws is p < 0.1%, which is a break, not luck.
4. Content is never retried. A run that completes with a wrong answer is a
   regression and fails on the first observation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .client import RunResult, SubmitRefused


@dataclass(frozen=True)
class FlakeSignature:
    """One known failure mode with its measured rate (for the report only)."""

    name: str
    known_rate: str
    provenance: str


CLAIM_BINDING_NO_SPAN = FlakeSignature(
    name="claim-binding-no-span",
    known_rate="~1 in 11",
    provenance="POST_DEMO.md #18; DEMO_RUNBOOK Beat 2b stage rule",
)
MODEL_ADMISSION_TIMEOUT = FlakeSignature(
    name="model-admission-timeout",
    known_rate="transient; private-default lane only",
    provenance="POST_DEMO.md #19; DEMO_RUNBOOK hard rules (six occurrences 2026-08-28)",
)
FRONTIER_REPLY_SCHEMA_REJECTION = FlakeSignature(
    name="frontier-reply-schema-rejection",
    known_rate="~1 in 3 (S1, warm)",
    provenance="DEMO_RUNBOOK Beat 2 (json_invalid; returns no answer, never a wrong one)",
)


def classify_failure(outcome: RunResult | SubmitRefused) -> FlakeSignature | None:
    """Match a FAILED outcome against the known signatures; ``None`` = unknown.

    Matching is deliberately substring-based on the published safe reason
    (``failure_reason`` carries either the raiser's marked reason or
    ``Recorded failure class: <class>.`` — ne-runtime ``run_finalizer``).
    """
    if isinstance(outcome, SubmitRefused):
        text = outcome.body.lower()
        # The admission failure surfaces as a refused submission (5xx/409 with a
        # model-selection detail) with nothing written to the thread.
        if outcome.status_code >= 500 or "model" in text or "policy" in text:
            return MODEL_ADMISSION_TIMEOUT
        return None
    if outcome.status != "failed":
        return None
    reason = outcome.failure_reason.lower()
    if "outputcontract" in reason or "span" in reason:
        return CLAIM_BINDING_NO_SPAN
    if "json_invalid" in reason or "replycontract" in reason or "json" in reason:
        return FRONTIER_REPLY_SCHEMA_REJECTION
    if "selection" in reason or "policy_version" in reason or "admission" in reason:
        return MODEL_ADMISSION_TIMEOUT
    return None


@dataclass
class FlakeLedger:
    """Session-wide record of every attempt: attribution, never amnesia."""

    path: Path
    entries: list[dict[str, object]] = field(default_factory=list)

    def record(
        self,
        *,
        scenario: str,
        attempt: int,
        outcome: str,
        signature: FlakeSignature | None,
        run_id: str | None,
        detail: str,
    ) -> None:
        self.entries.append(
            {
                "scenario": scenario,
                "attempt": attempt,
                "outcome": outcome,
                "signature": signature.name if signature else None,
                "known_rate": signature.known_rate if signature else None,
                "run_id": run_id,
                "detail": detail[:500],
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1))

    def summary(self) -> str:
        if not self.entries:
            return "flake ledger: no attempts recorded"
        lines = ["flake ledger (every attempt, attributed):"]
        for entry in self.entries:
            lines.append(
                f"  {entry['scenario']} attempt {entry['attempt']}: {entry['outcome']}"
                + (
                    f" [{entry['signature']} — known rate {entry['known_rate']}]"
                    if entry["signature"]
                    else ""
                )
                + (f" run={entry['run_id']}" if entry["run_id"] else "")
            )
        return "\n".join(lines)


def run_with_attribution(
    scenario: str,
    attempt_fn: Callable[[int], RunResult],
    *,
    ledger: FlakeLedger,
    max_attempts: int,
    settle_ok: Callable[[RunResult], bool],
) -> RunResult:
    """Run ``attempt_fn`` until it yields an assessable outcome.

    ``settle_ok`` says whether a collected run is an ASSESSABLE outcome (e.g.
    ``status == "completed"`` — or ``refused`` where a refusal is the expected
    behaviour). Content assertions belong to the caller and are NEVER retried
    here; only known-signature failures are.
    """
    last_signature: FlakeSignature | None = None
    signature_hits = 0
    for attempt in range(1, max_attempts + 1):
        try:
            result = attempt_fn(attempt)
        except SubmitRefused as refused:
            signature = classify_failure(refused)
            ledger.record(
                scenario=scenario,
                attempt=attempt,
                outcome="submit-refused",
                signature=signature,
                run_id=None,
                detail=str(refused),
            )
            if signature is None:
                raise AssertionError(
                    f"{scenario}: submission refused with an UNKNOWN signature — this is "
                    f"a regression, not a flake. {refused}"
                ) from refused
            last_signature, signature_hits = _bump(last_signature, signature, signature_hits)
            continue
        if settle_ok(result):
            ledger.record(
                scenario=scenario,
                attempt=attempt,
                outcome=f"settled:{result.status}/{result.verdict}",
                signature=None,
                run_id=result.run_id,
                detail="",
            )
            return result
        signature = classify_failure(result)
        ledger.record(
            scenario=scenario,
            attempt=attempt,
            outcome=f"unsettled:{result.status}/{result.verdict}",
            signature=signature,
            run_id=result.run_id,
            detail=result.failure_reason,
        )
        if signature is None:
            raise AssertionError(
                f"{scenario}: run {result.run_id} ended {result.status} "
                f"(verdict={result.verdict}, reason={result.failure_reason!r}) and matches NO "
                f"known flake signature — treat as a regression, not a flake."
            )
        last_signature, signature_hits = _bump(last_signature, signature, signature_hits)
    assert last_signature is not None
    raise AssertionError(
        f"{scenario}: {signature_hits} consecutive '{last_signature.name}' failures across "
        f"{max_attempts} attempts, against a known rate of {last_signature.known_rate} "
        f"({last_signature.provenance}). That is far outside the known rate — treat as a "
        f"RATE REGRESSION of a known failure mode, not as bad luck."
    )


def _bump(
    last: FlakeSignature | None, current: FlakeSignature, hits: int
) -> tuple[FlakeSignature, int]:
    return current, (hits + 1 if last is None or last.name == current.name else 1)
