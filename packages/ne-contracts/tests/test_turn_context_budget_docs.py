# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTX-IN-FORCE round 4: the ADR-0029 doc surfaces describe the SHIPPED protocol.

``TurnContextBudget.context_budget_for_turn`` was amended twice inside the one
1.26.0 release cycle (``lane`` in round 3; async + ``TurnModelBudget`` in round
4), and both ``docs/adr/README.md`` and ``packages/ne-contracts/README.md``
kept describing the pre-amendment method — a sync call with no ``lane``
returning a bare ``ContextBudget``. An implementer following either would fail
the moment the runtime ``await``s the method or reads ``resolved.budget`` /
``resolved.model``. These pins hold every describing surface to the code:

* the protocol itself is async, takes ``lane``, answers ``TurnModelBudget``;
* the in-package ADR-0029 states that exact signature;
* both README index entries say async / ``lane`` / ``TurnModelBudget`` and no
  longer claim a bare ``ContextBudget`` return.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from ne.contracts import TurnContextBudget

_PACKAGE = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[3]
_ADR_INDEX = _REPO / "docs" / "adr" / "README.md"
_CONTRACTS_README = _PACKAGE / "README.md"
_ADR_0029 = _PACKAGE / "adr" / "ADR-0029-turn-context-budget.md"


def _flat(text: str) -> str:
    """Markdown-wrapped prose, whitespace-normalized for phrase assertions."""
    return re.sub(r"\s+", " ", text)


def _adr_index_entry() -> str:
    text = _ADR_INDEX.read_text(encoding="utf-8")
    match = re.search(r"- `0029-turn-context-budget`.*?\*\*Accepted\*\*", text, re.S)
    assert match is not None, f"{_ADR_INDEX} no longer indexes ADR-0029"
    return _flat(match.group(0))


def _contracts_1_26_entry() -> str:
    text = _CONTRACTS_README.read_text(encoding="utf-8")
    match = re.search(r"- \*\*1\.26\.0\*\*.*?(?=\n- \*\*|\Z)", text, re.S)
    assert match is not None, f"{_CONTRACTS_README} no longer documents 1.26.0"
    return _flat(match.group(0))


def test_the_shipped_protocol_is_async_takes_lane_and_answers_turn_model_budget() -> None:
    method = TurnContextBudget.context_budget_for_turn
    assert inspect.iscoroutinefunction(method), "the shipped protocol method is async"
    parameters = inspect.signature(method).parameters
    assert "lane" in parameters, "the shipped protocol method takes lane"
    assert parameters["lane"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "TurnModelBudget" in str(inspect.signature(method).return_annotation)


def test_adr_0029_itself_states_the_final_amended_signature() -> None:
    # Amended twice in place inside the 1.26.0 cycle — confirm the file kept up.
    text = _ADR_0029.read_text(encoding="utf-8")
    assert "async def context_budget_for_turn" in text
    assert "lane: Lane | None = None" in text
    assert "TurnModelBudget | None" in text


def test_adr_index_describes_the_shipped_async_lane_turn_model_budget_form() -> None:
    entry = _adr_index_entry()
    assert "async context_budget_for_turn" in entry
    assert "lane" in entry
    assert "TurnModelBudget | None" in entry
    # The pre-amendment claim an implementer would code against and fail on:
    assert "-> ContextBudget | None" not in entry


def test_contracts_readme_describes_the_shipped_async_lane_turn_model_budget_form() -> None:
    entry = _contracts_1_26_entry()
    assert "async context_budget_for_turn" in entry
    assert "lane" in entry
    assert "TurnModelBudget" in entry
    # The stale phrasing: a sync call "returning the ContextBudget".
    assert "returning the `ContextBudget`" not in entry
