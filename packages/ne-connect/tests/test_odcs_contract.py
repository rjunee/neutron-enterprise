# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OdcsDataContract: fail-loud construction + Protocol conformance."""

from __future__ import annotations

import pytest

from ne.connect import OdcsDataContract, PermissionMode, PermissionModelSpec, PiiClass
from ne.contracts.integration import DataContract

_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string", "minLength": 1}},
}


def _contract(**overrides: object) -> OdcsDataContract:
    kwargs: dict[str, object] = {
        "source": "acme",
        "owner": "data-eng@acme.test",
        "schema_def": _SCHEMA,
        "freshness_sla": "PT15M",
        "pii": PiiClass.PERSONAL,
        "permissions": PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
    }
    kwargs.update(overrides)
    return OdcsDataContract(**kwargs)  # type: ignore[arg-type]


def test_malformed_json_schema_fails_loud() -> None:
    with pytest.raises(ValueError):
        _contract(schema_def={"type": 12})


@pytest.mark.parametrize("bad", ["", "P", "PT", "15m"])
def test_invalid_freshness_sla_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _contract(freshness_sla=bad)


@pytest.mark.parametrize("good", ["PT15M", "P1D", "P1DT12H", "PT0.5S"])
def test_valid_freshness_sla_accepted(good: str) -> None:
    assert _contract(freshness_sla=good).freshness_sla == good


@pytest.mark.parametrize("field", ["source", "owner"])
def test_empty_source_or_owner_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _contract(**{field: ""})


def test_validate_returns_empty_on_conforming_record() -> None:
    assert _contract().validate({"name": "Ada"}) == []


def test_validate_returns_path_messages_on_violation() -> None:
    errors = _contract().validate({})
    assert errors
    assert all(": " in message for message in errors)


def test_protocol_conformance_and_string_properties() -> None:
    contract: DataContract = _contract()
    assert isinstance(contract.pii_class, str)
    assert contract.pii_class == "personal"
    assert isinstance(contract.permission_model, str)
    assert contract.permission_model == "source-acl"
    # The TYPED spec stays available as .permissions (the NE-042/043 seam).
    assert _contract().permissions.mode is PermissionMode.SOURCE_ACL
