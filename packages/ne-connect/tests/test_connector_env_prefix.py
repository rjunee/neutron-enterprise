# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Distinct per-connector env_prefix binding + governance parity (NE-FIX-CONNECTOR-ENVPREFIX).

Each of the 7 connector settings classes carries its OWN ``env_prefix`` (NE_JIRA_,
NE_SALESFORCE_, NE_SLACK_, NE_SQL_, NE_TEAMS_, NE_ZENDESK_, NE_M365_) so a shared
field name no longer collides across connectors and an operator following the
platform convention is honored (not silently ignored). These tests lock: the new
prefix binds, the old bare ``NE_`` namespace no longer binds, cross-connector
no-collision, prefix-adjacency safety vs ne-chat, the exact/distinct prefix set,
config_hash byte-parity across the prefix change (env_prefix never enters the
preimage), TOML unaffected, and the production ``build_sync_settings`` env path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

from ne.connect.jira.config import JiraConnectorSettings, JiraProjectConfig
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.runner import SyncNotConfiguredError, build_sync_settings
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.slack.config import SlackConnectorSettings
from ne.connect.sql.config import SqlConnectorSettings, SqlTableAclConfig, SqlTableConfig
from ne.connect.teams.config import TeamsConnectorSettings
from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.contracts.config import compute_config_hash

# The 7 connector settings classes keyed by prefix-less source name.
_CLASSES = {
    "jira": JiraConnectorSettings,
    "salesforce": SalesforceConnectorSettings,
    "slack": SlackConnectorSettings,
    "sql": SqlConnectorSettings,
    "teams": TeamsConnectorSettings,
    "zendesk": ZendeskConnectorSettings,
    "m365": M365ConnectorSettings,
}

# The expected distinct, conventional env prefix per connector.
_PREFIXES = {
    "jira": "NE_JIRA_",
    "salesforce": "NE_SALESFORCE_",
    "slack": "NE_SLACK_",
    "sql": "NE_SQL_",
    "teams": "NE_TEAMS_",
    "zendesk": "NE_ZENDESK_",
    "m365": "NE_M365_",
}

# FULL minimal valid env under the NEW prefix, one dict per connector.
_ENV: dict[str, dict[str, str]] = {
    "jira": {
        "NE_JIRA_BASE_URL": "https://acme.atlassian.net",
        "NE_JIRA_EMAIL": "ops@acme.test",
        "NE_JIRA_API_TOKEN": "tok",
        "NE_JIRA_OWNER": "data-eng",
        "NE_JIRA_PROJECTS": '[{"key": "ACME"}]',
    },
    "zendesk": {
        "NE_ZENDESK_BASE_URL": "https://acme.zendesk.com",
        "NE_ZENDESK_EMAIL": "ops@acme.test",
        "NE_ZENDESK_API_TOKEN": "tok",
        "NE_ZENDESK_OWNER": "data-eng",
    },
    "slack": {
        "NE_SLACK_BOT_TOKEN": "xoxb-1",
        "NE_SLACK_OWNER": "data-eng",
    },
    "teams": {
        "NE_TEAMS_TENANT_ID": "tid",
        "NE_TEAMS_CLIENT_ID": "cid",
        "NE_TEAMS_OWNER": "data-eng",
        "NE_TEAMS_TEAM_IDS": '["team-1"]',
        "NE_TEAMS_AUTH_METHOD": "secret",
        "NE_TEAMS_CLIENT_SECRET": "s",
    },
    "m365": {
        "NE_M365_TENANT_ID": "tid",
        "NE_M365_CLIENT_ID": "cid",
        "NE_M365_OWNER": "data-eng",
        "NE_M365_DRIVE_IDS": '["d1"]',
        "NE_M365_AUTH_METHOD": "secret",
        "NE_M365_CLIENT_SECRET": "s",
    },
    "salesforce": {
        "NE_SALESFORCE_CLIENT_ID": "cid",
        "NE_SALESFORCE_OWNER": "data-eng",
        "NE_SALESFORCE_INSTANCE_URL": "https://acme.my.salesforce.com",
        "NE_SALESFORCE_AUTH_METHOD": "client_credentials",
        "NE_SALESFORCE_CLIENT_SECRET": "s",
        "NE_SALESFORCE_OBJECTS": '[{"name": "Account", "content_fields": ["Name"]}]',
    },
    "sql": {
        "NE_SQL_CONNECTION_URL": "postgresql://db.internal:5432/app",
        "NE_SQL_OWNER": "data-eng",
        "NE_SQL_TABLES": (
            '[{"table": "docs", "id_column": "id", "content_columns": ["body"], '
            '"acl": {"principals": ["alice@example.com"]}}]'
        ),
    },
}

# Equivalent init-kwargs per connector (typed objects; SecretStr for secrets).
_KWARGS: dict[str, dict[str, object]] = {
    "jira": {
        "base_url": "https://acme.atlassian.net",
        "email": "ops@acme.test",
        "api_token": SecretStr("tok"),
        "owner": "data-eng",
        "projects": (JiraProjectConfig(key="ACME"),),
    },
    "zendesk": {
        "base_url": "https://acme.zendesk.com",
        "email": "ops@acme.test",
        "api_token": SecretStr("tok"),
        "owner": "data-eng",
    },
    "slack": {
        "bot_token": SecretStr("xoxb-1"),
        "owner": "data-eng",
    },
    "teams": {
        "tenant_id": "tid",
        "client_id": "cid",
        "owner": "data-eng",
        "team_ids": ("team-1",),
        "auth_method": "secret",
        "client_secret": SecretStr("s"),
    },
    "m365": {
        "tenant_id": "tid",
        "client_id": "cid",
        "owner": "data-eng",
        "drive_ids": ("d1",),
        "auth_method": "secret",
        "client_secret": SecretStr("s"),
    },
    "salesforce": {
        "client_id": "cid",
        "owner": "data-eng",
        "instance_url": "https://acme.my.salesforce.com",
        "auth_method": "client_credentials",
        "client_secret": SecretStr("s"),
        "objects": (SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
    },
    "sql": {
        "connection_url": SecretStr("postgresql://db.internal:5432/app"),
        "owner": "data-eng",
        "tables": (
            SqlTableConfig(
                table="docs",
                id_column="id",
                content_columns=("body",),
                acl=SqlTableAclConfig(principals=("alice@example.com",)),
            ),
        ),
    },
}

# (attr, expected) — one connector-specific NON-secret field per connector.
_SPECIFIC: dict[str, tuple[str, str]] = {
    "jira": ("base_url", "https://acme.atlassian.net"),
    "zendesk": ("base_url", "https://acme.zendesk.com"),
    "slack": ("source_name", "slack"),
    "teams": ("tenant_id", "tid"),
    "m365": ("tenant_id", "tid"),
    "salesforce": ("instance_url", "https://acme.my.salesforce.com"),
    "sql": ("source_name", "sql"),
}

# (attr, expected secret material) — one SecretStr field per connector.
_SECRET: dict[str, tuple[str, str]] = {
    "jira": ("api_token", "tok"),
    "zendesk": ("api_token", "tok"),
    "slack": ("bot_token", "xoxb-1"),
    "teams": ("client_secret", "s"),
    "m365": ("client_secret", "s"),
    "salesforce": ("client_secret", "s"),
    "sql": ("connection_url", "postgresql://db.internal:5432/app"),
}

_CONNECTORS = list(_CLASSES)


# The 7 old-bare subclasses: the pre-fix bare NE_ prefix, used ONLY to prove
# config_hash byte-parity (env_prefix never enters the model_dump preimage).
_BARE = SettingsConfigDict(
    env_prefix="NE_",
    env_nested_delimiter="__",
    extra="forbid",
    toml_file=Path("ne.toml"),
)


class _OldBareJira(JiraConnectorSettings):
    model_config = _BARE


class _OldBareSalesforce(SalesforceConnectorSettings):
    model_config = _BARE


class _OldBareSlack(SlackConnectorSettings):
    model_config = _BARE


class _OldBareSql(SqlConnectorSettings):
    model_config = _BARE


class _OldBareTeams(TeamsConnectorSettings):
    model_config = _BARE


class _OldBareZendesk(ZendeskConnectorSettings):
    model_config = _BARE


class _OldBareM365(M365ConnectorSettings):
    model_config = _BARE


_OLD_BARE = {
    "jira": _OldBareJira,
    "salesforce": _OldBareSalesforce,
    "slack": _OldBareSlack,
    "sql": _OldBareSql,
    "teams": _OldBareTeams,
    "zendesk": _OldBareZendesk,
    "m365": _OldBareM365,
}


@pytest.fixture(autouse=True)
def _scrub_ne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every ``NE_``-prefixed env var so ambient config never leaks in."""
    for name in [k for k in os.environ if k.startswith("NE_")]:
        monkeypatch.delenv(name)


def _bare_key(prefix: str, key: str) -> str:
    """The collision-prone bare name for a prefixed env var (``NE_JIRA_X`` -> ``NE_X``)."""
    return "NE_" + key[len(prefix) :]


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_new_prefixed_env_binds(connector: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _ENV[connector].items():
        monkeypatch.setenv(key, value)
    settings = _CLASSES[connector]()
    assert settings.owner == "data-eng"
    attr, expected = _SPECIFIC[connector]
    assert getattr(settings, attr) == expected
    secret_attr, secret_value = _SECRET[connector]
    assert getattr(settings, secret_attr).get_secret_value() == secret_value


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_old_bare_env_no_longer_binds(connector: str, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = _PREFIXES[connector]
    for key, value in _ENV[connector].items():
        monkeypatch.setenv(_bare_key(prefix, key), value)
    with pytest.raises(ValidationError):
        _CLASSES[connector]()


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_default_construction_still_fails_loud(connector: str) -> None:
    with pytest.raises(ValidationError):
        _CLASSES[connector]()


def test_cross_connector_no_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _ENV["jira"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NE_JIRA_OWNER", "jira-owner")
    for key, value in _ENV["zendesk"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NE_ZENDESK_OWNER", "zendesk-owner")
    # Bare shared field names must feed NEITHER connector.
    monkeypatch.setenv("NE_OWNER", "bare-owner")
    monkeypatch.setenv("NE_BASE_URL", "https://bare.example.com")

    jira = JiraConnectorSettings()
    zendesk = ZendeskConnectorSettings()

    assert jira.owner == "jira-owner"
    assert zendesk.owner == "zendesk-owner"
    assert jira.base_url == "https://acme.atlassian.net"
    assert zendesk.base_url == "https://acme.zendesk.com"


def test_chat_namespaces_do_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # NE_TEAMS_ (connector) is prefix-adjacent to ne-chat's NE_TEAMS_CHAT_.
    for key, value in _ENV["teams"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NE_TEAMS_CHAT_APP_ID", "zzz")
    teams = TeamsConnectorSettings()
    assert teams.tenant_id == "tid"

    for key, value in _ENV["slack"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NE_SLACK_CHAT_LIVE", "1")
    slack = SlackConnectorSettings()
    assert slack.bot_token.get_secret_value() == "xoxb-1"


def test_env_prefixes_distinct_and_conventional() -> None:
    resolved = {name: cls.model_config["env_prefix"] for name, cls in _CLASSES.items()}
    assert resolved == _PREFIXES
    prefixes = list(resolved.values())
    assert len(prefixes) == len(set(prefixes))  # pairwise distinct
    reserved = {"NE_TEAMS_CHAT_", "NE_CHAT_", "NE_CONNECT_SYNC_", "NE_CONNECT_FIXTURE_", "NE_"}
    assert reserved.isdisjoint(prefixes)


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_config_hash_parity_across_prefix_change(connector: str) -> None:
    kwargs = _KWARGS[connector]
    new = _CLASSES[connector](**kwargs)
    old = _OLD_BARE[connector](**kwargs)
    assert compute_config_hash(new) == compute_config_hash(old)


def test_toml_keys_unaffected_by_env_prefix(tmp_path: Path) -> None:
    cfg = tmp_path / "ne.toml"
    cfg.write_text(
        'base_url = "https://acme.zendesk.com"\n'
        'email = "ops@acme.test"\n'
        'api_token = "tok"\n'
        'owner = "data-eng"\n'
    )
    settings = ZendeskConnectorSettings.from_config_file(cfg)
    assert settings.base_url == "https://acme.zendesk.com"
    assert settings.owner == "data-eng"
    assert settings.api_token.get_secret_value() == "tok"


def test_build_sync_settings_binds_new_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _ENV["zendesk"].items():
        monkeypatch.setenv(key, value)
    settings = build_sync_settings("zendesk")
    assert settings.base_url == "https://acme.zendesk.com"
    assert settings.owner == "data-eng"


def test_build_sync_settings_bare_env_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_BASE_URL", "https://acme.zendesk.com")
    monkeypatch.setenv("NE_EMAIL", "ops@acme.test")
    monkeypatch.setenv("NE_API_TOKEN", "tok")
    monkeypatch.setenv("NE_OWNER", "data-eng")
    with pytest.raises(SyncNotConfiguredError):
        build_sync_settings("zendesk")
