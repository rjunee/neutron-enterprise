# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.readers``."""

from __future__ import annotations

import traceback
import tracemalloc
from pathlib import Path

import pytest

from ne.schema_bootstrap.errors import EmptySampleError, SampleFormatError
from ne.schema_bootstrap.readers import read_sample_directory


def test_sorted_non_recursive_discovery_filters_suffixes(tmp_path: Path) -> None:
    (tmp_path / "zebra.csv").write_text("id\n1\n")
    (tmp_path / "alpha.jsonl").write_text('{"id": "1"}\n')
    (tmp_path / "beta.TSV").write_text("id\n1\n")  # case-insensitive suffix
    (tmp_path / "ignored.txt").write_text("not a sample file")
    (tmp_path / "ignored.md").write_text("# notes")
    subdir = tmp_path / "nested"
    subdir.mkdir()
    (subdir / "should_not_be_found.csv").write_text("id\n1\n")

    tables = read_sample_directory(tmp_path)

    assert [t.name for t in tables] == ["alpha", "beta", "zebra"]


def test_csv_header_first_and_empty_cell_becomes_none(tmp_path: Path) -> None:
    (tmp_path / "people.csv").write_text("id,name,phone\n1,Ada,\n2,,555-1234\n")

    (table,) = read_sample_directory(tmp_path)

    assert table.columns == ["id", "name", "phone"]
    assert table.rows == [["1", "Ada", None], ["2", None, "555-1234"]]


def test_tsv_uses_tab_delimiter(tmp_path: Path) -> None:
    (tmp_path / "people.tsv").write_text("id\tname\n1\tAda\n2\tGrace\n")

    (table,) = read_sample_directory(tmp_path)

    assert table.columns == ["id", "name"]
    assert table.rows == [["1", "Ada"], ["2", "Grace"]]


def test_jsonl_scalar_normalization_and_first_seen_column_order(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        "\n".join(
            [
                '{"id": 1, "active": true, "score": 3.5, "tags": ["a", "b"]}',
                '{"id": 2, "active": false, "extra": "z"}',
                "",  # blank line ignored
            ]
        )
    )

    (table,) = read_sample_directory(tmp_path)

    # union-of-keys, first-seen order: id, active, score, tags (row 1) then extra (row 2).
    assert table.columns == ["id", "active", "score", "tags", "extra"]
    assert table.rows[0] == ["1", "true", "3.5", '["a","b"]', None]
    assert table.rows[1] == ["2", "false", None, None, "z"]


def test_json_array_object_rows_with_nested_canonical_json(tmp_path: Path) -> None:
    (tmp_path / "orders.json").write_text(
        '[{"id": 1, "meta": {"b": 2, "a": 1}}, {"id": 2, "meta": null}]'
    )

    (table,) = read_sample_directory(tmp_path)

    assert table.columns == ["id", "meta"]
    # dict keys sorted, no whitespace, deterministic regardless of insertion order.
    assert table.rows[0] == ["1", '{"a":1,"b":2}']
    assert table.rows[1] == ["2", None]


def test_json_array_bool_and_number_normalization(tmp_path: Path) -> None:
    (tmp_path / "flags.json").write_text('[{"n": 1, "f": 1.5, "b": true, "s": "x"}]')

    (table,) = read_sample_directory(tmp_path)
    row = dict(zip(table.columns, table.rows[0], strict=True))

    assert row["n"] == "1"
    assert row["f"] == "1.5"
    assert row["b"] == "true"
    assert row["s"] == "x"


def test_non_array_json_raises_sample_format_error_with_filename_not_content(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.json").write_text('{"this": "is an object, not an array"}')

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "bad.json"
    assert "bad.json" in str(excinfo.value)
    # Control: the error carries the filename but never the row content.
    assert "is an object" not in str(excinfo.value)


def test_json_array_non_object_row_raises_sample_format_error(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("[1, 2, 3]")

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "bad.json"


def test_jsonl_non_object_row_raises_sample_format_error(tmp_path: Path) -> None:
    (tmp_path / "bad.jsonl").write_text('"just a string"\n')

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "bad.jsonl"


def test_empty_directory_raises_empty_sample_error(tmp_path: Path) -> None:
    with pytest.raises(EmptySampleError):
        read_sample_directory(tmp_path)


def test_directory_with_only_unsupported_files_raises_empty_sample_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "readme.txt").write_text("hello")

    with pytest.raises(EmptySampleError):
        read_sample_directory(tmp_path)


def test_zero_data_row_csv_is_a_valid_empty_table(tmp_path: Path) -> None:
    (tmp_path / "empty.csv").write_text("id,name\n")

    (table,) = read_sample_directory(tmp_path)

    assert table.columns == ["id", "name"]
    assert table.rows == []


def test_non_utf8_delimited_file_raises_sample_format_error_no_pii_in_repr(
    tmp_path: Path,
) -> None:
    # 2026-07-28 Argus P1: a Windows-1252/latin-1 CSV export (the common case
    # for enterprise discovery samples) previously escaped as an untyped
    # `UnicodeDecodeError` whose `.object`/`.args[1]` carries the ENTIRE raw
    # file buffer — including the SSN below — so any repr()/logging of the
    # exception (terminal output, an issue report, a debugger frame) would
    # leak customer data through an uncontrolled channel. Must raise typed
    # SampleFormatError instead, naming the file but never the row content.
    (tmp_path / "latin.csv").write_bytes(
        "id,name,ssn\n1,Jos\xe9 Garcia,123-45-6789\n".encode("latin-1")
    )

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "latin.csv"
    # Control: str() AND repr() both stay clean — a str()-only assertion
    # would be the vacuous-guard shape this brief warns about, since
    # UnicodeDecodeError's raw-buffer payload surfaces via repr()/args, not
    # via str() of a wrapping SampleFormatError.
    assert "123-45-6789" not in str(excinfo.value)
    assert "123-45-6789" not in repr(excinfo.value)
    assert "Garcia" not in repr(excinfo.value)


def test_non_utf8_jsonl_file_raises_sample_format_error(tmp_path: Path) -> None:
    (tmp_path / "latin.jsonl").write_bytes('{"name": "Jos\xe9"}\n'.encode("latin-1"))

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "latin.jsonl"


def test_non_utf8_json_array_file_raises_sample_format_error(tmp_path: Path) -> None:
    (tmp_path / "latin.json").write_bytes('[{"name": "Jos\xe9"}]'.encode("latin-1"))

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "latin.json"


def test_duplicate_column_names_raise_sample_format_error(tmp_path: Path) -> None:
    # 2026-07-28 Argus P3: a CSV header with a literally-repeated column name
    # silently collapsed `ProfileBundle.column_values` (last-wins keying by
    # name alone), corrupting FK containment for the shadowed column — a
    # wrong-answer path, not just a dropped one. Duplicate headers are common
    # in real spreadsheet exports; refuse them at read time with a typed,
    # file-named error rather than silently building a lossy table.
    (tmp_path / "widgets.csv").write_text("id,val,val\n1,a,b\n2,c,d\n")

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "widgets.csv"
    assert "widgets.csv" in str(excinfo.value)


def test_duplicate_table_names_across_formats_raise_sample_format_error(
    tmp_path: Path,
) -> None:
    # 2026-07-28 Argus P3: `a.csv` and `a.jsonl` in one sample directory both
    # produce `RawTable(name="a")` (name = file stem), silently colliding
    # table names one level up from the column collision above. Refuse
    # rather than letting the second file's table shadow the first's.
    (tmp_path / "a.csv").write_text("id\n1\n")
    (tmp_path / "a.jsonl").write_text('{"id": "1"}\n')

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert "a.jsonl" in str(excinfo.value)


def test_oversized_delimited_cell_raises_sample_format_error(tmp_path: Path) -> None:
    # A single cell over Python's csv.field_size_limit() (131072 chars) makes
    # the stdlib csv reader raise a raw _csv.Error, which must not escape the
    # reader as an uncontrolled exception type — refusing with a typed,
    # file-named SampleFormatError keeps memory bounded and diagnosable.
    huge_cell = "x" * 200_000
    (tmp_path / "oversized.csv").write_text(f"id,blob\nr1,{huge_cell}\n")

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "oversized.csv"
    # Control: the error carries the filename but never the row content.
    assert huge_cell not in str(excinfo.value)


def test_non_utf8_delimited_error_severs_exception_chain(tmp_path: Path) -> None:
    # 2026-07-28 Argus P3: `raise SampleFormatError(...) from exc` set
    # `__cause__` to the original `UnicodeDecodeError`, whose `.object`/
    # `.args[1]` still carries the ENTIRE offending byte buffer — exactly
    # what the sibling test above (str()/repr() of the wrapper) does NOT
    # cover, since a "routinely-copied artifact" (errors.py's own stated
    # threat model — terminal output, an issue report, a debugger frame)
    # is virtually always produced via `traceback`-module formatting
    # (default exception printing, `logging.exception`,
    # `traceback.format_exc`), which DOES respect `__suppress_context__`.
    # `from None` sets both `__cause__ = None` and `__suppress_context__ =
    # True`, so both the explicit chain AND default traceback rendering are
    # clean, even though the raw `__context__` attribute itself remains set
    # (an artifact of Python's implicit exception chaining that only a
    # bespoke reporter walking `.__context__` directly, bypassing
    # `traceback`, would ever see — outside this fix's stated scope).
    (tmp_path / "latin.csv").write_bytes(
        "id,name,ssn\n1,Jos\xe9 Garcia,123-45-6789\n".encode("latin-1")
    )

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert "123-45-6789" not in rendered
    assert "Garcia" not in rendered


def test_oversized_delimited_cell_error_severs_exception_chain(tmp_path: Path) -> None:
    huge_cell = "x" * 200_000
    (tmp_path / "oversized.csv").write_text(f"id,blob\nr1,{huge_cell}\n")

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.__cause__ is None


def test_large_delimited_file_bounds_memory_not_row_count(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2 / SPEC §6.1.1 arbiter ruling: the reader used to
    # materialize the ENTIRE file into memory as one `list(csv.reader(...))`
    # before any cap applied — an ~8.5x blow-up over file size for a
    # 400,000-row CSV. Reading is now a single lazy stream through
    # `sample_stream`, so peak memory during `read_sample_directory` should
    # be governed by the row CAP, not by how many rows the file actually
    # contains.
    #
    # This fixture DELIBERATELY INCLUDES an identifier column (`id`) on a
    # 50,000-row file — the arbiter's diagnosis was that a prior fixture
    # dodged this exact case (deliberately avoiding any identifier-shaped
    # column name) to keep a since-deleted full-identifier-value collection
    # feature looking bounded. That collection is gone: phase 1
    # (`read_sample_directory`) never collects a per-column distinct-value
    # set at all, identifier columns included. FK-containment evidence is
    # now built in a bounded, exact phase 2
    # (`ne.schema_bootstrap.profiles.profile_samples`, tested in
    # `test_infer_fk_junction.py`), so this test can — and now does —
    # include the identifier column the old fixture had to avoid.
    n = 50_000
    lines = ["id,val"] + [f"cust-{i:08d},{'x' * 20}" for i in range(n)]
    (tmp_path / "widgets.csv").write_text("\n".join(lines) + "\n")

    tracemalloc.start()
    try:
        (table,) = read_sample_directory(tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert table.row_count_seen == n
    assert len(table.rows) == 200  # DEFAULT_CONFIG.rows_per_table
    # Non-vacuity control: a naive `list(csv.reader(...))` full-materialize
    # of the same file measures ~10 MB peak (empirically) — comfortably
    # over this bound, so the assertion below actually distinguishes
    # bounded from unbounded reading rather than passing regardless.
    assert peak < 3_000_000, f"peak traced memory {peak} bytes — ingestion is not bounded"


def test_over_wide_delimited_row_refused(tmp_path: Path) -> None:
    # A row with MORE cells than the header row used to be silently
    # truncated (the excess cell dropped with no error and no signal) —
    # data loss, not a legitimate "extra column" the header just doesn't
    # know about. Refuse instead.
    (tmp_path / "widgets.csv").write_text("id,name\n1,Ada,EXTRA-CELL\n")

    with pytest.raises(SampleFormatError) as excinfo:
        read_sample_directory(tmp_path)

    assert excinfo.value.filename == "widgets.csv"
    # Control: the error names the file but never the row content.
    assert "EXTRA-CELL" not in str(excinfo.value)
    assert "EXTRA-CELL" not in repr(excinfo.value)


def test_narrow_delimited_row_still_pads_with_none(tmp_path: Path) -> None:
    # Bidirectional control for the over-wide refusal above: a row with
    # FEWER cells than the header is unaffected, unchanged behavior — only
    # an over-wide row is refused.
    (tmp_path / "widgets.csv").write_text("id,name,phone\n1,Ada\n")

    (table,) = read_sample_directory(tmp_path)

    assert table.rows == [["1", "Ada", None]]
