# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded column/table profiling: the profiler half of the pipeline.

``profile_samples`` reads a sample directory, applies the deterministic
head+reservoir cap (:mod:`ne.schema_bootstrap.sampling`), and reduces each
sampled column to a bounded :class:`ColumnProfile` — type, null/distinct
stats, at most K scrubbed exemplars, deterministic PII flags with exemplar
suppression (design §3.3).

It returns a :class:`ProfileBundle`: the serializable :class:`SampleProfile`
(the ONLY artifact meant to be persisted/reviewed) paired with two in-memory,
deliberately NON-pydantic, NON-serializable carriers that
:mod:`ne.schema_bootstrap.infer` consumes for FK-containment checks —
``column_values`` (the sampled/capped values) and ``fk_target_membership``
(bounded, EXACT containment evidence: see the "Two-phase FK-containment
evidence" section below). ``ProfileBundle`` has no ``model_dump`` — raw
sampled values can never leak via serialization because the type that
carries them cannot be serialized.

Two-phase FK-containment evidence (SPEC §6.1.1 arbiter ruling, Seam 1):
containment never needs the TARGET's full value set — it needs membership of
the SOURCE's already-*sampled* FK values (at most ``rows_per_table`` per
column) in the target column. So ``profile_samples`` runs a second phase
after profiling: (1) pick each table's identifier column
(:func:`select_identifier`); (2) for every non-identifier column whose name
matches the FK heuristic (:func:`ne.schema_bootstrap.model.fk_candidate_targets`
— the SAME helper :mod:`ne.schema_bootstrap.infer` uses, so the two phases
never drift into independently-transliterated copies of the rule), collect
the deduplicated SAMPLED source values into a probe set keyed by
``(target_table, target_identifier_column)``; (3) for each probe, re-stream
ONLY that target file's identifier column
(:func:`ne.schema_bootstrap.readers.stream_column_values`), testing
membership one value at a time, with an early exit once every probed value
has been found. Peak memory for this phase is
``O(#fk-columns x rows_per_table)`` — exact, deterministic, and independent
of the target table's true row count or column cardinality (proven in
``tests/test_infer_fk_junction.py::test_fk_pipeline_memory_bounded_with_over_cap_identifier_target``,
with a non-vacuity control in
``tests/test_infer_fk_junction.py::test_memory_bound_would_catch_unbounded_identifier_collection``).

Single retention choke point (Seam 2): every value-bearing
:class:`ColumnProfile` field (``min_value``, ``max_value``, ``exemplars``,
``distinct_values``) is derived from a raw sampled value through exactly ONE
function, :func:`_retain_value` — PII-gate then scrub then truncate, in that
order, every time. No other code path in this module may call
:func:`ne.schema_bootstrap.hygiene.scrub_text` directly. See
``tests/test_retention_choke.py`` for the reflection-shaped guard that
enumerates :class:`ColumnProfile`'s value-bearing fields FROM the pydantic
schema (not a hardcoded list) and proves every one is blanked when
``pii_flag`` is set.
"""

from __future__ import annotations

import datetime
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_CONFIG, BootstrapConfig
from .hygiene import scrub_text
from .model import (
    IDENTIFIER_PRIORITY_NAMES,
    fk_candidate_targets,
    identifier_candidate_names,
    to_snake_case,
)
from .pii import pii_flags
from .readers import RawTable, read_sample_directory, stream_column_values

InferredType = Literal["int", "float", "bool", "date", "datetime", "string"]
FormatPattern = Literal["email", "url", "domain"]

# Mirrors operational.yaml Person.email / Account.domain (design §4.1(g)).
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_PATTERN = re.compile(r"^https?://\S+$")
DOMAIN_PATTERN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

PATTERN_SOURCES: dict[str, str] = {
    "email": EMAIL_PATTERN.pattern,
    "url": URL_PATTERN.pattern,
    "domain": DOMAIN_PATTERN.pattern,
}

_BOOL_LITERALS = {"true", "false", "t", "f", "yes", "no"}


def _is_bool(value: str) -> bool:
    return value.lower() in _BOOL_LITERALS


def _is_int(value: str) -> bool:
    if "." in value:
        return False
    try:
        int(value)
    except ValueError:
        return False
    return True


def _is_float(value: str) -> bool:
    # `float()` is more permissive than the "float" label implies: a numeral
    # far too large to be a real magnitude (well beyond `int()`'s own
    # string-conversion digit limit, so `_is_int` above already rejects it)
    # still parses successfully — to `inf`. Left unguarded, that silently
    # becomes a WRONG min/max value ("inf") written into a reviewed profile
    # artifact with no error (Argus P2). Reject non-finite parses outright:
    # they were never a legitimate float value in the source data.
    try:
        parsed = float(value)
    except ValueError:
        return False
    return math.isfinite(parsed)


def _is_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_datetime(value: str) -> bool:
    if "T" not in value and " " not in value:
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


_TYPE_CHECKS: tuple[tuple[InferredType, Callable[[str], bool]], ...] = (
    ("bool", _is_bool),
    ("int", _is_int),
    ("float", _is_float),
    ("date", _is_date),
    ("datetime", _is_datetime),
)


def infer_column_type(values: list[str], *, threshold: float) -> InferredType:
    """Parse-vote type inference: most-specific-first, first to meet ``threshold`` wins."""
    if not values:
        return "string"
    n = len(values)
    for type_name, check in _TYPE_CHECKS:
        matches = sum(1 for v in values if check(v))
        if matches / n >= threshold:
            return type_name
    return "string"


def _min_max_raw(
    non_null_values: list[str], inferred_type: InferredType
) -> tuple[str | None, str | None]:
    """min/max, rendered via ``str()``, over the ORDERABLE inferred types only.

    Returns RAW values — no scrubbing, no truncation. This is deliberately
    NOT the retention choke point: :func:`_retain_value` is the only function
    that applies the PII gate + scrub + truncate, and every caller (min/max
    included) must route through it before a value reaches a
    :class:`ColumnProfile` field (Seam 2, SPEC §6.1.1 arbiter ruling).
    """
    if inferred_type == "int":
        ints = [int(v) for v in non_null_values if _is_int(v)]
        return (str(min(ints)), str(max(ints))) if ints else (None, None)
    if inferred_type == "float":
        floats = [float(v) for v in non_null_values if _is_float(v)]
        return (str(min(floats)), str(max(floats))) if floats else (None, None)
    if inferred_type == "date":
        dates = [datetime.date.fromisoformat(v) for v in non_null_values if _is_date(v)]
        return (str(min(dates)), str(max(dates))) if dates else (None, None)
    if inferred_type == "datetime":
        datetimes = [datetime.datetime.fromisoformat(v) for v in non_null_values if _is_datetime(v)]
        if not datetimes:
            return (None, None)
        # A column can freely mix timezone-AWARE ("...T00:00:00Z") and
        # NAIVE ("...T00:00:00") ISO-8601 values -- both pass `_is_datetime`
        # -- but Python refuses to order an aware datetime against a naive
        # one (`min()`/`max()` over such a list raises `TypeError`), which
        # crashed `profile_samples` on otherwise-valid sample data (Codex
        # C1). Order by a key normalized to naive UTC so the ordering is
        # total and deterministic regardless of the tzinfo mix; `str()` is
        # still applied to the ORIGINAL datetime object, so an aware value's
        # offset is preserved in the rendered min/max.
        return (
            str(min(datetimes, key=_datetime_order_key)),
            str(max(datetimes, key=_datetime_order_key)),
        )
    return (None, None)


def _datetime_order_key(dt: datetime.datetime) -> datetime.datetime:
    """Total-order key for mixed aware/naive datetimes: normalize aware -> naive UTC."""
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.UTC).replace(tzinfo=None)
    return dt


def _retain_value(raw: str, *, pii_flag: bool, config: BootstrapConfig) -> str | None:
    """THE ONLY path from a raw sampled value to any :class:`ColumnProfile` field.

    ``pii_flag`` gates first (a flagged column retains NOTHING value-bearing
    — not even transiently, unlike the pre-Seam-2 code where min/max and
    exemplars/distinct-values each re-implemented this gate independently,
    a recurring source of new leaks whenever a new field was added). A
    non-flagged value is scrubbed (credential-shape redaction) and truncated
    to ``config.exemplar_max_chars`` — the SAME bound applied uniformly to
    every value-bearing field, not a per-field magic number.
    """
    if pii_flag:
        return None
    return scrub_text(raw, max_chars=config.exemplar_max_chars)


def _retain_non_pii(raw: str, *, config: BootstrapConfig) -> str:
    """Non-``Optional`` convenience wrapper around :func:`_retain_value`.

    Only ever called from a branch where ``pii_flag`` has ALREADY been
    established ``False`` (so a raw sampled value is genuinely being
    retained, never suppressed) — the ``assert`` documents that guarantee
    for callers/the type checker. Delegates to :func:`_retain_value` rather
    than calling :func:`ne.schema_bootstrap.hygiene.scrub_text` itself, so
    ``_retain_value`` stays the ONLY function in this module that touches
    the scrubber directly (Seam 2's single-choke-point invariant).
    """
    retained = _retain_value(raw, pii_flag=False, config=config)
    assert retained is not None  # pii_flag=False guarantees a str, never None
    return retained


_PATTERN_CHECKS: tuple[tuple[FormatPattern, re.Pattern[str]], ...] = (
    ("email", EMAIL_PATTERN),
    ("url", URL_PATTERN),
    ("domain", DOMAIN_PATTERN),
)


def _format_pattern(
    non_null_values: list[str], inferred_type: InferredType, *, threshold: float
) -> FormatPattern | None:
    if inferred_type != "string" or not non_null_values:
        return None
    n = len(non_null_values)
    for name, pattern in _PATTERN_CHECKS:
        matches = sum(1 for v in non_null_values if pattern.match(v))
        if matches / n >= threshold:
            return name
    return None


class ColumnProfile(BaseModel):
    """A bounded, deterministic profile of one sampled column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    inferred_type: InferredType
    null_count: int
    non_null_count: int
    distinct_count: int
    distinct_ratio: float
    min_value: str | None
    max_value: str | None
    unique_in_sample: bool
    format_pattern: FormatPattern | None
    pii_flag: bool
    pii_reason: str | None
    exemplars: list[str]
    exemplars_suppressed: bool
    distinct_values: list[str] | None


class TableProfile(BaseModel):
    """A bounded profile of one sampled table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    row_count_sampled: int
    row_count_seen: int
    columns: list[ColumnProfile]


class SampleProfile(BaseModel):
    """The full, serializable profile of a sample directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tables: list[TableProfile]


@dataclass(frozen=True)
class ProfileBundle:
    """In-memory pairing of the serializable profile with raw sampled values.

    ``column_values`` is keyed by ``(table_name, column_name)`` -> the
    SAMPLED raw string values for that column (post-cap, pre-scrub).
    ``fk_target_membership`` is keyed by ``(target_table,
    target_identifier_column)`` -> the SUBSET of probed source FK values that
    were found present in that target column — what
    :mod:`ne.schema_bootstrap.infer` measures FK containment against. This
    is bounded BY CONSTRUCTION: ``membership ⊆ probe ⊆ union of deduplicated
    sampled source values``, so its size is
    ``O(#fk-columns x rows_per_table)`` regardless of the target table's true
    size or column cardinality (SPEC §6.1.1 arbiter ruling, Seam 1 — see the
    module docstring). This is a plain ``dataclass``, NOT a pydantic model,
    and deliberately exposes no ``model_dump`` — it must never be
    serialized. Only :data:`SampleProfile` (via ``bundle.profile``) is meant
    to be written to disk or reviewed.
    """

    profile: SampleProfile
    column_values: dict[tuple[str, str], list[str | None]]
    fk_target_membership: dict[tuple[str, str], frozenset[str]]


def select_identifier(table: TableProfile) -> ColumnProfile | None:
    """Pick at most one identifier column for ``table``.

    Moved here from :mod:`ne.schema_bootstrap.infer` (SPEC §6.1.1 arbiter
    ruling) — byte-identical logic, relocated because phase-2 containment
    probing in :func:`profile_samples` needs it too, and this module already
    owns :class:`TableProfile`/:class:`ColumnProfile` without needing to
    import :mod:`ne.schema_bootstrap.infer` (which would create an import
    cycle, since ``infer`` imports from ``profiles``).

    A candidate column must be unique in the sample AND have a name matching
    :func:`ne.schema_bootstrap.model.identifier_candidate_names` for this
    table. Ties broken by :data:`ne.schema_bootstrap.model.IDENTIFIER_PRIORITY_NAMES`
    priority, then by name.
    """
    candidate_names = identifier_candidate_names(table.name)

    def is_identifier_candidate(col: ColumnProfile) -> bool:
        return to_snake_case(col.name) in candidate_names

    candidates = [
        col for col in table.columns if col.unique_in_sample and is_identifier_candidate(col)
    ]
    if not candidates:
        return None

    def priority(col: ColumnProfile) -> tuple[int, str]:
        normalized = to_snake_case(col.name)
        if normalized in IDENTIFIER_PRIORITY_NAMES:
            return IDENTIFIER_PRIORITY_NAMES.index(normalized), col.name
        return len(IDENTIFIER_PRIORITY_NAMES), col.name  # self-referential tier

    return min(candidates, key=priority)


def _profile_column(
    column_name: str, sampled_values: list[str | None], *, config: BootstrapConfig
) -> ColumnProfile:
    non_null = [v for v in sampled_values if v is not None]
    null_count = len(sampled_values) - len(non_null)
    non_null_count = len(non_null)
    distinct_set = set(non_null)
    distinct_count = len(distinct_set)
    distinct_ratio = round(distinct_count / non_null_count, 6) if non_null_count else 0.0
    unique_in_sample = non_null_count > 0 and null_count == 0 and distinct_count == non_null_count

    inferred_type = infer_column_type(non_null, threshold=config.type_vote_threshold)
    format_pattern = _format_pattern(non_null, inferred_type, threshold=config.pattern_threshold)

    # PII flag computed BEFORE any value retention — including min/max, not
    # just exemplars/distinct-values — so raw values are never even
    # transiently placed into a retained field for a flagged column (design
    # §3.3 / brief STEP 8). An orderable PII-flagged column (e.g. a `dob`
    # date column) would otherwise leak two real customer values via
    # min_value/max_value even though exemplars and distinct_values are
    # already suppressed below.
    pii_flag, pii_reason = pii_flags(
        column_name, non_null, inferred_type=inferred_type, config=config
    )

    # Seam 2 (SPEC §6.1.1 arbiter ruling): every value-bearing field below
    # routes through the SAME `_retain_value` choke point — no field
    # re-implements the PII gate / scrub / truncate sequence on its own.
    raw_min, raw_max = _min_max_raw(non_null, inferred_type)
    min_value = (
        _retain_value(raw_min, pii_flag=pii_flag, config=config) if raw_min is not None else None
    )
    max_value = (
        _retain_value(raw_max, pii_flag=pii_flag, config=config) if raw_max is not None else None
    )

    if pii_flag:
        exemplars: list[str] = []
        exemplars_suppressed = True
    else:
        exemplars_suppressed = False
        # Residual #2 (U1, fixed at source): dedupe on the RETAINED value, not
        # the raw one — mirrors the `distinct_values` fix directly above.
        # Two RAW-distinct values can become IDENTICAL once retained (e.g.
        # two credential-shaped values that both scrub to "[REDACTED]", or
        # two values sharing their first `exemplar_max_chars` characters
        # after truncation); deduping only on the raw value let both survive
        # as separate, visually-duplicate entries in what is supposed to be
        # a K-DISTINCT exemplar list. Walk `sampled_values` in order (not
        # `distinct_set`, which is unordered) so
        # `test_exemplars_are_first_k_distinct_in_sampled_order` keeps
        # holding: exemplars are still the first K DISTINCT (by retained
        # value) values in SAMPLED order.
        seen_raw_exemplars: set[str] = set()
        seen_retained_exemplars: set[str] = set()
        exemplars = []
        for v in sampled_values:
            if v is None or v in seen_raw_exemplars:
                continue
            seen_raw_exemplars.add(v)
            retained = _retain_non_pii(v, config=config)
            if retained in seen_retained_exemplars:
                continue
            seen_retained_exemplars.add(retained)
            exemplars.append(retained)
            if len(exemplars) >= config.exemplars_k:
                break

    if pii_flag or distinct_count > config.enum_max_distinct:
        distinct_values_out: list[str] | None = None
    else:
        # Retention (scrub + truncate) is applied AFTER the raw distinct set
        # is built, so two RAW-distinct values can become identical once
        # retained (e.g. two strings sharing their first
        # `exemplar_max_chars` characters after truncation, or two
        # credential-shaped values that scrub to the same redaction
        # marker). Deduplicating only the raw values (as this used to do)
        # lets those collisions through as duplicate entries in a supposedly
        # DISTINCT list -- a malformed/silently-inflated enum candidate
        # downstream (Codex C2). Dedupe on the RETAINED value, preserving
        # first-occurrence order over the deterministic raw-sorted walk.
        seen_retained: set[str] = set()
        distinct_values_out = []
        for v in sorted(distinct_set):
            retained = _retain_non_pii(v, config=config)
            if retained not in seen_retained:
                seen_retained.add(retained)
                distinct_values_out.append(retained)

    return ColumnProfile(
        name=column_name,
        inferred_type=inferred_type,
        null_count=null_count,
        non_null_count=non_null_count,
        distinct_count=distinct_count,
        distinct_ratio=distinct_ratio,
        min_value=min_value,
        max_value=max_value,
        unique_in_sample=unique_in_sample,
        format_pattern=format_pattern,
        pii_flag=pii_flag,
        pii_reason=pii_reason,
        exemplars=exemplars,
        exemplars_suppressed=exemplars_suppressed,
        distinct_values=distinct_values_out,
    )


def _profile_table(
    raw: RawTable, *, config: BootstrapConfig
) -> tuple[TableProfile, dict[str, list[str | None]]]:
    # `raw.rows` is already capped at read time (readers.py streams through
    # `sample_stream` — Argus P2), so no re-sampling is needed here.
    sampled_rows = raw.rows
    column_profiles: list[ColumnProfile] = []
    column_values: dict[str, list[str | None]] = {}
    for idx, col_name in enumerate(raw.columns):
        col_values = [row[idx] if idx < len(row) else None for row in sampled_rows]
        column_values[col_name] = col_values
        column_profiles.append(_profile_column(col_name, col_values, config=config))
    table_profile = TableProfile(
        name=raw.name,
        row_count_sampled=len(sampled_rows),
        row_count_seen=raw.row_count_seen,
        columns=column_profiles,
    )
    return table_profile, column_values


def _probe_fk_targets(
    table_profiles: list[TableProfile],
    column_values: dict[tuple[str, str], list[str | None]],
    identifiers: dict[str, ColumnProfile | None],
) -> dict[tuple[str, str], set[str]]:
    """Build the phase-2 probe sets: ``(target_table, target_id_col) -> {source values}``.

    A column is probed against a target only when: it isn't its OWN table's
    selected identifier (identifiers are never FK candidates — mirrors
    :mod:`ne.schema_bootstrap.infer`'s pass-1 rule); the FK-name heuristic
    (:func:`ne.schema_bootstrap.model.fk_candidate_targets`) names at least
    one target table; and that target table itself has a selected
    identifier column to probe against.
    """
    all_table_names = [t.name for t in table_profiles]
    probes: dict[tuple[str, str], set[str]] = {}
    for table in table_profiles:
        id_col = identifiers[table.name]
        for col in table.columns:
            if id_col is not None and col.name == id_col.name:
                continue
            targets = fk_candidate_targets(col.name, all_table_names)
            if not targets:
                continue
            target_table = targets[0]
            target_id = identifiers.get(target_table)
            if target_id is None:
                continue
            values = column_values.get((table.name, col.name), [])
            probe_key = (target_table, target_id.name)
            probes.setdefault(probe_key, set()).update(v for v in values if v is not None)
    return probes


def _resolve_fk_target_membership(
    probes: dict[tuple[str, str], set[str]],
    raw_by_name: dict[str, RawTable],
) -> dict[tuple[str, str], frozenset[str]]:
    """Phase 2: re-stream each probed target column, testing membership.

    Iterates probe keys in SORTED order (determinism discipline — see the
    module docstring). Early-exits a given probe's stream once every probed
    value has been found, since no further membership evidence can change
    the result.
    """
    fk_target_membership: dict[tuple[str, str], frozenset[str]] = {}
    for probe_key in sorted(probes):
        target_table, target_column = probe_key
        probe = probes[probe_key]
        found: set[str] = set()
        if probe:
            target_path = raw_by_name[target_table].path
            for value in stream_column_values(target_path, target_column):
                if value is not None and value in probe:
                    found.add(value)
                    if len(found) == len(probe):
                        break
        fk_target_membership[probe_key] = frozenset(found)
    return fk_target_membership


def profile_samples(path: Path, *, config: BootstrapConfig = DEFAULT_CONFIG) -> ProfileBundle:
    """Read + sample + profile a sample directory into a :class:`ProfileBundle`.

    Two phases (see the module docstring): phase 1 reads and profiles every
    table with no identifier collection; phase 2 (below) computes bounded,
    exact FK-containment evidence by re-streaming only the targeted
    identifier columns of the targeted files. All I/O — including phase 2's
    re-streaming — happens here; :func:`ne.schema_bootstrap.infer.infer_candidate_model`
    remains a pure function of the returned bundle.
    """
    raw_tables = read_sample_directory(path, config=config)
    table_profiles: list[TableProfile] = []
    column_values: dict[tuple[str, str], list[str | None]] = {}
    for raw in raw_tables:
        table_profile, col_values = _profile_table(raw, config=config)
        table_profiles.append(table_profile)
        for col_name, values in col_values.items():
            column_values[(raw.name, col_name)] = values

    identifiers: dict[str, ColumnProfile | None] = {
        t.name: select_identifier(t) for t in table_profiles
    }
    probes = _probe_fk_targets(table_profiles, column_values, identifiers)
    raw_by_name = {raw.name: raw for raw in raw_tables}
    fk_target_membership = _resolve_fk_target_membership(probes, raw_by_name)

    sample_profile = SampleProfile(tables=sorted(table_profiles, key=lambda t: t.name))
    return ProfileBundle(
        profile=sample_profile,
        column_values=column_values,
        fk_target_membership=fk_target_membership,
    )
