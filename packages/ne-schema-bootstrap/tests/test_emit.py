# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.emit`` — the house-dialect LinkML emitter."""

from __future__ import annotations

import enum
from pathlib import Path

import pytest
import yaml

from ne.schema_bootstrap.emit import (
    _dump_yaml,
    _is_enum_private_member_name,
    _NoAliasSafeDumper,
    emit_linkml,
)
from ne.schema_bootstrap.errors import (
    DuplicateClassNameError,
    DuplicateRecordSetSlotNameError,
    InvalidClassNameError,
    InvalidEngagementNameError,
)
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.model import (
    CandidateClass,
    CandidateEnum,
    CandidateModel,
    CandidateSlot,
)
from ne.schema_bootstrap.profiles import profile_samples


def _model_for(tmp_path: Path):
    bundle = profile_samples(tmp_path)
    return infer_candidate_model(bundle)


def _widgets_model_with_pvs(pvs: list[str]) -> CandidateModel:
    """A hand-built ``CandidateModel`` — one ``WidgetsRecord`` class with a
    required ``id`` slot and a ``tag`` slot whose enum's permissible values
    are ``pvs`` — used to unit-test ``_enum_decline_reason`` against exact
    rule-triggering shapes without going through the profiler/inferencer.
    Mirrored (not shared) in ``tests/test_roundtrip.py`` for the oracle
    tests that feed this same shape to the real generator.
    """
    id_slot = CandidateSlot(
        name="id",
        mechanical_name="id",
        range="string",
        required=True,
        required_is_sample_derived=True,
        identifier=True,
        pattern=None,
        enum_name=None,
    )
    tag_slot = CandidateSlot(
        name="tag",
        mechanical_name="tag",
        range="string",
        required=False,
        required_is_sample_derived=False,
        identifier=False,
        pattern=None,
        enum_name="WidgetsTagEnum",
    )
    widgets = CandidateClass(
        name="WidgetsRecord",
        source_table="widgets",
        is_junction=False,
        junction_note=None,
        seed_anchor=None,
        seed_anchor_evidence=[],
        slots=[id_slot, tag_slot],
        edges=[],
    )
    enum = CandidateEnum(name="WidgetsTagEnum", permissible_values=sorted(pvs))
    return CandidateModel(classes=[widgets], enums=[enum])


def test_is_enum_private_member_name_matches_stdlib() -> None:
    """:func:`_is_enum_private_member_name` is a verbatim-derived mirror of
    CPython's own ``enum._is_private`` — proven here by exhaustive
    comparison against the REAL stdlib function, not merely trusted from the
    derivation's own re-reading of its source (2026-07-28 Argus P1, fourth
    review round). Bidirectional per the vacuous-guard rule: includes shapes
    that ARE private (the silent-drop hazard this predicate exists to
    catch), shapes that must NOT be flagged (dunders, a name containing the
    pattern from the WRONG class, a name merely containing the pattern
    mid-string, the exact-pattern boundary case), plus the ``enum.py``
    source's own list-vs-str quirk (``name[pat_len:pat_len+1] != ['_']``,
    always ``True``) documented in the function's docstring.
    """
    # `getattr` (not `enum._is_private` directly): the real stdlib oracle is
    # a private module attribute pyright cannot see a declared type for —
    # `getattr` returns `Any` here on purpose, same shape as this file's own
    # `getattr(module, enum_name)` pattern elsewhere for generated classes.
    stdlib_is_private = getattr(enum, "_is_private")  # noqa: B009 -- private stdlib symbol

    cls_name = "WidgetsTagEnum"
    cases = [
        f"_{cls_name}__foo",  # the live P1 repro
        f"_{cls_name}__",  # exact pattern, no suffix -- NOT private (len == pat_len)
        f"_{cls_name}___",  # ends in '__' -- dunder-shaped, NOT private
        f"_{cls_name}__f_",  # single trailing underscore -- IS private
        f"_{cls_name}",  # missing the trailing '__' -- NOT private
        f"_{cls_name}_",  # missing the trailing '__' -- NOT private
        "__init__",
        "__str__",
        "_name_",
        "plain",
        f"x_{cls_name}__foo",  # pattern mid-string, not a prefix -- NOT private
        "_OtherEnum__foo",  # a DIFFERENT class's private-name pattern -- NOT private HERE
        f"_{cls_name}__a__b",  # doesn't end in '__' -- IS private
    ]
    for name in cases:
        expected = stdlib_is_private(cls_name, name)
        assert _is_enum_private_member_name(cls_name, name) == expected, (
            f"{name!r} vs stdlib enum._is_private({cls_name!r}, {name!r}) == {expected}"
        )
    # Non-vacuity: prove the oracle itself actually discriminates in both
    # directions before trusting agreement with it means anything.
    assert stdlib_is_private(cls_name, f"_{cls_name}__foo") is True
    assert stdlib_is_private(cls_name, "plain") is False


def test_emit_is_deterministic_byte_identical(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    model_a = _model_for(tmp_path)
    model_b = _model_for(tmp_path)

    emitted_a = emit_linkml(model_a, engagement="acme")
    emitted_b = emit_linkml(model_b, engagement="acme")
    assert emitted_a.yaml_text == emitted_b.yaml_text

    # Sensitivity control: a change that flips an inferred fact (name becomes
    # not-required by adding a null) must change the emitted YAML — proves
    # the equality check above isn't vacuous.
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n" + "\n".join(f"c{i},,user{i}@example.com" for i in range(10)) + "\n"
    )
    model_c = _model_for(tmp_path)
    emitted_c = emit_linkml(model_c, engagement="acme")
    assert emitted_c.yaml_text != emitted_a.yaml_text


def test_emitted_yaml_is_house_dialect(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        "id,customer_id,amount\n" + "\n".join(f"o{i},c{i % 10},{10 + i}" for i in range(10)) + "\n"
    )
    model = _model_for(tmp_path)
    emitted = emit_linkml(model, engagement="acme")

    data = yaml.safe_load(emitted.yaml_text)
    assert data["id"] == "https://neutron.enterprise/schema/acme"
    assert data["name"] == "neutron-acme"
    assert data["version"] == "0.1.0-draft"
    assert data["license"] == "Apache-2.0"
    assert data["default_range"] == "string"
    assert data["imports"] == ["linkml:types"]
    assert "neacme" in data["prefixes"]
    assert data["prefixes"]["linkml"] == "https://w3id.org/linkml/"
    assert data["default_prefix"] == "neacme"

    base = data["classes"]["OperationalEntity"]
    assert base["abstract"] is True
    assert base["slots"] == [
        "id",
        "source_system",
        "source_record_id",
        "created_at",
        "updated_at",
    ]
    assert data["slots"]["id"]["identifier"] is True
    assert data["slots"]["id"]["required"] is True

    for cls_name in ("CustomersRecord", "OrdersRecord"):
        assert data["classes"][cls_name]["is_a"] == "OperationalEntity"

    fk_attr = data["classes"]["OrdersRecord"]["attributes"]["customer_id"]
    assert fk_attr["range"] == "CustomersRecord"
    assert fk_attr["inlined"] is False

    email_attr = data["classes"]["CustomersRecord"]["attributes"]["email"]
    assert email_attr["pattern"] == r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

    record_set = data["classes"][emitted.top_class]
    assert record_set["tree_root"] is True
    for attr in record_set["attributes"].values():
        assert attr["multivalued"] is True
        assert attr["inlined_as_list"] is True


def test_provenance_named_columns_not_redeclared(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "id,created_at,name\n"
        + "\n".join(f"c{i},2024-01-0{(i % 9) + 1},Cust {i}" for i in range(10))
        + "\n"
    )
    model = _model_for(tmp_path)
    emitted = emit_linkml(model, engagement="acme")

    data = yaml.safe_load(emitted.yaml_text)
    attrs = data["classes"]["CustomersRecord"]["attributes"]
    assert "id" not in attrs
    assert "created_at" not in attrs
    assert "name" in attrs  # control: a non-provenance column IS an attribute

    assert emitted.provenance_mapped["CustomersRecord"] == ["created_at", "id"]


def test_identifier_slot_emitted_without_identifier_flag(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "customer_id,name\n" + "\n".join(f"c{i},Cust {i}" for i in range(10)) + "\n"
    )
    model = _model_for(tmp_path)
    emitted = emit_linkml(model, engagement="acme")

    data = yaml.safe_load(emitted.yaml_text)
    attrs = data["classes"]["CustomersRecord"]["attributes"]
    assert "customer_id" in attrs
    assert attrs["customer_id"]["required"] is True
    assert attrs["customer_id"].get("identifier") is None
    assert attrs["customer_id"]["description"] == "source natural key (unique in sample)"

    # No `identifier: true` anywhere except the base `id` slot.
    assert data["slots"]["id"]["identifier"] is True
    for name, attr in attrs.items():
        assert attr.get("identifier") is None, f"{name} unexpectedly carries identifier: true"


def test_enum_values_are_retained_list_and_divergence_is_possible(tmp_path: Path) -> None:
    # Two raw-distinct values that COLLIDE after truncation (share the first
    # `exemplar_max_chars`=80 characters): the retained list collapses to 1.
    shared_prefix = "A" * 80
    raw_a = shared_prefix + "-onetail"
    raw_b = shared_prefix + "-twotail"
    rows = ["id,tag"]
    for i in range(20):
        rows.append(f"w{i},{raw_a if i % 2 == 0 else raw_b}")
    (tmp_path / "widgets.csv").write_text("\n".join(rows) + "\n")

    model = _model_for(tmp_path)
    (enum,) = model.enums
    assert len(enum.permissible_values) == 1  # retained list already collapsed by infer.py

    emitted = emit_linkml(model, engagement="acme")
    data = yaml.safe_load(emitted.yaml_text)
    pvs = list(data["enums"][enum.name]["permissible_values"])
    assert pvs == enum.permissible_values  # emitted == retained, never a raw/count claim

    # Control: a column with NO collisions emits every value. 3 distinct
    # values over 30 rows keeps distinct_ratio at the 0.1 boundary.
    rows2 = ["id,tag2"] + [f"w{i},val{i % 3}" for i in range(30)]
    (tmp_path / "widgets2.csv").write_text("\n".join(rows2) + "\n")
    model2 = _model_for(tmp_path)
    enum2 = next(e for e in model2.enums if "Tag2" in e.name)
    assert len(enum2.permissible_values) == 3
    emitted2 = emit_linkml(model2, engagement="acme")
    data2 = yaml.safe_load(emitted2.yaml_text)
    assert sorted(data2["enums"][enum2.name]["permissible_values"]) == sorted(
        enum2.permissible_values
    )


def test_enum_declined_on_unsafe_values_falls_back_to_string(tmp_path: Path) -> None:
    # 'mro' is probe-proven FATAL against the real generator (R5, EMPIRICAL —
    # collides with Enum.mro). NOT a backtick: the pre-revision charset
    # wrongly declined backtick, but the revised gate correctly ADMITS it
    # (see test_charset_gate_accepted_values_roundtrip_through_real_generator).
    rows = ["id,tag"]
    for i in range(20):
        rows.append(f"w{i},{'mro' if i % 2 == 0 else 'safe-value'}")
    (tmp_path / "widgets.csv").write_text("\n".join(rows) + "\n")
    model = _model_for(tmp_path)
    (enum,) = model.enums

    emitted = emit_linkml(model, engagement="acme")
    data = yaml.safe_load(emitted.yaml_text)

    assert enum.name not in emitted.emitted_enums
    assert enum.name in emitted.declined_enums
    assert "enums" not in data or enum.name not in data.get("enums", {})
    tag_attr = data["classes"]["WidgetsRecord"]["attributes"]["tag"]
    assert tag_attr["range"] == "string"

    # Bidirectional control: safe PVs — including "in progress" and "n/a" —
    # stay a real emitted enum. 3 distinct values over 30 rows keeps
    # distinct_ratio at the 0.1 boundary.
    rows2 = ["id,status"]
    values = ["in progress", "n/a", "done"]
    for i in range(30):
        rows2.append(f"w{i},{values[i % 3]}")
    (tmp_path / "widgets2.csv").write_text("\n".join(rows2) + "\n")
    model2 = _model_for(tmp_path)
    status_enum = next(e for e in model2.enums if "Status" in e.name)
    emitted2 = emit_linkml(model2, engagement="acme")
    assert status_enum.name in emitted2.emitted_enums
    assert status_enum.name not in emitted2.declined_enums
    data2 = yaml.safe_load(emitted2.yaml_text)
    assert set(data2["enums"][status_enum.name]["permissible_values"]) == set(values)


def test_dumper_emits_no_anchors_or_tags() -> None:
    hostile_scalars: dict[str, object] = {
        "alias_like": "*alias",
        "mapping_like": "{a: b}",
        "sequence_like": "- item",
        "quoted": "has \"double\" and 'single' quotes",
    }
    dumped = _dump_yaml(hostile_scalars)
    loaded = yaml.safe_load(dumped)
    assert loaded == hostile_scalars

    shared_list = ["x", "y", "z"]
    data_with_shared_object: dict[str, object] = {"first": shared_list, "second": shared_list}
    dumped2 = _dump_yaml(data_with_shared_object)
    assert "&" not in dumped2
    assert "*" not in dumped2

    # Proven DIRECTLY, not just asserted in a comment: a default (aliasing)
    # plain yaml.SafeDumper WOULD emit `&id001`/`*id001` for the identical
    # same-object structure above — same options, only the Dumper class
    # differs. Without this contrast, the two asserts above could pass for
    # any reason (e.g. a PyYAML version that never aliases short lists);
    # this proves ignore_aliases=True is actually load-bearing.
    default_dumped = yaml.dump(
        data_with_shared_object,
        Dumper=yaml.SafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    assert "&id" in default_dumped
    assert "*id" in default_dumped
    assert yaml.safe_load(default_dumped) == data_with_shared_object

    # And directly against the class under test (not just the _dump_yaml
    # wrapper), same options as _dump_yaml minus the Dumper.
    no_alias_dumped = yaml.dump(
        data_with_shared_object,
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    assert "&" not in no_alias_dumped
    assert "*" not in no_alias_dumped


def test_dumper_bare_key_none_representation() -> None:
    dumped = _dump_yaml({"a": None, "b": "value"})
    assert dumped.splitlines()[0] in ("a:", "a: ")
    loaded = yaml.safe_load(dumped)
    assert loaded == {"a": None, "b": "value"}


@pytest.mark.parametrize(
    "name,should_raise",
    [
        ("Acme", True),
        ("a b", True),
        ("", True),
        ("-acme", True),
        ("acme-co", False),
        ("a", False),
    ],
)
def test_invalid_engagement_name_refused(tmp_path: Path, name: str, should_raise: bool) -> None:
    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\n")
    model = _model_for(tmp_path)
    if should_raise:
        with pytest.raises(InvalidEngagementNameError):
            emit_linkml(model, engagement=name)
    else:
        emitted = emit_linkml(model, engagement=name)
        assert emitted.yaml_text


@pytest.mark.parametrize(
    "hostile_value,reason_substring",
    [
        ("", "empty or whitespace-only"),
        ("  ", "empty or whitespace-only"),
        ("line1\nline2", "newline"),
        ("trail\\", "backslash"),
        ("a\x00b", "control character"),
        ("mro", "Enum.mro"),
        ("mro ", "Enum.mro"),
        ("_name_", "underscore-bookended"),
        ("_missing_", "underscore-bookended"),
        ("mro,", "Enum.mro"),
        ("-name-", "underscore-bookended"),
        (",,", "not a valid Python identifier"),
        ("½ inch", "not a valid Python identifier"),
        ("ｍｒｏ", "Enum.mro"),  # noqa: RUF001 -- deliberately FULLWIDTH LATIN
        # 2026-07-28 differential-fuzz round, P0 regression pins: trailing-
        # position NFKC-folding characters must be declined on the RAW
        # label, not admitted by a normalized-form check.
        ("m²", "not a valid Python identifier"),
        ("10 m³", "not a valid Python identifier"),
        # P1 regression pin: a codepoint assigned in the platform's Unicode
        # database but unassigned in the pinned .codegen-venv's database.
        ("grade\U0002ebf0", "Unicode"),
        # Every character class the module docstring/README name, pinned in
        # BOTH leading and trailing position (2026-07-28 P3 re-derivation —
        # "any sentence naming a specific character class must have a test
        # with that character in it").
        ("½x", "not a valid Python identifier"),
        ("¼x", "not a valid Python identifier"),
        ("¾x", "not a valid Python identifier"),
        ("²x", "not a valid Python identifier"),
        ("³x", "not a valid Python identifier"),
        ("¹x", "not a valid Python identifier"),
        ("x½", "not a valid Python identifier"),
        ("x¼", "not a valid Python identifier"),
        ("x¾", "not a valid Python identifier"),
        ("x²", "not a valid Python identifier"),
        ("x³", "not a valid Python identifier"),
        ("x¹", "not a valid Python identifier"),
        # 2026-07-28 Argus P1, fourth review round: an accept-side silent
        # drop the curated oracle never imagined until this review — a
        # member label that Python's own name-mangling treats as PRIVATE to
        # the generated 'WidgetsTagEnum' class.
        ("_WidgetsTagEnum__foo", "PRIVATE"),
    ],
)
def test_enum_gate_decline_reasons_rule_specific(hostile_value: str, reason_substring: str) -> None:
    """Each of the gate's rules declines with its OWN rule-specific reason
    (bidirectional per-rule coverage — 2026-07-28 REVISE, replacing the
    single-regex charset). Paired with a benign second PV ('plain') so the
    enum stays non-trivial.
    """
    model = _widgets_model_with_pvs([hostile_value, "plain"])
    emitted = emit_linkml(model, engagement="acme")

    assert "WidgetsTagEnum" in emitted.declined_enums, (
        f"{hostile_value!r} should have been declined"
    )
    assert reason_substring in emitted.declined_enums["WidgetsTagEnum"]
    data = yaml.safe_load(emitted.yaml_text)
    assert data["classes"]["WidgetsRecord"]["attributes"]["tag"]["range"] == "string"


@pytest.mark.parametrize(
    "value_a,value_b",
    [
        ("a b", "a_b"),
        ("in-progress", "in progress"),
        ("a,b", "ab"),
        ("1st", "number_1st"),
        ("class", "class_"),
        ("µm", "μm"),
        # 2026-07-28 differential-fuzz round: the venv-isidentifier
        # emulation's own collision family — a KATAKANA MIDDLE DOT value
        # transliterates (real venv-side generator, XID-skew-aware) to
        # exactly the ASCII spelling of its own transliteration.
        ("a・b", "aKATAKANA_MIDDLE_DOTb"),
    ],
)
def test_enum_gate_collision_reason_names_both_values(value_a: str, value_b: str) -> None:
    """R6 across every step of the derived mangle (2026-07-28 P0 fix, plus
    the CPython-NFKC step added 2026-07-28 Argus P1 third round):
    whitespace-collapse, hyphen-vs-space, comma-elision, digit-prefix,
    keyword-masking, and NFKC identifier normalization — each a distinct
    collision-producing mangle step — see
    ``tests/test_roundtrip.py::test_real_mangle_collision_families_decline_and_prove_member_loss``
    for the same families forced through the real generator.
    """
    model = _widgets_model_with_pvs([value_a, value_b])
    emitted = emit_linkml(model, engagement="acme")

    assert "WidgetsTagEnum" in emitted.declined_enums
    reason = emitted.declined_enums["WidgetsTagEnum"]
    assert repr(value_a) in reason
    assert repr(value_b) in reason


@pytest.mark.parametrize(
    "safe_value",
    [
        "`backtick",
        "@mention",
        "%pct",
        "'quote",
        "<tag>",
        "a b: c",
        "Größe",
        "1st",
        "class",
        'say "hi"',
        "_",
        "MRO",
        "-item",
    ],
)
def test_enum_gate_widened_acceptance_set_emits(safe_value: str) -> None:
    """Bidirectional acceptance control (vacuous-guard memory): every one of
    these values was DECLINED by the pre-revision charset — Argus proved all
    of them generate fine through the real venv. The revised gate must EMIT
    a real enum for each, not silently keep declining them.
    """
    model = _widgets_model_with_pvs([safe_value, "plain"])
    emitted = emit_linkml(model, engagement="acme")

    assert emitted.declined_enums == {}, (
        f"{safe_value!r} should have been ADMITTED, not declined ({emitted.declined_enums})"
    )
    assert "WidgetsTagEnum" in emitted.emitted_enums
    data = yaml.safe_load(emitted.yaml_text)
    assert "WidgetsTagEnum" in data.get("enums", {})


def test_enum_gate_widened_acceptance_set_emits_as_one_enum() -> None:
    """Same acceptance set as ``test_enum_gate_widened_acceptance_set_emits``,
    combined into ONE enum (rather than probed one-at-a-time) — proves the
    widened acceptance set doesn't collide with itself under the derived
    ``_member_label`` mangle (e.g. 'class' and 'MRO' as siblings — 'class'
    masks to 'class_' via the keyword step, which does not collide with
    'MRO' since label matching is case-sensitive).
    """
    widened_acceptance = [
        "`backtick",
        "@mention",
        "%pct",
        "'quote",
        "<tag>",
        "a b: c",
        "Größe",
        "1st",
        "class",
        'say "hi"',
        "_",
        "MRO",
        "-item",
    ]
    model = _widgets_model_with_pvs(widened_acceptance)
    emitted = emit_linkml(model, engagement="acme")

    assert emitted.declined_enums == {}
    assert "WidgetsTagEnum" in emitted.emitted_enums


def test_digit_leading_table_stem_rejected_early_naming_the_file(tmp_path: Path) -> None:
    """P2: a sample file whose stem is entirely digits mechanically derives
    an invalid Python identifier class name (``2023Record``) — verified live
    against the real generator that this DOES fail deep inside
    ``PydanticGenerator``. ``emit_linkml`` must catch this EARLY, before any
    subprocess, naming both the offending file stem and the derived class.
    """
    rows = ["id,name"] + [f"r{i},Row {i}" for i in range(10)]
    (tmp_path / "2023.csv").write_text("\n".join(rows) + "\n")
    model = _model_for(tmp_path)

    with pytest.raises(InvalidClassNameError) as exc_info:
        emit_linkml(model, engagement="acme")
    assert "2023" in str(exc_info.value)
    assert "2023Record" in str(exc_info.value)

    # Control: a stem that starts with a letter (even though it still
    # contains digits) derives a valid class name and emits fine.
    tmp_path2 = tmp_path / "control"
    tmp_path2.mkdir()
    rows2 = ["id,name"] + [f"r{i},Row {i}" for i in range(10)]
    (tmp_path2 / "orders 2023.csv").write_text("\n".join(rows2) + "\n")
    model2 = _model_for(tmp_path2)
    emitted2 = emit_linkml(model2, engagement="acme")
    assert "Orders2023Record" in emitted2.yaml_text


def _minimal_class(name: str, source_table: str) -> CandidateClass:
    """A minimal, otherwise-valid ``CandidateClass`` (single required
    identifier slot) for tests that only care about class-level collision
    behavior, not slot/enum content."""
    id_slot = CandidateSlot(
        name="id",
        mechanical_name="id",
        range="string",
        required=True,
        required_is_sample_derived=True,
        identifier=True,
        pattern=None,
        enum_name=None,
    )
    return CandidateClass(
        name=name,
        source_table=source_table,
        is_junction=False,
        junction_note=None,
        seed_anchor=None,
        seed_anchor_evidence=[],
        slots=[id_slot],
        edges=[],
    )


def test_duplicate_class_name_rejected_before_emit() -> None:
    """Codex C1: two classes whose mechanically-derived ``name``s collide
    (e.g. source-table stems ``foo-bar``/``foo_bar`` both normalizing to
    ``FooBarRecord`` under ``class_name_for_table``) must not silently let
    the second ``classes[cls.name] = ...`` assignment in ``emit_linkml``
    overwrite the first. Exercised by handing ``emit_linkml`` a directly
    hand-built, colliding ``CandidateModel`` — the exact caller-constructed
    path ``infer_candidate_model``'s own upstream guard
    (``_check_no_duplicate_class_names``) never runs for, since every
    ``emit_linkml`` caller in this test module bypasses ``infer_candidate_model``
    entirely.
    """
    a = _minimal_class("FooBarRecord", "foo-bar")
    b = _minimal_class("FooBarRecord", "foo_bar")
    model = CandidateModel(classes=[a, b], enums=[])

    with pytest.raises(DuplicateClassNameError) as exc_info:
        emit_linkml(model, engagement="acme")
    assert "FooBarRecord" in str(exc_info.value)
    assert "foo-bar" in str(exc_info.value)
    assert "foo_bar" in str(exc_info.value)

    # Control (vacuous-guard rule): distinct class names emit fine, and
    # BOTH classes are present in the output -- proving the guard fires on
    # the collision and only the collision.
    c = _minimal_class("FooBarRecord", "foo-bar")
    d = _minimal_class("QuuxRecord", "quux")
    control_model = CandidateModel(classes=[c, d], enums=[])
    emitted = emit_linkml(control_model, engagement="acme")
    assert "FooBarRecord" in emitted.yaml_text
    assert "QuuxRecord" in emitted.yaml_text


def test_duplicate_record_set_slot_name_rejected_before_emit() -> None:
    """Codex C1: two classes with already-DISTINCT, non-colliding ``name``s
    can still collide on ``to_snake_case(source_table)`` -- the key
    ``emit_linkml``'s top-level record-set container uses for each class's
    per-table attribute -- since nothing couples a class's ``name`` field to
    its ``source_table`` field. Left unrefused, the second
    ``record_set_attrs[...] = ...`` assignment silently overwrites the
    first, dropping an entire sampled table's records attribute from the
    emitted schema with no error anywhere in ``EmittedSchema``.
    """
    a = _minimal_class("FooRecord", "foo-bar")
    b = _minimal_class("BarRecord", "foo_bar")
    model = CandidateModel(classes=[a, b], enums=[])

    with pytest.raises(DuplicateRecordSetSlotNameError) as exc_info:
        emit_linkml(model, engagement="acme")
    assert "foo_bar" in str(exc_info.value)
    assert "foo-bar" in str(exc_info.value)

    # Control (vacuous-guard rule): distinct source-table snake_case keys
    # emit fine, and the record-set container carries BOTH classes'
    # per-table attributes.
    c = _minimal_class("FooRecord", "foo-bar")
    d = _minimal_class("BarRecord", "baz-qux")
    control_model = CandidateModel(classes=[c, d], enums=[])
    emitted = emit_linkml(control_model, engagement="acme")
    assert "foo_bar:" in emitted.yaml_text
    assert "baz_qux:" in emitted.yaml_text
