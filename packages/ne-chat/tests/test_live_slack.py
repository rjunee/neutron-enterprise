# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED real-workspace end-to-end (NE-000).

The entire module is skipped unless ``NE_SLACK_CHAT_LIVE=1``. CI never touches
live Slack; this documents the manual e2e that a human runs against the NE-000
owner-provisioned workspace.

Manual e2e script (real workspace):

1. Create the CHAT app from ``src/ne/chat/slack/manifest.json`` (a SECOND app,
   separate from the ne-connect connector app). Set ``NE_CHAT_SIGNING_SECRET`` /
   ``NE_CHAT_BOT_TOKEN`` / ``NE_CHAT_IDENTITY_LINKS`` / ``NE_CHAT_APPROVER_ROLES``.
2. Point the app's event URL at ``…/slack/events`` and interactivity URL at
   ``…/slack/interactions`` on a deployed ingress.
3. From Slack, hold a governed conversation (DM the bot) — observe the streamed
   ``chat.update`` edits and confirm the audit trail shows ``pdp.decide`` +
   ``tool.execute``.
4. Trigger a Tier-3 action, observe the mirrored approval card, and action it
   from Slack AS THE MAPPED NAMED APPROVER — confirm the gate row records the
   platform ``principal.id`` (not the Slack id) and the second distinct approver
   completes it.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("NE_SLACK_CHAT_LIVE"),
    reason="live Slack workspace e2e is LIVE-PARKED (NE-000)",
)


def test_live_signed_event_round_trip() -> None:  # pragma: no cover - live only
    raise AssertionError("live e2e is manual; see the module docstring")
