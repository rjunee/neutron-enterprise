# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The grounding gate fires — with a REAL ``guardrail.block`` audit row — and a
clean control run does NOT trip it.

Measured 5-of-5 on train-60 (DEMO_RUNBOOK Beat 2c): the Meridian Grounded
Analyst, asked the S2 question, withholds its draft (run outcome REFUSED, the
server-authored decline) and mints exactly one ``guardrail.block`` audit event
whose trace id is the run's own id. The control — the plain Analyst on the same
question — completes normally and mints NO new gate event (the gate binds to
the grounded agent only; a gate that fires on everything is as broken as one
that never fires).

Rare-draw handling: measurement says a served answer instead of a refusal is
the rare draw. One re-run is allowed WITH ATTRIBUTION (recorded in the flake
ledger as ``gate-served-answer``); two serves in a row fails the scenario.
"""

from __future__ import annotations

import json

import pytest
from nelib.client import GROUNDED_ANALYST, MERIDIAN_ANALYST, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution

pytestmark = [pytest.mark.frontier]

_QUESTION = "Which open debit-card disputes are overdue for provisional credit under the SOP?"


def _gate_events(ops: NeutronClient) -> list[dict[str, object]]:
    return ops.audit_events(action="guardrail.block", limit=500)


def _event_seqs(events: list[dict[str, object]]) -> set[int]:
    return {int(e["seq"]) for e in events if "seq" in e}


@pytest.fixture(scope="module")
def gate(ops: NeutronClient, warm_box: None, flake_ledger: FlakeLedger) -> dict[str, object]:
    del warm_box
    before = _gate_events(ops)

    served_answers = 0

    def attempt(n: int) -> RunResult:
        return ops.run_turn(
            GROUNDED_ANALYST,
            _QUESTION,
            requested_model="frontier-default",
            label=f"gate-a{n}",
        )

    result: RunResult | None = None
    for attempt_number in (1, 2):
        candidate = run_with_attribution(
            f"grounding-gate-a{attempt_number}",
            attempt,
            ledger=flake_ledger,
            max_attempts=2,
            settle_ok=lambda r: r.status == "completed",
        )
        if candidate.verdict == "refused":
            result = candidate
            break
        served_answers += 1
        flake_ledger.record(
            scenario="grounding-gate",
            attempt=attempt_number,
            outcome=f"gate-served-answer:{candidate.verdict}",
            signature=None,
            run_id=candidate.run_id,
            detail="the rare draw: the model stayed inside the records (measured 5/5 fired; "
            "one serve tolerated with attribution, two in a row fails)",
        )
        result = candidate
    assert result is not None
    if result.verdict != "refused":
        pytest.fail(
            f"grounding gate did not fire in {served_answers} consecutive runs (measured "
            "fire rate was 5 of 5) — treat as a gate-wiring regression, not a flake."
        )
    after = _gate_events(ops)
    return {"result": result, "before": before, "after": after}


def test_gate_refuses_the_ungrounded_draft(gate: dict[str, object]) -> None:
    result: RunResult = gate["result"]  # type: ignore[assignment]
    assert result.status == "completed"
    assert result.verdict == "refused"
    # Not one byte of the withheld draft may be shown.
    assert "mer-dsp" not in result.answer, (
        f"the decline surface leaked withheld draft content: {result.answer!r}"
    )


def test_gate_minted_a_real_guardrail_block_audit_row(gate: dict[str, object]) -> None:
    result: RunResult = gate["result"]  # type: ignore[assignment]
    before: list[dict[str, object]] = gate["before"]  # type: ignore[assignment]
    after: list[dict[str, object]] = gate["after"]  # type: ignore[assignment]
    new_events = [e for e in after if _seq(e) not in _event_seqs(before)]
    assert new_events, "no new guardrail.block audit event after the refused run"
    bound = [e for e in new_events if result.run_id in json.dumps(e)]
    assert bound, (
        f"none of the new guardrail.block rows carries this run's id {result.run_id}; "
        f"new rows: {json.dumps(new_events)[:800]}"
    )


def test_control_run_does_not_trip_the_gate(
    ops: NeutronClient, gate: dict[str, object], flake_ledger: FlakeLedger
) -> None:
    """No collateral: the plain Analyst on the SAME question completes with its
    usual (often wrong — see S2) answer and mints NO gate event."""
    baseline = _event_seqs(_gate_events(ops))
    control = run_with_attribution(
        "grounding-gate-control",
        lambda attempt: ops.run_turn(
            MERIDIAN_ANALYST,
            _QUESTION,
            requested_model="frontier-default",
            label=f"gate-control-a{attempt}",
        ),
        ledger=flake_ledger,
        max_attempts=2,
        settle_ok=lambda r: r.status == "completed",
    )
    assert control.verdict in {"verified", "unavailable"}, (
        f"control run verdict {control.verdict!r} — the gate must bind only to the grounded agent"
    )
    assert _event_seqs(_gate_events(ops)) == baseline, (
        "the control run tripped guardrail.block — the gate is firing beyond its bound agent"
    )


def _seq(event: dict[str, object]) -> int:
    return int(event.get("seq", -1))
