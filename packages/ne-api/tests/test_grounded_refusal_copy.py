# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TEMPLATE-ENUM (SPEC §12 item 4): the withheld-notice copy never overclaims.

The live-walkthrough defect: the grounding gate correctly withheld a draft, then
the surfaced copy said *"the ticket tables contain 0 rows"* — the tables held 18;
the QUERIES returned zero. These tests pin the fix at both deterministic copy
surfaces (the fallback renderer and the G4 assist FAIL prompt): the claim is
scoped to what the gate verified — the queries' results — and nothing is asserted
about the underlying store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ne.api.platform import (
    _GROUNDED_ALL_QUERIES_EMPTY_NOTE,
    _GROUNDED_ASSIST_FAIL_SYSTEM,
    _GROUNDED_ASSIST_POLISH_SYSTEM,
    _render_grounded_fallback,
)

#: Store-level claim shapes the copy must never make: each asserts a fact about
#: the STORE ("the tables hold nothing") that a zero-row QUERY result cannot
#: establish. Canaries for the exact overclaim the owner caught live.
_STORE_CLAIM_CANARIES = (
    "tables contain",
    "table contains",
    "contain 0 rows",
    "contains 0 rows",
    "no data exists",
    "data does not exist.",
    "store is empty",
    "tables are empty.",
)


def _answer_payload(rows: int) -> dict[str, Any]:
    """A recorded VerifiedAnswer mapping (``model_dump(mode="json")`` shape)."""
    from ne.brain_ontology.verified.answers import VerifiedAnswer

    row_values = tuple((f"urn:ne:entity:t{i}", "open") for i in range(rows))
    return VerifiedAnswer(
        template_id="tickets-by-status",
        template_version=1,
        args={"status": "open"},
        columns=("ticket", "status"),
        rows=row_values,
        citations=tuple(r[0] for r in row_values),
        truncated=False,
        row_limit=500,
        returned_rows=rows,
        total_rows=rows,
        truncation_reason=None,
        config_hash="a" * 64,
        ontology_version="b" * 64,
        executed_at=datetime.now(UTC),
    ).model_dump(mode="json")


def test_all_zero_row_fetches_scope_the_claim_to_the_queries() -> None:
    """SPEC §12 item 4 decisive test: when every query returned zero rows, the
    withheld notice states THAT — queries returned no rows — and makes no claim
    about the underlying store. (This test fails against the pre-unit wording,
    which carried no query-scoped sentence at all.)"""
    output = _render_grounded_fallback([_answer_payload(0), _answer_payload(0)])
    assert "withheld (audited)" in output  # the honest withhold beat is intact
    assert _GROUNDED_ALL_QUERIES_EMPTY_NOTE in output
    assert "returned no rows" in _GROUNDED_ALL_QUERIES_EMPTY_NOTE  # query-scoped fact
    assert "not a statement that the underlying tables" in _GROUNDED_ALL_QUERIES_EMPTY_NOTE
    lowered = output.lower()
    for canary in _STORE_CLAIM_CANARIES:
        assert canary not in lowered, canary


def test_non_empty_fetch_carries_no_zero_row_sentence_control() -> None:
    # CONTROL (bidirectional): with any rows fetched, the zero-row sentence is
    # absent — the copy is driven by the condition, not appended unconditionally.
    output = _render_grounded_fallback([_answer_payload(0), _answer_payload(3)])
    assert "withheld (audited)" in output
    assert _GROUNDED_ALL_QUERIES_EMPTY_NOTE not in output
    assert "urn:ne:entity:t0" in output  # the fetched table still renders


def test_no_fetches_copy_unchanged_control() -> None:
    # CONTROL: the no-fetch branch keeps its exact pre-unit wording (pinned by
    # test_grounding_wiring's SSE assertions too — do not drift it here).
    output = _render_grounded_fallback([])
    assert output.endswith("No verified data was fetched this turn.")
    assert _GROUNDED_ALL_QUERIES_EMPTY_NOTE not in output


def test_assist_fail_prompt_scopes_the_no_answer_claim_to_the_queries() -> None:
    """The G4 FAIL-redraft system prompt must instruct the model to state the
    query-scoped fact and forbid the store-level claim. (Fails against the
    pre-unit wording, which said only "If the tables do not answer the
    question, say so.")"""
    assert "say that the queries run this turn returned no matching rows" in (
        _GROUNDED_ASSIST_FAIL_SYSTEM
    )
    assert "never state or imply that the underlying tables" in _GROUNDED_ASSIST_FAIL_SYSTEM
    assert "you know only what these queries returned" in _GROUNDED_ASSIST_FAIL_SYSTEM
    assert "say so." not in _GROUNDED_ASSIST_FAIL_SYSTEM  # the old open-ended copy is gone
    # The strict-grounding sentences the prompt must keep (control: the fix added
    # scoping, it did not weaken the answer-only-from-tables contract).
    assert "answer strictly and only from the verified" in _GROUNDED_ASSIST_FAIL_SYSTEM
    assert "must appear in the tables" in _GROUNDED_ASSIST_FAIL_SYSTEM


def test_assist_polish_prompt_unchanged_control() -> None:
    # CONTROL: the PASS-polish shape is untouched by the refusal-copy fix.
    assert _GROUNDED_ASSIST_POLISH_SYSTEM == (
        "You polish an already-verified draft answer. Use "
        "ONLY facts present in the draft and the verified "
        "tables below. Never add a new fact, number, or name."
    )
