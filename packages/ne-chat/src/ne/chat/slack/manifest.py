# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shipped customer-internal Slack CHAT app manifest (NE-063).

This is a SECOND, SEPARATE Slack app from the ne-connect data-connector app: the
connector READS channel history (``channels:history`` etc.); this chat app is the
WRITE-side conversational surface. ``manifest.json`` is the JSON a customer pastes
into ``api.slack.com/apps -> Create New App -> From an app manifest`` to create
their OWN internal chat app.

:data:`BOT_SCOPES` is the single source of truth the least-privilege ratchet test
locks the manifest's ``oauth_config.scopes.bot`` against: exactly the four
WRITE-side / DM scopes below. There is deliberately NO ``channels:history`` /
``groups:history`` — reading channel history is the connector's job, not the chat
surface's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["BOT_SCOPES", "EVENT_SUBSCRIPTIONS", "MANIFEST_PATH", "load_app_manifest"]

MANIFEST_PATH = Path(__file__).parent / "manifest.json"

BOT_SCOPES: frozenset[str] = frozenset(
    {
        "app_mentions:read",
        "chat:write",
        "im:history",
        "im:read",
    }
)

#: The exact bot event subscriptions the chat app requires.
EVENT_SUBSCRIPTIONS: frozenset[str] = frozenset({"app_mention", "message.im"})


def load_app_manifest() -> dict[str, Any]:
    """Parse and return the shipped chat-app manifest JSON."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
