# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsConnectorSettings validation (NE-080)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ne.connect.teams.config import TeamsConnectorSettings
from ne.contracts.config import compute_config_hash

from .teams_transport import teams_settings


def test_valid_settings_construct() -> None:
    settings = teams_settings()
    assert settings.team_ids == ("T0001",)
    assert settings.include_replies is True
    assert settings.source_name == "teams"


def test_empty_team_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit scoping"):
        teams_settings(team_ids=())


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty owner"):
        teams_settings(owner="")


def test_missing_tenant_or_client_rejected() -> None:
    with pytest.raises(ValidationError, match="tenant_id and client_id"):
        teams_settings(tenant_id="")
    with pytest.raises(ValidationError, match="tenant_id and client_id"):
        teams_settings(client_id="")


def test_secret_mode_requires_secret() -> None:
    with pytest.raises(ValidationError, match="non-empty client_secret"):
        teams_settings(auth_method="secret", client_secret="")


def test_certificate_mode_requires_pem_paths() -> None:
    with pytest.raises(ValidationError, match="certificate_pem_path"):
        teams_settings(auth_method="certificate", client_secret="")


def test_certificate_mode_valid_with_paths() -> None:
    settings = teams_settings(
        auth_method="certificate",
        client_secret="",
        certificate_pem_path="/tmp/cert.pem",
        private_key_pem_path="/tmp/key.pem",
    )
    assert settings.auth_method == "certificate"


def test_client_secret_masked_in_model_dump() -> None:
    settings = teams_settings(client_secret="super-secret-teams-value")
    dumped = settings.model_dump(mode="json")
    assert dumped["client_secret"] == "**********"
    assert "super-secret-teams-value" not in str(dumped)


def test_client_secret_masked_in_config_hash() -> None:
    a = teams_settings(client_secret="secret-one")
    b = teams_settings(client_secret="secret-two")
    assert compute_config_hash(a) == compute_config_hash(b)


def test_defaults() -> None:
    settings = TeamsConnectorSettings(
        tenant_id="t",
        client_id="c",
        auth_method="secret",
        client_secret=SecretStr("s"),
        team_ids=("T0001",),
        owner="o",
    )
    assert settings.graph_base_url == "https://graph.microsoft.com/v1.0"
    assert settings.login_base_url == "https://login.microsoftonline.com"
    assert settings.freshness_sla == "PT1H"
