# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure parser for Slack interactivity (block_actions) payloads.

Slack posts interactivity as ``application/x-www-form-urlencoded`` with a single
``payload`` field holding a JSON document. This module parses ONLY the
``block_actions`` shape we surface (the approve/deny buttons) into a frozen model.

No trust decisions are made here — parsing runs AFTER signature verification. A
malformed or unrecognized shape returns ``None`` so the caller can ack 200 and
ignore it (Slack otherwise retries).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from ne.chat.slack.blocks import APPROVE_ACTION_ID, DENY_ACTION_ID

__all__ = ["SlackBlockAction", "parse_block_action"]

_KNOWN_ACTION_IDS = {APPROVE_ACTION_ID, DENY_ACTION_ID}


class SlackBlockAction(BaseModel):
    """A parsed, trusted-shape Slack ``block_actions`` interaction."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    action_id: str
    value: str
    channel_id: str
    message_ts: str
    response_url: str


def parse_block_action(form_body: Mapping[str, str]) -> SlackBlockAction | None:
    """Parse a ``block_actions`` interaction, or return ``None`` for any other shape."""
    raw = form_body.get("payload")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None

    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    action = actions[0]
    if not isinstance(action, dict):
        return None
    action_id = action.get("action_id")
    if action_id not in _KNOWN_ACTION_IDS:
        return None

    user = payload.get("user")
    channel = payload.get("channel")
    message = payload.get("message")
    user_id = user.get("id") if isinstance(user, dict) else None
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    message_ts = message.get("ts") if isinstance(message, dict) else None
    value = action.get("value")
    response_url = payload.get("response_url")

    if not (
        isinstance(user_id, str)
        and isinstance(value, str)
        and isinstance(channel_id, str)
        and isinstance(message_ts, str)
        and isinstance(response_url, str)
    ):
        return None

    return SlackBlockAction(
        user_id=user_id,
        action_id=action_id,
        value=value,
        channel_id=channel_id,
        message_ts=message_ts,
        response_url=response_url,
    )
