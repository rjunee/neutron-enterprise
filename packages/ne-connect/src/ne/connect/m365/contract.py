# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for M365 driveItem records (NE-044).

``m365_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: this is the
fail-closed BACKSTOP. The connector itself quarantines any item it cannot resolve
(so a resolvable item always arrives with a crawled ``AclEntry``), and the empty
default means that if an unlabeled item ever somehow reached the runner, the
AclMapper raises ``AclResolutionError`` and aborts the run LOUDLY rather than
defaulting the item open. ``principal_namespace='entra'`` names the store the raw
GUID labels resolve against.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from ne.brain.ingest.chunker import StructuralChunker
from ne.connect.authority_admission import SweepCadencePlan
from ne.connect.contracts import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    m365_source_contract_identity,
)

# The SAME fixed-duration parse the SourceContractIdentity factory hashes. The
# receipt's persisted SLA and the SLA inside the contract hash must be one
# value; a second parser here would be a second source of truth for exactly the
# number a freshness decision is made against.
from ne.connect.contracts import _fixed_duration_seconds as _contract_fixed_duration_seconds
from ne.connect.endpoints import validate_source_url
from ne.connect.m365.acl import ACL_MAPPER_REVISION
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.operational_graph import (
    MERIDIAN_OPERATIONAL_SCHEMA,
    MERIDIAN_OPERATIONAL_SCHEMA_VERSION,
)
from ne.contracts.source_authority import SourceContractIdentity
from ne.net.ssrf import normalize_host

__all__ = [
    "M365_BODY_PARSER_IDENTITY",
    "M365_SOURCE_KIND",
    "m365_admitted_target_digest",
    "m365_config_revision",
    "m365_contract_identity",
    "m365_contract_sla_seconds",
    "m365_data_contract",
    "m365_source_link_mapper_revision",
    "m365_sweep_plan",
]

#: The exact supported source kind every M365 authority identity carries.
M365_SOURCE_KIND = "m365-sharepoint"

#: ``2``: a parsed document's chunk text became the document PROSE (record
#: payload moved to ``PARSED_RECORD_METADATA_KEY`` metadata — see
#: ``make_m365_record_mapper``). The bump changes the dedup content hash for
#: every retained M365 record, so the next governed sync REWRITES (re-chunks,
#: re-embeds) the corpus instead of reporting it ``skipped_unchanged`` — the
#: designed backfill, no manual state surgery.
_SOURCE_LINK_MAPPER_VERSION = 2
_ADMITTED_TARGET_VERSION = 1
_CONFIG_REVISION_VERSION = 1

M365_BODY_PARSER_IDENTITY = "neutron-m365-document-parser/v1"

#: driveItem record schema. Every emitted file is explicit about whether its body
#: was parsed or the type is unsupported; supported-file failures never emit a
#: Record. Required metadata keys are always present on a Graph driveItem.
_SCHEMA_DEF = {
    "type": "object",
    "required": [
        "id",
        "name",
        "web_url",
        "last_modified",
        "graph_doc_type",
        "body_ingestion_status",
    ],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "web_url": {"type": "string"},
        "last_modified": {"type": "string"},
        "graph_doc_type": {"const": "other"},
        "created": {"type": "string"},
        "size": {"type": "integer"},
        "mime_type": {"type": "string"},
        "path": {"type": "string"},
        "etag": {"type": "string"},
        "body_ingestion_status": {"enum": ["parsed", "unsupported", "structured"]},
        "body": {"type": "string", "minLength": 1},
        "body_blocks": {"type": "array", "minItems": 1},
        "body_block_count": {"type": "integer", "minimum": 1},
        "body_content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "body_schema_version": {"const": "ne.parsed_document/1"},
        "body_source_format": {"enum": ["pdf", "docx", "pptx", "html", "md"]},
        "body_parser_identity": {"const": M365_BODY_PARSER_IDENTITY},
        "ne_meridian_operational_schema": {"const": MERIDIAN_OPERATIONAL_SCHEMA},
        "ne_meridian_operational_schema_version": {"const": MERIDIAN_OPERATIONAL_SCHEMA_VERSION},
        "ne_meridian_operational_entity_class": {
            "enum": ["Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"]
        },
        "ne_meridian_operational_record_id": {"type": "string", "minLength": 1},
        "ne_meridian_operational_content_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
    },
    "allOf": [
        {
            "if": {"properties": {"body_ingestion_status": {"const": "parsed"}}},
            "then": {
                "required": [
                    "body",
                    "body_blocks",
                    "body_block_count",
                    "body_content_sha256",
                    "body_schema_version",
                    "body_source_format",
                    "body_parser_identity",
                ]
            },
        },
        {
            "if": {"properties": {"body_ingestion_status": {"const": "unsupported"}}},
            "then": {
                "not": {
                    "anyOf": [
                        {"required": ["body"]},
                        {"required": ["body_blocks"]},
                        {"required": ["body_block_count"]},
                        {"required": ["body_content_sha256"]},
                        {"required": ["body_schema_version"]},
                        {"required": ["body_source_format"]},
                        {"required": ["body_parser_identity"]},
                    ]
                }
            },
        },
        {
            "if": {"properties": {"body_ingestion_status": {"const": "structured"}}},
            "then": {
                "required": [
                    "ne_meridian_operational_schema",
                    "ne_meridian_operational_schema_version",
                    "ne_meridian_operational_entity_class",
                    "ne_meridian_operational_record_id",
                    "ne_meridian_operational_content_sha256",
                ],
                "not": {
                    "anyOf": [
                        {"required": ["body"]},
                        {"required": ["body_blocks"]},
                        {"required": ["body_parser_identity"]},
                    ]
                },
            },
        },
    ],
}


def m365_data_contract(settings: M365ConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's driveItem records."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_SCHEMA_DEF,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace="entra",
        ),
    )


def _canonical_authority(value: str) -> str:
    """Normalize one configured base URL to its scheme/host/port/path authority."""
    parsed = urlsplit(validate_source_url(value, allow_external=True))
    host = normalize_host(parsed.hostname or "")
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def _digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def m365_source_link_mapper_revision(settings: M365ConnectorSettings) -> str:
    """Hash the normalized source-link authority, parsed-body pipeline and ACL rules.

    The hashed members cover the values that change a mapped document without
    changing the record's own bytes, so bumping any one of them invalidates prior
    mappings: :data:`_SOURCE_LINK_MAPPER_VERSION` for link-mapping semantics,
    :data:`~ne.connect.m365.acl.ACL_MAPPER_REVISION` for the ACL mapper's
    semantics — which decide a driveItem's principal set, and therefore whether
    it is quarantined at all — and the parser, schema, chunker and host members
    for the body pipeline.
    """
    authority = sorted(normalize_host(host) for host in settings.source_link_allowed_hosts)
    return _digest(
        {
            "acl_mapper_revision": ACL_MAPPER_REVISION,
            "connector": "m365",
            "mapper_version": _SOURCE_LINK_MAPPER_VERSION,
            "parser_identity": M365_BODY_PARSER_IDENTITY,
            "parsed_schema": "ne.parsed_document/1",
            "operational_schema": MERIDIAN_OPERATIONAL_SCHEMA,
            "operational_schema_version": MERIDIAN_OPERATIONAL_SCHEMA_VERSION,
            "semantic_chunker": {
                "revision": StructuralChunker.EVIDENCE_REVISION,
                "max_chars": StructuralChunker.DEFAULT_MAX_CHARS,
                "min_chars": StructuralChunker.DEFAULT_MIN_CHARS,
                "include_headings_in_text": StructuralChunker.DEFAULT_INCLUDE_HEADINGS_IN_TEXT,
            },
            "source_link_allowed_hosts": authority,
        }
    )


def m365_admitted_target_digest(settings: M365ConnectorSettings) -> str:
    """Hash the exact CREDENTIAL-FREE cloud, tenant and admitted crawl scope.

    This digest is what a connector-instance id is bound to. No credential
    member is accepted, which is precisely why rotating the client secret or the
    signing certificate loads the SAME instance while changing the Graph tenant,
    the cloud authority or the admitted site/drive scope does not.
    """
    return _digest(
        {
            "cloud": {
                "graph_authority": _canonical_authority(settings.graph_base_url),
                "login_authority": _canonical_authority(settings.login_base_url),
            },
            "connector": "m365",
            "drive_ids": sorted(set(settings.drive_ids)),
            "site_ids": sorted(set(settings.site_ids)),
            "source": settings.source_name,
            "target_version": _ADMITTED_TARGET_VERSION,
            "tenant_id": settings.tenant_id.strip().lower(),
        }
    )


def m365_config_revision(settings: M365ConnectorSettings) -> str:
    """Hash the credential-free configuration that changes record INTERPRETATION.

    Owner, schema, permission model, PII class and freshness SLA already enter
    the contract identity through the ODCS contract itself; this covers the rest
    of the connector configuration a reader would need to interpret the admitted
    bytes the same way.
    """
    return _digest(
        {
            "admitted_target": m365_admitted_target_digest(settings),
            "body_parser_identity": M365_BODY_PARSER_IDENTITY,
            "operational_schema": MERIDIAN_OPERATIONAL_SCHEMA,
            "operational_schema_version": MERIDIAN_OPERATIONAL_SCHEMA_VERSION,
            "config_revision_version": _CONFIG_REVISION_VERSION,
            "content_allowed_hosts": sorted(set(settings.content_allowed_hosts)),
            "max_content_bytes": settings.max_content_bytes,
            "max_content_redirects": settings.max_content_redirects,
            "source_link_allowed_hosts": sorted(set(settings.source_link_allowed_hosts)),
        }
    )


def m365_contract_identity(settings: M365ConnectorSettings) -> SourceContractIdentity:
    """The exact configured M365 ``SourceContractIdentity`` for these settings.

    Both the worker (from the full connector configuration) and the API (from a
    credential-free projection of it) call THIS function, so "API and worker
    report the same configured hash" holds by construction rather than by two
    implementations agreeing.
    """
    return m365_source_contract_identity(
        m365_data_contract(settings),
        mapper_revision=m365_source_link_mapper_revision(settings),
        config_revision=m365_config_revision(settings),
    )


def m365_contract_sla_seconds(settings: M365ConnectorSettings) -> Decimal:
    """The contract's exact fixed freshness SLA in seconds.

    Rejects the same calendar-ambiguous durations the contract factory rejects,
    because a receipt whose persisted SLA disagreed with the hashed one would
    make every freshness decision unfalsifiable.
    """
    seconds = _contract_fixed_duration_seconds(settings.freshness_sla)
    if seconds is None or seconds <= 0:
        raise ValueError(
            "M365 authority requires an exact fixed-duration freshness SLA "
            f"(got {settings.freshness_sla!r})"
        )
    return seconds


def m365_sweep_plan(settings: M365ConnectorSettings) -> SweepCadencePlan | None:
    """The configured sub-SLA permission-sweep plan, or ``None`` when unset.

    Feasibility is deliberately NOT decided here: the caller composing the
    producer decides, because a sweep that cannot beat the SLA must leave the
    scope Unknown and unadvertised rather than take down an unrelated process.
    """
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
