# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Seam 2 guard: the single retention choke point (SPEC §6.1.1 arbiter ruling).

The retention theme recurred across three review rounds because every
value-bearing field of :class:`ColumnProfile` used to re-implement its own
bound + scrub + PII-gate — so every NEW field was a new opportunity to leak.
This test derives the set of value-bearing string fields FROM the pydantic
schema itself (never a hardcoded list of field names, which would merely
restate the current field set and catch nothing on the next field added —
[[vacuous-guard-assertion-shapes]]) and proves every one of them is
None/empty whenever a column is PII-flagged, over the model's *serialized*
form.
"""

from __future__ import annotations

import types
import typing
from pathlib import Path

from ne.schema_bootstrap.profiles import ColumnProfile, profile_samples

_UNION_ORIGINS = (typing.Union, types.UnionType)

# Metadata exclusions: fields that admit `str` content structurally but are
# NOT raw sampled customer values — excluded ONLY by name, and each is
# justified individually, not just asserted:
#   - "name": the column's own NAME (schema metadata), never a sampled value.
#   - "pii_reason": a fixed classification tag ("name:email", "shape:phone",
#     ...) drawn from a small closed vocabulary in pii.py, never derived from
#     row content.
# Deliberately NOT excluded: everything else that structurally admits `str`
# content joins the guard automatically — default-suspicious polarity, so a
# newly-added value-bearing field is caught rather than silently exempted.
_METADATA_EXCLUSIONS = frozenset({"name", "pii_reason"})


def _admits_str_content(annotation: object) -> bool:
    """Does ``annotation`` structurally admit free-form ``str`` content?

    ``True`` for `str`, `str | None`, and ANY parameterised container whose
    arguments admit `str` — `list[str]`, `dict[str, str]`, `tuple[str, ...]`,
    `set[str]`, `list[list[str]]`, and unions of those.
    ``False`` for a `Literal[...]` (and `Literal[...] | None`) — a
    categorical/enum-shaped field (e.g. `inferred_type`, `format_pattern`)
    is never raw sampled row content, however string-typed its Python
    representation is.

    **Argus P3, treated as BLOCKING class (3) rather than filed:** an earlier
    version special-cased `list` ONLY and checked `args[0]`, so
    `dict[str, str]`, `tuple[str, ...]`, `set[str]` and `list[list[str]]` all
    fell through to `annotation is str` and returned False. A future
    value-bearing field in any of those shapes would have been SILENTLY
    EXEMPTED from this guard — with no exclusion entry to review — which
    partially reopens the exact class Seam 2 exists to close. A guard with a
    hole for the next field added is a guard satisfiable by the change it
    forbids, so it is fixed here rather than tracked. Now recurses through
    EVERY argument of ANY parameterised generic, keeping the
    default-suspicious polarity.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return False
    args = [a for a in typing.get_args(annotation) if a is not type(None) and a is not Ellipsis]
    if args:
        # Covers unions AND every parameterised container uniformly: if any
        # argument admits str content, so does the field.
        return any(_admits_str_content(a) for a in args)
    return annotation is str


def _value_bearing_string_fields(model: type[ColumnProfile]) -> set[str]:
    """Derive ``model``'s value-bearing string field names FROM its schema.

    Walks ``model.model_fields`` (the pydantic-resolved, not string,
    annotations — verified in this package's Python/pydantic combination) and
    keeps every field whose annotation structurally admits `str` content,
    minus the documented :data:`_METADATA_EXCLUSIONS`.
    """
    return {
        field_name
        for field_name, field_info in model.model_fields.items()
        if field_name not in _METADATA_EXCLUSIONS and _admits_str_content(field_info.annotation)
    }


def test_derived_fields_include_all_four_known_value_bearing_fields() -> None:
    derived = _value_bearing_string_fields(ColumnProfile)
    assert {"min_value", "max_value", "exemplars", "distinct_values"} <= derived


def test_reflection_catches_a_new_value_bearing_field() -> None:
    # Control: the derivation is not hardcoded to today's field set — a
    # NEWLY added value-bearing string field on a subclass is picked up
    # automatically, without editing this test.
    class _ColumnProfileWithNewField(ColumnProfile):
        sample_blob: str | None = None

    derived = _value_bearing_string_fields(_ColumnProfileWithNewField)
    assert "sample_blob" in derived


def test_every_value_bearing_field_blanked_when_pii_flagged(tmp_path: Path) -> None:
    derived = _value_bearing_string_fields(ColumnProfile)

    dates = ["1961-03-04", "1975-08-19", "1988-11-30", "1994-02-14", "1999-12-31", "2001-06-06"]
    lines = ["id,email,reachpoint,dob,plan"]
    for i, dob in enumerate(dates):
        lines.append(f"c{i},user{i}@example.com,(415) 555-{i:04d},{dob},gold")
    (tmp_path / "customers.csv").write_text("\n".join(lines) + "\n")

    bundle = profile_samples(tmp_path)
    (table,) = bundle.profile.tables

    # Three independently-triggered PII signals, all of which must blank
    # every derived field identically: "email" (NAME family, non-orderable
    # `string` type), "reachpoint" (VALUE-SHAPE phone, non-orderable
    # `string` type), and "dob" (NAME family, ORDERABLE `date` type — the
    # only one of the three that can actually exercise min_value/max_value
    # blanking, since a non-orderable `string` column's min/max are already
    # `None` from `_min_max_raw` regardless of the PII gate).
    (email_col,) = [c for c in table.columns if c.name == "email"]
    (reachpoint_col,) = [c for c in table.columns if c.name == "reachpoint"]
    (dob_col,) = [c for c in table.columns if c.name == "dob"]
    assert email_col.pii_flag is True
    assert email_col.pii_reason == "name:email"
    assert reachpoint_col.pii_flag is True
    assert reachpoint_col.pii_reason == "shape:phone"
    assert dob_col.pii_flag is True
    assert dob_col.pii_reason == "name:dob"
    assert dob_col.inferred_type == "date"  # confirms it IS orderable

    for col in (email_col, reachpoint_col, dob_col):
        dumped = col.model_dump()
        for field in derived:
            value = dumped[field]
            assert value in (None, []), (
                f"{col.name}.{field} not blanked on a PII-flagged column: {value!r}"
            )

    # Non-vacuity control: a non-PII column's exemplars are NOT blanked —
    # proves the assertions above can fail, they're not vacuously true for
    # every column regardless of pii_flag.
    (plan_col,) = [c for c in table.columns if c.name == "plan"]
    assert plan_col.pii_flag is False
    assert plan_col.exemplars != []


def test_container_shaped_fields_are_not_silently_exempted() -> None:
    """Argus P3, fixed not filed: the guard must see str inside ANY container.

    An earlier `_admits_str_content` special-cased `list` and checked
    `args[0]`, so `dict[str, str]`, `tuple[str, ...]`, `set[str]` and
    `list[list[str]]` returned False and a future value-bearing field in one
    of those shapes would have been SILENTLY EXEMPTED — no exclusion entry,
    no failing test, the exact class Seam 2 exists to close.

    Controls in BOTH directions matter here: the negative cases keep this from
    degrading into "return True for everything", which would sweep metadata
    fields into the guard and make it fire on things that legitimately hold no
    row content.
    """
    admits = [
        str,
        str | None,
        list[str],
        dict[str, str],
        tuple[str, ...],
        set[str],
        list[list[str]],
    ]
    for annotation in admits:
        assert _admits_str_content(annotation) is True, annotation

    # Controls: these must stay OUT, or the guard is trivially true.
    refuses = [int, float, bool, list[int], tuple[int, ...], typing.Literal["a", "b"]]
    for annotation in refuses:
        assert _admits_str_content(annotation) is False, annotation
