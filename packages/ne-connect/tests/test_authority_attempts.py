# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Canonical metadata-only routing for current durable authority attempts."""

from __future__ import annotations

from dataclasses import fields

import pytest
import sqlalchemy as sa

from ne.connect.authority_admission import (
    AdmissionStage,
    SqlAlchemyAdmissionAttemptStore,
    SqlAlchemySourceContractHeadStore,
    connector_instance_scope,
    drive_scope,
    ensure_admission_schema,
)
from ne.connect.authority_attempts import (
    AuthorityAttemptNotPendingError,
    AuthorityAttemptRead,
    AuthorityAttemptReader,
    UnknownAuthorityAttemptError,
    UnknownAuthoritySourceKindError,
)
from ne.connect.salesforce.authority import salesforce_connector_scope, salesforce_object_scope
from ne.contracts.source_authority import SourceContractIdentity, SourceReconciliationScope

DEPLOYMENT = "deployment-alpha"
INSTANCE = "connector-instance-1"
TENANT = "tenant-guid"
ORGANIZATION = "00D000000000001AAA"


def _engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_admission_schema(engine)
    return engine


def _contract(source_kind: str, descriptor_hash: str = "a" * 64) -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind=source_kind,
        descriptor_schema_version=1,
        descriptor_hash=descriptor_hash,
    )


def _m365_scopes() -> tuple[SourceReconciliationScope, SourceReconciliationScope]:
    return (
        connector_instance_scope(
            deployment_id=DEPLOYMENT,
            connector_instance_id=INSTANCE,
            graph_tenant_id=TENANT,
        ),
        drive_scope(
            deployment_id=DEPLOYMENT,
            connector_instance_id=INSTANCE,
            graph_tenant_id=TENANT,
            drive_id="drive-1",
        ),
    )


def _salesforce_scopes() -> tuple[SourceReconciliationScope, SourceReconciliationScope]:
    return (
        salesforce_connector_scope(
            deployment_id=DEPLOYMENT,
            connector_instance_id=INSTANCE,
            organization_id=ORGANIZATION,
        ),
        salesforce_object_scope(
            deployment_id=DEPLOYMENT,
            connector_instance_id=INSTANCE,
            organization_id=ORGANIZATION,
            object_name="Account",
        ),
    )


@pytest.mark.parametrize(
    ("scopes", "source_kind", "source_binding"),
    (
        (_m365_scopes(), "m365-sharepoint", "m365"),
        (_salesforce_scopes(), "salesforce", "salesforce"),
    ),
    ids=("m365", "salesforce"),
)
def test_reader_routes_durable_attempt_id_through_server_owned_binding(
    scopes: tuple[SourceReconciliationScope, SourceReconciliationScope],
    source_kind: str,
    source_binding: str,
) -> None:
    engine = _engine()
    attempts = SqlAlchemyAdmissionAttemptStore(engine)
    parent, child = scopes
    head = SqlAlchemySourceContractHeadStore(engine).claim_or_load(
        parent,
        source_generation=1,
        contract=_contract(source_kind),
    )
    attempts.claim(
        attempt_id="provider-durable-attempt-7",
        scope=child,
        identity_hash="b" * 64,
        source_revision="revision-7",
        contract_epoch=head.contract_epoch,
        prior_artifact_id=None,
    )

    read = AuthorityAttemptReader(attempts, (child, child)).read("provider-durable-attempt-7")

    assert (read.source_kind, read.source_binding) == (source_kind, source_binding)
    assert read.attempt_id == "provider-durable-attempt-7"
    assert read.stage is AdmissionStage.CLAIMED


def test_reader_refuses_an_unknown_durable_attempt_id() -> None:
    _parent, child = _m365_scopes()
    reader = AuthorityAttemptReader(SqlAlchemyAdmissionAttemptStore(_engine()), (child,))

    with pytest.raises(UnknownAuthorityAttemptError):
        reader.read("not-a-derived-id")


def test_reader_refuses_an_attempt_from_a_superseded_parent_epoch() -> None:
    engine = _engine()
    attempts = SqlAlchemyAdmissionAttemptStore(engine)
    heads = SqlAlchemySourceContractHeadStore(engine)
    parent, child = _m365_scopes()
    head = heads.claim_or_load(
        parent,
        source_generation=1,
        contract=_contract("m365-sharepoint"),
    )
    attempts.claim(
        attempt_id="durable-stale-id",
        scope=child,
        identity_hash="b" * 64,
        source_revision="revision-1",
        contract_epoch=head.contract_epoch,
        prior_artifact_id=None,
    )
    heads.claim_or_load(
        parent,
        source_generation=1,
        contract=_contract("m365-sharepoint", "c" * 64),
    )

    with pytest.raises(AuthorityAttemptNotPendingError):
        AuthorityAttemptReader(attempts, (child,)).read("durable-stale-id")


def test_reader_rejects_scopes_without_a_canonical_server_binding() -> None:
    unknown_scope = SourceReconciliationScope(
        path=("unknown-source", DEPLOYMENT, INSTANCE, "tenant", "thing", "one")
    )

    with pytest.raises(UnknownAuthoritySourceKindError):
        AuthorityAttemptReader(SqlAlchemyAdmissionAttemptStore(_engine()), (unknown_scope,))


def test_reader_result_is_metadata_only() -> None:
    assert {field.name for field in fields(AuthorityAttemptRead)} == {
        "attempt_id",
        "source_kind",
        "source_binding",
        "scope_hash",
        "identity_hash",
        "stage",
        "source_revision",
        "contract_epoch",
        "prior_artifact_id",
        "receipt_id",
        "receipt_hash",
    }
