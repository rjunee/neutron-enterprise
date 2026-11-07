# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``CompletionRequest.requested_model`` — additive carrier (ADR-0020)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts import ChatMessage, CompletionRequest


def _payload() -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "task": "agent",
    }


def test_pre_1_17_payload_defaults_requested_model_to_none() -> None:
    request = CompletionRequest.model_validate(_payload())

    assert request.requested_model is None


def test_requested_model_round_trips_the_exact_registry_alias() -> None:
    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        task="agent",
        requested_model="frontier-anthropic",
    )

    rehydrated = CompletionRequest.model_validate(request.model_dump())

    assert rehydrated.requested_model == "frontier-anthropic"
    assert rehydrated.requested_model == request.requested_model


def test_completion_request_remains_frozen() -> None:
    request = CompletionRequest.model_validate(_payload())

    with pytest.raises(ValidationError):
        request.requested_model = "private-gpu"  # type: ignore[misc]
