# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The inference core: :class:`ProfileBundle` -> :class:`CandidateModel`.

``infer_candidate_model`` is a PURE function of its ``bundle`` argument — no
I/O, no clock, no randomness. Same profile bundle in, byte-identical
candidate model out (design §4.1's "pure function of the profiles" clause,
the ``map_record`` determinism discipline this package mirrors).

Rule order per table: (0) refuse a duplicate mechanically-derived class name
across tables (see
``tests/test_infer_types.py::test_duplicate_candidate_class_names_refused_naming_both_tables``);
(0.5) refuse two columns on the SAME table that mechanically normalize to one
slot name (see
``tests/test_infer_types.py::test_duplicate_mechanical_slot_names_refused_naming_both_columns``);
(1) pick at most one identifier column (:func:`ne.schema_bootstrap.profiles.select_identifier`,
also used by phase-2 containment probing — see that module); (2) detect FK
edges on the remaining columns (needs every table's identifier resolved
first, since containment checks against a TARGET table's identifier, using
:func:`ne.schema_bootstrap.model.fk_candidate_targets` for the name
heuristic and ``bundle.fk_target_membership`` for containment evidence —
both bounded and exact, proven by ``tests/test_infer_fk_junction.py``); (3)
build enum candidates on the columns that are neither the identifier nor an
FK; (4) flag junction tables; (5) anchor against the seed operational
classes. Every set-derived list is sorted before it lands on the model.
"""

from __future__ import annotations

from .config import DEFAULT_CONFIG, BootstrapConfig
from .errors import DuplicateClassNameError, DuplicateSlotNameError
from .model import (
    CandidateClass,
    CandidateEdge,
    CandidateEnum,
    CandidateModel,
    CandidateSlot,
    class_name_for_table,
    fk_candidate_targets,
    to_camel_case,
    to_snake_case,
)
from .profiles import (
    PATTERN_SOURCES,
    ColumnProfile,
    ProfileBundle,
    TableProfile,
    select_identifier,
)
from .seeds import PROVENANCE_SLOTS, SEED_CLASS_SIGNATURES

_SCALAR_RANGE: dict[str, str] = {
    "int": "integer",
    "float": "float",
    "bool": "boolean",
    "date": "date",
    "datetime": "datetime",
    "string": "string",
}

_JUNCTION_NOTE = (
    "junction: modeled as thin association class carrying both edges; "
    "alternative (multivalued slot on one side) is a reviewer decision"
)

_TEMPORAL_TYPES = {"date", "datetime"}


def _try_fk_edge(
    table: TableProfile,
    col: ColumnProfile,
    all_tables: list[TableProfile],
    identifiers: dict[str, ColumnProfile | None],
    class_names: dict[str, str],
    bundle: ProfileBundle,
    config: BootstrapConfig,
) -> CandidateEdge | None:
    normalized = to_snake_case(col.name)
    # Parity by construction with the phase-2 probing that already computed
    # `bundle.fk_target_membership` (:mod:`ne.schema_bootstrap.profiles`) —
    # both sides call the SAME helper rather than each transliterating the
    # `<prefix>_id` name rule independently (SPEC §6.1.1 arbiter ruling).
    matching_tables = fk_candidate_targets(col.name, [t.name for t in all_tables])
    if not matching_tables:
        return None
    target_table = matching_tables[0]

    target_id = identifiers.get(target_table)
    if target_id is None:
        return None

    source_values = bundle.column_values.get((table.name, col.name), [])
    non_null_source = [v for v in source_values if v is not None]
    if not non_null_source:
        return None

    # Containment is measured against `bundle.fk_target_membership`: the
    # bounded, EXACT subset of this column's own sampled/deduplicated values
    # that phase 2 (`profiles.profile_samples`) confirmed present in the
    # target identifier column by re-streaming that one column of that one
    # file — never the target's full value set (SPEC §6.1.1 arbiter ruling,
    # Seam 1). Because `membership` is defined as exactly the probed values
    # found present, `v in membership` for a probed `v` is equivalent to
    # `v in <the target column's true value set>` — so this produces the
    # SAME containment count a full-value-set comparison would, without
    # ever holding that full set in memory (see the module docstring in
    # profiles.py and the memory-bound tests in test_infer_fk_junction.py).
    target_membership = bundle.fk_target_membership.get((target_table, target_id.name), frozenset())

    contained = sum(1 for v in non_null_source if v in target_membership)
    containment = round(contained / len(non_null_source), 6)
    if containment < config.containment_threshold:
        return None

    return CandidateEdge(
        name=normalized,
        source_class=class_names[table.name],
        target_class=class_names[target_table],
        containment=containment,
        via_name_heuristic=True,
    )


def _seed_anchor(
    non_provenance_cols: list[ColumnProfile], *, config: BootstrapConfig
) -> tuple[str | None, list[str]]:
    if not non_provenance_cols:
        return None, []
    normalized_names = {to_snake_case(col.name) for col in non_provenance_cols}
    passing: list[tuple[str, set[str]]] = []
    for seed_name, seed_attrs in SEED_CLASS_SIGNATURES.items():
        matched = normalized_names & seed_attrs
        if len(matched) < config.anchor_min_matching_attrs:
            continue
        ratio = len(matched) / len(non_provenance_cols)
        if ratio < config.anchor_min_overlap_ratio:
            continue
        passing.append((seed_name, matched))
    if not passing:
        return None, []
    passing.sort(key=lambda item: (-len(item[1]), item[0]))
    winner_name, winner_matched = passing[0]
    return winner_name, sorted(winner_matched)


def _check_no_duplicate_class_names(
    tables: list[TableProfile], class_names: dict[str, str]
) -> None:
    """Refuse two source tables that mechanically map to the same class name.

    ``class_name_for_table`` is lossy (CamelCase + "Record"): table stems
    that differ only in non-alnum runs, e.g. ``customer_orders.csv`` and
    ``customer-orders.csv``, both mechanically produce ``CustomerOrdersRecord``
    — silently letting the second table's class shadow the first's would be
    a wrong-answer path, not just a naming quirk. Tables are walked in their
    existing (sorted-by-file-name) order so the "first" vs "second" table
    named in the error is deterministic.
    """
    seen_by_class: dict[str, str] = {}
    for table in tables:
        class_name = class_names[table.name]
        if class_name in seen_by_class:
            raise DuplicateClassNameError(class_name, seen_by_class[class_name], table.name)
        seen_by_class[class_name] = table.name


def _check_no_duplicate_mechanical_names(table: TableProfile) -> None:
    """Refuse two columns on ``table`` that normalize to the same mechanical name.

    ``to_snake_case`` is the same lossy mechanical transform used for
    ``mechanical_name`` on every :class:`CandidateSlot` — headers that differ
    only in non-alnum runs (e.g. ``first-name`` and ``first_name``) both
    produce ``mechanical_name="first_name"``. Left unrefused, a consumer
    keyed by that normalized name (Codex C3) can only represent one of the
    two columns, and the second one silently wins or is dropped. Columns are
    walked in their existing (source-file) order so the "first" vs "second"
    column named in the error is deterministic.
    """
    seen_by_name: dict[str, str] = {}
    for col in table.columns:
        mechanical_name = to_snake_case(col.name)
        if mechanical_name in seen_by_name:
            raise DuplicateSlotNameError(
                table.name, mechanical_name, seen_by_name[mechanical_name], col.name
            )
        seen_by_name[mechanical_name] = col.name


def infer_candidate_model(
    bundle: ProfileBundle, *, config: BootstrapConfig = DEFAULT_CONFIG
) -> CandidateModel:
    """Infer the candidate model from an already-built :class:`ProfileBundle`.

    Pure function of ``bundle`` — see module docstring.
    """
    tables = bundle.profile.tables
    class_names = {t.name: class_name_for_table(t.name) for t in tables}
    _check_no_duplicate_class_names(tables, class_names)
    for table in tables:
        _check_no_duplicate_mechanical_names(table)
    identifiers: dict[str, ColumnProfile | None] = {t.name: select_identifier(t) for t in tables}

    classes: list[CandidateClass] = []
    all_enums: list[CandidateEnum] = []

    for table in tables:
        class_name = class_names[table.name]
        id_col = identifiers[table.name]

        # Pass 1: FK edges (identifier columns are never FK candidates).
        edges: list[CandidateEdge] = []
        fk_by_col: dict[str, CandidateEdge] = {}
        for col in table.columns:
            is_id = id_col is not None and col.name == id_col.name
            if is_id:
                continue
            edge = _try_fk_edge(table, col, tables, identifiers, class_names, bundle, config)
            if edge is not None:
                fk_by_col[col.name] = edge
                edges.append(edge)

        # Pass 2: slots (+ enums for the columns that are neither id nor FK).
        slots: list[CandidateSlot] = []
        class_enums: list[CandidateEnum] = []
        for col in table.columns:
            is_id = id_col is not None and col.name == id_col.name
            edge = fk_by_col.get(col.name)
            required = col.null_count == 0 and col.non_null_count > 0

            range_: str
            pattern: str | None = None
            enum_name: str | None = None

            if edge is not None:
                range_ = edge.target_class
            elif (
                not is_id
                and col.inferred_type == "string"
                and col.format_pattern is None
                and not col.pii_flag
                and col.distinct_values is not None
                and 0 < col.distinct_count <= config.enum_max_distinct
                and col.distinct_ratio <= config.enum_max_distinct_ratio
            ):
                enum_name = f"{class_name}{to_camel_case(col.name)}Enum"
                class_enums.append(
                    CandidateEnum(name=enum_name, permissible_values=sorted(col.distinct_values))
                )
                range_ = enum_name
            else:
                range_ = _SCALAR_RANGE[col.inferred_type]
                if col.format_pattern is not None:
                    pattern = PATTERN_SOURCES[col.format_pattern]

            slots.append(
                CandidateSlot(
                    name=col.name,
                    mechanical_name=to_snake_case(col.name),
                    range=range_,
                    required=required,
                    required_is_sample_derived=required,
                    identifier=is_id,
                    pattern=pattern,
                    enum_name=enum_name,
                )
            )
        all_enums.extend(class_enums)

        # Junction (J-rule): ignore the identifier + any temporal columns;
        # exactly two remaining columns, both FK-candidates -> junction.
        remaining = [
            col
            for col in table.columns
            if not (id_col is not None and col.name == id_col.name)
            and col.inferred_type not in _TEMPORAL_TYPES
        ]
        is_junction = len(remaining) == 2 and all(col.name in fk_by_col for col in remaining)
        junction_note = _JUNCTION_NOTE if is_junction else None

        # Seed anchoring: non-provenance columns only.
        non_provenance_cols = [
            col for col in table.columns if to_snake_case(col.name) not in PROVENANCE_SLOTS
        ]
        seed_anchor, seed_anchor_evidence = _seed_anchor(non_provenance_cols, config=config)

        classes.append(
            CandidateClass(
                name=class_name,
                source_table=table.name,
                is_junction=is_junction,
                junction_note=junction_note,
                seed_anchor=seed_anchor,
                seed_anchor_evidence=seed_anchor_evidence,
                slots=slots,
                edges=sorted(edges, key=lambda e: e.name),
            )
        )

    return CandidateModel(
        classes=sorted(classes, key=lambda c: c.name),
        enums=sorted(all_enums, key=lambda e: e.name),
    )
