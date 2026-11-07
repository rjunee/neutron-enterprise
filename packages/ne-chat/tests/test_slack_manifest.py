# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chat-app manifest — least-privilege scope ratchet + event/interactivity wiring."""

from __future__ import annotations

from ne.chat.slack.manifest import BOT_SCOPES, EVENT_SUBSCRIPTIONS, load_app_manifest

# History-read scopes belong to the ne-connect connector app, NOT the chat app.
_FORBIDDEN_SCOPES = {"channels:history", "groups:history", "channels:read", "groups:read"}


def test_manifest_parses() -> None:
    manifest = load_app_manifest()
    assert manifest["display_information"]["name"]
    assert manifest["features"]["bot_user"]["display_name"]


def test_bot_scopes_match_least_privilege_ratchet() -> None:
    manifest = load_app_manifest()
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert scopes == set(BOT_SCOPES)
    # No history-read / admin scopes leak into the chat app.
    assert scopes.isdisjoint(_FORBIDDEN_SCOPES)
    assert frozenset({"app_mentions:read", "chat:write", "im:history", "im:read"}) == BOT_SCOPES


def test_event_subscriptions_exact() -> None:
    manifest = load_app_manifest()
    events = set(manifest["settings"]["event_subscriptions"]["bot_events"])
    assert events == set(EVENT_SUBSCRIPTIONS) == {"app_mention", "message.im"}


def test_interactivity_enabled_and_urls_wired() -> None:
    manifest = load_app_manifest()
    settings = manifest["settings"]
    assert settings["interactivity"]["is_enabled"] is True
    assert settings["interactivity"]["request_url"].endswith("/slack/interactions")
    assert settings["event_subscriptions"]["request_url"].endswith("/slack/events")
    assert settings["socket_mode_enabled"] is False
