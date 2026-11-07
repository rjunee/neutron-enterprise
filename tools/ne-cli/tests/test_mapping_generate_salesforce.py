# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""U3 named-persona Salesforce ACL mapping generation."""

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
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.types import Principal
from ne.events import ensure_outbox_schema
from ne.permissions.expand import PrincipalExpander
from ne.permissions.mapping import AclMappingSource
from ne.permissions.models import PrincipalKind
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.permissions.sync import PrincipalSyncService

permissions_cli = importlib.import_module("ne.cli.permissions")
permissions = permissions_cli.permissions

CASE_READER_ID = "0PS000000000001AAA"
NO_CASE_ID = "0PS000000000002AAA"
PROFILE_ID = "00e000000000003AAA"


class _TestSettings(NeSettings):
    suite: str = "mapping-generate-salesforce"


@pytest.fixture
def frozen_config() -> Iterator[None]:
    reset_config_hash_for_testing()
    freeze_config_hash(_TestSettings())
    try:
        yield
    finally:
        reset_config_hash_for_testing()


def _write_manifest(
    path: Path,
    *,
    case_reader: object = CASE_READER_ID,
    no_case: object = NO_CASE_ID,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tenant": "salesforce",
                "seeded_at": "2026-08-17T00:00:00+00:00",
                "corpus_version": "test",
                "personas": {
                    "per-0006": "Acme_Case_Reader",
                    "per-0016": "Acme_No_Case",
                    "per-0000": None,
                    "per-0038": None,
                },
                "docs": {},
                "records": {},
                "channels": {},
                "messages": {},
                "permsets": {
                    "Acme_Case_Reader": case_reader,
                    "Acme_No_Case": no_case,
                },
                "grants": {},
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )


def _invoke(manifest: Path, *extra: str):
    return CliRunner().invoke(
        permissions,
        [
            "mapping",
            "generate-salesforce",
            "--manifest",
            str(manifest),
            "--executive-profile-id",
            PROFILE_ID,
            *extra,
        ],
    )


def _expected_document() -> dict[str, object]:
    return {
        "name": "salesforce-demo-acl-mapping",
        "mappings": [
            {
                "principal": "dept-engineering",
                "kind": "group",
                "grants": [f"sf-permset:{CASE_READER_ID}"],
            },
            {
                "principal": "dept-sales",
                "kind": "group",
                "grants": [f"sf-permset:{NO_CASE_ID}"],
            },
            {
                "principal": "executive",
                "kind": "group",
                "grants": [f"sf-profile:{PROFILE_ID}"],
            },
        ],
    }


def test_happy_path_writes_exact_named_persona_mapping(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document == _expected_document()
    entries = document["mappings"]
    assert len(entries) == 3
    assert "dept-legal" not in {entry["principal"] for entry in entries}
    assert all(entry["kind"] == PrincipalKind.GROUP.value for entry in entries)
    assert not any(entry["principal"].startswith("per-") for entry in entries)
    assert "dept-legal -> []" in result.stderr


def test_generated_mapping_round_trips_through_sync_and_expansion(
    tmp_path: Path, frozen_config: None
) -> None:
    del frozen_config
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    result = _invoke(manifest, "--out", str(output))
    assert result.exit_code == 0, result.output

    permissions_engine = sa.create_engine(f"sqlite:///{tmp_path / 'permissions.db'}")
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_permissions_schema(permissions_engine)
    ensure_outbox_schema(audit_engine)
    ensure_audit_schema(audit_engine)
    store = PrincipalStore(permissions_engine)
    try:
        source = AclMappingSource.from_path(output)
        asyncio.run(PrincipalSyncService(store, PostgresAuditSink(audit_engine)).run_once(source))
        expander = PrincipalExpander(store)

        engineering = expander.expand(Principal(id="persona-p2", groups=("dept-engineering",)))
        sales = expander.expand(Principal(id="persona-p3", groups=("dept-sales",)))
        legal = expander.expand(Principal(id="persona-p4", groups=("dept-legal",)))
        unmapped = expander.expand(Principal(id="persona-other", groups=("dept-other",)))
        executive = expander.expand(Principal(id="persona-p1", groups=("executive",)))

        assert {label for label in engineering if label.startswith("sf-")} == {
            f"sf-permset:{CASE_READER_ID}"
        }
        assert {label for label in sales if label.startswith("sf-")} == {f"sf-permset:{NO_CASE_ID}"}
        assert not any(label.startswith("sf-") for label in legal)
        assert not any(label.startswith("sf-") for label in unmapped)
        assert not any(store.is_known(pid) for pid in ("persona-p2", "persona-p3", "persona-p4"))

        # Exercise the real retrieval label seam against representative ACLs from
        # the demo corpus.  Engineering and executive each get their intended
        # positive, while sales/legal/unknown cannot see either protected row.
        case_acl = {f"sf-permset:{CASE_READER_ID}"}
        profile_acl = {f"sf-profile:{PROFILE_ID}"}
        engineering_labels = set(
            acl_match_labels(Principal(id="persona-p2", groups=("dept-engineering",)), expander)
        )
        sales_labels = set(
            acl_match_labels(Principal(id="persona-p3", groups=("dept-sales",)), expander)
        )
        legal_labels = set(
            acl_match_labels(Principal(id="persona-p4", groups=("dept-legal",)), expander)
        )
        unknown_labels = set(
            acl_match_labels(Principal(id="persona-other", groups=("dept-other",)), expander)
        )
        executive_labels = set(
            acl_match_labels(Principal(id="persona-p1", groups=("executive",)), expander)
        )

        assert engineering_labels & case_acl
        assert not engineering_labels & profile_acl
        assert executive_labels & profile_acl
        assert not sales_labels & (case_acl | profile_acl)
        assert not legal_labels & (case_acl | profile_acl)
        assert not unknown_labels & (case_acl | profile_acl)
        assert f"sf-profile:{PROFILE_ID}" in executive
    finally:
        permissions_engine.dispose()
        audit_engine.dispose()


def test_direction_guard_accepts_output_and_rejects_hand_inverted_mapping(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    result = _invoke(manifest, "--out", str(output))
    assert result.exit_code == 0, result.output

    document = json.loads(output.read_text(encoding="utf-8"))
    AclMappingSource.from_data(document)
    for entry in document["mappings"]:
        assert not entry["principal"].startswith("sf-")
        assert all(grant.startswith("sf-") for grant in entry["grants"])

    with pytest.raises(ValueError, match="mapping direction may be inverted"):
        AclMappingSource.from_data(
            {
                "mappings": [
                    {
                        "principal": f"sf-permset:{CASE_READER_ID}",
                        "grants": ["dept-engineering"],
                    }
                ]
            }
        )


def test_missing_manifest_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    output = tmp_path / "mapping.json"

    result = _invoke(tmp_path / "missing.json", "--out", str(output))

    assert result.exit_code == 1
    assert "missing.json" in result.output
    assert not output.exists()


def test_manifest_missing_case_reader_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["permsets"]["Acme_Case_Reader"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert "Acme_Case_Reader" in result.output
    assert not output.exists()


def test_manifest_missing_no_case_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["permsets"]["Acme_No_Case"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert "Acme_No_Case" in result.output
    assert not output.exists()


def test_missing_executive_profile_id_exits_nonzero_with_measurement_help(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)

    result = CliRunner().invoke(
        permissions,
        ["mapping", "generate-salesforce", "--manifest", str(manifest), "--out", str(output)],
    )

    assert result.exit_code == 1
    assert "measure the sf-profile: label on live Salesforce chunks" in result.output
    assert "--executive-profile-id" in result.output
    assert not output.exists()


def test_ill_shaped_executive_profile_id_exits_nonzero_and_writes_nothing(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)

    result = CliRunner().invoke(
        permissions,
        [
            "mapping",
            "generate-salesforce",
            "--manifest",
            str(manifest),
            "--executive-profile-id",
            "00e-too-short",
            "--out",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "15- or 18-character alphanumeric Salesforce id" in result.output
    assert not output.exists()


def test_permset_id_passed_as_executive_profile_fails_loudly(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)

    result = CliRunner().invoke(
        permissions,
        [
            "mapping",
            "generate-salesforce",
            "--manifest",
            str(manifest),
            "--executive-profile-id",
            CASE_READER_ID,
            "--out",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "must use Salesforce '00e' id prefix" in result.output
    assert not output.exists()


@pytest.mark.parametrize(
    ("case_reader", "no_case", "field"),
    [
        ("00e000000000004AAA", NO_CASE_ID, "Acme_Case_Reader"),
        (CASE_READER_ID, "not-a-salesforce-id", "Acme_No_Case"),
    ],
)
def test_invalid_manifest_permset_id_exits_nonzero_and_writes_nothing(
    tmp_path: Path, case_reader: object, no_case: object, field: str
) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest, case_reader=case_reader, no_case=no_case)

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert field in result.output
    assert not output.exists()


def test_existing_output_without_force_is_unchanged(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    output.write_text("do not replace\n", encoding="utf-8")

    result = _invoke(manifest, "--out", str(output))

    assert result.exit_code == 1
    assert "pass --force" in result.output
    assert output.read_text(encoding="utf-8") == "do not replace\n"


def test_force_replaces_existing_output_atomically(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    output = tmp_path / "mapping.json"
    _write_manifest(manifest)
    output.write_text("old\n", encoding="utf-8")

    result = _invoke(manifest, "--out", str(output), "--force")

    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8")) == _expected_document()


def test_prefixed_and_bare_profile_ids_produce_byte_identical_files(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    bare = tmp_path / "bare.json"
    prefixed = tmp_path / "prefixed.json"
    _write_manifest(manifest)
    runner = CliRunner()

    bare_result = _invoke(manifest, "--out", str(bare))
    prefixed_result = runner.invoke(
        permissions,
        [
            "mapping",
            "generate-salesforce",
            "--manifest",
            str(manifest),
            "--executive-profile-id",
            f"sf-profile:{PROFILE_ID}",
            "--out",
            str(prefixed),
        ],
    )

    assert bare_result.exit_code == 0, bare_result.output
    assert prefixed_result.exit_code == 0, prefixed_result.output
    assert bare.read_bytes() == prefixed.read_bytes()


def test_without_out_prints_only_mapping_body_to_stdout(tmp_path: Path) -> None:
    manifest = tmp_path / "salesforce.json"
    _write_manifest(manifest)

    result = _invoke(manifest)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _expected_document()
    assert "dept-engineering ->" in result.stderr


def test_default_manifest_path_is_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / ".ne" / "acme-seed" / "salesforce.json"
    _write_manifest(manifest)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(
        permissions,
        ["mapping", "generate-salesforce", "--executive-profile-id", PROFILE_ID],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _expected_document()


def test_generate_salesforce_help_renders() -> None:
    result = CliRunner().invoke(permissions, ["mapping", "generate-salesforce", "--help"])

    assert result.exit_code == 0, result.output
    assert "--manifest" in result.output
    assert "--executive-profile-id" in result.output
    assert "--out" in result.output
    assert "--force" in result.output
