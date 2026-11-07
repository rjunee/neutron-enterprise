# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsChatSettings — required app_id/app_secret, C12 masking, env prefix."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from ne.chat.teams.config import TeamsChatSettings
from ne.contracts import compute_config_hash


def test_requires_app_id() -> None:
    with pytest.raises(ValueError, match="app_id"):
        TeamsChatSettings(app_secret=SecretStr("s"))


def test_requires_app_secret() -> None:
    with pytest.raises(ValueError, match="app_secret"):
        TeamsChatSettings(app_id="bot")


def test_secrets_masked_in_model_dump_and_config_hash() -> None:
    settings = TeamsChatSettings(app_id="bot", app_secret=SecretStr("super-secret-app-secret"))
    dumped = json.dumps(settings.model_dump(mode="json"))
    assert "super-secret-app-secret" not in dumped
    assert compute_config_hash(settings) == compute_config_hash(settings)


def test_config_hash_ignores_secret_value() -> None:
    a = TeamsChatSettings(app_id="bot", app_secret=SecretStr("one"))
    b = TeamsChatSettings(app_id="bot", app_secret=SecretStr("two"))
    assert compute_config_hash(a) == compute_config_hash(b)


def test_env_prefix_ne_teams_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_TEAMS_CHAT_APP_ID", "env-bot")
    monkeypatch.setenv("NE_TEAMS_CHAT_APP_SECRET", "env-secret")
    settings = TeamsChatSettings()
    assert settings.app_id == "env-bot"
    assert settings.app_secret.get_secret_value() == "env-secret"


def test_defaults() -> None:
    settings = TeamsChatSettings(app_id="bot", app_secret=SecretStr("s"))
    assert settings.tenant_id == "botframework.com"
    assert settings.allowed_issuers == ("https://api.botframework.com",)
    assert settings.service_url_allowed_hosts == ("smba.trafficmanager.net",)
    assert settings.metadata_allowed_hosts == ("login.botframework.com",)
    assert settings.token_scope == "https://api.botframework.com/.default"


def test_jwt_leeway_bounded() -> None:
    with pytest.raises(ValueError, match="jwt_leeway_s"):
        TeamsChatSettings(app_id="bot", app_secret=SecretStr("s"), jwt_leeway_s=999.0)
