# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce organization identity and authority-producer invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ne.connect.authority_admission import AuthorityAdmissionError
from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.authority import observed_salesforce_permissions
from ne.connect.salesforce.contract import (
    salesforce_contract_identity,
    salesforce_contract_sla_seconds,
    salesforce_sweep_plan,
)
from ne.connect.salesforce.identity import (
    canonical_salesforce_organization_id,
    organization_id_from_identity_url,
)

from .salesforce_transport import ORGANIZATION_ID, salesforce_settings


def test_provider_org_id_is_case_safe_and_not_a_hostname() -> None:
    assert canonical_salesforce_organization_id("00D000000000001") == "00D000000000001EAA"
    with pytest.raises(SalesforceSyncError, match="invalid organization_id"):
        canonical_salesforce_organization_id("acme.my.salesforce.com")


def test_identity_url_must_be_allowlisted_and_exactly_shaped() -> None:
    allowed = ("login.salesforce.com", ".my.salesforce.com")
    assert (
        organization_id_from_identity_url(
            "https://login.salesforce.com/id/00D000000000001/005000000000001AAA",
            allowed_hosts=allowed,
        )
        == "00D000000000001EAA"
    )
    for value in (
        "https://evil.example/id/00D000000000001/005000000000001AAA",
        "https://login.salesforce.com/not-id/00D000000000001/005000000000001AAA",
        "https://login.salesforce.com/id/00D000000000001",
    ):
        with pytest.raises(SalesforceSyncError, match="Identity URL"):
            organization_id_from_identity_url(value, allowed_hosts=allowed)


def test_partial_object_permissions_cannot_mint_authority_evidence() -> None:
    with pytest.raises(AuthorityAdmissionError, match="incomplete"):
        observed_salesforce_permissions(
            object_name="Account",
            principals=("sf-profile:00e000000000001AAA",),
            pages_read=1,
            collection_complete=False,
        )


def test_contract_identity_is_provider_org_specific_and_credential_free() -> None:
    first = salesforce_settings(client_id="client-a")
    other_credentials = salesforce_settings(client_id="client-b")
    identity = salesforce_contract_identity(first, ORGANIZATION_ID)

    assert identity == salesforce_contract_identity(other_credentials, ORGANIZATION_ID)
    assert identity != salesforce_contract_identity(first, "00D000000000002EAA")
    assert identity != salesforce_contract_identity(
        first.model_copy(update={"rest_max_result_pages": first.rest_max_result_pages + 1}),
        ORGANIZATION_ID,
    )


def test_contract_sla_and_sweep_are_exact_bounded_values() -> None:
    assert salesforce_contract_sla_seconds(salesforce_settings()) == Decimal(3600)
    assert salesforce_contract_sla_seconds(salesforce_settings(freshness_sla="PT15M")) == Decimal(
        900
    )
    with pytest.raises(ValueError, match="exact fixed-duration"):
        salesforce_contract_sla_seconds(salesforce_settings(freshness_sla="P1M"))

    plan = salesforce_sweep_plan(
        salesforce_settings(
            authority_enabled=True,
            authority_sweep_cadence_seconds=60,
            authority_sweep_batch_size=10,
            authority_sweep_max_batch_duration_seconds=5,
            authority_sweep_max_points_per_scope=100,
        )
    )
    assert plan is not None
    assert plan.cadence_seconds == Decimal(60)
    assert plan.batch_size == 10
    assert plan.max_batch_duration_seconds == Decimal(5)
    assert plan.max_points_per_scope == 100
