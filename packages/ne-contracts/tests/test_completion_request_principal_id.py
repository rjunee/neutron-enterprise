# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CompletionRequest.principal_id — additive, optional, frozen (ADR-0018, 1.15.0).

The calling principal's opaque identifier is carried AS DATA into the
gateway. It must be optional (default None so every pre-1.15.0 constructor
stays valid), settable explicitly, and stay frozen (Checkpoint-A immutability).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.models import CompletionRequest
from ne.contracts.types import ChatMessage

_MSGS = (ChatMessage(role="user", content="hi"),)


def test_principal_id_defaults_to_none() -> None:
    # Backward-compat construction: the pre-1.15.0 kwarg set stays valid.
    request = CompletionRequest(messages=_MSGS, task="draft")
    assert request.principal_id is None


def test_principal_id_stores_explicit_value() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", principal_id="user:ada")
    assert request.principal_id == "user:ada"


def test_principal_id_round_trips() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", principal_id="user:bob")
    dumped = request.model_dump()
    assert dumped["principal_id"] == "user:bob"
    restored = CompletionRequest.model_validate(dumped)
    assert restored.principal_id == "user:bob"


def test_model_stays_frozen() -> None:
    request = CompletionRequest(messages=_MSGS, task="draft", principal_id="user:ada")
    with pytest.raises(ValidationError):
        request.principal_id = "other"  # type: ignore[misc]


def test_pre_1_15_0_payload_without_principal_id_key_validates_as_none() -> None:
    """Backward compat: a payload dict serialized before this change (no
    ``principal_id`` key at all) must still validate, with the field defaulting
    to None — old rows/producers are never rewritten or invalidated."""
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "task": "draft",
        "lane": None,
        "params": {},
        "trace_id": None,
        # no "principal_id" key present
    }
    restored = CompletionRequest.model_validate(payload)
    assert restored.principal_id is None
