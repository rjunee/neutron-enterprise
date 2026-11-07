# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""U2 operator surface for one-shot ACL mapping apply and read-only show."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema
from ne.permissions.config import PermissionsSettings
from ne.permissions.expand import PrincipalExpander
from ne.permissions.store import PrincipalStore, ensure_permissions_schema
from ne.permissions.sync import PrincipalSyncService

permissions_cli = importlib.import_module("ne.cli.permissions")
permissions = permissions_cli.permissions

_SF_LABEL = "sf-permset:0PS000000000AbCAAA"


class _TestSettings(NeSettings):
    suite: str = "permissions-mapping-cli"


@pytest.fixture
def frozen_config() -> Iterator[None]:
    reset_config_hash_for_testing()
    freeze_config_hash(_TestSettings())
    try:
        yield
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def platform(tmp_path: Path, frozen_config: None) -> Iterator[SimpleNamespace]:
    del frozen_config
    permissions_engine = sa.create_engine(f"sqlite:///{tmp_path / 'permissions.db'}")
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_permissions_schema(permissions_engine)
    ensure_outbox_schema(audit_engine)
    ensure_audit_schema(audit_engine)
    store = PrincipalStore(permissions_engine)
    sink = PostgresAuditSink(audit_engine)
    settings = PermissionsSettings(database_url=str(permissions_engine.url))
    value = SimpleNamespace(
        engines=[permissions_engine, audit_engine],
        expander=PrincipalExpander(store, max_depth=settings.max_expansion_depth),
        perm_store=store,
        perm_sync=PrincipalSyncService(store, sink),
        permissions_settings=settings,
        rebac_client=None,
        sink=sink,
    )
    try:
        yield value
    finally:
        for engine in value.engines:
            engine.dispose()


def _write_mapping(path: Path, principal: str = "dept-engineering") -> None:
    path.write_text(
        json.dumps(
            {"mappings": [{"principal": principal, "kind": "group", "grants": [_SF_LABEL]}]}
        ),
        encoding="utf-8",
    )


def test_mapping_apply_runs_audited_sync_and_populates_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: SimpleNamespace
) -> None:
    mapping_path = tmp_path / "mapping.json"
    _write_mapping(mapping_path)
    monkeypatch.setattr(permissions_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(permissions, ["mapping", "apply", "--path", str(mapping_path)])

    assert result.exit_code == 0, result.output
    assert "source=acl-mapping" in result.output
    assert "memberships_added=1" in result.output
    assert platform.perm_store.direct_groups_of(["dept-engineering"]) == {
        "dept-engineering": frozenset({_SF_LABEL})
    }
    with platform.sink.engine.connect() as conn:
        sync_events = conn.execute(
            sa.select(ne_audit_log.c.action, ne_audit_log.c.outcome).where(
                ne_audit_log.c.action == "permissions.principal_sync"
            )
        ).all()
    assert sync_events == [("permissions.principal_sync", "success")]


def test_mapping_apply_invalid_file_exits_nonzero_without_store_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: SimpleNamespace
) -> None:
    mapping_path = tmp_path / "invalid.json"
    _write_mapping(mapping_path, principal="sf-permset:inverted")
    monkeypatch.setattr(permissions_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(permissions, ["mapping", "apply", "--path", str(mapping_path)])

    assert result.exit_code == 1
    assert "mapping direction may be inverted" in result.output
    assert platform.perm_store.all_principal_ids() == set()


def test_mapping_apply_without_path_exits_nonzero_before_platform_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NE_PERMISSIONS_MAPPING_PATH", raising=False)

    def _unexpected_platform() -> object:
        raise AssertionError("missing path must fail before platform composition")

    monkeypatch.setattr(permissions_cli, "_build_platform", _unexpected_platform)

    result = CliRunner().invoke(permissions, ["mapping", "apply"])

    assert result.exit_code == 1
    assert "no mapping path configured" in result.output
    assert "NE_PERMISSIONS_MAPPING_PATH" in result.output


def test_mapping_show_uses_live_expander_and_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: SimpleNamespace
) -> None:
    mapping_path = tmp_path / "mapping.json"
    _write_mapping(mapping_path)
    monkeypatch.setattr(permissions_cli, "_build_platform", lambda: platform)
    runner = CliRunner()
    apply_result = runner.invoke(permissions, ["mapping", "apply", "--path", str(mapping_path)])
    assert apply_result.exit_code == 0, apply_result.output
    ids_before_show = platform.perm_store.all_principal_ids()

    mapped = runner.invoke(
        permissions, ["mapping", "show", "persona-p2", "--groups", "dept-engineering"]
    )
    unmapped = runner.invoke(permissions, ["mapping", "show", "persona-p4"])

    assert mapped.exit_code == 0, mapped.output
    assert _SF_LABEL in json.loads(mapped.output)
    assert unmapped.exit_code == 0, unmapped.output
    assert not any(label.startswith("sf-") for label in json.loads(unmapped.output))
    assert platform.perm_store.all_principal_ids() == ids_before_show


@pytest.mark.parametrize(
    "args",
    [
        ["mapping", "--help"],
        ["mapping", "apply", "--help"],
        ["mapping", "show", "--help"],
    ],
)
def test_mapping_help_renders(args: list[str]) -> None:
    result = CliRunner().invoke(permissions, args)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
