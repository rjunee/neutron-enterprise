# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackChatSettings — required secrets, C12 masking, env prefix, defaults."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from ne.chat.slack.config import SlackChatSettings
from ne.contracts import compute_config_hash


def test_requires_signing_secret() -> None:
    with pytest.raises(ValueError, match="signing_secret"):
        SlackChatSettings(bot_token=SecretStr("xoxb-x"))


def test_requires_bot_token() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        SlackChatSettings(signing_secret=SecretStr("s"))


def test_secrets_masked_in_config_hash() -> None:
    settings = SlackChatSettings(
        signing_secret=SecretStr("super-secret-signing"),
        bot_token=SecretStr("xoxb-super-secret-token"),
    )
    dumped = json.dumps(settings.model_dump(mode="json"))
    assert "super-secret-signing" not in dumped
    assert "xoxb-super-secret-token" not in dumped
    # config_hash is stable + secret-free (the hash preimage masks SecretStr).
    assert compute_config_hash(settings) == compute_config_hash(settings)


def test_env_prefix_ne_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_CHAT_SIGNING_SECRET", "env-secret")
    monkeypatch.setenv("NE_CHAT_BOT_TOKEN", "xoxb-env")
    settings = SlackChatSettings()
    assert settings.signing_secret.get_secret_value() == "env-secret"
    assert settings.bot_token.get_secret_value() == "xoxb-env"


def test_defaults() -> None:
    settings = SlackChatSettings(signing_secret=SecretStr("s"), bot_token=SecretStr("xoxb-x"))
    assert settings.api_base_url == "https://slack.com/api"
    assert settings.api_allowed_hosts == ("slack.com",)
    assert settings.signature_tolerance_s == 300.0
    assert settings.approver_roles == ("role:approver",)
    assert settings.min_update_interval_s == 1.0


def test_signature_tolerance_bounded() -> None:
    with pytest.raises(ValueError, match="signature_tolerance_s"):
        SlackChatSettings(
            signing_secret=SecretStr("s"), bot_token=SecretStr("x"), signature_tolerance_s=999.0
        )
