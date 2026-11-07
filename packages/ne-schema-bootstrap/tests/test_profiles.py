# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.profiles`` (the profiler)."""

from __future__ import annotations

import json
from pathlib import Path

from ne.schema_bootstrap.config import BootstrapConfig
from ne.schema_bootstrap.profiles import profile_samples


def _table_named(bundle, name: str):
    (table,) = [t for t in bundle.profile.tables if t.name == name]
    return table


def _col_named(table, name: str):
    (col,) = [c for c in table.columns if c.name == name]
    return col


def test_null_nonnull_distinct_ratio_exact(tmp_path: Path) -> None:
    # 6 data rows: "status" has 2 nulls, 4 non-null with 2 distinct values.
    (tmp_path / "widgets.csv").write_text(
        "id,status\nw1,active\nw2,active\nw3,\nw4,inactive\nw5,\nw6,inactive\n"
    )

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "status")

    assert col.null_count == 2
    assert col.non_null_count == 4
    assert col.distinct_count == 2
    assert col.distinct_ratio == 0.5


def test_distinct_ratio_zero_when_all_null(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,note\nw1,\nw2,\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "note")

    assert col.non_null_count == 0
    assert col.distinct_ratio == 0.0


def test_min_max_only_on_orderable_types_rendered_as_str(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text(
        "id,qty,opened_at,label\n"
        "w1,10,2024-03-01,zeta\n"
        "w2,3,2024-01-15,alpha\n"
        "w3,7,2024-02-10,mid\n"
    )

    bundle = profile_samples(tmp_path)
    table = _table_named(bundle, "widgets")

    qty = _col_named(table, "qty")
    assert qty.inferred_type == "int"
    assert (qty.min_value, qty.max_value) == ("3", "10")

    opened = _col_named(table, "opened_at")
    assert opened.inferred_type == "date"
    assert (opened.min_value, opened.max_value) == ("2024-01-15", "2024-03-01")

    label = _col_named(table, "label")
    assert label.inferred_type == "string"
    assert (label.min_value, label.max_value) == (None, None)


def test_min_max_retained_for_bare_digit_run_column_not_falsely_pii_flagged(
    tmp_path: Path,
) -> None:
    # 2026-07-28 Argus P2: the phone-shape heuristic used to over-fire on any
    # bare 7-20 digit numeric column (no separator required), and the PII
    # min/max suppression gate then silently amplified that into a BLANKED
    # numeric range on a genuinely non-PII, seed-anchoring-relevant column
    # (`order_number` is a literal Order attribute in seeds.py). This is the
    # mandatory bidirectional control for that fix: an opaque, separator-free
    # digit-run column must NOT be PII-flagged and must retain its min/max.
    lines = ["id,order_number"] + [f"r{i},100000{i}" for i in range(1, 5)]
    (tmp_path / "orders.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "orders"), "order_number")

    assert col.pii_flag is False
    assert col.pii_reason is None
    assert col.inferred_type == "int"
    assert (col.min_value, col.max_value) == ("1000001", "1000004")


def test_min_max_retained_for_seven_digit_float_column(tmp_path: Path) -> None:
    # 2026-07-28 SPEC §6.1.1 arbiter ruling: the value-shape phone check ran
    # regardless of the column's inferred type, so a 7-digit FLOAT column
    # (e.g. "12345.67") could coincidentally satisfy the phone-shape charset
    # (digits plus "." as a "separator") and get silently PII-suppressed —
    # the arbiter reproduced this exactly. Gating value-shape checks on
    # `inferred_type == "string"` fixes it at the pipeline level, not just
    # the unit level (see
    # `tests/test_pii.py::test_numeric_amount_and_balance_columns_not_flagged_as_phone`
    # for the direct `pii_flags` reproduction).
    lines = ["id,amount"] + [f"r{i},{12345 + i}.67" for i in range(20)]
    (tmp_path / "ledger.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "ledger"), "amount")

    assert col.inferred_type == "float"
    assert col.pii_flag is False
    assert col.pii_reason is None
    assert col.min_value is not None
    assert col.max_value is not None
    assert len(col.exemplars) > 0


def test_min_max_datetime_column_mixed_aware_and_naive_does_not_crash(tmp_path: Path) -> None:
    # Codex C1: an ISO-8601 datetime column can freely mix timezone-AWARE
    # values ("...Z" / "...+00:00") and NAIVE values ("...T00:00:00") — both
    # forms pass `_is_datetime` individually — but Python refuses to order
    # an aware `datetime` against a naive one, so `min()`/`max()` over the
    # parsed list used to raise `TypeError`, crashing `profile_samples` on
    # otherwise-valid sample data. `_min_max_raw` must normalize to a total
    # order (naive-UTC) instead of raising, while still rendering the
    # ORIGINAL (non-normalized) value via `str()`.
    values = [
        "2024-06-15T12:00:00",  # naive, mid
        "2024-01-01T00:00:00Z",  # aware UTC, earliest instant
        "2024-12-31T23:59:59+00:00",  # aware +00:00, latest instant
        "2024-03-01T08:30:00+05:00",  # aware +05:00 -> 2024-03-01T03:30:00 UTC
    ]
    lines = ["id,opened_at"] + [f"r{i},{v}" for i, v in enumerate(values)]
    (tmp_path / "events.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "events"), "opened_at")

    assert col.inferred_type == "datetime"
    # Earliest by normalized-UTC instant is the "Z"-suffixed AWARE value
    # (rendered with its +00:00 offset); latest is the explicit +00:00
    # AWARE value. Neither bound is the naive mid-value or the +05:00 value
    # — proving both the crash is gone AND the ordering is genuinely
    # correct across the aware/naive mix, not merely non-crashing.
    assert col.min_value == "2024-01-01 00:00:00+00:00"
    assert col.max_value == "2024-12-31 23:59:59+00:00"


def test_distinct_values_deduplicated_after_truncation_not_only_before(tmp_path: Path) -> None:
    # Codex C2: two RAW-distinct values that share their first
    # `exemplar_max_chars` characters become IDENTICAL once retained
    # (scrubbed + truncated). Deduplicating only the raw values (pre-
    # retention) lets both survive as separate entries in what is supposed
    # to be a DISTINCT list — a duplicated/malformed enum-candidate
    # downstream. Dedup must happen on the RETAINED value.
    shared_prefix = "A" * 80
    raw_a = shared_prefix + "-one-tail"
    raw_b = shared_prefix + "-two-tail"
    assert raw_a != raw_b  # distinct RAW values...
    lines = ["id,note", f"r0,{raw_a}", f"r1,{raw_b}"]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "note")

    assert col.pii_flag is False
    assert col.distinct_values is not None
    # ...but identical once truncated to `exemplar_max_chars` (80) — the
    # retained list must collapse to ONE entry, not two.
    assert col.distinct_values == [shared_prefix]


def test_unique_in_sample_true_direction(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,code\nw1,c1\nw2,c2\nw3,c3\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "code")

    assert col.unique_in_sample is True


def test_unique_in_sample_false_on_duplicate_value(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,code\nw1,c1\nw2,c1\nw3,c3\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "code")

    assert col.unique_in_sample is False


def test_unique_in_sample_false_when_a_null_is_present(tmp_path: Path) -> None:
    # All non-null values are individually unique, but a null disqualifies it.
    (tmp_path / "widgets.csv").write_text("id,code\nw1,c1\nw2,c2\nw3,\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "code")

    assert col.unique_in_sample is False


def test_exemplars_are_first_k_distinct_in_sampled_order(tmp_path: Path) -> None:
    lines = ["id,tag"] + [f"w{i},tag{i}" for i in range(50)]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "tag")

    assert col.distinct_count == 50
    assert col.exemplars == ["tag0", "tag1", "tag2", "tag3", "tag4"]
    assert len(col.exemplars) == 5


def test_exemplars_truncated_to_max_chars(tmp_path: Path) -> None:
    long_value = "x" * 200
    (tmp_path / "widgets.csv").write_text(f"id,blob\nw1,{long_value}\nw2,{long_value}y\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "blob")

    assert all(len(v) <= 80 for v in col.exemplars)
    assert len(col.exemplars) > 0


def test_distinct_values_populated_when_at_or_under_bound(tmp_path: Path) -> None:
    lines = ["id,category"] + [f"w{i},cat{i % 24}" for i in range(48)]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "category")

    assert col.distinct_count == 24
    assert col.distinct_values is not None
    assert len(col.distinct_values) == 24
    assert col.distinct_values == sorted(col.distinct_values)


def test_distinct_values_absent_above_bound_pathological_column_leaks_nothing(
    tmp_path: Path,
) -> None:
    # 10,000 genuinely-distinct values; row cap (default 200) bounds how many
    # ever get sampled/profiled, and distinct_count over the sample already
    # exceeds enum_max_distinct(24), so distinct_values must be None — a
    # pathological high-cardinality column serializes with NO value list.
    lines = ["id,uid"] + [f"w{i},uid-{i:06d}" for i in range(10_000)]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    table = _table_named(bundle, "widgets")
    col = _col_named(table, "uid")

    assert table.row_count_seen == 10_000
    assert table.row_count_sampled == 200
    assert col.distinct_count > 24
    assert col.distinct_values is None
    assert len(col.exemplars) <= 5


def test_enormous_jsonl_cell_profile_stays_bounded(tmp_path: Path) -> None:
    # JSONL has no csv field-size limit at all — a single pathologically
    # large string value must still be bounded by the profiler itself
    # (exemplar truncation), proven over the SERIALIZED profile, not just
    # the in-memory ColumnProfile object.
    huge_value = "y" * 500_000
    lines = [
        f'{{"id": "r1", "blob": "{huge_value}"}}',
        '{"id": "r2", "blob": "short"}',
    ]
    (tmp_path / "widgets.jsonl").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    table = _table_named(bundle, "widgets")
    col = _col_named(table, "blob")

    serialized = bundle.profile.model_dump_json()

    assert col.pii_flag is False  # "blob" is not a PII-name-family column
    assert len(col.exemplars) > 0
    for exemplar in col.exemplars:
        assert len(exemplar) <= 80  # DEFAULT_CONFIG.exemplar_max_chars
    assert huge_value not in serialized


def test_min_max_bounded_and_scrubbed_in_serialized_profile(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2: `_min_max` used to bypass BOTH the exemplar
    # length bound and the credential scrubber entirely — a legitimate
    # (non-PII-named) orderable value long enough to be a real customer
    # secret would reach `min_value`/`max_value` raw and unbounded even
    # though the SAME column's exemplars/distinct_values were already
    # bounded. `huge_int` is under CPython's ~4300-digit int-string
    # conversion limit, so it is a genuine, correctly-typed "int" value —
    # this isn't the non-finite-overflow case below, it's the plain
    # unbounded-retention defect.
    huge_int = "9" * 4000
    lines = [
        json.dumps({"id": "r1", "acct": "1"}),
        json.dumps({"id": "r2", "acct": "2"}),
        json.dumps({"id": "r3", "acct": huge_int}),
    ]
    (tmp_path / "accounts.jsonl").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    serialized = bundle.profile.model_dump_json()
    col = _col_named(_table_named(bundle, "accounts"), "acct")

    assert col.inferred_type == "int"
    assert col.max_value is not None
    assert len(col.max_value) <= 80  # DEFAULT_CONFIG.exemplar_max_chars
    # Bound proven over the SERIALIZED form, not just the in-memory object.
    assert huge_int not in serialized


def test_min_max_excludes_non_finite_overflow_value_no_inf_leak(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2 (secondary defect on the same code path): a
    # numeral longer than CPython's int-string-conversion digit limit fails
    # `_is_int` but still parses via `float()`, which silently returns
    # `inf` for a magnitude this large — an unguarded float `_min_max` would
    # then write the WRONG value ("inf") into a reviewed artifact with no
    # error. `_is_float` must reject non-finite parses so such a value is
    # excluded from both the type-vote and the min/max computation.
    huge_numeral = "9" * 300_000
    lines = [json.dumps({"id": f"r{i}", "amount": f"{i}.0"}) for i in range(1, 20)]
    lines.append(json.dumps({"id": "r_huge", "amount": huge_numeral}))
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "ledger"), "amount")

    assert col.inferred_type == "float"
    assert col.min_value == "1.0"
    assert col.max_value == "19.0"
    assert col.max_value != "inf"


def test_bounded_profile_guarantee_1000_rows_caps_at_200(tmp_path: Path) -> None:
    lines = ["id,val"] + [f"w{i},v{i}" for i in range(1000)]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path, config=BootstrapConfig())
    table = _table_named(bundle, "widgets")

    assert table.row_count_seen == 1000
    assert table.row_count_sampled == 200
    for col in table.columns:
        assert len(col.exemplars) <= 5
        assert all(len(v) <= 80 for v in col.exemplars)


def test_exemplars_dedupe_on_retained_value(tmp_path: Path) -> None:
    # U2 residual #2 (inherited from U1): `exemplars` used to dedupe on the
    # RAW pre-retention value while `distinct_values` deduped on the
    # RETAINED value — so a credential-shaped column could show duplicate
    # `[REDACTED]` entries in `exemplars`. Two credential-shaped values that
    # BOTH scrub to the same "[REDACTED]" marker must yield exactly ONE
    # exemplar, not two.
    lines = ["id,secret"]
    lines.append("r0,password=alpha-value")
    lines.append("r1,password=beta-value")
    lines.append("r2,ordinary-value-1")
    lines.append("r3,ordinary-value-2")
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    col = _col_named(_table_named(bundle, "widgets"), "secret")

    assert col.pii_flag is False  # credential-shape scrub, not a PII-name/shape flag
    assert col.exemplars.count("[REDACTED]") == 1
    assert "ordinary-value-1" in col.exemplars
    assert "ordinary-value-2" in col.exemplars
    # 4 raw-distinct values collapse to 3 retained-distinct exemplars.
    assert len(col.exemplars) == 3

    # Control: distinct BENIGN values (no scrub collision) still yield all K
    # (bounded at exemplars_k=5 by DEFAULT_CONFIG).
    lines2 = ["id,tag"] + [f"w{i},tag{i}" for i in range(10)]
    (tmp_path / "widgets2.csv").write_text("\n".join(lines2) + "\n")
    bundle2 = profile_samples(tmp_path)
    col2 = _col_named(_table_named(bundle2, "widgets2"), "tag")
    assert len(col2.exemplars) == 5
    assert len(set(col2.exemplars)) == 5  # no duplicates in the control either
