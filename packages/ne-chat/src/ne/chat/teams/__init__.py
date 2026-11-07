# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Teams-specific implementation of the ChatAdapter contract (NE-080).

Everything under this subpackage is Teams-specific (Bot Framework activity JWT
validation, Connector-API client, serviceUrl refs, ingress route, adapter). The
channel-agnostic services live at the :mod:`ne.chat` root (ConversationService /
ChatIdentityLinker), so a Teams message runs the SAME governed C6/C7/HITL turn
path Slack uses. The real-tenant round trip is LIVE-PARKED behind an NE-000 dev
tenant + an Azure Bot resource (see the README runbook); CI is fixture-first.
"""

from __future__ import annotations

from ne.chat.teams.adapter import TeamsChatAdapter
from ne.chat.teams.config import TeamsChatSettings
from ne.chat.teams.ingress import create_teams_ingress

__all__ = ["TeamsChatAdapter", "TeamsChatSettings", "create_teams_ingress"]
