# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ZendeskConnectorSettings validation (NE-093)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.contracts.config import compute_config_hash

from .zendesk_transport import zendesk_settings


def test_valid_settings_construct() -> None:
    settings = zendesk_settings()
    assert settings.sync_tickets and settings.sync_articles
    assert settings.allowed_hosts == (".zendesk.com",)


def test_empty_base_url_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty base_url"):
        zendesk_settings(base_url="")


def test_empty_email_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty email"):
        zendesk_settings(email="")


def test_empty_api_token_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty api_token"):
        zendesk_settings(api_token="")


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty owner"):
        zendesk_settings(owner="")


def test_at_least_one_stream_required() -> None:
    with pytest.raises(ValidationError, match="at least one of sync_tickets"):
        zendesk_settings(sync_tickets=False, sync_articles=False)


def test_single_stream_is_allowed() -> None:
    assert zendesk_settings(sync_articles=False).sync_tickets
    assert zendesk_settings(sync_tickets=False).sync_articles


def test_api_token_masked_in_model_dump() -> None:
    settings = zendesk_settings(api_token="super-secret-zendesk-token")
    dumped = settings.model_dump(mode="json")
    assert dumped["api_token"] == "**********"
    assert "super-secret-zendesk-token" not in str(dumped)


def test_api_token_masked_in_config_hash() -> None:
    a = zendesk_settings(api_token="secret-one")
    b = zendesk_settings(api_token="secret-two")
    assert compute_config_hash(a) == compute_config_hash(b)


def test_defaults() -> None:
    settings = ZendeskConnectorSettings(
        base_url="https://acme.zendesk.com",
        email="e@acme.test",
        api_token=SecretStr("t"),
        owner="o",
    )
    assert settings.tickets_start_time == 1
    assert settings.articles_start_time == 1
    assert settings.include_ticket_comments is True
