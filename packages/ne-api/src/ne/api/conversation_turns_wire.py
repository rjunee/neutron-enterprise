# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed wire for canonical PRE-010G-authored Conversation turns."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ne.api.runs_wire import RunView

_CURSOR_DOMAIN = b"ne.api.conversation-turn.v1\x00"


def encode_turn_cursor(sequence: int) -> str:
    body = str(sequence).encode("ascii")
    check = hashlib.sha256(_CURSOR_DOMAIN + body).hexdigest().encode("ascii")
    return base64.urlsafe_b64encode(body + b"." + check).decode("ascii").rstrip("=")


def decode_turn_cursor(token: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, supplied = raw.split(b".", 1)
        expected = hashlib.sha256(_CURSOR_DOMAIN + body).hexdigest().encode("ascii")
        value = int(body.decode("ascii"))
        if value < 1 or not hmac.compare_digest(expected, supplied):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("turn cursor unavailable") from exc


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationTurnView(_Wire):
    turn_id: str
    turn_sequence: int
    user_content_state: Literal["available", "unavailable"]
    user_content: str | None
    run: RunView


class ConversationTurnPage(_Wire):
    items: tuple[ConversationTurnView, ...]
    next_cursor: str | None
