# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure-function tests for :mod:`ne.schema_bootstrap.refine` — the LOCKED
six-rule three-way merge behind ``ne schema bootstrap --refine``.

Every rule gets a POSITIVE case (the rule fires and produces the documented
outcome) and a NEGATIVE/control case (a sibling entry that does NOT trigger
the rule takes the expected different path) in the same test, per
[[vacuous-guard-assertion-shapes]].
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import types

import pytest
import yaml

import ne.schema_bootstrap as sb_pkg
import ne.schema_bootstrap.errors as errors_mod
import ne.schema_bootstrap.refine as refine_mod
import ne.schema_bootstrap.workspace as workspace_mod
from ne.schema_bootstrap.errors import RefineError
from ne.schema_bootstrap.refine import (
    find_tree_root_class,
    merged_expected_names,
    orphaned_classes,
    parse_merged_yaml,
    three_way_merge,
)


def _doc(**kwargs: object) -> str:
    return yaml.safe_dump(kwargs, sort_keys=False, allow_unicode=True)


def _parsed(merged_yaml_text: str) -> dict:
    return yaml.safe_load(merged_yaml_text)


def test_human_edit_wins_over_machine() -> None:
    # FooRecord: base -> human edits description -> machine ALSO changes it
    # differently. BarRecord (control): human leaves it untouched; machine
    # changes it -> the untouched entry must take `theirs`, not `ours`.
    base = _doc(
        classes={
            "FooRecord": {"description": "base foo"},
            "BarRecord": {"description": "base bar"},
        }
    )
    ours = _doc(
        classes={
            "FooRecord": {"description": "human-edited foo"},
            "BarRecord": {"description": "base bar"},
        }
    )
    theirs = _doc(
        classes={
            "FooRecord": {"description": "machine-v2 foo"},
            "BarRecord": {"description": "machine-v2 bar"},
        }
    )

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    assert merged["classes"]["FooRecord"]["description"] == "human-edited foo"
    assert "classes.FooRecord" in result.human_kept

    # Control: BarRecord was never touched by the human -> takes `theirs`.
    assert merged["classes"]["BarRecord"]["description"] == "machine-v2 bar"
    assert "classes.BarRecord" in result.machine_updated
    assert "classes.BarRecord" not in result.human_kept


def test_human_deletion_tombstones_and_excludes() -> None:
    base = _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}})
    # Human deleted FooRecord entirely; kept BarRecord untouched.
    ours = _doc(classes={"BarRecord": {"description": "d"}})
    theirs = _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}})

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    assert "FooRecord" not in merged["classes"]
    assert result.tombstoned_classes == ["FooRecord"]

    # Control: BarRecord, untouched by either side, survives normally.
    assert "BarRecord" in merged["classes"]
    assert "BarRecord" not in result.tombstoned_classes


def test_tombstone_blocks_resurrection() -> None:
    base = _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}})
    ours = _doc(classes={"BarRecord": {"description": "d"}})
    # The machine independently proposes FooRecord again on this run.
    theirs = _doc(classes={"FooRecord": {"description": "d-v2"}, "BarRecord": {"description": "d"}})

    result = three_way_merge(
        base, ours, theirs, tombstoned_classes=["FooRecord"], tombstoned_enums=[]
    )
    merged = _parsed(result.merged_yaml_text)

    assert "FooRecord" not in merged["classes"]
    assert result.tombstoned_classes == ["FooRecord"]
    assert "classes.FooRecord" in result.blocked_resurrections

    # Control: a tombstoned name the machine does NOT re-propose stays
    # excluded with no resurrection recorded.
    result2 = three_way_merge(
        _doc(classes={"BazRecord": {"description": "d"}}),
        _doc(classes={}),
        _doc(classes={}),
        tombstoned_classes=["BazRecord"],
        tombstoned_enums=[],
    )
    assert "classes.BazRecord" not in result2.blocked_resurrections
    assert result2.tombstoned_classes == ["BazRecord"]


def test_human_readd_clears_tombstone() -> None:
    base = _doc(classes={"FooRecord": {"description": "d"}})
    # The human re-adds FooRecord themselves (edited content).
    ours = _doc(classes={"FooRecord": {"description": "human-re-added"}})
    theirs = _doc(classes={})

    result = three_way_merge(
        base, ours, theirs, tombstoned_classes=["FooRecord"], tombstoned_enums=[]
    )
    merged = _parsed(result.merged_yaml_text)

    assert merged["classes"]["FooRecord"]["description"] == "human-re-added"
    assert "FooRecord" not in result.tombstoned_classes
    assert "classes.FooRecord" in result.human_readded

    # Control: a DIFFERENT tombstoned name the human does NOT re-add stays
    # tombstoned and is never reported re-added.
    result2 = three_way_merge(
        _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}}),
        _doc(classes={"FooRecord": {"description": "human-re-added"}}),
        _doc(classes={}),
        tombstoned_classes=["FooRecord", "BarRecord"],
        tombstoned_enums=[],
    )
    assert result2.tombstoned_classes == ["BarRecord"]
    assert "classes.BarRecord" not in result2.human_readded


def test_machine_update_flows_through_untouched() -> None:
    base = _doc(classes={"FooRecord": {"description": "base"}})
    ours = _doc(classes={"FooRecord": {"description": "base"}})  # untouched
    # A brand-new machine entry too (absent from base AND ours).
    theirs = _doc(
        classes={
            "FooRecord": {"description": "machine-v2"},
            "NewRecord": {"description": "brand new"},
        }
    )

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    assert merged["classes"]["FooRecord"]["description"] == "machine-v2"
    assert "classes.FooRecord" in result.machine_updated
    assert merged["classes"]["NewRecord"]["description"] == "brand new"
    assert "classes.NewRecord" in result.machine_updated

    # Control: if `theirs` is IDENTICAL to `base`, nothing should be
    # reported as a machine update even though rule 5 fires.
    result2 = three_way_merge(base, ours, base, tombstoned_classes=[], tombstoned_enums=[])
    assert "classes.FooRecord" not in result2.machine_updated


def test_machine_deletion_of_untouched_entry_drops() -> None:
    base = _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}})
    ours = _doc(classes={"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}})
    # Machine dropped FooRecord on this run; BarRecord untouched by machine.
    theirs = _doc(classes={"BarRecord": {"description": "d"}})

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    assert "FooRecord" not in merged["classes"]
    # A machine-side deletion of an entry the human never touched is NOT a
    # human decision — it must not be tombstoned.
    assert result.tombstoned_classes == []

    # Control: BarRecord (present everywhere, untouched) survives normally.
    assert "BarRecord" in merged["classes"]


def test_scalar_keys_ours_iff_changed_else_theirs() -> None:
    base = _doc(
        id="https://neutron.enterprise/schema/acme",
        title="base title",
        description="base description",
        classes={"FooRecord": {"description": "d"}},
    )
    ours = _doc(
        id="https://neutron.enterprise/schema/acme",
        title="human title",
        description="base description",
        custom_note="human-only key",
        classes={"FooRecord": {"description": "d"}},
    )
    theirs = _doc(
        id="https://neutron.enterprise/schema/acme",
        title="machine title v2",
        description="machine description v2",
        classes={"FooRecord": {"description": "d"}},
    )

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    # `title` differs ours-vs-base -> ours wins.
    assert merged["title"] == "human title"
    # `description` unchanged ours-vs-base -> theirs wins.
    assert merged["description"] == "machine description v2"
    # `id` unchanged everywhere -> stays what it was (theirs, since
    # ours == base at that key).
    assert merged["id"] == "https://neutron.enterprise/schema/acme"
    # A key existing only on the `ours` side is kept.
    assert merged["custom_note"] == "human-only key"


def test_scalar_key_deletion_by_human_is_not_resurrected_by_theirs() -> None:
    # `description`: present in `base`, the human REMOVED it entirely from
    # `ours` (not merely edited it) -> the deletion must be honored even
    # though the fresh machine draft (`theirs`) still emits the key. This
    # is the scalar-key mirror of Rule (3) for `classes`/`enums`: a
    # base-present, ours-absent entry is a human decision, not noise.
    base = _doc(
        id="x",
        title="base title",
        description="base description",
        classes={},
        enums={},
    )
    ours = _doc(id="x", title="human title", classes={}, enums={})
    theirs = _doc(
        id="x",
        title="machine title v2",
        description="machine description v2",
        classes={},
        enums={},
    )

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)

    # The deleted key must not reappear, whatever `theirs` says about it.
    assert "description" not in merged

    # Control (bidirectional, per [[vacuous-guard-assertion-shapes]]): the
    # fix must not be so broad it swallows ordinary edits/carries -- `title`
    # (human-edited, present everywhere) and `id` (untouched everywhere)
    # still merge exactly as `test_scalar_keys_ours_iff_changed_else_theirs`
    # documents.
    assert merged["title"] == "human title"
    assert merged["id"] == "x"


def test_top_level_slots_block_merges_as_one_opaque_key() -> None:
    # Declared residual (2026-07-29 round 4, README "top-level slots:
    # freeze"): the top-level `slots:` block is merged by
    # `_merge_scalar_keys` as ONE OPAQUE value, same as `id`/`title`/
    # `description` — a human edit anywhere inside it freezes the WHOLE
    # shared-slot section to `ours` on every future refine, discarding a
    # machine update to a DIFFERENT sibling slot in that same block.
    base = _doc(id="x", slots={"a": 1, "b": 1}, classes={}, enums={})

    # Positive: the human edited slot `a`; the machine independently updated
    # slot `b` on the SAME block -> the machine's `b` update is discarded
    # because the whole block is frozen to `ours`.
    ours = _doc(id="x", slots={"a": 2, "b": 1}, classes={}, enums={})
    theirs = _doc(id="x", slots={"a": 1, "b": 9}, classes={}, enums={})

    result = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    merged = _parsed(result.merged_yaml_text)
    assert merged["slots"] == {"a": 2, "b": 1}

    # Control (bidirectional, per [[vacuous-guard-assertion-shapes]]): the
    # human never touched `slots` at all (`ours == base`) -> the machine's
    # `b` update flows through normally.
    ours_untouched = _doc(id="x", slots={"a": 1, "b": 1}, classes={}, enums={})
    result_control = three_way_merge(
        base, ours_untouched, theirs, tombstoned_classes=[], tombstoned_enums=[]
    )
    merged_control = _parsed(result_control.merged_yaml_text)
    assert merged_control["slots"] == {"a": 1, "b": 9}


def test_merge_is_deterministic() -> None:
    base = _doc(
        title="t",
        classes={"FooRecord": {"description": "d"}},
        enums={"FooEnum": {"permissible_values": {"a": None}}},
    )
    ours = _doc(
        title="t-human",
        classes={"FooRecord": {"description": "d-human"}},
        enums={"FooEnum": {"permissible_values": {"a": None}}},
    )
    theirs = _doc(
        title="t-machine",
        classes={"FooRecord": {"description": "d-machine"}, "BarRecord": {"description": "new"}},
        enums={"FooEnum": {"permissible_values": {"a": None, "b": None}}},
    )

    r1 = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])
    r2 = three_way_merge(base, ours, theirs, tombstoned_classes=[], tombstoned_enums=[])

    assert r1.merged_yaml_text == r2.merged_yaml_text
    assert r1 == r2


def test_find_tree_root_unique_and_errors() -> None:
    zero = {"classes": {"FooRecord": {"description": "d"}, "BarRecord": {"description": "d"}}}
    with pytest.raises(RefineError, match="no class"):
        find_tree_root_class(zero)

    two = {
        "classes": {
            "FooRecordSet": {"tree_root": True},
            "BarRecordSet": {"tree_root": True},
            "BazRecord": {"description": "d"},
        }
    }
    with pytest.raises(RefineError) as exc_info:
        find_tree_root_class(two)
    assert "BarRecordSet" in str(exc_info.value)
    assert "FooRecordSet" in str(exc_info.value)

    # Control: exactly one tree_root class resolves cleanly.
    one = {
        "classes": {
            "FooRecordSet": {"tree_root": True},
            "BazRecord": {"description": "d"},
        }
    }
    assert find_tree_root_class(one) == "FooRecordSet"


def test_merged_expected_names_excludes_abstract() -> None:
    merged = {
        "classes": {
            "OperationalEntity": {"abstract": True},
            "FooRecord": {"description": "d"},
            "AcmeRecordSet": {"tree_root": True},
        },
        "enums": {"FooEnum": {"permissible_values": {"a": None}}},
    }
    class_names, enum_names = merged_expected_names(merged)

    assert class_names == ["AcmeRecordSet", "FooRecord"]
    assert "OperationalEntity" not in class_names
    assert enum_names == ["FooEnum"]

    # Control: a merged draft with no abstract class at all returns every
    # class (nothing wrongly excluded when the abstract flag is absent).
    no_abstract = {"classes": {"FooRecord": {"description": "d"}}, "enums": {}}
    class_names2, enum_names2 = merged_expected_names(no_abstract)
    assert class_names2 == ["FooRecord"]
    assert enum_names2 == []


def test_parse_merged_yaml_round_trips_and_handles_empty() -> None:
    text = _doc(classes={"FooRecord": {"description": "d"}})
    assert parse_merged_yaml(text) == {"classes": {"FooRecord": {"description": "d"}}}
    assert parse_merged_yaml("") == {}


def test_three_way_merge_raises_refine_error_on_malformed_yaml() -> None:
    # 2026-07-29 Argus P2: a hand-edited draft with a YAML syntax error must
    # raise RefineError (naming which of base/ours/theirs, never surrounding
    # content) instead of a bare yaml.YAMLError traceback.
    good = _doc(classes={"FooRecord": {"description": "d"}})
    malformed = "classes:\n  FooRecord: [unterminated\n"

    with pytest.raises(RefineError, match="ours draft"):
        three_way_merge(good, malformed, good, tombstoned_classes=[], tombstoned_enums=[])

    # Control: the SAME malformed text in the `base`/`theirs` slots is named
    # correctly too — the label tracks which argument failed, not a fixed
    # string.
    with pytest.raises(RefineError, match="base draft"):
        three_way_merge(malformed, good, good, tombstoned_classes=[], tombstoned_enums=[])
    with pytest.raises(RefineError, match="theirs draft"):
        three_way_merge(good, good, malformed, tombstoned_classes=[], tombstoned_enums=[])

    # A document that parses cleanly but is not a mapping (e.g. a bare YAML
    # list) must also raise RefineError, not AttributeError from `.get`.
    non_mapping = "- a\n- b\n"
    with pytest.raises(RefineError, match="mapping"):
        three_way_merge(good, non_mapping, good, tombstoned_classes=[], tombstoned_enums=[])

    # Control: well-formed input on all three sides still succeeds.
    result = three_way_merge(good, good, good, tombstoned_classes=[], tombstoned_enums=[])
    assert "FooRecord" in parse_merged_yaml(result.merged_yaml_text)["classes"]


def test_three_way_merge_raises_refine_error_on_non_mapping_named_section() -> None:
    # 2026-07-29 Codex C1: a hand-edited draft that stays valid YAML but
    # rewrites `classes:` (or `enums:`) to a non-mapping value (a YAML list
    # here) previously reached `_merge_named_section`'s `base_sec.get(...)`
    # and raised a bare `AttributeError: 'list' object has no attribute
    # 'get'` — escaping the CLI's `SchemaBootstrapError`-only catch instead
    # of surfacing as a reviewed `RefineError`.
    good = _doc(classes={"FooRecord": {"description": "d"}})
    classes_is_list = "classes:\n  - a\n  - b\n"

    with pytest.raises(RefineError, match="ours draft") as exc_info:
        three_way_merge(good, classes_is_list, good, tombstoned_classes=[], tombstoned_enums=[])
    assert "classes" in str(exc_info.value)

    # Control: the SAME shape in the `base`/`theirs` slots is named
    # correctly too, and the same defect shape on `enums:` is caught.
    with pytest.raises(RefineError, match="base draft"):
        three_way_merge(classes_is_list, good, good, tombstoned_classes=[], tombstoned_enums=[])
    with pytest.raises(RefineError, match="theirs draft"):
        three_way_merge(good, good, classes_is_list, tombstoned_classes=[], tombstoned_enums=[])

    enums_is_scalar = "classes:\n  FooRecord:\n    description: d\nenums: not-a-mapping\n"
    with pytest.raises(RefineError, match="enums") as exc_info2:
        three_way_merge(good, enums_is_scalar, good, tombstoned_classes=[], tombstoned_enums=[])
    assert "ours draft" in str(exc_info2.value)

    # Control: well-formed input on all three sides still succeeds (this
    # guard does not fire on the normal shape).
    result = three_way_merge(good, good, good, tombstoned_classes=[], tombstoned_enums=[])
    assert "FooRecord" in parse_merged_yaml(result.merged_yaml_text)["classes"]


def test_refine_error_excludes_source_snippet_from_malformed_yaml() -> None:
    # 2026-07-29 Argus P1 live repro: PyYAML's `MarkedYAMLError.__str__`
    # (and `Mark.__str__`) call `Mark.get_snippet()`, which echoes back the
    # surrounding SOURCE LINES from the buffer being parsed. A malformed
    # `schema-draft.yaml` with a planted value sitting on (or near) the
    # broken line must not put that value on `str(RefineError(...))`.
    good = _doc(classes={"FooRecord": {"tree_root": True}})
    sentinel_ssn = "123-45-6789"
    sentinel_name = "patient-Jane-Doe"
    malformed = (
        "classes:\n"
        "  FooRecord:\n"
        f"    exemplars: [SSN-{sentinel_ssn}, {sentinel_name}\n"
        "    other: x\n"
    )

    with pytest.raises(RefineError) as exc_info:
        three_way_merge(good, malformed, good, tombstoned_classes=[], tombstoned_enums=[])
    message = str(exc_info.value)

    assert sentinel_ssn not in message
    assert sentinel_name not in message
    # Also assert against the raw PyYAML exception's own snippet-bearing
    # string, so this test would fail if RefineError ever went back to
    # `f"...{exc}"` — a message that merely omits the two exact sentinels
    # above (but still leaks snippet text generally) would slip past the
    # two assertions above alone.
    raw_str = None
    try:
        yaml.safe_load(malformed)
    except yaml.YAMLError as exc:
        raw_str = str(exc)
    assert raw_str is not None and sentinel_ssn in raw_str, (
        "fixture must actually trigger a snippet"
    )

    # Control (non-vacuity): the position diagnostic IS present, so this
    # isn't proven by the message being empty/generic.
    assert "line" in message
    assert "column" in message


def test_orphaned_classes_finds_unreachable_non_abstract_classes() -> None:
    merged = {
        "classes": {
            "OperationalEntity": {"abstract": True},
            "AcmeRecordSet": {
                "tree_root": True,
                "attributes": {"customers": {"range": "CustomersRecord", "multivalued": True}},
            },
            "CustomersRecord": {"description": "d"},
            # Present in `classes:` but never referenced by any `range:`
            # anywhere reachable from the tree-root container.
            "OrdersRecord": {"description": "d"},
        }
    }
    orphans = orphaned_classes(merged, "AcmeRecordSet")
    assert orphans == ["OrdersRecord"]

    # Control: once the container is wired to reference it, it is no longer
    # orphaned, and the abstract provenance base is never reported even
    # though it too is unreferenced (abstract classes are excluded).
    merged["classes"]["AcmeRecordSet"]["attributes"]["orders"] = {
        "range": "OrdersRecord",
        "multivalued": True,
    }
    assert orphaned_classes(merged, "AcmeRecordSet") == []


def test_orphaned_classes_follows_transitive_fk_references() -> None:
    # A class reachable only THROUGH another reachable class's FK attribute
    # (not directly from the tree-root container) must still count as
    # reachable, not orphaned.
    merged = {
        "classes": {
            "AcmeRecordSet": {
                "tree_root": True,
                "attributes": {"orders": {"range": "OrdersRecord", "multivalued": True}},
            },
            "OrdersRecord": {
                "attributes": {"customer": {"range": "CustomersRecord", "inlined": False}}
            },
            "CustomersRecord": {"description": "d"},
        }
    }
    assert orphaned_classes(merged, "AcmeRecordSet") == []


@pytest.mark.parametrize("section", ["classes", "enums"])
@pytest.mark.parametrize("side", ["base", "ours", "theirs"])
def test_three_way_merge_raises_refine_error_on_non_string_section_key(
    side: str, section: str
) -> None:
    # 2026-07-29 Argus P2, round 5: an unquoted numeric key under
    # `classes:`/`enums:` (an ordinary hand-edit mistake — a forgotten
    # `"2024"` quote — not a hostile input) previously reached
    # `_merge_named_section`'s `for name in sorted(names)` and raised a raw
    # `TypeError: '<' not supported between instances of 'int' and 'str'`
    # — the C1 fix validated the section's CONTAINER type but not its KEY
    # type, one layer shallower than this shape needed. Live-proven by
    # Argus end-to-end through the CLI (fresh run, hand-add an unquoted
    # numeric class key, `--refine` -> raw traceback).
    good = _doc(**{section: {"2024": {"description": "d"}}})
    unquoted = f"{section}:\n  2024:\n    description: d\n"
    quoted = f'{section}:\n  "2024":\n    description: d\n'

    def _docs(mutated_text: str) -> dict[str, str]:
        docs = {"base": good, "ours": good, "theirs": good}
        docs[side] = mutated_text
        return docs

    bad_docs = _docs(unquoted)
    with pytest.raises(RefineError) as exc_info:
        three_way_merge(
            bad_docs["base"],
            bad_docs["ours"],
            bad_docs["theirs"],
            tombstoned_classes=[],
            tombstoned_enums=[],
        )
    message = str(exc_info.value)
    assert f"{side} draft" in message
    assert section in message
    assert "2024" in message

    # Control (non-vacuity, [[vacuous-guard-assertion-shapes]]): the SAME
    # shape, quoted, merges cleanly and the entry lands in the merged text.
    quoted_docs = _docs(quoted)
    result = three_way_merge(
        quoted_docs["base"],
        quoted_docs["ours"],
        quoted_docs["theirs"],
        tombstoned_classes=[],
        tombstoned_enums=[],
    )
    assert "2024" in parse_merged_yaml(result.merged_yaml_text)[section]


def test_three_way_merge_complex_yaml_key_raises_refine_error() -> None:
    # A COMPLEX key (a YAML sequence used as a mapping key) is a DIFFERENT
    # PyYAML failure mode than a non-string SCALAR key: `yaml.safe_load`
    # itself raises `ConstructorError` (unhashable key) while constructing
    # the document, before `_safe_load_draft`'s own key-type check ever
    # runs — already caught by the existing `yaml.YAMLError` handler. This
    # test turns that sweep reasoning into a test rather than leaving it
    # asserted in `_safe_load_draft`'s docstring alone.
    good = _doc(classes={"FooRecord": {"description": "d"}})
    complex_key_doc = "classes:\n  ? [a, b]\n  : {description: d}\n"

    with pytest.raises(RefineError, match="ours draft"):
        three_way_merge(good, complex_key_doc, good, tombstoned_classes=[], tombstoned_enums=[])

    # Control: well-formed input on all three sides still succeeds (this
    # shape does not fire on the normal document).
    result = three_way_merge(good, good, good, tombstoned_classes=[], tombstoned_enums=[])
    assert "FooRecord" in parse_merged_yaml(result.merged_yaml_text)["classes"]


def test_three_way_merge_admits_non_string_top_level_scalar_keys() -> None:
    # A non-string SCALAR key ELSEWHERE at the top level (outside
    # `classes:`/`enums:`) is an ADMITTED, non-crashing shape —
    # `_merge_scalar_keys` has no type check (only `classes:`/`enums:`
    # entry names are constrained to strings, matching what
    # `_merge_named_section`'s `sorted(names)` call actually needs).
    # Documented here as a decided-admitted shape, not silently assumed.
    doc = "classes:\n  FooRecord:\n    description: d\n2024: scalar-value\n"

    result = three_way_merge(doc, doc, doc, tombstoned_classes=[], tombstoned_enums=[])

    # `parse_merged_yaml`'s declared return type is `dict[str, Any]` (the
    # NORMAL shape); this test deliberately exercises the admitted-but-
    # off-contract non-string-top-level-key shape, so the raw `yaml.safe_load`
    # result is used directly rather than through the typed accessor.
    merged_raw: dict[object, object] = yaml.safe_load(result.merged_yaml_text)
    assert merged_raw[2024] == "scalar-value"
    merged = parse_merged_yaml(result.merged_yaml_text)
    assert "FooRecord" in merged["classes"]


def _enclosing_raise_sites(source: str, exc_name: str) -> set[str]:
    """AST-scan ``source`` and return the set of NEAREST-ENCLOSING function
    names for every ``raise <exc_name>(...)`` statement — immune to the
    exception name appearing in a comment/docstring/string literal, and
    immune to a `raise` inside a NESTED function being misattributed to its
    outer function (parent-tracked, not a flat ``ast.walk``).
    """
    tree = ast.parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _enclosing_function_name(node: ast.AST) -> str | None:
        current: ast.AST | None = parent.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
            current = parent.get(current)
        return None

    sites: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        name: str | None = None
        if isinstance(exc, ast.Call):
            func = exc.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
        elif isinstance(exc, ast.Name):
            name = exc.id
        if name != exc_name:
            continue
        fn_name = _enclosing_function_name(node)
        assert fn_name is not None, (
            f"raise {exc_name} found outside any function in {source[:40]!r}"
        )
        sites.add(fn_name)
    return sites


def test_refine_error_raise_sites_are_exactly_the_documented_shapes() -> None:
    # The docstring-completeness ratchet (2026-07-29 round 5, P3 #1): pins
    # `RefineError`'s docstring enumeration (errors.py, the single
    # authority) to the ACTUAL raise sites via an AST scan, not trust. A new
    # `raise RefineError`/`raise WorkspaceIOError` anywhere in refine.py or
    # workspace.py fails this test until the enumeration above is updated
    # to match — see errors.py::RefineError's docstring.
    refine_source = inspect.getsource(refine_mod)
    workspace_source = inspect.getsource(workspace_mod)

    assert _enclosing_raise_sites(refine_source, "RefineError") == {
        "_safe_load_draft",
        "find_tree_root_class",
    }
    assert _enclosing_raise_sites(workspace_source, "RefineError") == {
        "_require_state_file",
        "_read_state_text",
        "_load_tombstones",
    }
    assert _enclosing_raise_sites(workspace_source, "WorkspaceIOError") == {
        "_clear_staging",
        "_write_staged_and_commit",
        "_ensure_gitignore",
    }


def _all_raise_exception_names(source: str) -> set[str]:
    """AST-scan ``source`` and return the set of exception NAMES raised by
    every ``ast.Raise`` statement it contains — the COMPLETENESS direction,
    as opposed to ``_enclosing_raise_sites`` above (which pins WHERE a given,
    already-known exception name is raised). A bare ``raise`` (no ``exc``)
    inside an ``except`` block re-propagates whatever RAW exception was
    caught — exactly the untyped-escape class round 5's sweep closed — so it
    is treated as a hard failure here, not silently skipped.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        if exc is None:
            pytest.fail(
                "bare `raise` (re-propagates a caught raw exception, exactly the "
                "untyped-escape class round 5's sweep closed) found in scanned source "
                f"near {source.splitlines()[max(node.lineno - 1, 0)]!r}"
            )
        name: str | None = None
        if isinstance(exc, ast.Call):
            func = exc.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
        elif isinstance(exc, ast.Name):
            name = exc.id
        elif isinstance(exc, ast.Attribute):
            name = exc.attr
        assert name is not None, f"could not resolve raised exception name at line {node.lineno}"
        names.add(name)
    return names


def _package_modules_to_scan() -> list[types.ModuleType]:
    """Every module of :mod:`ne.schema_bootstrap` — including the package's
    own ``__init__.py`` — EXCEPT :mod:`ne.schema_bootstrap.errors` itself.

    ``errors`` is excluded because it is the SOURCE of the ``allowed`` set
    below — scanning it for membership in a set it defines would be
    circular, not a completeness check. (It also raises nothing today: an
    independent grep across the whole package for ``raise `` found zero
    hits outside the modules this scan covers plus ``errors.py``, and
    ``errors.py``'s one hit is inside a docstring, not code.)

    2026-07-30 (NE-ESB-CLI continuation, Argus P3): round 5's sweep, and
    this test as originally written, covered only ``refine.py`` and
    ``workspace.py`` — the two modules that round's crash-class was found
    in. That left every OTHER raise-bearing module (``emit.py``,
    ``infer.py``, ``readers.py``, ``report.py``, ``roundtrip.py``) unpinned
    against the same completeness claim, for no reason other than scope
    inertia. Scanning the whole package via :func:`pkgutil.iter_modules`
    (rather than a hand-picked import list, which would just be a smaller
    version of the same drift trap ``_EXPECTED_PUBLIC_API`` documents)
    closes that for free: it costs nothing today (every other module is
    already raw-raise-free) and it means ``readers.py`` — the module
    parsing the most untrusted input (raw sample files) — is covered too.

    2026-07-30 (NE-ESB-CLI continuation, Argus P2 re-review): the widening
    above was incomplete. :func:`pkgutil.iter_modules` enumerates
    SUBMODULES of a package only — it never yields the package itself, so
    ``ne/schema_bootstrap/__init__.py`` silently escaped the "whole
    package" scan its own docstring claimed to perform (a probe planting an
    uncalled raw raise there passed every test, undetected — see the
    README's mutation row for this fix). ``sb_pkg`` (the already-imported
    ``ne.schema_bootstrap`` package module, i.e. ``__init__.py``) is now
    appended explicitly so the docstring's claim and the code's behavior
    match.
    """
    modules: list[types.ModuleType] = [sb_pkg]
    for info in pkgutil.iter_modules(sb_pkg.__path__, sb_pkg.__name__ + "."):
        if info.name == f"{sb_pkg.__name__}.errors":
            continue
        modules.append(importlib.import_module(info.name))
    return modules


def test_raw_exception_sweep_is_complete_no_untyped_raise_sites() -> None:
    """The COMPLETENESS direction of round 5's raw-exception sweep, added in
    this continuation (2026-07-29/30, NE-ESB-CLI REVISE 7; scope widened to
    the whole package 2026-07-30, Argus P3).

    ``test_refine_error_raise_sites_are_exactly_the_documented_shapes`` above
    pins WHERE the package's typed errors are raised — but nothing pinned
    that nothing ELSE is ever raised. A probe (recorded in this unit's
    README) planted an uncalled ``raise ValueError("probe")`` in
    ``workspace.py`` and every existing test — including the enumeration
    test above — stayed GREEN: a NEW raw raise in an unexercised branch of
    any module in this package could ship completely undetected. THIS test
    closes that gap: every ``ast.Raise`` in every module of the package
    (except ``errors.py`` itself — see :func:`_package_modules_to_scan`),
    including a bare re-raise (which re-propagates a caught raw exception),
    must name an exception that is one of this package's OWN typed errors.

    ``allowed`` is deliberately derived from :mod:`ne.schema_bootstrap.errors`
    — a DIFFERENT module than the ones scanned here — so the same edit that
    plants a new raw raise anywhere in the scanned modules cannot also
    auto-satisfy this check; a legitimate new ``SchemaBootstrapError``
    subclass extends ``allowed`` for free (it lives in ``errors.py``), while
    a raw ``ValueError``/``TypeError``/bare-reraise does not.
    """
    allowed = {
        name
        for name, obj in vars(errors_mod).items()
        if isinstance(obj, type)
        and issubclass(obj, Exception)
        and obj.__module__ == errors_mod.__name__
    }

    raised: set[str] = set()
    for module in _package_modules_to_scan():
        raised |= _all_raise_exception_names(inspect.getsource(module))

    assert raised <= allowed, (
        f"raw (non-package-typed) exception raised somewhere in "
        f"ne.schema_bootstrap: {raised - allowed!r} — wrap it in a typed error from "
        f"errors.py, or if it is a legitimate new typed error, add it to errors.py first"
    )

    # Non-vacuity control (per docs/testing/vacuous-guards.md's
    # cardinality-trivial shape): a scanner that always returns the empty
    # set would trivially satisfy `raised <= allowed` above, so also assert
    # the scan actually found the four exception names round 5's sweep is
    # known to raise.
    assert {
        "RefineError",
        "WorkspaceIOError",
        "EmptySampleError",
        "WorkspaceExistsError",
    } <= raised
