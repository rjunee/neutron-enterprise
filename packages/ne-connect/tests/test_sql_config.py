# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlTableConfig / SqlConnectorSettings validation — fail-closed by construction.

THE unit invariant lives here: a table configured WITHOUT an ``acl`` block is
UNREPRESENTABLE ("no config != public").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pydantic
import pytest

from ne.connect.contracts import PermissionMode
from ne.connect.sql.config import SqlTableAclConfig, SqlTableConfig
from ne.connect.sql.contract import sql_data_contract

from .sql_fixture import sql_settings


def _make_table(**overrides: Any) -> SqlTableConfig:
    kwargs: dict[str, Any] = {
        "table": "docs",
        "id_column": "id",
        "content_columns": ("title", "body"),
    }
    kwargs.update(overrides)
    return SqlTableConfig(**kwargs)  # type: ignore[arg-type]


# -- (a) THE fail-closed test ------------------------------------------------


def test_table_without_acl_is_unrepresentable() -> None:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        _make_table()
    assert "acl" in str(excinfo.value)


# -- (b) acl XOR --------------------------------------------------------------


def test_acl_both_principals_and_column_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principals=("g-eng",), principal_column="owner")


def test_acl_neither_principals_nor_column_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig()


def test_acl_static_only_accepts() -> None:
    cfg = SqlTableAclConfig(principals=("g-eng",))
    assert cfg.principals == ("g-eng",)


def test_static_principals_are_stored_stripped() -> None:
    # Codex P2: validation used .strip() but the model kept the raw value, so a
    # padded principal (" g-eng ") emitted a whitespace-laden ACL that never matched.
    cfg = SqlTableAclConfig(principals=(" g-eng ", "\tg-sales\n"))
    assert cfg.principals == ("g-eng", "g-sales")


def test_static_padded_tenant_all_is_recognized() -> None:
    # A padded reserved label must be stored stripped so it IS recognized as
    # tenant-wide (previously " acl:tenant-all " passed the check but stayed padded).
    cfg = SqlTableAclConfig(principals=(" acl:tenant-all ",))
    assert cfg.principals == ("acl:tenant-all",)


def test_acl_column_only_accepts() -> None:
    cfg = SqlTableAclConfig(principal_column="owner_principal")
    assert cfg.principal_column == "owner_principal"


# -- (c) reserved static labels -----------------------------------------------


def test_static_role_prefix_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principals=("role:admin",))


def test_static_acl_prefix_other_than_tenant_all_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principals=("acl:public-link",))


def test_static_exactly_tenant_all_accepted() -> None:
    cfg = SqlTableAclConfig(principals=("acl:tenant-all",))
    assert cfg.principals == ("acl:tenant-all",)


def test_static_empty_principal_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principals=("  ",))


# -- (d) principal_separator without principal_column ------------------------


def test_separator_without_column_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principals=("g-eng",), principal_separator=",")


# -- (e) identifier validation (injection-shaped names) -----------------------


def test_table_name_injection_shape_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _make_table(table="users; DROP TABLE x", acl=SqlTableAclConfig(principals=("g",)))


def test_id_column_injection_shape_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _make_table(id_column="id --", acl=SqlTableAclConfig(principals=("g",)))


def test_principal_column_injection_shape_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        SqlTableAclConfig(principal_column="owner; DROP TABLE x")


def test_valid_identifiers_accepted() -> None:
    cfg = _make_table(acl=SqlTableAclConfig(principals=("g-eng",)))
    assert cfg.table == "docs"


# -- (f) metadata_columns reserved-key collisions -----------------------------


@pytest.mark.parametrize(
    "reserved_key",
    ["connector", "source_id", "record_id", "pii_class", "table", "source", "provenance"],
)
def test_metadata_column_reserved_key_collision_rejected(reserved_key: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        _make_table(metadata_columns=(reserved_key,), acl=SqlTableAclConfig(principals=("g",)))


# -- (g) settings invariants ---------------------------------------------------


def test_settings_empty_tables_rejected(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        sql_settings(tmp_path, tables=())


def test_settings_empty_owner_rejected(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        sql_settings(tmp_path, owner="")


def test_settings_duplicate_table_refs_rejected(tmp_path: Path) -> None:
    docs = _make_table(acl=SqlTableAclConfig(principals=("g-eng",)))
    with pytest.raises(pydantic.ValidationError):
        sql_settings(tmp_path, tables=(docs, docs))


def test_settings_empty_connection_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        sql_settings(tmp_path, connection_url="")


# -- (h) sql_data_contract fail-closed backstop -------------------------------


def test_sql_data_contract_is_source_acl_fail_closed_backstop(tmp_path: Path) -> None:
    settings = sql_settings(tmp_path, principal_namespace="entra")
    contract = sql_data_contract(settings)
    assert contract.permissions.mode is PermissionMode.SOURCE_ACL
    assert contract.permissions.default_acl == ()
    assert contract.permission_model == "source-acl"
    assert contract.permissions.principal_namespace == "entra"
