# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RedactionStage: backend-agnostic, fail-closed Redactor contract."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ne.contracts.governance import RedactionResult
from ne.govern.errors import RedactionError
from ne.govern.redaction import RedactionStage


class _FakeBackend:
    def __init__(self) -> None:
        self.seen_categories: Sequence[str] | None = "unset"  # type: ignore[assignment]

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        self.seen_categories = categories
        return RedactionResult(text=f"[{text}]", redactions=("EMAIL_ADDRESS",))


class _BoomBackend:
    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        raise ValueError("backend exploded")


class _RedactionErrorBackend:
    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        raise RedactionError("already fail-closed")


def test_stage_delegates_to_backend() -> None:
    stage = RedactionStage(_FakeBackend())
    result = stage.redact("hi")
    assert result.text == "[hi]"
    assert result.redactions == ("EMAIL_ADDRESS",)


def test_categories_pass_through_unchanged() -> None:
    backend = _FakeBackend()
    RedactionStage(backend).redact("hi", categories=["US_SSN"])
    assert backend.seen_categories == ["US_SSN"]


def test_backend_error_fails_closed_and_withholds_text() -> None:
    with pytest.raises(RedactionError):
        RedactionStage(_BoomBackend()).redact("secret 123-45-6789")


def test_backend_redaction_error_propagates_as_is() -> None:
    with pytest.raises(RedactionError):
        RedactionStage(_RedactionErrorBackend()).redact("x")


def test_stage_structurally_satisfies_redactor() -> None:
    stage = RedactionStage(_FakeBackend())
    assert callable(stage.redact)
    assert isinstance(stage.redact("t"), RedactionResult)
