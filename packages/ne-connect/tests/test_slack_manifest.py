# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shipped Slack app manifest — the least-privilege ratchet (NE-045)."""

from __future__ import annotations

from ne.connect.slack.manifest import BOT_SCOPES, load_app_manifest


def test_manifest_loads() -> None:
    manifest = load_app_manifest()
    assert manifest["display_information"]["name"] == "Neutron Enterprise Connector"


def test_bot_scopes_are_exactly_the_seven_read_scopes() -> None:
    manifest = load_app_manifest()
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert scopes == BOT_SCOPES
    expected = {
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "mpim:history",
        "mpim:read",
        "users:read",
    }
    assert expected == BOT_SCOPES


def test_no_write_scope_present() -> None:
    manifest = load_app_manifest()
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert not any("write" in scope for scope in scopes)


def test_no_user_scopes_key() -> None:
    manifest = load_app_manifest()
    assert "user" not in manifest["oauth_config"]["scopes"]


def test_im_scopes_absent() -> None:
    manifest = load_app_manifest()
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert "im:history" not in scopes
    assert "im:read" not in scopes


def test_socket_mode_org_deploy_and_token_rotation_are_off() -> None:
    manifest = load_app_manifest()
    settings = manifest["settings"]
    assert settings["socket_mode_enabled"] is False
    assert settings["org_deploy_enabled"] is False
    assert settings["token_rotation_enabled"] is False
