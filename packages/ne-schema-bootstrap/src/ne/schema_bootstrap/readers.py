# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sample-directory readers: CSV/TSV/JSONL/JSON-array -> :class:`RawTable`.

One file = one candidate table, discovered non-recursively and in sorted
order so the SET and ORDER of tables read from a directory is deterministic.
All parsed values are normalized onto a single value plane (``str | None``)
so downstream profiling never has to branch on source format.

Bounded ingestion (Argus P2): ``.csv``/``.tsv``/``.jsonl`` are read as a
single lazy stream through :func:`ne.schema_bootstrap.sampling.sample_stream`
— at most ``config.rows_per_table`` rows are ever held in memory at once,
regardless of how many rows the file actually contains, so a
much-larger-than-cap file no longer has to be materialized in full before
the cap can apply. ``.jsonl`` needs a first pass over the file to discover
the union-of-keys column set before rows can be normalized to a fixed
width; that pass parses each line but retains only the small `columns` name
list, not the parsed row objects. ``.json`` (a single top-level JSON array)
is the one format this package cannot stream with the stdlib ``json``
module alone: parsing requires materializing the full parsed object graph
in one call before any row can be inspected (a real limitation of staying
dependency-free — a streaming JSON-array parser would need a third-party
library such as ``ijson``, out of scope for this package). For ``.json``,
only the RETAINED `rows` list is capped immediately after parsing; the
transient full parse is not bounded.

Two-phase FK-containment evidence (SPEC §6.1.1 arbiter ruling, Seam 1): this
module no longer collects a per-column FULL distinct-value set during the
phase-1 read above — that "bounded to a small number of columns" claim was
true of column COUNT and false of column CARDINALITY (see
``tests/test_infer_fk_junction.py::test_memory_bound_would_catch_unbounded_identifier_collection``
for the proof, and
``tests/test_readers.py::test_large_delimited_file_bounds_memory_not_row_count``
for the fixture that now deliberately includes an identifier column). Instead,
:func:`ne.schema_bootstrap.profiles.profile_samples` runs a SEPARATE phase 2
after profiling: it computes FK-candidate pairs from the sampled data (never
needs the target's full value set — only membership of the SOURCE's already
-sampled, deduplicated FK values), then calls :func:`stream_column_values`
here to re-stream ONLY the specific targeted identifier column of the
specific targeted file, testing membership one value at a time. Peak memory
for that phase is ``O(#fk-columns x rows_per_table)`` — exact, deterministic,
and independent of the target table's true row count or the target column's
cardinality (proven in
``tests/test_infer_fk_junction.py::test_fk_pipeline_memory_bounded_with_over_cap_identifier_target``).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_CONFIG, BootstrapConfig
from .errors import EmptySampleError, SampleFormatError
from .sampling import sample_stream

_SUFFIXES = {".csv", ".tsv", ".jsonl", ".json"}


class RawTable(BaseModel):
    """A single sample file, parsed to a uniform row/column shape.

    ``rows`` is already capped at read time (at most ``config.rows_per_table``
    rows; see the module docstring) — ``row_count_seen`` is the TRUE total
    row count of the source file, always accurate regardless of the cap.
    ``path`` is the source file, retained so a later phase-2 containment
    probe (:func:`stream_column_values`) can re-open exactly this file
    without re-discovering it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: Path
    columns: list[str]
    rows: list[list[str | None]]
    row_count_seen: int


def read_sample_directory(
    path: Path, *, config: BootstrapConfig = DEFAULT_CONFIG
) -> list[RawTable]:
    """Read every ``.csv``/``.tsv``/``.jsonl``/``.json`` file in ``path``.

    Discovery is non-recursive, case-insensitive on suffix, and sorted by
    file name for deterministic table order. Raises :class:`EmptySampleError`
    when no matching files exist.
    """
    files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in _SUFFIXES)
    if not files:
        raise EmptySampleError(str(path))

    tables: list[RawTable] = []
    seen_table_names: dict[str, str] = {}
    for file in files:
        suffix = file.suffix.lower()
        if suffix == ".csv":
            table = _read_delimited(file, config=config, delimiter=",")
        elif suffix == ".tsv":
            table = _read_delimited(file, config=config, delimiter="\t")
        elif suffix == ".jsonl":
            table = _read_jsonl(file, config=config)
        else:
            table = _read_json_array(file, config=config)
        # Table name = file stem, so e.g. `a.csv` and `a.jsonl` in the same
        # directory would otherwise silently collide (last-wins), the same
        # failure class as the duplicate-column-name check below one level
        # up. Refuse rather than letting one file's table shadow another's.
        if table.name in seen_table_names:
            raise SampleFormatError(
                file.name,
                f"duplicate table name {table.name!r} (also produced by "
                f"{seen_table_names[table.name]!r})",
            )
        seen_table_names[table.name] = file.name
        tables.append(table)
    return tables


def _read_delimited(file: Path, *, config: BootstrapConfig, delimiter: str) -> RawTable:
    try:
        with file.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            try:
                header = next(reader)
            except StopIteration:
                raise SampleFormatError(file.name, "delimited file has no header row") from None
            except csv.Error:
                # A cell over csv.field_size_limit() (or other malformed-CSV
                # shape the stdlib reader rejects) is pathological for a
                # discovery sample — refuse with a typed, file-named error
                # rather than raising the raw _csv.Error, and never raise the
                # limit: a >128KB single cell is unbounded-memory risk, not a
                # legitimate sample value, and refusing keeps memory bounded
                # while naming the file so the operator can trim it. Severed
                # from the original exception (`from None`, not `from exc`):
                # errors.py's own threat model is that an exception is a
                # routinely-copied artifact, and `__cause__` is exactly as
                # reachable via repr()/structured error reporting as the
                # exception's own `.args`.
                raise SampleFormatError(
                    file.name, "delimited cell exceeds the csv field-size limit or is malformed"
                ) from None
            if len(set(header)) != len(header):
                # A literally-repeated header name (real spreadsheet exports
                # do this) would silently collapse `ProfileBundle.column_values`
                # downstream (last-wins keying by name alone), corrupting FK
                # containment for the shadowed column — a wrong-answer path,
                # not just a dropped one. Refuse at read time instead.
                raise SampleFormatError(file.name, "duplicate column name in header row")
            width = len(header)

            def _rows() -> Iterator[list[str | None]]:
                try:
                    for raw_row in reader:
                        if len(raw_row) > width:
                            # More cells than the header names — silently
                            # dropping the excess (the previous behavior)
                            # loses data with no error and no signal. Refuse
                            # instead; a narrow row (fewer cells) still pads
                            # with None below, unchanged.
                            raise SampleFormatError(
                                file.name, "delimited row has more cells than the header row"
                            )
                        normalized: list[str | None] = []
                        for i in range(width):
                            cell = raw_row[i] if i < len(raw_row) else None
                            normalized.append(None if cell in (None, "") else cell)
                        yield normalized
                except csv.Error:
                    raise SampleFormatError(
                        file.name,
                        "delimited cell exceeds the csv field-size limit or is malformed",
                    ) from None

            sampled_rows, row_count_seen = sample_stream(
                file.stem, _rows(), cap=config.rows_per_table
            )
    except UnicodeDecodeError:
        # A raw UnicodeDecodeError's `.object`/`.args[1]` carries the ENTIRE
        # offending byte buffer (customer row content), and its repr()
        # exposes that buffer through any uncontrolled channel that prints
        # the exception (terminal output, an issue report, a debugger
        # frame). Non-UTF-8 exports (Windows-1252/latin-1) are the common
        # case for enterprise discovery samples, not an exotic path — refuse
        # with a typed, file-named, content-free error instead, severed from
        # the original exception (see the csv.Error comment above for why).
        raise SampleFormatError(file.name, "file is not valid UTF-8") from None
    return RawTable(
        name=file.stem,
        path=file,
        columns=header,
        rows=sampled_rows,
        row_count_seen=row_count_seen,
    )


def _jsonl_discover_columns(file: Path) -> list[str]:
    """First pass: union-of-keys column discovery, first-seen order.

    Parses every line but retains only the (small) key-name list, not the
    parsed row objects — the memory cost of this pass is bounded by the
    number of DISTINCT keys across the file, not the row count.
    """
    columns: list[str] = []
    seen: set[str] = set()
    with file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                raise SampleFormatError(file.name, "invalid JSON on a line") from None
            if not isinstance(obj, dict):
                raise SampleFormatError(file.name, "JSONL row is not a JSON object")
            for key in obj:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
    return columns


def _read_jsonl(file: Path, *, config: BootstrapConfig) -> RawTable:
    try:
        columns = _jsonl_discover_columns(file)
    except UnicodeDecodeError:
        raise SampleFormatError(file.name, "file is not valid UTF-8") from None

    def _rows() -> Iterator[list[str | None]]:
        with file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    raise SampleFormatError(file.name, "invalid JSON on a line") from None
                if not isinstance(obj, dict):
                    raise SampleFormatError(file.name, "JSONL row is not a JSON object")
                yield _normalize_json_row(obj, columns)

    try:
        sampled_rows, row_count_seen = sample_stream(file.stem, _rows(), cap=config.rows_per_table)
    except UnicodeDecodeError:
        raise SampleFormatError(file.name, "file is not valid UTF-8") from None
    return RawTable(
        name=file.stem,
        path=file,
        columns=columns,
        rows=sampled_rows,
        row_count_seen=row_count_seen,
    )


def _read_json_array(file: Path, *, config: BootstrapConfig) -> RawTable:
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SampleFormatError(file.name, "file is not valid UTF-8") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise SampleFormatError(file.name, "invalid JSON") from None
    if not isinstance(data, list):
        raise SampleFormatError(file.name, "top-level JSON value is not an array")
    columns: list[str] = []
    seen: set[str] = set()
    for obj in data:
        if not isinstance(obj, dict):
            raise SampleFormatError(file.name, "array element is not a JSON object")
        for key in obj:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    def _rows() -> Iterator[list[str | None]]:
        for obj in data:
            yield _normalize_json_row(obj, columns)

    # `data` is already fully materialized (the stdlib-json limitation noted
    # in the module docstring) — `sample_stream` at least bounds what's
    # RETAINED afterward to `config.rows_per_table`, rather than keeping a
    # second full-length `rows` structure alongside it.
    sampled_rows, row_count_seen = sample_stream(file.stem, _rows(), cap=config.rows_per_table)
    return RawTable(
        name=file.stem,
        path=file,
        columns=columns,
        rows=sampled_rows,
        row_count_seen=row_count_seen,
    )


def stream_column_values(file: Path, column_name: str) -> Iterator[str | None]:
    """Re-stream a SINGLE column of ``file``, one value at a time.

    Phase-2 primitive for bounded FK-containment probing
    (:func:`ne.schema_bootstrap.profiles.profile_samples`): the caller wants
    to test membership of a small, already-deduplicated set of SOURCE values
    against this target column, so no per-row materialization is needed here
    — this generator is dispatched on ``file``'s suffix the same way
    :func:`read_sample_directory` is, and errors use the SAME typed,
    content-free :class:`SampleFormatError` pattern as phase 1 (phase 1 has
    already fully streamed this file once without error, so these are
    defense-in-depth, not the primary validation path).

    Raises :class:`SampleFormatError` naming ``column_name`` if it isn't a
    column of ``file`` (a caller bug — phase 2 only ever probes columns
    :func:`read_sample_directory` itself discovered).
    """
    suffix = file.suffix.lower()
    if suffix in (".csv", ".tsv"):
        delimiter = "," if suffix == ".csv" else "\t"
        try:
            with file.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter)
                try:
                    header = next(reader)
                except StopIteration:
                    raise SampleFormatError(file.name, "delimited file has no header row") from None
                except csv.Error:
                    raise SampleFormatError(
                        file.name,
                        "delimited cell exceeds the csv field-size limit or is malformed",
                    ) from None
                if column_name not in header:
                    raise SampleFormatError(
                        file.name, "target column not present in delimited header row"
                    )
                idx = header.index(column_name)
                try:
                    for raw_row in reader:
                        cell = raw_row[idx] if idx < len(raw_row) else None
                        yield None if cell in (None, "") else cell
                except csv.Error:
                    raise SampleFormatError(
                        file.name,
                        "delimited cell exceeds the csv field-size limit or is malformed",
                    ) from None
        except UnicodeDecodeError:
            raise SampleFormatError(file.name, "file is not valid UTF-8") from None
    elif suffix == ".jsonl":
        try:
            with file.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        raise SampleFormatError(file.name, "invalid JSON on a line") from None
                    if not isinstance(obj, dict):
                        raise SampleFormatError(file.name, "JSONL row is not a JSON object")
                    yield _normalize_json_value(obj.get(column_name))
        except UnicodeDecodeError:
            raise SampleFormatError(file.name, "file is not valid UTF-8") from None
    else:
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SampleFormatError(file.name, "file is not valid UTF-8") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise SampleFormatError(file.name, "invalid JSON") from None
        if not isinstance(data, list):
            raise SampleFormatError(file.name, "top-level JSON value is not an array")
        for obj in data:
            if not isinstance(obj, dict):
                raise SampleFormatError(file.name, "array element is not a JSON object")
            yield _normalize_json_value(obj.get(column_name))


def _normalize_json_value(value: object) -> str | None:
    """Map a JSON scalar/structure onto the uniform ``str | None`` plane.

    Pinned normalization (design §... / brief STEP 5): ``None`` -> ``None``;
    ``bool`` -> ``"true"``/``"false"``; ``int``/``float``/``str`` -> ``str()``;
    nested ``dict``/``list`` -> canonical ``json.dumps`` (sorted keys, no
    whitespace) so the mapping is deterministic.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _normalize_json_row(obj: dict[str, object], columns: list[str]) -> list[str | None]:
    return [_normalize_json_value(obj.get(key)) for key in columns]
