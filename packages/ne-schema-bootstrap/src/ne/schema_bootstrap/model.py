# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The typed candidate model — the load-bearing output shape of this package.

``CandidateModel`` and its parts are what U2-U7 build against; names are
pinned and must not change casually. Every set-derived collection here is
sorted before it becomes part of a model (``classes``, ``enums``, ``edges``,
``seed_anchor_evidence``, ``permissible_values``) so
``CandidateModel.model_dump_json()`` is byte-identical across re-runs of the
same input — the determinism discipline this whole package exists to prove.

This module also hosts the mechanical-naming helpers (`to_snake_case`,
`to_camel_case`, `singularize`, `class_name_for_table`) used both here and by
:mod:`ne.schema_bootstrap.infer` and :mod:`ne.schema_bootstrap.pii`, so there
is exactly one normalization rule in the package. `fk_candidate_targets` is
the same kind of single-source-of-truth helper for the FK-name heuristic:
both :mod:`ne.schema_bootstrap.profiles` (bounded phase-2 containment
probing) and :mod:`ne.schema_bootstrap.infer` (edge construction) call it
rather than each transliterating the `<prefix>_id` rule independently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def to_snake_case(name: str) -> str:
    """Normalize ``name``: lowercase, non-alnum runs collapsed to ``_``."""
    normalized = _NON_ALNUM.sub("_", name).strip("_").lower()
    return re.sub(r"_+", "_", normalized)


def to_camel_case(name: str) -> str:
    """Split ``name`` on non-alnum runs and capitalize each part."""
    parts = _NON_ALNUM.split(name)
    return "".join(part.capitalize() for part in parts if part)


def singularize(stem: str) -> str:
    """Best-effort English singularization for table-stem name heuristics."""
    if stem.endswith("ies"):
        return stem[:-3] + "y"
    if stem.endswith("s") and not stem.endswith("ss"):
        return stem[:-1]
    return stem


def class_name_for_table(table_stem: str) -> str:
    """Mechanical class name for a table: CamelCase(stem) + ``"Record"``.

    No singularization (``customers.csv`` -> ``CustomersRecord``) — pinned by
    design §2's own example (``AcmeCustomersRecord``).
    """
    return f"{to_camel_case(table_stem)}Record"


IDENTIFIER_PRIORITY_NAMES: tuple[str, ...] = ("id", "uuid", "guid", "key", "pk")
"""Column names (after :func:`to_snake_case`) that make a column an
IDENTIFIER candidate for its own table, in priority order."""


def identifier_candidate_names(table_stem: str) -> frozenset[str]:
    """Normalized column names that make a column an identifier CANDIDATE
    for a table named ``table_stem``.

    The single source of truth for a naming rule used on BOTH sides of FK
    containment: :mod:`ne.schema_bootstrap.profiles`'s ``select_identifier``
    (gated additionally on sample-uniqueness) picks a TABLE's identifier
    column using this same name set — the source side uses it to decide
    which column is its own identifier (and therefore never itself an FK
    candidate), and the SAME selection on the target side is what phase-2
    containment probing (see :mod:`ne.schema_bootstrap.readers` and
    :func:`ne.schema_bootstrap.profiles.profile_samples`) probes against.
    Fixed priority names, plus the table's own self-referential
    ``<singular>_id`` name.
    """
    self_ref_name = f"{singularize(table_stem.lower())}_id"
    return frozenset({*IDENTIFIER_PRIORITY_NAMES, self_ref_name})


def fk_candidate_targets(column_name: str, table_names: Iterable[str]) -> list[str]:
    """Candidate TARGET table(s) for ``column_name`` under the ``_id`` name heuristic.

    The single source of truth for the "does this column name look like a
    foreign key, and if so, to which table(s)?" rule — used both by
    :mod:`ne.schema_bootstrap.profiles` (to decide which columns need a
    bounded phase-2 containment probe) and :mod:`ne.schema_bootstrap.infer`
    (to decide which columns get an FK edge), so the two never drift into
    two independently-transliterated copies of the same rule (arbiter
    ruling, SPEC §6.1.1).

    A normalized column name of the form ``<prefix>_id`` is a candidate FK
    to any table whose (lowercased) name equals ``prefix`` or whose
    singularized (lowercased) name equals ``prefix``. Returns ``[]`` when the
    column name doesn't end in ``_id`` with a non-empty prefix, or when no
    table name matches.
    """
    normalized = to_snake_case(column_name)
    if not normalized.endswith("_id"):
        return []
    prefix = normalized[: -len("_id")]
    if not prefix:
        return []
    return sorted(t for t in table_names if prefix == t.lower() or prefix == singularize(t.lower()))


class CandidateSlot(BaseModel):
    """One inferred attribute on a :class:`CandidateClass`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    mechanical_name: str
    range: str
    required: bool
    required_is_sample_derived: bool
    identifier: bool
    pattern: str | None
    enum_name: str | None


class CandidateEnum(BaseModel):
    """An inferred enumeration and its observed permissible values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    permissible_values: list[str]


class CandidateEdge(BaseModel):
    """An inferred FK relation from one candidate class to another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_class: str
    target_class: str
    containment: float
    via_name_heuristic: bool


class CandidateClass(BaseModel):
    """One inferred class, derived from one source table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_table: str
    is_junction: bool
    junction_note: str | None
    seed_anchor: str | None
    seed_anchor_evidence: list[str]
    slots: list[CandidateSlot]
    edges: list[CandidateEdge]


class CandidateModel(BaseModel):
    """The full inferred candidate model for a sample directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: list[CandidateClass]
    enums: list[CandidateEnum]
