# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AclMappingSource parsing, validation, sync, and expansion integration."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from ne.audit import PostgresAuditSink
from ne.brain.trimming import acl_match_labels
from ne.contracts.types import Principal
from ne.permissions import AclMappingSource
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import TENANT_ALL, PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService
from ne.permissions.testing.fixtures import FixturePrincipalSource

CASE_READER = "sf-permset:0PS000000000AbCAAA"
SALESFORCE_PROFILE = "sf-profile:00e000000000XyZAAA"
M365_OPS = "306773cc-b05a-43b4-bbb2-7858bfdb67dc"
NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _data() -> dict[str, Any]:
    return {
        "name": "demo-acl-mapping",
        "mappings": [
            {"principal": "dept-engineering", "grants": [CASE_READER]},
            {
                "principal": "dept-sales",
                "kind": "group",
                "grants": [SALESFORCE_PROFILE, CASE_READER, CASE_READER],
            },
        ],
    }


def _run_sync(
    source: AclMappingSource | FixturePrincipalSource,
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
) -> None:
    asyncio.run(PrincipalSyncService(store, audit_sink).run_once(source))


def _assert_value_error(data: dict[str, Any], offending: object) -> None:
    with pytest.raises(ValueError, match=re.escape(repr(offending))):
        AclMappingSource.from_data(data)


def test_parse_round_trip_is_stable_and_direction_explicit() -> None:
    source = AclMappingSource.from_data(_data())
    first = asyncio.run(source.fetch({"demo-acl-mapping:cursor": "unchanged"}))
    second = asyncio.run(source.fetch({"demo-acl-mapping:cursor": "unchanged"}))

    assert source.name == "demo-acl-mapping"
    assert first == second
    assert [(p.principal_id, p.kind) for p in first.principals] == [
        ("dept-engineering", PrincipalKind.GROUP),
        ("dept-sales", PrincipalKind.GROUP),
        (CASE_READER, PrincipalKind.GROUP),
        (SALESFORCE_PROFILE, PrincipalKind.GROUP),
    ]
    assert [(edge.member_id, edge.group_id) for edge in first.edges] == [
        ("dept-engineering", CASE_READER),
        ("dept-sales", SALESFORCE_PROFILE),
        ("dept-sales", CASE_READER),
    ]
    assert first.replaced_members == ("dept-engineering", "dept-sales")
    assert first.is_full is True
    assert dict(first.state) == {"demo-acl-mapping:cursor": "unchanged"}


def test_from_path_loads_utf8_json(tmp_path: Path) -> None:
    path = tmp_path / "acl-mapping.json"
    path.write_text(json.dumps(_data()), encoding="utf-8")

    source = AclMappingSource.from_path(path)

    assert source.name == "demo-acl-mapping"


def test_public_export_and_entry_point_registration() -> None:
    eps = entry_points(group="ne.plugins.principal_source")

    assert eps["acl-mapping"].load() is AclMappingSource


def test_sync_and_expansion_acceptance_path(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    _run_sync(AclMappingSource.from_data(_data()), store, audit_sink)
    expander = PrincipalExpander(store)

    p2 = expander.expand(Principal(id="sub-p2", groups=("dept-engineering",)))
    p4 = expander.expand(Principal(id="sub-p4", groups=("dept-legal",)))
    unknown = expander.expand(Principal(id="sub-unknown"))

    assert CASE_READER in p2
    assert not any(label.startswith("sf-") for label in p4)
    assert not any(label.startswith("sf-") for label in unknown)


def test_m365_direction_marker_persists_raw_acl_identity_and_expands(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    source = AclMappingSource(
        entries=[
            {
                "principal": "meridian-ops",
                "kind": "group",
                "grants": [f"m365-group:{M365_OPS}"],
            }
        ]
    )
    _run_sync(source, store, audit_sink)

    labels = PrincipalExpander(store).expand(
        Principal(id="keycloak-subject", groups=("meridian-ops",))
    )

    assert M365_OPS in labels
    assert f"m365-group:{M365_OPS}" not in labels


@pytest.mark.parametrize(
    "grant",
    [
        "m365-group:not-a-uuid",
        "m365-group:306773CC-b05a-43b4-bbb2-7858bfdb67dc",
        "m365-group:306773cc-b05a-43b4-bbb2-7858bfdb67d",
    ],
)
def test_validation_rejects_noncanonical_m365_group_grant(grant: str) -> None:
    _assert_value_error({"mappings": [{"principal": "meridian-ops", "grants": [grant]}]}, grant)


def test_mapping_is_one_way(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    _run_sync(AclMappingSource.from_data(_data()), store, audit_sink)

    result = PrincipalExpander(store).expand(Principal(id=CASE_READER))

    assert not any(label.startswith("dept-") for label in result)


def test_replacing_and_removing_mapping_revokes_edges(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    service = PrincipalSyncService(store, audit_sink)
    principal = Principal(id="sub-p2", groups=("dept-engineering",))
    expander = PrincipalExpander(store)

    v1 = AclMappingSource(entries=[{"principal": "dept-engineering", "grants": [CASE_READER]}])
    asyncio.run(service.run_once(v1))
    assert CASE_READER in expander.expand(principal)

    v2 = AclMappingSource(
        entries=[{"principal": "dept-engineering", "grants": [SALESFORCE_PROFILE]}]
    )
    asyncio.run(service.run_once(v2))
    assert CASE_READER not in expander.expand(principal)
    assert SALESFORCE_PROFILE in expander.expand(principal)

    asyncio.run(service.run_once(AclMappingSource(entries=[])))
    assert not store.is_known("dept-engineering")
    assert store.direct_groups_of(["dept-engineering"]) == {}
    assert not any(label.startswith("sf-") for label in expander.expand(principal))


def test_full_mapping_sync_is_source_scoped(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    store.upsert_principals(
        [SyncedPrincipal(principal_id="entra-user", kind=PrincipalKind.USER)],
        source="entra",
        now=NOW,
    )

    _run_sync(AclMappingSource.from_data(_data()), store, audit_sink)

    assert store.is_known("entra-user")


@pytest.mark.parametrize(
    ("principal", "grant"),
    [
        ("acl:tenant-all", CASE_READER),
        ("role:admin", CASE_READER),
        ("dept-engineering", "acl:tenant-all"),
        ("dept-engineering", "role:admin"),
    ],
)
def test_validation_rejects_reserved_namespaces_on_both_sides(principal: str, grant: str) -> None:
    _assert_value_error(
        {"mappings": [{"principal": principal, "grants": [grant]}]},
        principal if principal.startswith(("acl:", "role:")) else grant,
    )


@pytest.mark.parametrize("principal", [SALESFORCE_PROFILE, CASE_READER])
def test_validation_rejects_source_native_principal(principal: str) -> None:
    _assert_value_error(
        {"mappings": [{"principal": principal, "grants": ["source-label"]}]}, principal
    )


def test_validation_rejects_non_source_native_grant() -> None:
    _assert_value_error(
        {"mappings": [{"principal": "dept-engineering", "grants": ["dept-legal"]}]},
        "dept-legal",
    )


def test_validation_rejects_grant_that_is_also_a_principal() -> None:
    _assert_value_error(
        {
            "mappings": [
                {"principal": "dept-engineering", "grants": ["dept-sales"]},
                {"principal": "dept-sales", "grants": [CASE_READER]},
            ]
        },
        "dept-sales",
    )


def test_validation_rejects_duplicate_principal_entries() -> None:
    _assert_value_error(
        {
            "mappings": [
                {"principal": "dept-engineering", "grants": [CASE_READER]},
                {"principal": "dept-engineering", "grants": [SALESFORCE_PROFILE]},
            ]
        },
        "dept-engineering",
    )


@pytest.mark.parametrize(
    ("entry", "offending"),
    [
        ({"principal": "", "grants": [CASE_READER]}, ""),
        ({"grants": [CASE_READER]}, None),
        ({"principal": "dept-engineering"}, None),
        ({"principal": "dept-engineering", "grants": []}, []),
        ({"principal": "dept-engineering", "grants": [42]}, 42),
    ],
)
def test_validation_rejects_empty_principal_or_invalid_grants(
    entry: dict[str, Any], offending: object
) -> None:
    _assert_value_error({"mappings": [entry]}, offending)


def test_validation_rejects_unknown_kind() -> None:
    _assert_value_error(
        {
            "mappings": [
                {"principal": "dept-engineering", "kind": "service", "grants": [CASE_READER]}
            ]
        },
        "service",
    )


def test_inverted_edge_fails_closed_at_expansion(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    inverted = FixturePrincipalSource(
        "inverted-mapping",
        [
            {
                "principals": [
                    {"principal_id": "dept-engineering", "kind": "group"},
                    {"principal_id": CASE_READER, "kind": "group"},
                ],
                # Deliberate direction mutant: source label -> product group.
                "edges": [{"member_id": CASE_READER, "group_id": "dept-engineering"}],
                "replaced_members": [CASE_READER],
                "is_full": True,
            }
        ],
    )
    _run_sync(inverted, store, audit_sink)

    principal = Principal(id="sub-p2", groups=("dept-engineering",))
    labels = acl_match_labels(principal, PrincipalExpander(store))

    assert CASE_READER not in labels
    assert not set(labels) & {CASE_READER, SALESFORCE_PROFILE}


def test_tenant_all_widening_is_limited_to_user_keyed_mapping(
    store: PrincipalStore,
    audit_sink: PostgresAuditSink,
    frozen_config: str,
) -> None:
    del frozen_config
    service = PrincipalSyncService(store, audit_sink)
    expander = PrincipalExpander(store)
    group_keyed = AclMappingSource(
        entries=[{"principal": "dept-engineering", "grants": [CASE_READER]}]
    )
    asyncio.run(service.run_once(group_keyed))

    group_result = expander.expand(Principal(id="sub-p2", groups=("dept-engineering",)))
    assert CASE_READER in group_result
    assert TENANT_ALL not in group_result

    user_keyed = AclMappingSource(
        entries=[{"principal": "sub-p2", "kind": "user", "grants": [CASE_READER]}]
    )
    asyncio.run(service.run_once(user_keyed))

    # Deliberate and documented: a user-keyed subject is now store-known.
    assert TENANT_ALL in expander.expand(Principal(id="sub-p2"))
