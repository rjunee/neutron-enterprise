# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for Salesforce connector records (NE-047).

``salesforce_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: this is the
fail-closed BACKSTOP. The connector itself quarantines any SObject it cannot
resolve a non-empty, untainted OBJECT-LEVEL ACL for (so a record that does
reach the runner always carries a crawled ``AclEntry``), and the empty default
means that if an unlabeled record ever somehow reached the runner, the
``AclMapper`` raises ``AclResolutionError`` and aborts the run LOUDLY rather
than defaulting the record open.

``principal_namespace='salesforce'`` names which principal store resolves the
``sf-profile:<ProfileId>`` / ``sf-permset:<PermissionSetId>`` group labels this
connector's :mod:`ne.connect.salesforce.acl` mapper emits — the NE-042
principal store.

The record schema is generated from the exact configured Bulk query shapes. It
requires every selected field, constrains the identity and ``SystemModstamp``
version fields, and rejects surplus fields. The schema intentionally validates
the raw CSV payload; ``Record.source_id`` remains the Connector Protocol's
separate object-identity member and is bound by the connector/conformance kit.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from ne.connect.authority_admission import SweepCadencePlan
from ne.connect.contracts import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    _strict_json_value,
)
from ne.connect.contracts import _fixed_duration_seconds as _contract_fixed_duration_seconds
from ne.connect.endpoints import UnsafeEndpointError, validate_source_url
from ne.connect.salesforce.config import SalesforceConnectorSettings
from ne.connect.salesforce.identity import canonical_salesforce_organization_id
from ne.connect.source_url import canonical_salesforce_instance_origin
from ne.contracts.source_authority import SourceContractIdentity
from ne.net.ssrf import normalize_host

__all__ = [
    "SALESFORCE_SOURCE_KIND",
    "salesforce_admitted_target_digest",
    "salesforce_config_revision",
    "salesforce_contract_identity",
    "salesforce_contract_sla_seconds",
    "salesforce_data_contract",
    "salesforce_source_link_mapper_revision",
    "salesforce_sweep_plan",
]

SALESFORCE_SOURCE_KIND = "salesforce"

_SOURCE_LINK_MAPPER_VERSION = 1
_ADMITTED_TARGET_VERSION = 1
_CONFIG_REVISION_VERSION = 1

_SALESFORCE_ID_PATTERN = r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$"
_SYSTEM_MODSTAMP_PATTERN = (
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)


def _salesforce_record_schema(settings: SalesforceConnectorSettings) -> dict[str, object]:
    """Build the closed union of exact configured Bulk-CSV record shapes."""
    shapes: list[dict[str, object]] = []
    seen_fields: set[tuple[str, ...]] = set()
    for cfg in settings.objects:
        fields = tuple(
            dict.fromkeys(
                [cfg.id_field, cfg.modstamp_field, *cfg.content_fields, *cfg.metadata_fields]
            )
        )
        if fields in seen_fields:
            continue
        seen_fields.add(fields)
        properties: dict[str, object] = {field: {"type": "string"} for field in fields}
        properties[cfg.id_field] = {
            "type": "string",
            "pattern": _SALESFORCE_ID_PATTERN,
        }
        properties[cfg.modstamp_field] = {
            "type": "string",
            "pattern": _SYSTEM_MODSTAMP_PATTERN,
        }
        shapes.append(
            {
                "type": "object",
                "required": list(fields),
                "properties": properties,
                "additionalProperties": False,
            }
        )
    return {"anyOf": shapes}


def salesforce_data_contract(settings: SalesforceConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's configured SObjects."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_salesforce_record_schema(settings),
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace="salesforce",
        ),
    )


def _digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_authority(
    value: str,
    *,
    allowed_hosts: tuple[str, ...],
    suffix_patterns: bool,
) -> str:
    parsed = urlsplit(validate_source_url(value, allow_external=True))
    host = normalize_host(parsed.hostname or "")
    allowed = any(
        host.endswith(entry) if suffix_patterns and entry.startswith(".") else host == entry
        for entry in allowed_hosts
    )
    if not allowed:
        raise UnsafeEndpointError("Salesforce target authority is not in its allowlist")
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def salesforce_source_link_mapper_revision(settings: SalesforceConnectorSettings) -> str:
    """Hash the exact non-secret source-link mapping decision."""
    origin = canonical_salesforce_instance_origin(
        instance_url=settings.instance_url,
        source_link_allowed_hosts=settings.source_link_allowed_hosts,
    )
    return _digest(
        {
            "connector": SALESFORCE_SOURCE_KIND,
            "mapper_version": _SOURCE_LINK_MAPPER_VERSION,
            "source_link_origin": origin if origin is not None else "invalid",
        }
    )


def salesforce_admitted_target_digest(
    settings: SalesforceConnectorSettings, organization_id: str
) -> str:
    """Hash the provider-issued organization and exact credential-free crawl target."""
    canonical_organization_id = canonical_salesforce_organization_id(organization_id)
    objects = sorted(
        (
            {
                "name": cfg.name,
                "id_field": cfg.id_field,
                "modstamp_field": cfg.modstamp_field,
                "content_fields": list(cfg.content_fields),
                "metadata_fields": list(cfg.metadata_fields),
            }
            for cfg in settings.objects
        ),
        key=lambda item: str(item["name"]),
    )
    return _digest(
        {
            "connector": SALESFORCE_SOURCE_KIND,
            "target_binding_version": _ADMITTED_TARGET_VERSION,
            "source": settings.source_name,
            "organization_id": canonical_organization_id,
            "instance_authority": _canonical_authority(
                settings.instance_url,
                allowed_hosts=settings.instance_allowed_hosts,
                suffix_patterns=True,
            ),
            "login_authority": _canonical_authority(
                settings.login_base_url,
                allowed_hosts=settings.login_allowed_hosts,
                suffix_patterns=False,
            ),
            "api_version": settings.api_version,
            "objects": objects,
        }
    )


def salesforce_config_revision(settings: SalesforceConnectorSettings, organization_id: str) -> str:
    """Hash non-secret settings that change admitted Salesforce interpretation."""
    return _digest(
        {
            "admitted_target": salesforce_admitted_target_digest(settings, organization_id),
            "bulk_max_polls": settings.bulk_max_polls,
            "bulk_max_records_per_job": settings.bulk_max_records_per_job,
            "bulk_max_records_per_page": settings.bulk_max_records_per_page,
            "bulk_max_result_pages": settings.bulk_max_result_pages,
            "config_revision_version": _CONFIG_REVISION_VERSION,
            "identity_allowed_hosts": sorted(set(settings.identity_allowed_hosts)),
            "rest_max_result_pages": settings.rest_max_result_pages,
            "source_link_mapper_revision": salesforce_source_link_mapper_revision(settings),
        }
    )


def salesforce_contract_sla_seconds(settings: SalesforceConnectorSettings) -> Decimal:
    """Return the contract's exact fixed-duration freshness SLA in seconds."""
    seconds = _contract_fixed_duration_seconds(settings.freshness_sla)
    if seconds is None or seconds <= 0:
        raise ValueError(
            "Salesforce authority requires an exact fixed-duration freshness SLA "
            f"(got {settings.freshness_sla!r})"
        )
    return seconds


def salesforce_contract_identity(
    settings: SalesforceConnectorSettings, organization_id: str
) -> SourceContractIdentity:
    """Build the exact provider-organization-specific source contract identity."""
    contract = salesforce_data_contract(settings)
    descriptor = {
        "config_revision": salesforce_config_revision(settings, organization_id),
        "freshness_sla": contract.freshness_sla,
        "freshness_sla_seconds": str(salesforce_contract_sla_seconds(settings)),
        "mapper_revision": salesforce_source_link_mapper_revision(settings),
        "owner": contract.owner,
        "permission_model": _strict_json_value(contract.permissions.model_dump(mode="json")),
        "pii_class": contract.pii.value,
        "schema": _strict_json_value(contract.schema_def),
        "source": contract.source,
        "wire": 1,
    }
    return SourceContractIdentity(
        source_kind=SALESFORCE_SOURCE_KIND,
        descriptor_schema_version=1,
        descriptor_hash=_digest(descriptor),
    )


def salesforce_sweep_plan(settings: SalesforceConnectorSettings) -> SweepCadencePlan | None:
    """Return the explicit bounded quiet-point sweep plan, or ``None`` when disabled."""
    cadence = settings.authority_sweep_cadence_seconds
    batch_size = settings.authority_sweep_batch_size
    max_batch = settings.authority_sweep_max_batch_duration_seconds
    max_points = settings.authority_sweep_max_points_per_scope
    if cadence is None or batch_size is None or max_batch is None or max_points is None:
        return None
    return SweepCadencePlan(
        cadence_seconds=Decimal(cadence),
        batch_size=int(batch_size),
        max_batch_duration_seconds=Decimal(max_batch),
        max_points_per_scope=int(max_points),
    )
