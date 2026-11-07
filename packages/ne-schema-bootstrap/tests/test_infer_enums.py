# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for enum candidate detection (``ne.schema_bootstrap.infer``)."""

from __future__ import annotations

from pathlib import Path

from ne.schema_bootstrap.config import BootstrapConfig
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples


def _class(model, name: str):
    (cls,) = [c for c in model.classes if c.name == name]
    return cls


def _slot(cls, name: str):
    (slot,) = [s for s in cls.slots if s.name == name]
    return slot


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def test_enum_boundary_24_distinct_over_4800_emits_sorted_enum(tmp_path: Path) -> None:
    rows = [[f"w{i}", f"cat{i % 24:02d}"] for i in range(4800)]
    _write_csv(tmp_path / "widgets.csv", ["id", "category"], rows)

    config = BootstrapConfig(rows_per_table=5000)
    model = infer_candidate_model(profile_samples(tmp_path, config=config), config=config)

    cls = _class(model, "WidgetsRecord")
    slot = _slot(cls, "category")
    assert slot.enum_name == "WidgetsRecordCategoryEnum"
    assert slot.range == "WidgetsRecordCategoryEnum"

    (enum,) = [e for e in model.enums if e.name == "WidgetsRecordCategoryEnum"]
    assert len(enum.permissible_values) == 24
    assert enum.permissible_values == sorted(enum.permissible_values)


def test_enum_too_many_distinct_rejected(tmp_path: Path) -> None:
    rows = [[f"w{i}", f"cat{i % 25:02d}"] for i in range(4800)]
    _write_csv(tmp_path / "widgets.csv", ["id", "category"], rows)

    config = BootstrapConfig(rows_per_table=5000)
    model = infer_candidate_model(profile_samples(tmp_path, config=config), config=config)

    slot = _slot(_class(model, "WidgetsRecord"), "category")
    assert slot.enum_name is None
    assert not any(e.name.endswith("CategoryEnum") for e in model.enums)


def test_enum_distinct_ratio_just_above_threshold_rejected(tmp_path: Path) -> None:
    # 3 distinct values over 20 rows = ratio 0.15 > enum_max_distinct_ratio(0.1),
    # even though distinct_count(3) is well under enum_max_distinct(24).
    rows = [[f"w{i}", f"cat{i % 3}"] for i in range(20)]
    _write_csv(tmp_path / "widgets.csv", ["id", "category"], rows)

    model = infer_candidate_model(profile_samples(tmp_path))

    slot = _slot(_class(model, "WidgetsRecord"), "category")
    assert slot.enum_name is None


def test_low_distinct_int_column_is_not_an_enum(tmp_path: Path) -> None:
    rows = [[f"w{i}", str(i % 3)] for i in range(30)]
    _write_csv(tmp_path / "widgets.csv", ["id", "priority"], rows)

    model = infer_candidate_model(profile_samples(tmp_path))

    cls = _class(model, "WidgetsRecord")
    slot = _slot(cls, "priority")
    assert slot.enum_name is None
    assert slot.range == "integer"


def test_domain_pattern_column_is_not_an_enum(tmp_path: Path) -> None:
    # Low-cardinality (3 distinct) but 100% domain-shaped -> pattern excludes
    # it from enum eligibility, independent of any PII signal (domain is not
    # a PII shape check, and "homepage" matches no PII name family).
    domains = ["acme.com", "globex.com", "initech.com"]
    rows = [[f"w{i}", domains[i % 3]] for i in range(30)]
    _write_csv(tmp_path / "widgets.csv", ["id", "homepage"], rows)

    model = infer_candidate_model(profile_samples(tmp_path))
    bundle = profile_samples(tmp_path)
    (table,) = bundle.profile.tables
    (col,) = [c for c in table.columns if c.name == "homepage"]
    assert col.format_pattern == "domain"
    assert col.pii_flag is False

    slot = _slot(_class(model, "WidgetsRecord"), "homepage")
    assert slot.enum_name is None


def test_pii_flagged_low_distinct_column_is_not_an_enum(tmp_path: Path) -> None:
    # Column name "ssn" trips the national_id NAME family -> pii_flag True
    # via name match (not value-shape), so distinct_values is suppressed and
    # the column can never become an enum candidate.
    rows = [[f"w{i}", ["111-22-3333", "222-33-4444"][i % 2]] for i in range(30)]
    _write_csv(tmp_path / "widgets.csv", ["id", "ssn"], rows)

    bundle = profile_samples(tmp_path)
    (table,) = bundle.profile.tables
    (col,) = [c for c in table.columns if c.name == "ssn"]
    assert col.pii_flag is True
    assert col.distinct_values is None

    model = infer_candidate_model(bundle)
    slot = _slot(_class(model, "WidgetsRecord"), "ssn")
    assert slot.enum_name is None
