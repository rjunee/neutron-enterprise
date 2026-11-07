# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED real-tenant Teams end-to-end (NE-000).

The entire module is skipped unless ``NE_TEAMS_CHAT_LIVE=1``. CI never touches a
live tenant or Azure Bot; this documents the manual e2e a human runs against the
NE-000 owner-provisioned dev tenant after Microsoft protected-API approval.

Manual e2e script (real Azure Bot + Teams):

1. Create an Azure Bot resource; capture its Microsoft App ID + secret. Set
   ``NE_TEAMS_CHAT_APP_ID`` / ``NE_TEAMS_CHAT_APP_SECRET`` /
   ``NE_TEAMS_CHAT_IDENTITY_LINKS`` (``{aad_object_id: platform_subject}``). For a
   SINGLE-TENANT bot also set ``NE_TEAMS_CHAT_TENANT_ID`` to the directory id and
   append the ``login.microsoftonline.com/{tid}/v2.0`` issuer to
   ``NE_TEAMS_CHAT_ALLOWED_ISSUERS``.
2. Point the bot's messaging endpoint at ``…/teams/messages`` on a deployed
   ingress and enable the Microsoft Teams channel; sideload the bot into a team.
3. From Teams, @mention the bot in a channel and hold a governed conversation —
   observe the placeholder activity then the edited answer, and confirm the audit
   trail shows ``pdp.decide`` + ``tool.execute``.
4. Trigger a Tier-3 action and confirm the VIEW-ONLY approval prompt renders with
   the request id (actioning is from the console or Slack in v1).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("NE_TEAMS_CHAT_LIVE"),
    reason="live Teams tenant e2e is LIVE-PARKED (NE-000)",
)


def test_live_signed_activity_round_trip() -> None:  # pragma: no cover - live only
    raise AssertionError("live e2e is manual; see the module docstring")
