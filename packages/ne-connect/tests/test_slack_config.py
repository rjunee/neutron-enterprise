# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackConnectorSettings validation — early, clear failures."""

from __future__ import annotations

import pydantic
import pytest

from ne.connect.slack.config import SlackConnectorSettings

from .slack_transport import slack_settings


def test_empty_bot_token_rejected() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        slack_settings(bot_token="")
    assert "bot_token" in str(excinfo.value)


def test_empty_owner_rejected() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        slack_settings(owner="")
    assert "owner" in str(excinfo.value)


def test_whitespace_owner_rejected() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        slack_settings(owner="   ")
    assert "owner" in str(excinfo.value)


def test_empty_conversation_types_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        slack_settings(conversation_types=())


def test_unsupported_conversation_type_rejected() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        slack_settings(conversation_types=("public_channel", "bogus"))
    assert "bogus" in str(excinfo.value)


def test_valid_settings_accept() -> None:
    settings = slack_settings()
    assert settings.owner == "data-eng@acme.test"
    assert settings.conversation_types == ("public_channel", "private_channel", "mpim")


def test_defaults_are_sane() -> None:
    # Constructed directly (not via the ``slack_settings`` test helper, which
    # overrides the throttle rates for fixture-suite speed) so this locks the
    # PRODUCTION field defaults, incl. the conservative customer-internal-app
    # rate-limit regime documented on the class.
    settings = SlackConnectorSettings(
        bot_token="xoxb-fake-not-real",  # type: ignore[arg-type]
        owner="data-eng@acme.test",
    )
    assert settings.api_base_url == "https://slack.com/api"
    assert settings.api_allowed_hosts == ("slack.com",)
    assert settings.history_requests_per_minute == 30.0
    assert settings.requests_per_minute == 15.0
    assert settings.history_page_size == 200
    assert settings.page_size == 200
    assert settings.include_archived is False
    assert settings.channel_ids == ()


def test_secret_masked_in_repr_and_dump() -> None:
    settings = slack_settings()
    assert "xoxb-fake-not-real" not in repr(settings)
    dumped = settings.model_dump(mode="json")
    assert dumped["bot_token"] == "**********"
