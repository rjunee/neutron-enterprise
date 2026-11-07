# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JiraConnectorSettings / JiraProjectConfig validation (NE-093)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ne.connect.jira.config import JiraConnectorSettings, JiraProjectConfig
from ne.contracts.config import compute_config_hash

from .jira_transport import jira_settings


def test_valid_settings_construct() -> None:
    settings = jira_settings()
    assert settings.projects
    assert settings.allowed_hosts == (".atlassian.net",)


def test_empty_base_url_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty base_url"):
        jira_settings(base_url="")


def test_empty_email_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty email"):
        jira_settings(email="")


def test_empty_api_token_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty api_token"):
        jira_settings(api_token="")


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty owner"):
        jira_settings(owner="")


def test_empty_projects_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one configured project"):
        jira_settings(projects=())


def test_duplicate_project_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="unique keys"):
        jira_settings(projects=(JiraProjectConfig(key="ACME"), JiraProjectConfig(key="ACME")))


@pytest.mark.parametrize(
    "bad_key",
    [
        "ACME OR project=SECRET",  # JQL injection shape
        "acme",  # lowercase
        "A",  # too short (needs >= 2 chars)
        "1ACME",  # leading digit
        "ACME-1",  # hyphen
        'ACME" OR "1"="1',  # quote injection
    ],
)
def test_jql_injection_shaped_project_key_rejected(bad_key: str) -> None:
    with pytest.raises(ValidationError, match="JQL-injection guard"):
        JiraProjectConfig(key=bad_key)


def test_valid_project_keys_accepted() -> None:
    for key in ("ACME", "SECRET", "PROJ_2", "ABC123"):
        assert JiraProjectConfig(key=key).key == key


def test_bad_issue_field_name_rejected() -> None:
    with pytest.raises(ValidationError, match="valid bare Jira field name"):
        jira_settings(issue_fields=("summary", "status.name"))


def test_api_token_masked_in_model_dump() -> None:
    settings = jira_settings(api_token="super-secret-jira-token")
    dumped = settings.model_dump(mode="json")
    assert dumped["api_token"] == "**********"
    assert "super-secret-jira-token" not in str(dumped)


def test_api_token_masked_in_config_hash() -> None:
    a = jira_settings(api_token="secret-one")
    b = jira_settings(api_token="secret-two")
    assert compute_config_hash(a) == compute_config_hash(b)


def test_project_config_stream_toggles_default_true() -> None:
    cfg = JiraProjectConfig(key="ACME")
    assert cfg.include_comments is True
    assert cfg.include_sprints is True


def test_settings_are_frozen() -> None:
    settings = JiraConnectorSettings(
        base_url="https://acme.atlassian.net",
        email="e@acme.test",
        api_token=SecretStr("t"),
        owner="o",
        projects=(JiraProjectConfig(key="ACME"),),
    )
    assert settings.page_size == 100
