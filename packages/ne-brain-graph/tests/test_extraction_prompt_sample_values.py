# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests at the constraint-honouring sampler seam (NE-T2X-PROMPT-SCHEMA
REVISE, Argus P3): :func:`~ne.brain_graph.extraction.prompt._sample_for_pattern`
and :func:`~ne.brain_graph.extraction.prompt._sample_value` in isolation, one
level below the whole-prompt/whole-wall tests in
``test_extraction_prompt_wall_contract.py``. Imports the private helpers
directly -- precedent: the contract test file already imports
``_UNADVERTISED_OPTIONAL`` from the same module.
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, Field

from ne.brain_graph.extraction.prompt import (
    _sample_for_pattern,
    _sample_value,
    render_extraction_system_prompt,
)
from ne.schema import ENTITY_CONTRACTS


def _harvest_patterns() -> list[tuple[str, str, str]]:
    """Every ``(entity_class, field_name, pattern)`` triple with a pydantic
    ``pattern`` constraint anywhere in ``ENTITY_CONTRACTS`` -- a dynamic
    harvest, not a hand-picked list, so a future schema addition is covered
    automatically."""
    found: list[tuple[str, str, str]] = []
    for entity_class, contract in ENTITY_CONTRACTS.items():
        for field_name, field in contract.model_fields.items():
            for constraint in field.metadata:
                pattern = getattr(constraint, "pattern", None)
                if isinstance(pattern, str):
                    found.append((entity_class, field_name, pattern))
    return found


def test_sample_for_pattern_satisfies_every_schema_pattern() -> None:
    """Every pattern-constrained field in the real schema gets a value that
    satisfies its OWN pattern, checked with stdlib ``re`` -- an oracle
    independent of the ``regex`` package the sampler itself uses."""
    harvested = _harvest_patterns()

    # Harvest control: the dynamic collection above is not vacuous, and it
    # actually found the field this unit's evidence keys off (Order's
    # ``order_number``).
    assert harvested
    assert any(field_name == "order_number" for _cls, field_name, _pattern in harvested)

    for _entity_class, _field_name, pattern in harvested:
        value = _sample_for_pattern(pattern)
        assert re.fullmatch(pattern, value) is not None, (
            f"sample {value!r} for pattern {pattern!r} does not satisfy it"
        )


def test_sample_for_pattern_is_deterministic_and_pretty() -> None:
    """Double-call equality (no hidden randomness), plus characterization pins
    for the two live-schema patterns Argus validated by hand: ``ORD-0000``
    for Order's ``order_number``, ``AAA`` for Order's ``currency``."""
    order_number_pattern = r"^ORD-[0-9]{4,}$"
    currency_pattern = r"^[A-Z]{3}$"

    assert _sample_for_pattern(order_number_pattern) == _sample_for_pattern(order_number_pattern)
    assert _sample_for_pattern(order_number_pattern) == "ORD-0000"

    assert _sample_for_pattern(currency_pattern) == _sample_for_pattern(currency_pattern)
    assert _sample_for_pattern(currency_pattern) == "AAA"


def test_sample_for_pattern_fails_closed() -> None:
    """A pattern requiring 40 characters cannot be satisfied within
    ``_MAX_SAMPLE_LEN`` (32) -- the search must raise, never silently return
    a non-conforming value."""
    with pytest.raises(ValueError):
        _sample_for_pattern(r"^[0-9]{40}$")


def test_sample_value_honours_numeric_and_length_bounds() -> None:
    """Throwaway pydantic models exercise every numeric/length bound branch
    directly against real ``FieldInfo`` objects (not a hand-simulated
    metadata shape)."""

    class _GeModel(BaseModel):
        x: int = Field(ge=5)

    class _GtModel(BaseModel):
        x: int = Field(gt=3)

    class _LeModel(BaseModel):
        x: int = Field(le=0)

    class _FloatGeModel(BaseModel):
        x: float = Field(ge=2.5)

    class _MinLenModel(BaseModel):
        x: str = Field(min_length=10)

    class _ConflictModel(BaseModel):
        x: int = Field(ge=5, le=2)

    assert _sample_value(int, _GeModel.model_fields["x"]) == 5
    assert _sample_value(int, _GtModel.model_fields["x"]) == 4
    assert _sample_value(int, _LeModel.model_fields["x"]) == 0
    assert _sample_value(float, _FloatGeModel.model_fields["x"]) == 2.5
    assert len(_sample_value(str, _MinLenModel.model_fields["x"])) >= 10

    with pytest.raises(ValueError):
        _sample_value(int, _ConflictModel.model_fields["x"])


def test_render_prompt_is_deterministic() -> None:
    """E-P1-1 pin, now that the sampler is a richer search rather than a
    fixed constant table: ``render_extraction_system_prompt`` stays a pure
    function of import-time schema constants."""
    assert render_extraction_system_prompt() == render_extraction_system_prompt()
