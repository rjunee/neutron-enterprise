# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Lock the package-local CDC ACL core to the SQL connector's (NE-084).

The CDC sink keeps a byte-identical copy of ``ne.connect.sql.acl`` /
``SqlTableAclConfig`` so it can stay ``dlt``-free at runtime. These tests import
BOTH (a test may afford dlt) and assert equal behavior so the copy can never
drift from the SQL connector's security core.
"""

from __future__ import annotations

import pytest

from ne.connect.cdc.acl import resolve_row_principals as cdc_resolve
from ne.connect.cdc.config import CdcTableAclConfig
from ne.connect.sql.acl import resolve_row_principals as sql_resolve
from ne.connect.sql.config import SqlTableAclConfig

_CASES = [
    (None, None),
    ("", None),
    ("   ", None),
    ("g-eng", None),
    ("  g-eng  ", None),
    ("g-eng,g-sales", ","),
    ("g-eng, g-sales ,g-eng", ","),  # dedupe + strip
    (["g-eng", "g-sales"], None),
    (["g-eng", 42], None),  # non-string taint
    ("acl:tenant-all", None),  # reserved-label taint (source data)
    ("role:admin", None),  # reserved-label taint
    (123, None),  # unsupported type taint
    (["g-eng", "acl:secret"], None),  # reserved item taint
]


@pytest.mark.parametrize(("value", "separator"), _CASES)
def test_resolve_row_principals_matches_sql_connector(value: object, separator: str | None) -> None:
    cdc_out = cdc_resolve(value, separator=separator)
    sql_out = sql_resolve(value, separator=separator)
    assert cdc_out.principals == sql_out.principals
    assert cdc_out.tainted == sql_out.tainted
    assert cdc_out.reason == sql_out.reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"principals": ("g-eng",)},
        {"principals": ("  g-eng  ", " g-sales ")},  # stored stripped
        {"principals": ("acl:tenant-all",)},  # the ONE reserved carve-out
        {"principal_column": "acl_group"},
        {"principal_column": "acl_group", "principal_separator": ","},
    ],
)
def test_static_acl_validator_matches_sql(kwargs: dict[str, object]) -> None:
    cdc_cfg = CdcTableAclConfig(**kwargs)  # type: ignore[arg-type]
    sql_cfg = SqlTableAclConfig(**kwargs)  # type: ignore[arg-type]
    assert cdc_cfg.principals == sql_cfg.principals
    assert cdc_cfg.principal_column == sql_cfg.principal_column
    assert cdc_cfg.principal_separator == sql_cfg.principal_separator


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither static nor column
        {"principals": ("g-eng",), "principal_column": "acl_group"},  # both
        {"principal_separator": ",", "principals": ("g-eng",)},  # separator w/o column
        {"principals": ("role:admin",)},  # reserved static (not the carve-out)
        {"principals": ("  ",)},  # empty-after-strip static
    ],
)
def test_invalid_acl_config_rejected_like_sql(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CdcTableAclConfig(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SqlTableAclConfig(**kwargs)  # type: ignore[arg-type]
