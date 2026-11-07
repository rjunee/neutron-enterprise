# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Meridian-only Salesforce ACL mapping, source binding, and deny controls."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.trimming import acl_match_labels
from ne.connect.salesforce import map_object_permissions
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.mapping import AclMappingSource
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.permissions.sync import PrincipalSyncService

permissions = importlib.import_module("ne.cli.permissions").permissions

OPS_ID = "0PS000000000011AAA"
APPROVER_ID = "0PS000000000012AAA"
FRONTLINE_ID = "0PS000000000013AAA"
PERSONAS = {
    "ops": "Meridian_Ops_Reader",
    "approver": "Meridian_Approver_Reader",
    "frontline": "Meridian_Frontline_Reader",
}
PERMSETS = {
    "Meridian_Ops_Reader": OPS_ID,
    "Meridian_Approver_Reader": APPROVER_ID,
    "Meridian_Frontline_Reader": FRONTLINE_ID,
}


class _TestSettings(NeSettings):
    suite: str = "mapping-generate-meridian-salesforce"


@pytest.fixture
def frozen_config() -> Iterator[None]:
    reset_config_hash_for_testing()
    freeze_config_hash(_TestSettings())
    try:
        yield
    finally:
        reset_config_hash_for_testing()


def _write_manifest(path: Path, **updates: object) -> None:
    payload: dict[str, object] = {
        "tenant": "salesforce",
        "corpus_version": "meridian-v1",
        "personas": PERSONAS,
        "permsets": PERMSETS,
    }
    payload.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _invoke(manifest: Path, *extra: str):
    return CliRunner().invoke(
        permissions,
        ["mapping", "generate-meridian-salesforce", "--manifest", str(manifest), *extra],
    )


def _expected_document() -> dict[str, object]:
    return {
        "name": "meridian-salesforce-demo-acl-mapping",
        "mappings": [
            {
                "principal": "meridian-ops",
                "kind": "group",
                "grants": [f"sf-permset:{OPS_ID}"],
            },
            {
                "principal": "meridian-approver",
                "kind": "group",
                "grants": [f"sf-permset:{APPROVER_ID}"],
            },
            {
                "principal": "meridian-frontline",
                "kind": "group",
                "grants": [f"sf-permset:{FRONTLINE_ID}"],
            },
        ],
    }


def test_emits_only_exact_meridian_persona_edges(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8")) == _expected_document()
    assert "Acme" not in output.read_text(encoding="utf-8")
    assert "executive" not in output.read_text(encoding="utf-8")
    assert "meridian-frontline ->" in result.stderr


@pytest.mark.parametrize(
    "updates",
    [
        {"tenant": "m365"},
        {"corpus_version": "acme-v1"},
        {"personas": {"ops": "Meridian_Ops_Reader"}},
        {"personas": {**PERSONAS, "extra": "Meridian_Extra_Reader"}},
        {"permsets": {key: value for key, value in PERMSETS.items() if "Frontline" not in key}},
        {"permsets": {**PERMSETS, "Meridian_Extra_Reader": "0PS000000000014AAA"}},
        {"permsets": {**PERMSETS, "Meridian_Frontline_Reader": "00e000000000013AAA"}},
    ],
)
def test_unattested_manifest_writes_nothing_and_names_fallback(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest, **updates)

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert "Mapping is unattested; Salesforce content remains invisible" in result.output
    assert "write-back-only fallback" in result.output
    assert not output.exists()


def test_existing_output_is_not_replaced_without_force(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    output.write_text("keep\n", encoding="utf-8")

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert output.read_text(encoding="utf-8") == "keep\n"


def test_mapping_composes_with_connector_permissions_and_bidirectional_trimming(
    tmp_path: Path, frozen_config: None
) -> None:
    del frozen_config
    manifest = tmp_path / "salesforce.json"
    _write_manifest(manifest)
    result = _invoke(manifest)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)

    permissions_engine = sa.create_engine(f"sqlite:///{tmp_path / 'permissions.db'}")
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_permissions_schema(permissions_engine)
    ensure_outbox_schema(audit_engine)
    ensure_audit_schema(audit_engine)
    try:
        source = AclMappingSource.from_data(document)
        store = PrincipalStore(permissions_engine)
        asyncio.run(PrincipalSyncService(store, PostgresAuditSink(audit_engine)).run_once(source))
        expander = PrincipalExpander(store)

        rows = [
            {
                "SobjectType": sobject,
                "PermissionsRead": True,
                "Parent": {"IsOwnedByProfile": False, "Id": permset_id},
            }
            for permset_id, objects in (
                (OPS_ID, ("Case",)),
                (APPROVER_ID, ("Account", "Contact", "Case")),
                (FRONTLINE_ID, ("Account", "Contact")),
            )
            for sobject in objects
        ]
        outcomes = map_object_permissions(rows, ("Account", "Contact", "Case"))
        assert all(not outcome.tainted and not outcome.dropped for outcome in outcomes.values())
        assert set(outcomes["Case"].principals) == {
            f"sf-permset:{OPS_ID}",
            f"sf-permset:{APPROVER_ID}",
        }

        labels = {
            persona: set(
                acl_match_labels(Principal(id=f"user:{persona}", groups=(group,)), expander)
            )
            for persona, group in (
                ("ops", "meridian-ops"),
                ("approver", "meridian-approver"),
                ("frontline", "meridian-frontline"),
                ("unknown", "meridian-unknown"),
            )
        }
        acl = {name: set(outcome.principals) for name, outcome in outcomes.items()}

        assert labels["ops"] & acl["Case"]
        assert not labels["ops"] & acl["Account"]
        assert labels["approver"] & acl["Account"]
        assert labels["approver"] & acl["Contact"]
        assert labels["approver"] & acl["Case"]
        assert labels["frontline"] & acl["Account"]
        assert labels["frontline"] & acl["Contact"]
        assert not labels["frontline"] & acl["Case"]
        assert not labels["unknown"] & set().union(*acl.values())
    finally:
        permissions_engine.dispose()
        audit_engine.dispose()


def test_help_names_only_manifest_and_atomic_output_controls() -> None:
    result = CliRunner().invoke(permissions, ["mapping", "generate-meridian-salesforce", "--help"])
    assert result.exit_code == 0, result.output
    assert "--manifest" in result.output
    assert "--out" in result.output
    assert "--force" in result.output
    assert "--executive-profile-id" not in result.output
