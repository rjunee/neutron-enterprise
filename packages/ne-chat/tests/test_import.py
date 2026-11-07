# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne.chat imports cleanly and registers its chat_adapter entry point."""

from __future__ import annotations

import ne.chat
import ne.chat.slack.adapter
import ne.chat.teams.adapter
from ne.contracts.plugins import iter_plugins


def test_package_version() -> None:
    assert ne.chat.__version__ == "0.1.0"


def test_public_surface_importable() -> None:
    assert ne.chat.ConversationService is not None
    assert ne.chat.ChatIdentityLinker is not None
    assert ne.chat.ApprovalActionService is not None
    assert issubclass(ne.chat.SignatureVerificationError, ne.chat.ChatError)


def test_slack_entry_point_resolvable() -> None:
    plugins = iter_plugins("chat_adapter")
    assert "slack" in plugins
    loaded = plugins["slack"].load()
    assert loaded is ne.chat.slack.adapter.SlackChatAdapter


def test_teams_entry_point_resolvable() -> None:
    plugins = iter_plugins("chat_adapter")
    assert "teams" in plugins
    loaded = plugins["teams"].load()
    assert loaded is ne.chat.teams.adapter.TeamsChatAdapter
