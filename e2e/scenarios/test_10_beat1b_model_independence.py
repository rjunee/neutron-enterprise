# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Beat 1b — model independence, mid-conversation.

Three turns, TWO model swaps, ONE conversation. History must carry across both
swaps. The acceptance bar (DEMO_RUNBOOK Beat 1b, verified on trains 60/61/62):

* turn 1 (private-default): the DR-4830 counties question answers Columbia,
  Richmond, Jefferson — Complete, VERIFIED.
* turn 2 (frontier-default, SAME conversation): a recall question answered
  purely from carried history — zero tool calls, and verdict ``unavailable``,
  WHICH IS CORRECT: nothing was retrieved, so no answer-with-evidence was
  served. Treating this as a failure once inflated a 1-in-11 flake into a
  phantom 30% failure cluster; this suite pins it as the expected verdict.
* turn 3 (private-default again): "which county did you list first" — answered
  from history, names turn 1's first county, again ``unavailable`` + no tools.

The transient private-default admission failure (POST_DEMO #19) writes nothing
to the thread, so the retry-with-attribution loop resubmits into the SAME
conversation safely.
"""

from __future__ import annotations

import pytest
from nelib.client import MERIDIAN_ANALYST, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution

pytestmark = [pytest.mark.private_lane, pytest.mark.frontier]

_TURN1 = "Which counties are covered by disaster declaration DR-4830?"
_TURN2 = "Without looking anything up, how many counties did you name in your previous answer?"
_TURN3 = "Which county did you list first in your very first answer?"
_COUNTIES = ("Columbia", "Richmond", "Jefferson")


@pytest.fixture(scope="module")
def conversation(
    ops: NeutronClient, warm_box: None, flake_ledger: FlakeLedger
) -> dict[str, object]:
    del warm_box
    convo = ops.create_conversation(MERIDIAN_ANALYST)
    conversation_id = str(convo["conversation_id"])

    def turn(label: str, text: str, model: str, max_attempts: int) -> RunResult:
        return run_with_attribution(
            f"beat1b-{label}",
            lambda attempt: ops.run_turn(
                MERIDIAN_ANALYST,
                text,
                requested_model=model,
                conversation_id=conversation_id,
                label=f"beat1b-{label}-a{attempt}",
            ),
            ledger=flake_ledger,
            max_attempts=max_attempts,
            settle_ok=lambda r: r.status == "completed",
        )

    return {
        "turn1": turn("turn1", _TURN1, "private-default", 3),
        "turn2": turn("turn2", _TURN2, "frontier-default", 2),
        "turn3": turn("turn3", _TURN3, "private-default", 3),
    }


def test_turn1_grounded_counties_on_private_lane(conversation: dict[str, object]) -> None:
    turn1: RunResult = conversation["turn1"]  # type: ignore[assignment]
    assert turn1.verdict == "verified", (
        f"turn 1 must serve a VERIFIED grounded answer; got {turn1.verdict!r}: {turn1.answer!r}"
    )
    missing = [county for county in _COUNTIES if county not in turn1.answer]
    assert not missing, f"turn 1 answer omitted counties {missing}: {turn1.answer!r}"
    assert turn1.detail.get("requested_model") == "private-default"


def test_turn2_recalls_history_across_swap_and_unavailable_is_correct(
    conversation: dict[str, object],
) -> None:
    turn2: RunResult = conversation["turn2"]  # type: ignore[assignment]
    assert turn2.detail.get("requested_model") == "frontier-default"
    assert turn2.tool_calls == [], (
        "turn 2 must answer from carried history alone (zero tool calls); it retrieved: "
        f"{turn2.tool_calls}"
    )
    # ``unavailable`` IS the correct verdict for a history-only turn: no
    # answer-with-evidence was served. A ``verified`` here would mean it
    # retrieved instead of recalling — also acceptable only if tools were used,
    # which the assertion above already forbids.
    assert turn2.verdict == "unavailable", (
        f"history-only recall should carry verdict 'unavailable' (nothing retrieved, nothing "
        f"cited); got {turn2.verdict!r}"
    )
    answer = turn2.answer.lower()
    assert (
        # "three" spelled out, or all three county names recalled; a bare digit match
        # would false-positive on dates, so it is deliberately not accepted.
        "three" in answer or all(county.lower() in answer for county in _COUNTIES)
    ), f"turn 2 failed to recall the three counties from history: {turn2.answer!r}"


def test_turn3_first_county_recalled_after_second_swap(
    conversation: dict[str, object],
) -> None:
    turn1: RunResult = conversation["turn1"]  # type: ignore[assignment]
    turn3: RunResult = conversation["turn3"]  # type: ignore[assignment]
    assert turn3.detail.get("requested_model") == "private-default"
    assert turn3.tool_calls == []
    assert turn3.verdict == "unavailable"
    # The correct recall is whatever county turn 1 actually listed first.
    first_listed = next(
        (county for county in sorted(_COUNTIES, key=turn1.answer.find) if county in turn1.answer),
        None,
    )
    assert first_listed is not None
    assert first_listed in turn3.answer, (
        f"turn 3 should recall turn 1's first-listed county ({first_listed}); "
        f"answer: {turn3.answer!r}"
    )


def test_run_rows_evidence_the_two_swaps(conversation: dict[str, object]) -> None:
    """The swaps are evidenced by the run rows, not asserted from memory:
    private -> frontier -> private, all in ONE conversation."""
    turns = [conversation["turn1"], conversation["turn2"], conversation["turn3"]]
    models = [t.detail.get("requested_model") for t in turns]  # type: ignore[union-attr]
    assert models == ["private-default", "frontier-default", "private-default"]
    conversation_ids = {t.conversation_id for t in turns}  # type: ignore[union-attr]
    assert len(conversation_ids) == 1, "history carry requires ONE conversation across all turns"
