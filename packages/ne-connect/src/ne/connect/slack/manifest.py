# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shipped customer-internal Slack app manifest (NE-045).

``manifest.json`` is the JSON a customer pastes into
``api.slack.com/apps -> Create New App -> From an app manifest`` to create their
OWN internal app (see the README install steps). :data:`BOT_SCOPES` is the
single source of truth the least-privilege ratchet test locks the manifest's
``oauth_config.scopes.bot`` against: exactly the 7 read scopes below, no write
scope of any kind, and no ``im:history``/``im:read`` (1:1 DMs are out of scope
by default — enabling them requires editing both the manifest and
``conversation_types``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["BOT_SCOPES", "MANIFEST_PATH", "load_app_manifest"]

MANIFEST_PATH = Path(__file__).parent / "manifest.json"

BOT_SCOPES: frozenset[str] = frozenset(
    {
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "mpim:history",
        "mpim:read",
        "users:read",
    }
)


def load_app_manifest() -> dict[str, Any]:
    """Parse and return the shipped manifest JSON."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
