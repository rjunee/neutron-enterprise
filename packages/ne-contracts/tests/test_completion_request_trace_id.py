# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CompletionRequest.trace_id — additive, optional, frozen (ADR-0007, 1.4.0).

The run-scoped correlation id is carried AS DATA into the gateway. It must be
optional (default None so every pre-1.4.0 constructor stays valid), settable
explicitly, and stay frozen (Checkpoint-A immutability).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.models import CompletionRequest
from ne.contracts.types import ChatMessage

_MSGS = (ChatMessage(role="user", content="hi"),)


def test_trace_id_defaults_to_none() -> None:
    # Backward-compat construction: the pre-1.4.0 kwarg set stays valid.
    request = CompletionRequest(messages=_MSGS, task="draft")
    assert request.trace_id is None


def test_trace_id_stores_explicit_value() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", trace_id="abc123")
    assert request.trace_id == "abc123"


def test_trace_id_round_trips() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", trace_id="run-xyz")
    dumped = request.model_dump()
    assert dumped["trace_id"] == "run-xyz"
    restored = CompletionRequest.model_validate(dumped)
    assert restored.trace_id == "run-xyz"


def test_model_stays_frozen() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", trace_id="run-xyz")
    with pytest.raises(ValidationError):
        request.trace_id = "other"  # type: ignore[misc]


def test_pre_1_4_0_constructor_without_trace_id_still_valid() -> None:
    # The exact pre-NE-166 constructor shape (no trace_id kwarg at all).
    request = CompletionRequest(messages=_MSGS, task="draft", lane=None, params={})
    assert request.trace_id is None
