# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceConnectorSettings / SalesforceObjectConfig validation (NE-047)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.contracts.config import compute_config_hash

from .salesforce_transport import salesforce_settings


def _base_object(**overrides: object) -> SalesforceObjectConfig:
    kwargs: dict[str, object] = {"name": "Account", "content_fields": ("Name",)}
    kwargs.update(overrides)
    return SalesforceObjectConfig(**kwargs)  # type: ignore[arg-type]


def test_valid_settings_construct() -> None:
    settings = salesforce_settings()
    assert settings.objects


def test_empty_objects_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one configured object"):
        salesforce_settings(objects=())


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty owner"):
        salesforce_settings(owner="")


def test_empty_client_id_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty client_id"):
        salesforce_settings(client_id="")


def test_empty_instance_url_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty instance_url"):
        salesforce_settings(instance_url="")


def test_jwt_bearer_without_username_rejected() -> None:
    with pytest.raises(ValidationError, match="jwt_bearer"):
        salesforce_settings(
            auth_method="jwt_bearer", username="", private_key_pem_path="/tmp/key.pem"
        )


def test_jwt_bearer_without_key_path_rejected() -> None:
    with pytest.raises(ValidationError, match="jwt_bearer"):
        salesforce_settings(
            auth_method="jwt_bearer", username="int-user@acme.test", private_key_pem_path=None
        )


def test_client_credentials_without_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="client_credentials"):
        salesforce_settings(auth_method="client_credentials", client_secret="")


def test_duplicate_object_names_rejected() -> None:
    with pytest.raises(ValidationError, match="unique object names"):
        salesforce_settings(objects=(_base_object(), _base_object()))


def test_bad_api_version_rejected() -> None:
    with pytest.raises(ValidationError, match="api_version"):
        salesforce_settings(api_version="65.0")


@pytest.mark.parametrize("bad_api_version", ["v65", "v65.0.1", "65.0", "vX.0", ""])
def test_various_bad_api_versions_rejected(bad_api_version: str) -> None:
    with pytest.raises(ValidationError):
        salesforce_settings(api_version=bad_api_version)


def test_soql_injection_shaped_object_name_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid bare Salesforce API name"):
        _base_object(name="Account WHERE 1=1")


def test_soql_injection_shaped_field_name_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid bare Salesforce API name"):
        _base_object(content_fields=("Name FROM Account--",))


def test_relationship_field_dotted_name_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid bare Salesforce API name"):
        _base_object(content_fields=("Account.Name",))


def test_empty_content_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        SalesforceObjectConfig(name="Account", content_fields=())


def test_client_secret_masked_in_model_dump() -> None:
    settings = salesforce_settings(
        auth_method="client_credentials", client_secret="super-secret-value"
    )
    dumped = settings.model_dump(mode="json")
    assert dumped["client_secret"] == "**********"
    assert "super-secret-value" not in str(dumped)


def test_client_secret_masked_in_config_hash() -> None:
    settings_a = salesforce_settings(auth_method="client_credentials", client_secret="secret-one")
    settings_b = salesforce_settings(auth_method="client_credentials", client_secret="secret-two")
    # Only the secret VALUE differs — the hash is identical because SecretStr
    # renders as "**********" in the canonicalized preimage (C12).
    assert compute_config_hash(settings_a) == compute_config_hash(settings_b)


def test_default_settings_use_jwt_bearer() -> None:
    settings = SalesforceConnectorSettings(
        client_id="c",
        owner="o",
        instance_url="https://acme.my.salesforce.com",
        username="u",
        private_key_pem_path="/tmp/does-not-need-to-exist.pem",
        objects=(_base_object(),),
    )
    assert settings.auth_method == "jwt_bearer"


def test_instance_allowed_hosts_default_is_dot_suffix() -> None:
    settings = salesforce_settings()
    assert settings.instance_allowed_hosts == (
        ".my.salesforce.com",
        ".sandbox.my.salesforce.com",
    )
    assert settings.source_link_allowed_hosts == (
        ".my.salesforce.com",
        ".sandbox.my.salesforce.com",
    )


@pytest.mark.parametrize(
    "field", ("rest_max_result_pages", "bulk_max_result_pages", "bulk_max_records_per_job")
)
def test_salesforce_result_bounds_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        salesforce_settings(**{field: 0})


def test_authority_enabled_requires_an_explicit_bounded_sweep() -> None:
    with pytest.raises(ValidationError, match="explicit sub-SLA Salesforce permission sweep"):
        salesforce_settings(authority_enabled=True)


def test_authority_sweep_settings_construct_when_complete() -> None:
    settings = salesforce_settings(
        authority_enabled=True,
        authority_sweep_cadence_seconds=60,
        authority_sweep_batch_size=10,
        authority_sweep_max_batch_duration_seconds=5,
        authority_sweep_max_points_per_scope=100,
    )
    assert settings.authority_enabled is True
    assert settings.authority_sweep_batch_size == 10
