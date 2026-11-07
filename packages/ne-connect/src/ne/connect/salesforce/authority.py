# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce CurrentSourceAuthority producer over provider-issued organization identity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa

from ne.connect.authority_admission import (
    AdmissionPreparation,
    AuthorityAdmissionError,
    M365AuthorityAdmission,
    PermissionReadEvidence,
    SqlAlchemyAdmissionAttemptStore,
    SqlAlchemyHierarchicalAdmission,
    SqlAlchemySourceContractHeadStore,
    claim_or_load_connector_instance,
)
from ne.connect.identity import source_acl_hash, source_snapshot_hash
from ne.connect.salesforce.config import SalesforceConnectorSettings
from ne.connect.salesforce.contract import (
    SALESFORCE_SOURCE_KIND,
    salesforce_admitted_target_digest,
    salesforce_contract_identity,
    salesforce_contract_sla_seconds,
)
from ne.connect.salesforce.identity import canonical_salesforce_organization_id
from ne.contracts.integration import Record
from ne.contracts.source_authority import (
    SalesforceSourcePoint,
    SourceContentArtifactIdentity,
    SourceIdentity,
    SourceReconciliationScope,
)

if TYPE_CHECKING:
    from ne.brain.write import ImmutableKnowledgeArtifactStore
    from ne.connect.authority_admission import SourceAuthorityLineageSink
    from ne.contracts.source_authority import CurrentSourceAuthorityWriter

__all__ = [
    "SalesforceAuthorityAdmission",
    "SalesforceAuthorityFactory",
    "SalesforcePermissionReadEvidence",
    "observed_salesforce_permissions",
    "salesforce_connector_scope",
    "salesforce_object_scope",
]


def salesforce_connector_scope(
    *, deployment_id: str, connector_instance_id: str, organization_id: str
) -> SourceReconciliationScope:
    return SourceReconciliationScope(
        path=(SALESFORCE_SOURCE_KIND, deployment_id, connector_instance_id, organization_id)
    )


def salesforce_object_scope(
    *, deployment_id: str, connector_instance_id: str, organization_id: str, object_name: str
) -> SourceReconciliationScope:
    return SourceReconciliationScope(
        path=(
            SALESFORCE_SOURCE_KIND,
            deployment_id,
            connector_instance_id,
            organization_id,
            "object",
            object_name,
        )
    )


@dataclass(frozen=True)
class SalesforcePermissionReadEvidence(PermissionReadEvidence):
    """A complete ObjectPermissions collection applied to one configured object."""

    def matches(self, source_id: str) -> bool:
        return source_id == self.drive_id or source_id.startswith(f"{self.drive_id}:")


def observed_salesforce_permissions(
    *, object_name: str, principals: tuple[str, ...], pages_read: int, collection_complete: bool
) -> SalesforcePermissionReadEvidence:
    if not collection_complete or pages_read < 1 or not object_name:
        raise AuthorityAdmissionError(
            "an incomplete Salesforce ObjectPermissions collection cannot refresh authority"
        )
    return SalesforcePermissionReadEvidence(
        drive_id=object_name,
        item_id="*",
        principals=tuple(principals),
        pages_read=pages_read,
    )


class SalesforceAuthorityAdmission(M365AuthorityAdmission):
    """Authority admission specialized to ``organization/object/record`` points."""

    def __init__(self, *, organization_id: str, **kwargs: object) -> None:
        super().__init__(graph_tenant_id=organization_id, **kwargs)  # type: ignore[arg-type]
        self._organization_id = organization_id

    @property
    def organization_id(self) -> str:
        return self._organization_id

    async def prepare(
        self,
        record: Record,
        *,
        acl: tuple[str, ...],
        canonical_bytes: bytes,
        item_id: str,
        evidence: PermissionReadEvidence,
    ) -> AdmissionPreparation:
        object_name = record.source_id
        claim = self._require_claim(object_name)
        if not evidence.matches(object_name):
            raise AuthorityAdmissionError(
                "permission evidence belongs to another Salesforce object"
            )
        acl_hash = source_acl_hash(acl)
        if source_acl_hash(evidence.principals) != acl_hash:
            raise AuthorityAdmissionError(
                "the admitted ACL differs from ObjectPermissions evidence"
            )
        identity = self._source_identity(object_name, record.record_id)
        revision_value = record.payload.get("SystemModstamp")
        if not isinstance(revision_value, str) or not revision_value:
            raise AuthorityAdmissionError("Salesforce authority requires a source revision")
        artifact = SourceContentArtifactIdentity(
            schema_version=1,
            source_identity=identity,
            source_snapshot_hash=source_snapshot_hash(self._source_name, record),
            content_snapshot_hash=hashlib.sha256(canonical_bytes).hexdigest(),
            acl_snapshot_hash=acl_hash,
            source_contract=self._head.source_contract,
            artifact_schema_version=self._artifact_schema_version,
        )
        create_receipt = await self._artifacts.create_if_absent(artifact, canonical_bytes)
        return AdmissionPreparation(
            source_identity=identity,
            artifact_identity=artifact,
            create_receipt=create_receipt,
            acl_snapshot_hash=acl_hash,
            source_revision=revision_value,
            authority_as_of=self._observation_instant(
                claim,
                identity_hash=identity.identity_hash,
                source_revision=revision_value,
                acl_snapshot_hash=acl_hash,
                artifact_id=artifact.artifact_id,
            ),
            item_id=item_id,
            drive_id=object_name,
            graph_item_id=record.record_id,
            evidence=evidence,
            scope=claim.scope,
        )

    def _source_identity(self, drive_id: str, item_id: str) -> SourceIdentity:
        return SourceIdentity(
            deployment_id=self._deployment_id,
            source_kind="salesforce",
            connector_instance_id=self._instance.connector_instance_id,
            point=SalesforceSourcePoint(
                organization_id=self._organization_id,
                object_name=drive_id,
                record_id=item_id,
            ),
            generation=self._instance.source_generation,
        )


class SalesforceAuthorityFactory:
    """Late-bind one authority producer to provider-issued organization identity.

    The factory is composed without an organization id. Only
    :func:`run_salesforce_sync` can call :meth:`create` after OAuth validated the
    provider response and durably bound the exact credential-free crawl target.
    """

    def __init__(
        self,
        *,
        engine: sa.Engine,
        writer: CurrentSourceAuthorityWriter,
        artifacts: ImmutableKnowledgeArtifactStore,
        hierarchy: SqlAlchemyHierarchicalAdmission,
        attempts: SqlAlchemyAdmissionAttemptStore,
        contract_heads: SqlAlchemySourceContractHeadStore,
        deployment_id: str,
        settings: SalesforceConnectorSettings,
        lineage: SourceAuthorityLineageSink | None = None,
    ) -> None:
        self._engine = engine
        self._writer = writer
        self._artifacts = artifacts
        self._hierarchy = hierarchy
        self._attempts = attempts
        self._contract_heads = contract_heads
        self._deployment_id = deployment_id
        self._settings = settings
        self._lineage = lineage

    @property
    def settings(self) -> SalesforceConnectorSettings:
        return self._settings

    def create(
        self, *, organization_id: str, admitted_target_digest: str
    ) -> SalesforceAuthorityAdmission:
        canonical_organization_id = canonical_salesforce_organization_id(organization_id)
        expected_digest = salesforce_admitted_target_digest(
            self._settings, canonical_organization_id
        )
        if admitted_target_digest != expected_digest:
            raise AuthorityAdmissionError(
                "Salesforce authority factory received a foreign admitted target"
            )
        instance = claim_or_load_connector_instance(
            self._engine,
            deployment_id=self._deployment_id,
            source_name=self._settings.source_name,
            admitted_target_digest=admitted_target_digest,
        )
        connector_scope = salesforce_connector_scope(
            deployment_id=self._deployment_id,
            connector_instance_id=instance.connector_instance_id,
            organization_id=canonical_organization_id,
        )
        head = self._contract_heads.claim_or_load(
            connector_scope,
            source_generation=instance.source_generation,
            contract=salesforce_contract_identity(self._settings, canonical_organization_id),
        )
        admission = SalesforceAuthorityAdmission(
            engine=self._engine,
            writer=self._writer,
            artifacts=self._artifacts,
            hierarchy=self._hierarchy,
            attempts=self._attempts,
            contract_heads=self._contract_heads,
            contract_head=head,
            instance=instance,
            deployment_id=self._deployment_id,
            organization_id=canonical_organization_id,
            source_name=self._settings.source_name,
        )
        if self._lineage is not None:
            admission.bind_lineage(self._lineage)
        return admission

    def freshness_sla_seconds(self) -> Decimal:
        return salesforce_contract_sla_seconds(self._settings)
