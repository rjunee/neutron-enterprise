# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Credential-free M365 SourceContractIdentity factory controls."""

from __future__ import annotations

import inspect
import math

import pytest

from ne.connect import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    PiiClass,
    m365_source_contract_identity,
)


def _contract(**overrides: object) -> OdcsDataContract:
    values: dict[str, object] = {
        "source": "m365-sharepoint",
        "owner": "knowledge-platform",
        "schema_def": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
        "freshness_sla": "PT15M",
        "pii": PiiClass.SENSITIVE,
        "permissions": PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            principal_namespace="oidc",
        ),
    }
    values.update(overrides)
    return OdcsDataContract(**values)  # type: ignore[arg-type]


def _identity(**overrides: object):
    values: dict[str, object] = {
        "contract": _contract(),
        "mapper_revision": "mapper-v1",
        "config_revision": "config-v1",
    }
    values.update(overrides)
    return m365_source_contract_identity(**values)  # type: ignore[arg-type]


def test_factory_is_deterministic_and_credential_free() -> None:
    first = _identity()
    second = _identity()
    assert first == second
    assert first.source_kind == "m365-sharepoint"
    dumped = str(first.model_dump()).lower()
    assert "secret" not in dumped
    assert "credential" not in dumped
    assert "token" not in dumped


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "other-owner"),
        ("schema_def", {"type": "object", "properties": {"other": {"type": "string"}}}),
        ("freshness_sla", "PT16M"),
        ("pii", PiiClass.PERSONAL),
        (
            "permissions",
            PermissionModelSpec(
                mode=PermissionMode.SOURCE_ACL,
                principal_namespace="entra",
            ),
        ),
    ],
)
def test_every_contract_member_changes_identity(field: str, value: object) -> None:
    assert _identity(contract=_contract(**{field: value})) != _identity()


def test_mapper_and_config_revisions_are_identity_members() -> None:
    assert _identity(mapper_revision="mapper-v2") != _identity()
    assert _identity(config_revision="config-v2") != _identity()


def test_actual_m365_source_alias_is_hashed_but_canonical_kind_is_retained() -> None:
    alias = _identity(contract=_contract(source="meridian-sharepoint-prod"))
    assert alias.source_kind == "m365-sharepoint"
    assert alias != _identity()


def test_every_permission_member_including_default_acl_changes_identity() -> None:
    baseline = _identity()
    assert (
        _identity(
            contract=_contract(
                permissions=PermissionModelSpec(
                    mode=PermissionMode.PUBLIC_WITHIN_TENANT,
                    principal_namespace="oidc",
                    default_acl=("tenant:meridian",),
                )
            )
        )
        != baseline
    )
    assert (
        _identity(
            contract=_contract(
                permissions=PermissionModelSpec(
                    mode=PermissionMode.SOURCE_ACL,
                    principal_namespace="oidc",
                    default_acl=("department:operations",),
                )
            )
        )
        != baseline
    )


@pytest.mark.parametrize("bad", ["P1M", "P1Y", "P1Y2D", "P1MT1S", "P0D", "PT0S", "P0W"])
def test_calendar_duration_is_not_an_m365_fixed_freshness_sla(bad: str) -> None:
    with pytest.raises(ValueError, match="fixed-duration"):
        _identity(contract=_contract(freshness_sla=bad))


def test_fractional_fixed_duration_precision_is_not_truncated() -> None:
    half_second = _identity(contract=_contract(freshness_sla="PT0.5S"))
    six_tenths = _identity(contract=_contract(freshness_sla="PT0.6S"))
    assert half_second != six_tenths
    with pytest.raises(ValueError, match="precision/scale"):
        _identity(contract=_contract(freshness_sla="PT0.000000000000000000000000000000000000001S"))


def test_schema_mapping_order_does_not_change_identity() -> None:
    left = _contract(
        schema_def={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        }
    )
    right = _contract(
        schema_def={
            "properties": {"b": {"type": "integer"}, "a": {"type": "string"}},
            "type": "object",
        }
    )
    assert _identity(contract=left) == _identity(contract=right)


def test_factory_rejects_empty_revisions() -> None:
    with pytest.raises(ValueError):
        _identity(mapper_revision="")
    with pytest.raises(ValueError):
        _identity(config_revision="")


def test_factory_rejects_ambiguous_or_non_finite_schema_members() -> None:
    with pytest.raises(ValueError, match="unordered set"):
        _identity(
            contract=_contract(schema_def={"type": "object", "x-contract-values": {"a", "b"}})
        )
    with pytest.raises(ValueError, match="non-finite"):
        _identity(contract=_contract(schema_def={"type": "object", "x-contract-score": math.nan}))


def test_factory_surface_cannot_accept_credentials_or_secret_material() -> None:
    parameters = inspect.signature(m365_source_contract_identity).parameters
    assert tuple(parameters) == ("contract", "mapper_revision", "config_revision")
    assert not ({"credential", "secret", "token", "client_secret"} & parameters.keys())
