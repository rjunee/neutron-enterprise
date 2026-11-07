# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.sampling``."""

from __future__ import annotations

from ne.schema_bootstrap.sampling import sample_rows


def test_rows_at_or_under_cap_pass_through_unchanged() -> None:
    rows = [[str(i)] for i in range(50)]

    result = sample_rows("t", rows, cap=200)

    assert result == rows
    assert result is not rows  # a copy, not aliasing the caller's list


def test_rows_exactly_at_cap_pass_through_unchanged() -> None:
    rows = [[str(i)] for i in range(200)]

    result = sample_rows("t", rows, cap=200)

    assert result == rows


def test_head_plus_reservoir_yields_exact_cap_head_present_original_order() -> None:
    rows = [[str(i)] for i in range(1000)]

    result = sample_rows("orders", rows, cap=200)

    assert len(result) == 200
    # Head = rows[0:100] all present, at the front, unmodified.
    assert result[:100] == rows[:100]
    # Output is in original row order (indices strictly increasing).
    indices = [int(r[0]) for r in result]
    assert indices == sorted(indices)
    assert len(set(indices)) == 200  # no duplicates


def test_determinism_same_table_name_same_rows_identical_selection() -> None:
    rows = [[str(i)] for i in range(1000)]

    first = sample_rows("orders", rows, cap=200)
    second = sample_rows("orders", rows, cap=200)

    assert first == second


def test_seed_derives_from_table_name_different_names_differ_for_every_pair() -> None:
    # 2026-07-28 Argus P3: the original assertion (`differing >= 1`) was
    # cardinality-trivial — it is satisfiable even if the seed collapsed to
    # depending on, say, only the table name's first character (which would
    # make "table_a"/"table_b" collide, since they share a leading "t", yet
    # still pass with 4/5 pairs differing). Tightened to require EVERY named
    # pair to differ, so the seed-derivation claim is proven per-pair, not
    # merely "on average".
    rows = [[str(i)] for i in range(1000)]
    name_pairs = [
        ("orders", "customers"),
        ("table_a", "table_b"),
        ("people", "accounts"),
        ("north", "south"),
        ("alpha", "beta"),
    ]

    for name_a, name_b in name_pairs:
        result_a = sample_rows(name_a, rows, cap=200)
        result_b = sample_rows(name_b, rows, cap=200)
        assert result_a != result_b, f"{name_a!r} and {name_b!r} selected the same reservoir"
