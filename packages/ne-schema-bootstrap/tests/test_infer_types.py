# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for parse-vote type inference and format-pattern detection
(``ne.schema_bootstrap.profiles``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.schema_bootstrap.errors import DuplicateClassNameError, DuplicateSlotNameError
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples


def _col(bundle, table_name: str, col_name: str):
    (table,) = [t for t in bundle.profile.tables if t.name == table_name]
    (col,) = [c for c in table.columns if c.name == col_name]
    return col


def test_clean_int_column_infers_int(tmp_path: Path) -> None:
    lines = ["id,qty"] + [f"r{i},{i}" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "qty").inferred_type == "int"


def test_clean_float_column_infers_float(tmp_path: Path) -> None:
    lines = ["id,amt"] + [f"r{i},{i}.5" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "amt").inferred_type == "float"


def test_clean_bool_column_infers_bool(tmp_path: Path) -> None:
    values = ["true", "false", "yes", "no", "t", "f", "TRUE", "False", "Yes", "No"]
    lines = ["id,active"] + [f"r{i},{v}" for i, v in enumerate(values)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "active").inferred_type == "bool"


def test_zero_one_column_infers_int_not_bool(tmp_path: Path) -> None:
    lines = ["id,flag"] + [f"r{i},{i % 2}" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "flag").inferred_type == "int"


def test_clean_date_column_infers_date(tmp_path: Path) -> None:
    lines = ["id,opened"] + [f"r{i},2024-01-{i + 1:02d}" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "opened").inferred_type == "date"


def test_clean_datetime_column_infers_datetime_not_date(tmp_path: Path) -> None:
    lines = ["id,opened"] + [f"r{i},2024-01-{i + 1:02d}T10:30:00" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "opened").inferred_type == "datetime"


def test_type_vote_threshold_boundary_19_of_20_infers_int(tmp_path: Path) -> None:
    values = [str(i) for i in range(1, 20)] + ["not-a-number"]  # 19/20 = 0.95
    lines = ["id,qty"] + [f"r{i},{v}" for i, v in enumerate(values)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "qty").inferred_type == "int"


def test_type_vote_threshold_boundary_18_of_20_falls_back_to_string(tmp_path: Path) -> None:
    values = [str(i) for i in range(1, 19)] + ["junk1", "junk2"]  # 18/20 = 0.90
    lines = ["id,qty"] + [f"r{i},{v}" for i, v in enumerate(values)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "qty").inferred_type == "string"


def test_format_pattern_email_positive_at_100_percent(tmp_path: Path) -> None:
    lines = ["id,addr"] + [f"r{i},u{i}@example.com" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "addr").format_pattern == "email"


def test_format_pattern_email_negative_below_threshold_90_percent(tmp_path: Path) -> None:
    values = [f"u{i}@example.com" for i in range(18)] + ["not-an-email", "also-not"]  # 0.90
    lines = ["id,addr"] + [f"r{i},{v}" for i, v in enumerate(values)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "addr").format_pattern is None


def test_format_pattern_url_positive(tmp_path: Path) -> None:
    lines = ["id,link"] + [f"r{i},https://example.com/page/{i}" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "link").format_pattern == "url"


def test_format_pattern_domain_positive(tmp_path: Path) -> None:
    domains = [f"sub{i}.example.com" for i in range(20)]
    lines = ["id,site"] + [f"r{i},{v}" for i, v in enumerate(domains)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    assert _col(bundle, "t", "site").format_pattern == "domain"


def test_format_pattern_never_set_on_non_string_column(tmp_path: Path) -> None:
    lines = ["id,qty"] + [f"r{i},{i}" for i in range(20)]
    (tmp_path / "t.csv").write_text("\n".join(lines) + "\n")
    bundle = profile_samples(tmp_path)
    col = _col(bundle, "t", "qty")
    assert col.inferred_type == "int"
    assert col.format_pattern is None


def test_duplicate_candidate_class_names_refused_naming_both_tables(tmp_path: Path) -> None:
    # `customer_orders.csv` and `customer-orders.csv` are distinct table
    # stems (so `profile_samples` succeeds, no read-time collision), but
    # both mechanically CamelCase to the same class name
    # (`CustomerOrdersRecord`) — refuse at inference time rather than
    # silently letting the second table's class shadow the first's.
    (tmp_path / "customer_orders.csv").write_text("id,amount\no1,10\no2,20\n")
    (tmp_path / "customer-orders.csv").write_text("id,amount\no1,30\no2,40\n")

    bundle = profile_samples(tmp_path)

    with pytest.raises(DuplicateClassNameError) as excinfo:
        infer_candidate_model(bundle)

    assert "customer_orders" in str(excinfo.value)
    assert "customer-orders" in str(excinfo.value)


def test_duplicate_mechanical_slot_names_refused_naming_both_columns(tmp_path: Path) -> None:
    # Codex C3: `first-name` and `first_name` are distinct column headers
    # (so `profile_samples` succeeds — reading doesn't care about
    # mechanical names), but `to_snake_case` collapses both to the same
    # `mechanical_name="first_name"` on the emitted `CandidateSlot`. A
    # schema consumer keyed by that normalized name can only represent one
    # of the two columns — refuse at inference time, the same as the
    # duplicate-class-name check above, rather than silently letting the
    # second column's slot collide with the first's.
    (tmp_path / "people.csv").write_text(
        "id,first-name,first_name\np1,Ada,Lovelace\np2,Grace,Hopper\n"
    )

    bundle = profile_samples(tmp_path)

    with pytest.raises(DuplicateSlotNameError) as excinfo:
        infer_candidate_model(bundle)

    assert "people" in str(excinfo.value)
    assert "first-name" in str(excinfo.value)
    assert "first_name" in str(excinfo.value)
