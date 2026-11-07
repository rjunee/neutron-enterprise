# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The credential-free supported-source descriptors the API composition may hold.

The worker registers M365 authority from the FULL connector configuration —
tenant, certificate paths, client secret. The API must be able to report the
same configured contract hash and epoch without ever mounting any of that.

This module is that boundary, and it is a boundary rather than a convention:
:class:`M365SourceAuthorityDescriptor` has an exhaustive frozen field set, its
projector reads only non-credential members of the settings object, and
:data:`M365_CREDENTIAL_FIELD_NAMES` names every settings field a descriptor must
never carry so the guard is a value comparison rather than a review promise.

015A0-AUTHORITY-T3's :class:`GovernedLocalT3SourceAuthorityDescriptor` is the same
boundary for the other supported source kind. It has NO freshness SLA member at
all, because governed local T3 freshness is the atomically selected release head
plus backend health — not a connector heartbeat — and a nullable SLA field would
invite a consumer to treat the absence as "not configured yet" rather than "does
not exist for this kind".

Both descriptors carry no Writer, no engine and no claim: a projector that could
reach any of those would be producer capability living in the API.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from ne.contracts.source_authority import CurrentSourceContractHead

if TYPE_CHECKING:
    from ne.connect.m365.config import M365ConnectorSettings

__all__ = [
    "M365_CREDENTIAL_FIELD_NAMES",
    "GovernedLocalT3SourceAuthorityDescriptor",
    "M365SourceAuthorityDescriptor",
    "descriptor_carries_no_credential",
    "project_governed_local_t3_source_authority",
    "project_m365_source_authority",
]

#: Every ``M365ConnectorSettings`` field that is credential material or a path to
#: it. A descriptor carrying any of these values — under any field name — is a
#: credential mounted into API composition.
M365_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "certificate_pem_path",
        "client_id",
        "client_secret",
        "private_key_pem_path",
    }
)

_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class M365SourceAuthorityDescriptor(BaseModel):
    """What the API may know about the M365 source contract, and nothing more."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: Annotated[str, Field(min_length=1, max_length=128)]
    source_name: Annotated[str, Field(min_length=1, max_length=512)]
    connector_instance_id: Annotated[str, Field(min_length=1, max_length=512)]
    source_generation: Annotated[int, Field(ge=1)]
    contract_epoch: Annotated[int, Field(ge=1)]
    contract_identity_hash: _Sha256
    descriptor_schema_version: Annotated[int, Field(ge=1)]
    descriptor_hash: _Sha256
    head_revision: Annotated[str, Field(min_length=1, max_length=512)]
    freshness_sla_seconds: Decimal
    #: Whether an admission has actually succeeded under ``contract_epoch``. An
    #: unresolved epoch means every receipt for this scope is Unknown, so a
    #: consumer must not read ``False`` as "probably fine, just not swept yet".
    resolved: bool

    @property
    def configured_contract_hash(self) -> str:
        """The exact configured hash the worker must independently report."""
        return self.contract_identity_hash


def project_m365_source_authority(
    settings: M365ConnectorSettings,
    *,
    head: CurrentSourceContractHead,
    freshness_sla_seconds: Decimal,
    resolved: bool,
) -> M365SourceAuthorityDescriptor:
    """Project the credential-free M365 descriptor from settings plus its head.

    ``source_name`` is the only settings member read here, and it is the ODCS
    contract's own public source identity. Everything else comes from the
    already-credential-free ``CurrentSourceContractHead``.
    """
    return M365SourceAuthorityDescriptor(
        source_kind=head.source_contract.source_kind,
        source_name=settings.source_name,
        connector_instance_id=_connector_instance_id(head),
        source_generation=head.source_generation,
        contract_epoch=head.contract_epoch,
        contract_identity_hash=head.source_contract.identity_hash,
        descriptor_schema_version=head.source_contract.descriptor_schema_version,
        descriptor_hash=head.source_contract.descriptor_hash,
        head_revision=head.head_revision,
        freshness_sla_seconds=freshness_sla_seconds,
        resolved=resolved,
    )


def _connector_instance_id(head: CurrentSourceContractHead) -> str:
    """Read the connector instance out of the head's own reconciliation scope.

    The scope path is ``(kind, deployment, connector-instance, tenant)``; taking
    the component keeps the API's instance id identical to the producer's by
    construction instead of by both sides deriving it from settings.
    """
    path: tuple[str, ...] = head.connector_scope.path
    if len(path) < 3:
        raise ValueError("an M365 connector scope must carry its connector instance id")
    return path[2]


def descriptor_carries_no_credential(
    descriptor: M365SourceAuthorityDescriptor, settings: Any
) -> bool:
    """Whether no credential VALUE from ``settings`` appears in the descriptor.

    Compares values, not field names: renaming a descriptor field, or smuggling a
    PEM path into ``head_revision``, both fail this check.
    """
    rendered = {str(value) for value in descriptor.model_dump(mode="json").values()}
    for name in M365_CREDENTIAL_FIELD_NAMES:
        value = getattr(settings, name, None)
        secret = getattr(value, "get_secret_value", None)
        raw = secret() if callable(secret) else value
        if raw and str(raw) in rendered:
            return False
    return True


class GovernedLocalT3SourceAuthorityDescriptor(BaseModel):
    """What the API may know about the governed local T3 source contract.

    The exhaustive field set is the claim. There is deliberately no SLA, cadence,
    schedule, interval or heartbeat member: governed local T3 has no connector
    freshness at all, so the head revision below — the id of the atomically selected
    immutable release — plus ``resolved`` is the whole freshness story.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: Annotated[str, Field(min_length=1, max_length=128)]
    dataset_name: Annotated[str, Field(min_length=1, max_length=512)]
    dataset_instance_id: Annotated[str, Field(min_length=1, max_length=512)]
    source_generation: Annotated[int, Field(ge=1)]
    contract_epoch: Annotated[int, Field(ge=1)]
    contract_identity_hash: _Sha256
    descriptor_schema_version: Annotated[int, Field(ge=1)]
    descriptor_hash: _Sha256
    contract_head_revision: Annotated[str, Field(min_length=1, max_length=512)]
    #: The selected immutable-release head: a pure function of the release bytes, so
    #: a restart that rebuilds the identical release reports the identical value.
    selected_release_id: _Sha256
    selected_release_algorithm_version: Annotated[int, Field(ge=1)]
    #: Whether an admission has actually succeeded under ``contract_epoch``. An
    #: unresolved epoch means every receipt for this scope is Unknown, so a consumer
    #: must not read ``False`` as "probably fine, just not swept yet".
    resolved: bool

    @property
    def configured_contract_hash(self) -> str:
        """The exact configured hash the worker must independently report."""
        return self.contract_identity_hash


def project_governed_local_t3_source_authority(
    *,
    dataset_instance_id: str,
    source_generation: int,
    head: CurrentSourceContractHead,
    selected_release_id: str,
    selected_release_algorithm_version: int,
    dataset_name: str,
    resolved: bool,
) -> GovernedLocalT3SourceAuthorityDescriptor:
    """Project the credential-free governed local T3 descriptor from its head.

    Everything except the dataset name and the selected release id comes from the
    already-credential-free ``CurrentSourceContractHead``, which is the same value
    the producer registered — so API and producer agree because there is one head,
    not because two derivations happen to match.
    """
    return GovernedLocalT3SourceAuthorityDescriptor(
        source_kind=head.source_contract.source_kind,
        dataset_name=dataset_name,
        dataset_instance_id=dataset_instance_id,
        source_generation=source_generation,
        contract_epoch=head.contract_epoch,
        contract_identity_hash=head.source_contract.identity_hash,
        descriptor_schema_version=head.source_contract.descriptor_schema_version,
        descriptor_hash=head.source_contract.descriptor_hash,
        contract_head_revision=head.head_revision,
        selected_release_id=selected_release_id,
        selected_release_algorithm_version=selected_release_algorithm_version,
        resolved=resolved,
    )
