# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded wire projection for the governed conversation-disclosure ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ne.runtime.store import ConversationTurn, ConversationTurnPage

# Both bounds are server-owned. The route's Query constraint mirrors the turn
# ceiling for early 422 feedback; this byte ceiling remains authoritative over
# the complete serialized response and cannot be raised by a caller.
MAX_CONVERSATION_TURNS = 100
MAX_CONVERSATION_RESPONSE_BYTES = 1_000_000


class ConversationTurnView(BaseModel):
    """One complete retained turn body and its exact stored attribution."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_name: str
    department: str
    seq: int
    principal_id: str | None
    trace_id: str | None
    attribution: Literal["exact", "legacy-unattributed"]
    messages: list[dict[str, Any]]
    created_at: datetime


class ConversationLedgerView(BaseModel):
    """A whole-turn-only, bounded disclosure window."""

    model_config = ConfigDict(extra="forbid")

    turns: list[ConversationTurnView]
    total_turns: int
    returned_turns: int
    truncated: bool
    retention: Literal["until_governed_reset"] = "until_governed_reset"
    response_byte_limit: int = MAX_CONVERSATION_RESPONSE_BYTES


class ProtectedBindingView(BaseModel):
    """Safe authenticated address metadata; never the protected bytes."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["source_free", "source_bound_container", "legacy_unavailable"]
    protected_ref: str
    protected_hash: str


class _ProtectedConversationTurnBase(BaseModel):
    """Metadata shared by every strictly discriminated protected turn state."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_name: str
    department: str
    seq: int
    principal_id: str
    trace_id: str
    attribution: Literal["exact"] = "exact"
    transport_generation: Literal["agent-transport-v1"] = "agent-transport-v1"
    input: ProtectedBindingView
    terminal: ProtectedBindingView
    created_at: datetime


class AvailableProtectedConversationTurnView(_ProtectedConversationTurnBase):
    content_state: Literal["available"] = "available"
    messages: list[dict[str, Any]]


class WithheldProtectedConversationTurnView(_ProtectedConversationTurnBase):
    content_state: Literal["content_withheld"] = "content_withheld"


class LegacyUnavailableConversationTurnView(_ProtectedConversationTurnBase):
    content_state: Literal["legacy_unavailable"] = "legacy_unavailable"


ProtectedConversationTurnView = Annotated[
    AvailableProtectedConversationTurnView
    | WithheldProtectedConversationTurnView
    | LegacyUnavailableConversationTurnView,
    Field(discriminator="content_state"),
]


class ProtectedConversationLedgerView(BaseModel):
    """Bounded protected-transport disclosure without ambiguous empty bodies."""

    model_config = ConfigDict(extra="forbid")

    turns: list[ProtectedConversationTurnView]
    returned_turns: int
    truncated: bool
    retention: Literal["until_governed_reset"] = "until_governed_reset"


class ConversationTurnTooLargeError(ValueError):
    """The newest selected whole turn cannot fit the response byte ceiling."""


def encoded_protected_ledger(view: ProtectedConversationLedgerView) -> bytes:
    """The exact protected-ledger bytes the HTTP route serves and measures."""
    return view.model_dump_json().encode("utf-8")


def build_protected_conversation_ledger(
    turns: list[ProtectedConversationTurnView],
    *,
    total_turns: int,
    max_response_bytes: int = MAX_CONVERSATION_RESPONSE_BYTES,
) -> ProtectedConversationLedgerView:
    """Fit complete newest protected turns below the authoritative byte cap."""
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be at least 1")
    accepted: list[ProtectedConversationTurnView] = []
    for turn in turns:
        candidate_turns = [*accepted, turn]
        candidate = ProtectedConversationLedgerView(
            turns=candidate_turns,
            returned_turns=len(candidate_turns),
            truncated=total_turns > len(candidate_turns),
        )
        if len(encoded_protected_ledger(candidate)) <= max_response_bytes:
            accepted = candidate_turns
            continue
        if not accepted:
            raise ConversationTurnTooLargeError(
                "newest retained conversation turn exceeds the response byte ceiling"
            )
        break
    view = ProtectedConversationLedgerView(
        turns=accepted,
        returned_turns=len(accepted),
        truncated=total_turns > len(accepted),
    )
    if len(encoded_protected_ledger(view)) > max_response_bytes:
        raise ConversationTurnTooLargeError(
            "conversation ledger metadata exceeds the response byte ceiling"
        )
    return view


def _turn_view(turn: ConversationTurn) -> ConversationTurnView:
    exact = turn.principal_id is not None and turn.trace_id is not None
    return ConversationTurnView(
        session_id=turn.session_id,
        agent_name=turn.agent_name,
        department=turn.department,
        seq=turn.seq,
        principal_id=turn.principal_id,
        trace_id=turn.trace_id,
        attribution="exact" if exact else "legacy-unattributed",
        messages=list(turn.messages),
        created_at=turn.created_at,
    )


def encoded_ledger(view: ConversationLedgerView) -> bytes:
    """The exact bytes the HTTP route serves and the ceiling measures."""
    return view.model_dump_json().encode("utf-8")


def build_conversation_ledger(
    page: ConversationTurnPage,
    *,
    max_response_bytes: int = MAX_CONVERSATION_RESPONSE_BYTES,
) -> ConversationLedgerView:
    """Fit whole newest turns beneath the exact serialized-response ceiling.

    A turn is never sliced. If the first/newest selected turn cannot fit, fail
    honestly rather than returning an empty response that merely says truncated.
    """
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be at least 1")

    accepted: list[ConversationTurnView] = []
    for turn in page.turns:
        candidate_turns = [*accepted, _turn_view(turn)]
        candidate = ConversationLedgerView(
            turns=candidate_turns,
            total_turns=page.total_turns,
            returned_turns=len(candidate_turns),
            truncated=page.total_turns > len(candidate_turns),
            response_byte_limit=max_response_bytes,
        )
        if len(encoded_ledger(candidate)) <= max_response_bytes:
            accepted = candidate_turns
            continue
        if not accepted:
            raise ConversationTurnTooLargeError(
                "newest retained conversation turn exceeds the response byte ceiling"
            )
        break

    view = ConversationLedgerView(
        turns=accepted,
        total_turns=page.total_turns,
        returned_turns=len(accepted),
        truncated=page.total_turns > len(accepted),
        response_byte_limit=max_response_bytes,
    )
    # The final shape can differ from the last candidate only in the empty-page
    # case or after a rejected turn. Prove the exact bytes remain within the cap.
    if len(encoded_ledger(view)) > max_response_bytes:
        raise ConversationTurnTooLargeError(
            "conversation ledger metadata exceeds the response byte ceiling"
        )
    return view
