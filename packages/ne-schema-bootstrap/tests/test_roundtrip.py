# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.roundtrip`` — THE DEFINING PROOF of this unit.

Models ``packages/ne-schema/tests/test_generated_freshness.py`` (builds/uses
the real isolated codegen venv) and
``packages/ne-schema/tests/test_codegen_air_gap.py`` (AIR_GAP fail-closed).
These tests genuinely subprocess the venv — the round-trip is the whole
point of NE-ESB-EMIT; a YAML-well-formedness check would prove nothing.
"""

from __future__ import annotations

import ast
import json
import keyword
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from ne.envbuild import venv_python
from ne.net.airgap import AirGapViolationError
from ne.schema import codegen as ne_codegen
from ne.schema_bootstrap import emit as emit_mod
from ne.schema_bootstrap import sanitize
from ne.schema_bootstrap.emit import emit_linkml
from ne.schema_bootstrap.errors import DraftGenerationError, InvalidClassNameError
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.model import (
    CandidateClass,
    CandidateEdge,
    CandidateEnum,
    CandidateModel,
    CandidateSlot,
)
from ne.schema_bootstrap.prbody import render_pr_body
from ne.schema_bootstrap.profiles import profile_samples
from ne.schema_bootstrap.roundtrip import (
    _CODEGEN_VENV_DIR,
    _GENERATE_SCRIPT,
    _check_generated_module_imports,
    _verify_edge_defs,
    validate_draft_generates,
)

# tests/ -> ne-schema-bootstrap -> packages -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def _enum_member_names(module_text: str, enum_name: str) -> list[str]:
    """AST-walk the real generated module source and collect the member
    NAMES (not values) actually defined on the ``enum_name`` class — the
    ground truth this unit's oracle tests compare :func:`emit_mod._member_label`
    against. Raises if the class itself is absent (a fixture that stopped
    producing the target enum would otherwise silently prove nothing).

    **NFKC normalization is load-bearing here, on purpose (2026-07-28 Argus
    P2, third review round).** ``ast.Name.id`` is NOT the raw source text of
    the identifier token — the Python Language Reference guarantees
    identifiers are compared/stored by their NFKC normal form, and CPython's
    parser applies that normalization when it builds the ``Name`` node, so
    ``names`` here is the NFKC-normalized spelling, not necessarily what a
    ``grep`` over the source file would show. That is intentional, not an
    accident this helper papers over: :func:`emit_mod._member_label`
    ALSO applies ``unicodedata.normalize("NFKC", ...)`` as its own final
    step (mirroring exactly this CPython behavior — see that function's
    docstring, step 3), so both sides of every equivalence assertion below
    (``set(names) == {emit_mod._member_label(v) for v in ...}``) are
    normalized THE SAME WAY on purpose, not by accident of both happening to
    only be exercised with NFKC-invariant values. A single MICRO
    SIGN/GREEK-MU value pair (``'µm'``/``'μm'``) is exactly the case that
    would have made this comparison pass for the WRONG reason before
    :func:`emit_mod._member_label` normalized its own output — see
    ``test_real_mangle_collision_families_decline_and_prove_member_loss``'s
    ``nfkc-*`` families.
    """
    tree = ast.parse(module_text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == enum_name:
            names: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            names.append(target.id)
            return names
    raise AssertionError(f"class {enum_name!r} not found in generated module")


def _widgets_model_with_pvs(pvs: list[str]) -> CandidateModel:
    """Mirrors ``tests/test_emit.py::_widgets_model_with_pvs`` — a hand-built
    ``CandidateModel`` (one ``WidgetsRecord`` class, required ``id`` slot,
    ``tag`` slot enum-named ``WidgetsTagEnum``) so the oracle tests below can
    feed EXACT rule-triggering permissible-value shapes to the real
    generator without going through the profiler/inferencer.
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


def _three_table_fixture(tmp_path: Path) -> None:
    """customers + orders (customer_id FK >= 95%) + a 2-FK junction table +
    a status enum + an email-pattern column."""
    n_customers = 30
    _write_csv(
        tmp_path / "customers.csv",
        ["id", "name", "email", "status"],
        [
            [
                f"c{i}",
                f"Cust {i}",
                f"user{i}@example.com",
                "active" if i % 2 == 0 else "churned",
            ]
            for i in range(n_customers)
        ],
    )
    n_orders = 20
    _write_csv(
        tmp_path / "orders.csv",
        ["id", "customer_id", "status", "amount"],
        [
            [
                f"o{i}",
                f"c{i % n_customers}",
                "open" if i % 2 == 0 else "closed",
                str(10 + i),
            ]
            for i in range(n_orders)
        ],
    )
    n_reviews = 20
    _write_csv(
        tmp_path / "order_reviews.csv",
        ["id", "order_id", "customer_id"],
        [[f"r{i}", f"o{i % n_orders}", f"c{i % n_customers}"] for i in range(n_reviews)],
    )


def test_draft_roundtrips_through_real_codegen_venv(tmp_path: Path) -> None:
    _three_table_fixture(tmp_path)
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme-co")

    all_edges = [edge for cls in model.classes for edge in cls.edges]
    assert all_edges, "fixture must produce at least one FK edge"
    # Non-vacuity for the enum half of the import proof: without this, the
    # test would stay green (proving strictly less) the moment a config or
    # inference change stopped the fixture from producing an enum, since
    # `expected_names=[*expected_classes, *expected_enums, ...]` would just
    # shrink silently.
    assert emitted.emitted_enums, "fixture must produce at least one emitted enum"

    artifacts = validate_draft_generates(
        emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
        expected_edges=all_edges,
    )

    assert "class " in artifacts.pydantic_module
    assert artifacts.json_schema
    parsed = json.loads(artifacts.json_schema)
    assert emitted.top_class in parsed["$defs"]
    assert artifacts.manifest
    for edge in all_edges:
        assert (
            f'EdgeDef("{edge.name.upper()}", "{edge.source_class}", '
            f'"{edge.name}", "{edge.target_class}"'
        ) in artifacts.graph_module


def test_alternate_shapes_roundtrip_through_real_codegen_venv(tmp_path: Path) -> None:
    """2026-07-28 Argus P3: the happy-path test above only exercises the
    FK+junction+enum+pattern shape family. Three other emitter branches —
    each with its own dedicated EMIT-ONLY test in ``test_emit.py`` — were
    never actually fed to the real generator: the identifier-slot branch
    (``test_identifier_slot_emitted_without_identifier_flag``), the
    declined-enum ``range: string`` fallback
    (``test_enum_declined_on_unsafe_values_falls_back_to_string``), and the
    attribute-less all-provenance class (``_emit_class``'s ``if
    attributes:`` guard, reachable when every column on a table is
    provenance-named). The emitter could drift on any of these branches
    without a single test going red against the REAL generator. This test
    round-trips all three shapes in one draft through the real venv.
    """
    _write_csv(
        tmp_path / "customers.csv",
        ["customer_id", "name"],
        [[f"c{i}", f"Cust {i}"] for i in range(10)],
    )
    _write_csv(
        tmp_path / "widgets.csv",
        # 'mro' is probe-proven FATAL against the real generator (R5,
        # EMPIRICAL — collides with Enum.mro); the revised gate correctly
        # ADMITS a backtick value (see
        # test_charset_gate_accepted_values_roundtrip_through_real_generator),
        # so this fixture needed a genuinely-declining PV to keep exercising
        # the declined-enum "range: string" fallback branch.
        ["id", "tag"],
        [[f"w{i}", "mro" if i % 2 == 0 else "safe-value"] for i in range(20)],
    )
    _write_csv(
        tmp_path / "audits.csv",
        ["id", "source_system", "created_at"],
        [[f"a{i}", "salesforce", "2024-01-01"] for i in range(10)],
    )
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme-alt")

    # Confirm the fixture ACTUALLY exercises the three target branches
    # before spending the round-trip on it — a fixture that silently
    # stopped exercising one of them (e.g. a config/inference change that
    # stopped declining the hostile PV) would otherwise shrink to prove
    # strictly less without any signal.
    customers = next(c for c in model.classes if c.name == "CustomersRecord")
    id_slot = next(s for s in customers.slots if s.mechanical_name == "customer_id")
    assert id_slot.identifier, "fixture must produce a non-provenance identifier slot"
    assert "customer_id" in emitted.yaml_text
    assert emitted.declined_enums, "fixture must produce at least one declined enum"
    audits = next(c for c in model.classes if c.name == "AuditsRecord")
    assert emitted.provenance_mapped.get("AuditsRecord") == sorted(
        s.mechanical_name for s in audits.slots
    ), "fixture must produce an all-provenance (attribute-less) class"
    assert not audits.edges

    artifacts = validate_draft_generates(
        emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
        expected_edges=[],
    )

    assert "customer_id" in artifacts.pydantic_module
    assert "AuditsRecord" in artifacts.pydantic_module
    parsed = json.loads(artifacts.json_schema)
    assert "AuditsRecord" in parsed["$defs"]
    assert "CustomersRecord" in parsed["$defs"]
    assert "WidgetsRecord" in parsed["$defs"]


def test_broken_schema_fails_the_roundtrip_loudly(tmp_path: Path) -> None:
    # Non-vacuity control: without this test, an exit-0 assertion in the
    # happy-path test proves nothing about whether the validator can ever
    # detect a genuinely broken draft.
    _write_csv(tmp_path / "widgets.csv", ["id", "name"], [["w1", "A"], ["w2", "B"]])
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme")

    broken_yaml = emitted.yaml_text.replace(
        f"range: {emitted.top_class}", "range: NoSuchClassAtAll", 1
    )
    # If that replacement (RecordSet self-reference) didn't exist, fall back
    # to breaking the WidgetsRecord attribute's range directly.
    if broken_yaml == emitted.yaml_text:
        broken_yaml = emitted.yaml_text.replace(
            "range: WidgetsRecord\n", "range: NoSuchClassAtAll\n", 1
        )
    assert broken_yaml != emitted.yaml_text, "fixture failed to actually mutate a class range"

    with pytest.raises(DraftGenerationError):
        validate_draft_generates(
            broken_yaml,
            repo_root=_REPO_ROOT,
            top_class=emitted.top_class,
            expected_classes=[c.name for c in model.classes],
            expected_enums=emitted.emitted_enums,
        )


# ---------------------------------------------------------------------------
# The charset-gate oracle tests (2026-07-28 REVISE, NE-ESB-EMIT P1).
#
# The pre-revision `_ENUM_VALUE_SAFE` regex's docstring claimed to be
# "calibrated against the REAL generator ... the round-trip tests are the
# oracle for this charset, not a guess" — but NO test proved any of it. The
# tests below ARE that oracle: every EMPIRICAL rule in
# `ne.schema_bootstrap.emit._enum_decline_reason` is proven in BOTH
# directions against the real isolated codegen venv — the accepted set
# actually generates, and the rejected set actually breaks generation (or,
# for the one CONSERVATIVE rule, actually does NOT break generation).
# ---------------------------------------------------------------------------


def test_charset_gate_accepted_values_roundtrip_through_real_generator(
    tmp_path: Path,
) -> None:
    """The acceptance half of the oracle: 19 shapes the PRE-REVISION charset
    wrongly DECLINED (backtick, @, %, quote, <>, colon, unicode, digit-lead,
    a Python keyword, embedded double-quotes, emoji, non-ASCII script, comma,
    leading hyphen, hash, parens, pipe, an XID-skew punctuation mark, a
    multi-codepoint ZWJ emoji sequence) all emit as a real enum AND generate
    fine through the real ``.codegen-venv``, with the raw values surviving
    verbatim into the generated Pydantic module. ONE venv run.

    This test is now ALSO the P0 fix's two oracles in one run:

    - the P2 no-silent-drop COUNT oracle: the generated enum class must
      contain exactly ``len(accepted)`` members — a silent drop (the exact
      failure mode R6 exists to prevent) can no longer stay green here just
      because the round-trip didn't raise;
    - the derived-mangle EQUIVALENCE oracle: the SET of generated member
      names must equal ``{emit_mod._member_label(v) for v in accepted}`` —
      pinning ``_member_label`` against the real generator's actual output
      rather than against prose. Member ORDER is deliberately NOT asserted:
      the real generator does not preserve permissible-value order in the
      generated class body (probe-verified), so this must be a set
      comparison, not a list comparison.

    ``'a・b'`` (2026-07-28 differential-fuzz round) proves the
    :data:`emit_mod._XID_SKEW` emulation on the ACCEPT side: the real
    venv-side generator transliterates the KATAKANA MIDDLE DOT to
    ``'aKATAKANA_MIDDLE_DOTb'`` (it is not identifier-continue-legal in the
    venv's CPython 3.12, unlike the platform's 3.13), and only the
    equivalence assertion below — comparing against the real generated
    module, not prose — proves the mirror predicts that. ``'👨‍👩‍👧'``
    (a ZWJ emoji sequence, also XID-skew-bearing) is the same proof for a
    multi-codepoint value that a platform-only mirror would leave alone.
    """
    accepted = [
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
        "🔥hot",
        "状態",
        "a,b",
        "-item",
        "#tag",
        "x (old)",
        "a|b",
        "a・b",
        "👨‍👩‍👧",  # ZWJ family emoji sequence (XID skew, U+200D)
    ]
    model = _widgets_model_with_pvs(accepted)
    emitted = emit_linkml(model, engagement="acme")

    # Non-vacuity: the gate must have actually EMITTED this enum, not
    # silently declined it (which would make the round-trip below prove
    # nothing about the charset at all).
    assert emitted.emitted_enums == ["WidgetsTagEnum"]
    assert emitted.declined_enums == {}

    artifacts = validate_draft_generates(
        emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
    )

    assert '"`backtick"' in artifacts.pydantic_module
    assert '"@mention"' in artifacts.pydantic_module
    assert '"1st"' in artifacts.pydantic_module
    assert '"class"' in artifacts.pydantic_module

    names = _enum_member_names(artifacts.pydantic_module, "WidgetsTagEnum")
    assert len(names) == len(accepted), (
        f"expected {len(accepted)} generated members (no silent drop), got {len(names)}: {names}"
    )
    assert set(names) == {emit_mod._member_label(v) for v in accepted}


_REJECTED_EMPIRICAL_CASES = [
    ("", "empty-or-whitespace"),
    ("line1\nline2", "newline"),
    ("trail\\", "backslash"),
    ("a\x00b", "control-character"),
    ("mro", "enum-mro-collision"),
    ("_name_", "reserved-sunder"),
    ("mro,", "mro-comma-elided"),
    ("-name-", "sunder-hyphen-mangled"),
    (",,", "empty-label-after-mangle"),
    ("½ inch", "identifier-illegal-char"),
    ("ｍｒｏ", "nfkc-fullwidth-mro"),  # noqa: RUF001 -- deliberately FULLWIDTH LATIN
    # 2026-07-28 differential-fuzz round, P0 regression pin: 'm²' mangles to
    # the RAW label 'm²' (the transliteration step leaves '²' unchanged —
    # it satisfies str.isnumeric()), which is NOT a valid Python identifier.
    # Checking the NFKC-normalized form instead (normalize("NFKC", "m²") ==
    # "m2", legal) is the exact regression this round fixes: it wrongly
    # ADMITTED this value, and the real generator's raw token m² = "m²"
    # breaks with SyntaxError: invalid character '²' (U+00B2).
    ("m²", "trailing-superscript"),
    ("10 m³", "trailing-superscript-spaced"),
    # 2026-07-28 differential-fuzz round, P1 regression pin: a codepoint
    # assigned a name in the platform's Unicode 15.1 database (U+2EBF0, a
    # CJK Unified Ideographs Extension I character) but UNASSIGNED in the
    # pinned .codegen-venv's older 15.0 database — the real generator's
    # unicodedata.name() mangle (running INSIDE the venv) raises
    # ValueError: no such name.
    ("grade\U0002ebf0", "unicode-db-skew"),
]


@pytest.mark.parametrize(
    "hostile_value,label", _REJECTED_EMPIRICAL_CASES, ids=[c[1] for c in _REJECTED_EMPIRICAL_CASES]
)
def test_charset_gate_rejected_values_actually_break_the_real_generator(
    hostile_value: str, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection half of the oracle, one EMPIRICAL rule at a time: the
    gate declines ``hostile_value`` AND, when the decline is bypassed and the
    hostile permissible value is forced through to the real generator, the
    real generator genuinely fails. One venv run per case — the
    generator itself is the oracle for every rejection, not prose.
    """
    del label  # descriptive only; the assertion is against real behavior
    model = _widgets_model_with_pvs([hostile_value, "plain"])
    emitted = emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in emitted.declined_enums, (
        f"gate should have declined {hostile_value!r}"
    )

    monkeypatch.setattr(emit_mod, "_enum_decline_reason", lambda values, **_: None)
    forced = emit_mod.emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in forced.emitted_enums, (
        "forcing the decline off should have emitted the hostile enum"
    )

    with pytest.raises(DraftGenerationError):
        validate_draft_generates(
            forced.yaml_text,
            repo_root=_REPO_ROOT,
            top_class=forced.top_class,
            expected_classes=[c.name for c in model.classes],
            expected_enums=forced.emitted_enums,
        )


def test_charset_gate_conservative_declines_are_labeled_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``'_missing_'`` is declined by the gate (R5's underscore-bookended
    branch) but, forced through, GENERATES fine — living proof that the
    gate docstring's EMPIRICAL/CONSERVATIVE split is itself accurate, not
    decorative: this specific value is over-declined on purpose, not because
    it actually breaks the generator.
    """
    model = _widgets_model_with_pvs(["_missing_", "plain"])
    emitted = emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in emitted.declined_enums

    monkeypatch.setattr(emit_mod, "_enum_decline_reason", lambda values, **_: None)
    forced = emit_mod.emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in forced.emitted_enums

    # Must NOT raise — this is the point of the test.
    artifacts = validate_draft_generates(
        forced.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=forced.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=forced.emitted_enums,
    )
    assert '"_missing_"' in artifacts.pydantic_module


def test_colliding_member_labels_silently_drop_a_member_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R6 evidence: ``'a b'`` and ``'a_b'`` both mangle to the member
    label ``'a_b'``. The gate declines the collision; forced through, the
    real generator does NOT raise at all — it silently keeps one member and
    drops the other. A pass/fail round-trip alone can never catch this;
    only asserting on the generated module's actual member content can —
    which is exactly why R6 exists as a dedicated rule rather than relying
    on "the round-trip would have caught it".
    """
    model = _widgets_model_with_pvs(["a b", "a_b"])
    emitted = emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in emitted.declined_enums

    monkeypatch.setattr(emit_mod, "_enum_decline_reason", lambda values, **_: None)
    forced = emit_mod.emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in forced.emitted_enums

    artifacts = validate_draft_generates(
        forced.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=forced.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=forced.emitted_enums,
    )
    assert '"a b"' not in artifacts.pydantic_module, (
        "the generator was expected to silently drop this member"
    )
    assert '"a_b"' in artifacts.pydantic_module


_ENUM_MEMBERS_SCRIPT = """
import importlib.util
import json
import sys

module_path, enum_name = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("_ne_schema_bootstrap_draft_module", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cls = getattr(module, enum_name)
print(json.dumps(list(cls.__members__)))
"""


def _real_enum_member_names_at_runtime(module_path: Path, enum_name: str) -> list[str]:
    """Import the generated module in a FRESH platform-python subprocess
    (same isolation as ``roundtrip._check_generated_module_imports``) and
    return the ACTUAL runtime ``Enum.__members__`` names — deliberately NOT
    :func:`_enum_member_names`, which walks the generated SOURCE TEXT and so
    only ever sees the textual assignment targets the generator wrote. The
    two can diverge: ``enum._EnumDict.__setitem__`` silently refuses to
    RECORD a private-mangled name (``_<ClassName>__x``) as a member at all,
    so a value can have a normal-looking assignment LINE in the generated
    source and still be ABSENT from ``__members__`` at runtime — exactly the
    defect ``test_charset_gate_declines_private_mangled_enum_class_name``
    pins, and exactly why a textual/AST check alone cannot see it.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ENUM_MEMBERS_SCRIPT, str(module_path), enum_name],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: list[str] = json.loads(proc.stdout)
    return result


def test_charset_gate_declines_private_mangled_enum_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-07-28 Argus P1, fourth review round — an accept-side SILENT DROP
    the curated oracle never imagined across three prior rounds: a
    permissible value whose member label is a Python-mangled PRIVATE name
    relative to the enum's OWN generated class name (``_WidgetsTagEnum__foo``
    — CPython's own ``enum._is_private`` predicate, verbatim-derived in
    :func:`emit_mod._is_enum_private_member_name`) is silently dropped by
    ``enum._EnumDict.__setitem__`` while the generated module's class body
    executes — no exception, no warning (probe-verified live on both
    interpreters under ``-W error``) — so the generated module imports fine
    and LOOKS complete, but the member is gone: no ingested row carrying
    this value could ever validate. A pass/fail round-trip alone can never
    catch this (same argument as R6's own silent-drop families); only a
    RUNTIME member-content assertion against the generated module can — the
    generated SOURCE still contains the assignment textually, which is
    exactly why :func:`_real_enum_member_names_at_runtime` (not
    :func:`_enum_member_names`) is the oracle here.
    """
    model = _widgets_model_with_pvs(["_WidgetsTagEnum__foo", "plain"])
    emitted = emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in emitted.declined_enums, (
        "gate should have declined the private-mangled member label"
    )
    assert "private" in emitted.declined_enums["WidgetsTagEnum"].lower()

    monkeypatch.setattr(emit_mod, "_enum_decline_reason", lambda values, **_: None)
    forced = emit_mod.emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in forced.emitted_enums

    # Must NOT raise — the module imports cleanly; the defect is silent.
    artifacts = validate_draft_generates(
        forced.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=forced.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=forced.emitted_enums,
    )

    # The generated SOURCE still contains the assignment textually — a
    # textual/AST check alone would (wrongly) conclude the member survived.
    source_names = _enum_member_names(artifacts.pydantic_module, "WidgetsTagEnum")
    assert "_WidgetsTagEnum__foo" in source_names, (
        "fixture assumption broken: the generated SOURCE no longer contains the "
        "private-mangled assignment line, so this test would prove nothing about "
        "the textual-vs-runtime divergence it exists to demonstrate"
    )

    # But at RUNTIME, the class construction machinery drops it silently.
    module_path = tmp_path / "schema_draft.py"
    module_path.write_text(artifacts.pydantic_module, encoding="utf-8")
    runtime_names = _real_enum_member_names_at_runtime(module_path, "WidgetsTagEnum")
    assert "_WidgetsTagEnum__foo" not in runtime_names, (
        "the private-mangled member was expected to be silently dropped from "
        f"Enum.__members__ at runtime, got {runtime_names!r}"
    )
    assert "plain" in runtime_names


# ---------------------------------------------------------------------------
# The R6 P0 oracle (2026-07-28 REVISE, NE-ESB-EMIT CONTINUATION 2): the
# `_member_label` mirror now derives all three steps of the real mangle
# (underscore()'s comma-drop + hyphen-collapse, the number_ digit-prefix +
# unicodedata transliteration fallback, and the iskeyword masking step), so
# R6 must catch collision families produced by EVERY one of those steps —
# not just the whitespace-collapse step the pre-fix approximation covered.
# Each family below is both (a) DECLINED by the gate and (b) forced through
# the real generator to prove the actual failure mode: five families
# silently drop to exactly one surviving member (the P0 failure mode itself
# — a pass/fail round-trip alone could never see it), and three
# (keyword-suffix, nfkc-micro-sign, nfkc-fullwidth-letter) fail LOUD with a
# real TypeError at Enum-class construction — every loud family shares the
# same root cause: the collision is invisible to the generator's OWN
# label-generation dedup (it happens one step LATER — pydanticgen's
# keyword-masking for keyword-suffix, 2026-07-28 Argus P1 third round;
# CPython's own compile-time NFKC identifier normalization for the two nfkc
# families), so two textually-distinct assignment lines actually reach the
# generated source and Python itself raises on the duplicate definition
# (observed live: EnumDict.__setitem__ raises "TypeError: 'class_' already
# defined as 'class'" / "TypeError: 'μm' already defined as 'µm'";
# 2026-07-28 Argus P3 correction of an earlier ValueError misstatement for
# keyword-suffix, now pinned by asserting the exception TEXT below, not
# merely DraftGenerationError).
# ---------------------------------------------------------------------------

_COLLISION_FAMILIES = [
    ("hyphen-vs-space", ["in-progress", "in progress"], "silent-drop"),
    ("hyphen-vs-underscore", ["a-b", "a_b"], "silent-drop"),
    ("comma-elision", ["a,b", "ab"], "silent-drop"),
    ("digit-prefix", ["1st", "number_1st"], "silent-drop"),
    ("three-way", ["in-progress", "in progress", "in_progress"], "silent-drop"),
    ("keyword-suffix", ["class", "class_"], "loud-failure"),
    # 2026-07-28 Argus P1, third review round: collisions that only exist
    # because of CPython's own NFKC identifier normalization (step 3 of
    # _member_label) — i.e. the two values are DISTINCT, individually valid
    # identifiers at the oocodegen-mangle level (no dedup happens inside the
    # generator's own label-generation step, unlike the families above), so
    # BOTH survive into the generated source as separate assignment lines;
    # CPython's compile-time identifier normalization then collides them,
    # which is why this mode is "loud-failure" like keyword-suffix (a
    # duplicate-name TypeError at Enum-class construction) rather than the
    # "silent-drop" mode of the oocodegen-internal-mangle families above.
    ("nfkc-micro-sign", ["µm", "μm"], "loud-failure"),
    ("nfkc-fullwidth-letter", ["A", "Ａ"], "loud-failure"),  # noqa: RUF001
    # 2026-07-28 differential-fuzz round: the venv-isidentifier emulation
    # (`_venv_isidentifier`, `_XID_SKEW`) proves the mirror predicts the
    # real generator's transliteration of the four codepoints that are
    # identifier-continue-legal on the PLATFORM interpreter but NOT on the
    # venv's — e.g. 'a・b' (U+30FB KATAKANA MIDDLE DOT). A platform-only
    # mirror would judge 'a・b' already a legal identifier (no
    # transliteration) and would therefore never collide it with anything;
    # the real venv-side generator instead transliterates it to
    # 'aKATAKANA_MIDDLE_DOTb', which collides with any OTHER value whose
    # raw label already spells that out. Two independently-plausible
    # customer values collapse to the same real generated member.
    (
        "xid-skew-transliteration",
        ["a・b", "aKATAKANA_MIDDLE_DOTb"],
        "silent-drop",
    ),
]


@pytest.mark.parametrize(
    "family,pvs,mode",
    _COLLISION_FAMILIES,
    ids=[f[0] for f in _COLLISION_FAMILIES],
)
def test_real_mangle_collision_families_decline_and_prove_member_loss(
    family: str, pvs: list[str], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P0 oracle: for each collision family spanning a DIFFERENT step of
    the real mangle, the gate declines naming both colliding values, and —
    with the decline forced off — the real generator either silently drops
    to exactly one surviving member (``mode == "silent-drop"``) or fails
    loud with ``DraftGenerationError`` (``mode == "loud-failure"``).
    """
    del family  # descriptive only (parametrize id); assertions are behavioral
    model = _widgets_model_with_pvs(pvs)
    emitted = emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in emitted.declined_enums, f"gate should have declined {pvs!r}"
    reason = emitted.declined_enums["WidgetsTagEnum"]
    assert "mangle" in reason
    # The reason must name (at least) two of the colliding values.
    named = sum(1 for pv in pvs if repr(pv) in reason)
    assert named >= 2, f"reason should name at least two colliding values, got: {reason!r}"
    # 2026-07-28 Argus P3, third review round: the reason string must not
    # claim a SPECIFIC failure mode that is false for THIS family. The
    # pre-fix wording unconditionally asserted "not a generation failure"
    # for every collision, which is demonstrably false for keyword-suffix
    # and the two nfkc-* families below (all three are loud
    # DraftGenerationError, not a silent drop) — asserted for every family,
    # not just the loud ones, so a regression back to the family-blind claim
    # goes RED regardless of which family runs first.
    assert "not a generation failure" not in reason

    monkeypatch.setattr(emit_mod, "_enum_decline_reason", lambda values, **_: None)
    forced = emit_mod.emit_linkml(model, engagement="acme")
    assert "WidgetsTagEnum" in forced.emitted_enums

    if mode == "silent-drop":
        artifacts = validate_draft_generates(
            forced.yaml_text,
            repo_root=_REPO_ROOT,
            top_class=forced.top_class,
            expected_classes=[c.name for c in model.classes],
            expected_enums=forced.emitted_enums,
        )
        names = _enum_member_names(artifacts.pydantic_module, "WidgetsTagEnum")
        assert len(names) == 1, (
            f"expected the colliding family to collapse to exactly 1 surviving "
            f"member, got {len(names)}: {names}"
        )
    else:
        with pytest.raises(DraftGenerationError) as exc_info:
            validate_draft_generates(
                forced.yaml_text,
                repo_root=_REPO_ROOT,
                top_class=forced.top_class,
                expected_classes=[c.name for c in model.classes],
                expected_enums=forced.emitted_enums,
            )
        # 2026-07-28 Argus P3: pin the ACTUAL failure mode (TypeError from
        # enum.py's EnumDict.__setitem__ on duplicate-key definition), not
        # merely that SOME DraftGenerationError was raised — a docstring
        # that misnamed this ValueError went uncaught before this assertion
        # existed.
        assert "TypeError" in str(exc_info.value)
        assert "already defined as" in str(exc_info.value)


def test_platform_keyword_list_matches_codegen_venv_keyword_list() -> None:
    """2026-07-28 Argus P3: :func:`emit_mod._member_label`'s keyword-masking
    step calls ``keyword.iskeyword`` on the PLATFORM interpreter, mirroring
    the real generator's ``EnumValue.alias_python_keywords`` — which runs
    INSIDE the ``.codegen-venv``'s interpreter, a different Python minor
    version today (platform 3.13, venv 3.12). The mirror's correctness
    silently depends on ``keyword.kwlist`` being identical across both
    interpreters. This test PINS that parity by asking the real venv
    interpreter directly (one cheap subprocess, no code generation), rather
    than assuming it in a docstring that could go stale unnoticed.
    """
    ne_codegen.ensure_env(_REPO_ROOT)
    venv_py = venv_python(_REPO_ROOT / _CODEGEN_VENV_DIR)
    proc = subprocess.run(
        [str(venv_py), "-c", "import json, keyword; print(json.dumps(sorted(keyword.kwlist)))"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    venv_kwlist = set(json.loads(proc.stdout))
    platform_kwlist = set(keyword.kwlist)

    # Non-vacuity: prove this comparison actually discriminates before
    # trusting its positive result — a genuinely divergent list must fail
    # the identical assertion shape used below.
    assert platform_kwlist != (venv_kwlist - {"class"})

    assert platform_kwlist == venv_kwlist, (
        f"platform Python keyword.kwlist ({sorted(platform_kwlist - venv_kwlist)} extra) "
        f"diverged from the .codegen-venv's ({sorted(venv_kwlist - platform_kwlist)} extra) "
        "— _member_label's keyword.iskeyword() mirror no longer matches the real "
        "generator's EnumValue.alias_python_keywords check"
    )


# ---------------------------------------------------------------------------
# The P1 closure (2026-07-28, differential-fuzz round): a cross-interpreter
# PROPERTY-PARITY digest. `_venv_isidentifier` and `_member_label`'s NFKC
# step both assume every codepoint's mirror-relevant Unicode properties are
# identical across the platform interpreter (this gate's own runtime) and
# the pinned `.codegen-venv`'s interpreter (where the real generator's
# mangle actually runs), EXCEPT for the two declared skew sets
# (`_UNICODE_DB_SKEW`, `_XID_SKEW`). That "except" is proven exhaustive
# here, not assumed: the same 8-predicate signature — everything the mirror
# consults — is hashed over every non-surrogate codepoint EXCLUDING the
# declared skew, on BOTH interpreters, and the digests must match exactly.
# ---------------------------------------------------------------------------

#: Shared signature-digest logic, embedded so the SAME source text runs on
#: both interpreters (`exec`'d in-process for the platform side, passed to
#: `python -c` for the venv side) — no hand-duplicated second
#: implementation that could quietly drift from the first.
_SIGNATURE_FUNCS = r"""
import hashlib
import re
import unicodedata


def _property_signature_digest(exclude):
    h = hashlib.sha256()
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF or cp in exclude:
            continue
        ch = chr(cp)
        sig = (
            unicodedata.name(ch, ""),
            ch.isspace(),
            ch.isalpha() or ch.isnumeric(),
            ch.isidentifier(),
            ("_" + ch).isidentifier(),
            unicodedata.normalize("NFKC", ch),
            bool(re.match(r"\d", ch)),
            bool(re.match(r"\s", ch)),
        )
        h.update(repr(sig).encode("utf-8", "surrogatepass"))
    return h.hexdigest()
"""

_VENV_SIGNATURE_DRIVER = (
    _SIGNATURE_FUNCS
    + r"""
import json
import sys
import unicodedata

exclude = set(json.loads(sys.argv[1]))
xid_skew_cps = json.loads(sys.argv[2])
db_skew_cps = json.loads(sys.argv[3])
digest = _property_signature_digest(exclude)
xid_report = {str(cp): ("_" + chr(cp)).isidentifier() for cp in xid_skew_cps}
db_skew_named = {str(cp): unicodedata.name(chr(cp), None) is not None for cp in db_skew_cps}
print(
    json.dumps(
        {
            "unidata_version": unicodedata.unidata_version,
            "digest": digest,
            "xid_report": xid_report,
            "db_skew_named": db_skew_named,
        }
    )
)
"""
)


def _platform_signature_digest(exclude: frozenset[int] | set[int]) -> str:
    namespace: dict[str, object] = {}
    exec(compile(_SIGNATURE_FUNCS, "<signature-funcs>", "exec"), namespace)
    digest_fn = namespace["_property_signature_digest"]
    assert callable(digest_fn)
    result = digest_fn(exclude)
    assert isinstance(result, str)
    return result


#: Lean driver for the sensitivity control below — digest only, no
#: xid/db-skew reporting (those need the DECLARED skew sets specifically;
#: the control below deliberately uses OTHER exclusion sets).
_VENV_DIGEST_ONLY_DRIVER = (
    _SIGNATURE_FUNCS
    + r"""
import json
import sys

exclude = set(json.loads(sys.argv[1]))
print(json.dumps({"digest": _property_signature_digest(exclude)}))
"""
)


def _venv_signature_digest(venv_py: Path, exclude: frozenset[int] | set[int]) -> str:
    proc = subprocess.run(
        [str(venv_py), "-c", _VENV_DIGEST_ONLY_DRIVER, json.dumps(sorted(exclude))],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result: dict[str, str] = json.loads(proc.stdout)
    return result["digest"]


def test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew() -> None:
    """P1 closure: prove :data:`emit_mod._UNICODE_DB_SKEW` |
    :data:`emit_mod._XID_SKEW` is EXHAUSTIVE — every codepoint's
    mirror-relevant Unicode properties agree across the platform and venv
    interpreters outside those two declared sets — rather than assumed in a
    comment. The exclusion set is built FROM the emit module's own declared
    constants (imported, not copied), so this test proves the DECLARED sets
    are exhaustive, not a hand-copied duplicate of them.
    """
    ne_codegen.ensure_env(_REPO_ROOT)
    venv_py = venv_python(_REPO_ROOT / _CODEGEN_VENV_DIR)

    exclude_cps = emit_mod._UNICODE_DB_SKEW | emit_mod._XID_SKEW
    xid_skew_sorted = sorted(emit_mod._XID_SKEW)
    db_skew_sorted = sorted(emit_mod._UNICODE_DB_SKEW)

    proc = subprocess.run(
        [
            str(venv_py),
            "-c",
            _VENV_SIGNATURE_DRIVER,
            json.dumps(sorted(exclude_cps)),
            json.dumps(xid_skew_sorted),
            json.dumps(db_skew_sorted),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    venv_result: dict[str, Any] = json.loads(proc.stdout)

    platform_digest = _platform_signature_digest(exclude_cps)

    # (2) the hash-pinned venv is the STABLE side — its database version
    # must not have silently moved out from under the declared skew sets.
    assert venv_result["unidata_version"] == "15.0.0", (
        f"pinned .codegen-venv's unicodedata.unidata_version changed to "
        f"{venv_result['unidata_version']!r} — re-derive _UNICODE_DB_SKEW/"
        "_XID_SKEW and re-run this test before trusting the gate again"
    )

    # (1) the digests must match exactly outside the declared skew.
    assert platform_digest == venv_result["digest"], (
        "platform and venv Unicode property signatures diverge OUTSIDE the "
        "declared _UNICODE_DB_SKEW/_XID_SKEW sets — the mirror's emulation "
        "is no longer exhaustive; re-derive the skew sets against the real "
        "interpreters before trusting _venv_isidentifier or _member_label's "
        "NFKC step"
    )

    # (3) every _UNICODE_DB_SKEW codepoint: named on the platform (that is
    # WHY R4b, not R4, is needed for it) and unnamed on the venv (that is
    # WHY R4b must decline it before the real generator's mangle reaches
    # unicodedata.name() inside the venv and raises).
    for cp in db_skew_sorted:
        assert unicodedata.name(chr(cp), None) is not None, (
            f"U+{cp:04X} claimed as _UNICODE_DB_SKEW but is UNNAMED on the "
            "platform too — R4 would already decline it, R4b is redundant "
            "for this codepoint"
        )
    venv_named_skew_count = sum(1 for v in venv_result["db_skew_named"].values() if v)
    assert venv_named_skew_count == 0, (
        f"{venv_named_skew_count} of {len(db_skew_sorted)} _UNICODE_DB_SKEW codepoints "
        "are ALSO named in the venv's database — the skew set is stale (over-broad); "
        "re-derive it against the real 15.0/15.1 delta"
    )

    # (4) every _XID_SKEW codepoint: identifier-continue-legal on the
    # platform, illegal on the venv — the exact asymmetry
    # `_venv_isidentifier` exists to correct for.
    for cp in xid_skew_sorted:
        assert ("_" + chr(cp)).isidentifier(), (
            f"U+{cp:04X} claimed as _XID_SKEW but is NOT identifier-continue-legal "
            "on the platform either — the skew set is stale"
        )
        assert venv_result["xid_report"][str(cp)] is False, (
            f"U+{cp:04X} claimed as _XID_SKEW but IS identifier-continue-legal on "
            "the venv too — the skew set is stale (over-broad); re-derive it"
        )

    # Sensitivity control (vacuous-guard rule, 2026-07-28 Argus P2 fourth
    # review round). The PREVIOUS version of this control compared a
    # platform digest computed over `exclude_cps - {0x2EBF0}` against
    # `venv_result["digest"]`, which was computed over the FULL exclude set
    # — the two sides therefore hashed a DIFFERENT NUMBER of codepoints, so
    # the inequality held by construction regardless of whether any real
    # Unicode-database skew was involved (verified: excluding one BENIGN
    # codepoint instead of a real skew one also broke that comparison). Both
    # halves below run BOTH sides (platform AND venv) over the SAME
    # exclusion set, isolating cardinality from actual skew:
    #   - leaking one genuine skew codepoint (0x2EBF0) back into BOTH sides'
    #     exclusion set must break equality — the real skew becomes visible
    #     again, proving this comparison discriminates an actual leak;
    #   - excluding one extra BENIGN codepoint (0x41 'A', no real skew) from
    #     BOTH sides — the same CARDINALITY delta, deliberately — must NOT
    #     break equality, proving the previous version's false-positive
    #     mechanism (any exclusion-set-size change moves the digest) is
    #     ruled out here.
    leaked_exclude = exclude_cps - {0x2EBF0}
    leaked_platform_digest = _platform_signature_digest(leaked_exclude)
    leaked_venv_digest = _venv_signature_digest(venv_py, leaked_exclude)
    assert leaked_platform_digest != leaked_venv_digest, (
        "leaking one genuine skew codepoint (0x2EBF0) back into BOTH sides' exclusion "
        "set still produced matching digests — this comparison does not actually "
        "discriminate a leaked skew codepoint"
    )

    benign_exclude = exclude_cps | {0x41}
    benign_platform_digest = _platform_signature_digest(benign_exclude)
    benign_venv_digest = _venv_signature_digest(venv_py, benign_exclude)
    assert benign_platform_digest == benign_venv_digest, (
        "excluding one extra BENIGN codepoint (0x41, no real skew) from BOTH sides "
        "broke digest equality — the sensitivity control above would be confounded "
        "by exclusion-set cardinality alone rather than by actual skew"
    )


def test_near_miss_families_accepted_and_roundtrip_full_member_count() -> None:
    """The over-declining guard the stricter rule must not violate
    (orchestrator ruling point 3): eight NEAR-collision values, each close
    in shape to one of the collision families above but NOT actually
    colliding under the real mangle, must be ACCEPTED by the gate and must
    all eight survive into the generated module. A rule that declined
    "anything with punctuation" (or, per the 2026-07-28 Argus P1 third-round
    fix, "anything non-ASCII") would fail this test even though it would
    also pass the collision-family tests above. ``'µs'`` (MICRO SIGN) is the
    NFKC near-miss: it has no NFKC-equivalent partner in this set, so the
    new NFKC-normalization step in :func:`emit_mod._member_label` must not
    cause it to be declined or dropped on its own.
    """
    pvs = [
        "in-progress",
        "in review",
        "a,bc",
        "1st",
        "number_2nd",
        "a_b c",
        "None",
        "µs",
    ]
    model = _widgets_model_with_pvs(pvs)
    emitted = emit_linkml(model, engagement="acme")
    assert emitted.declined_enums == {}, (
        f"near-miss values should all be ADMITTED, not declined ({emitted.declined_enums})"
    )
    assert "WidgetsTagEnum" in emitted.emitted_enums

    artifacts = validate_draft_generates(
        emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
    )
    names = _enum_member_names(artifacts.pydantic_module, "WidgetsTagEnum")
    assert len(names) == len(pvs), (
        f"expected all {len(pvs)} members present, got {len(names)}: {names}"
    )
    assert set(names) == {emit_mod._member_label(v) for v in pvs}


def test_derived_class_name_oracle_bidirectional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2, both directions against the real venv.

    Positive: a sample stem that starts with a letter even though it
    contains digits (``orders 2023.csv``) derives a valid class name and
    round-trips green.

    Negative: a purely-digit stem (``2023.csv``) derives ``2023Record``,
    which is NOT a valid Python identifier. With the early
    ``_validate_class_names`` check bypassed (proving this isn't just an
    artifact of the early check itself), the SAME draft reaches the real
    generator and reproduces the raw failure the early check exists to
    prevent: ``... is not a valid Python identifier``.
    """
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    _write_csv(
        good_dir / "orders 2023.csv",
        ["id", "name"],
        [[f"r{i}", f"Row {i}"] for i in range(10)],
    )
    good_bundle = profile_samples(good_dir)
    good_model = infer_candidate_model(good_bundle)
    good_emitted = emit_linkml(good_model, engagement="acme")
    assert "Orders2023Record" in good_emitted.yaml_text

    good_artifacts = validate_draft_generates(
        good_emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=good_emitted.top_class,
        expected_classes=[c.name for c in good_model.classes],
        expected_enums=good_emitted.emitted_enums,
    )
    assert "Orders2023Record" in good_artifacts.pydantic_module

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write_csv(
        bad_dir / "2023.csv",
        ["id", "name"],
        [[f"r{i}", f"Row {i}"] for i in range(10)],
    )
    bad_bundle = profile_samples(bad_dir)
    bad_model = infer_candidate_model(bad_bundle)

    with pytest.raises(InvalidClassNameError):
        emit_linkml(bad_model, engagement="acme")

    monkeypatch.setattr(emit_mod, "_validate_class_names", lambda model: None)
    bypassed_emitted = emit_mod.emit_linkml(bad_model, engagement="acme")
    assert "2023Record" in bypassed_emitted.yaml_text

    with pytest.raises(DraftGenerationError) as exc_info:
        validate_draft_generates(
            bypassed_emitted.yaml_text,
            repo_root=_REPO_ROOT,
            top_class=bypassed_emitted.top_class,
            expected_classes=[c.name for c in bad_model.classes],
            expected_enums=bypassed_emitted.emitted_enums,
        )
    assert "is not a valid Python identifier" in str(exc_info.value)


def test_hostile_table_stem_roundtrips_and_prbody_fence_adapts(tmp_path: Path) -> None:
    """P3-1: a sample-file stem containing a 3-backtick run, an apostrophe,
    and a space is safe end-to-end — through the emitted YAML class
    description, through the real generator's docstring rendering, AND
    through ``prbody``'s dynamic fence — even though NONE of it is
    sanitized via ``safe_inline``/``fenced`` at the point it enters the YAML
    (see ``sanitize.py``'s corrected module docstring: this is the THIRD
    path, safe by construction of PyYAML + linkml + the dynamic fence, not
    by an explicit sanitizer call here).
    """
    stem = "wid```get's x"
    _write_csv(
        tmp_path / f"{stem}.csv",
        ["id", "name"],
        [[f"r{i}", f"Row {i}"] for i in range(10)],
    )
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme-host")

    # The raw stem survives into the emitted class description verbatim.
    assert stem in emitted.yaml_text

    artifacts = validate_draft_generates(
        emitted.yaml_text,
        repo_root=_REPO_ROOT,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
    )
    assert stem in artifacts.pydantic_module

    # The dynamic fence grows past the embedded 3-backtick run.
    assert sanitize.fence(emitted.yaml_text) == "`" * 4

    pr_body = render_pr_body(model, emitted, engagement="acme-host")
    assert pr_body.count("\n````") >= 2


_GOOD_MODULE_SOURCE = """
class OperationalEntity:
    pass

class FooRecord(OperationalEntity):
    pass

class TopRecordSet:
    def __init__(self):
        self.items = []
"""

_MISSING_NAME_MODULE_SOURCE = """
class OperationalEntity:
    pass

class TopRecordSet:
    def __init__(self):
        self.items = []
"""

_SYNTAX_ERROR_SOURCE = "def broken(:\n    pass\n"

_INSTANTIATION_RAISES_SOURCE = """
class OperationalEntity:
    pass

class FooRecord(OperationalEntity):
    pass

class TopRecordSet:
    def __init__(self):
        raise RuntimeError("instantiation-must-be-proven")
"""


def test_import_check_helper_bidirectional(tmp_path: Path) -> None:
    good = tmp_path / "good_module.py"
    good.write_text(_GOOD_MODULE_SOURCE, encoding="utf-8")
    _check_generated_module_imports(
        good, expected_names=["FooRecord", "OperationalEntity"], top_class="TopRecordSet"
    )  # must not raise

    syntax_error = tmp_path / "syntax_error_module.py"
    syntax_error.write_text(_SYNTAX_ERROR_SOURCE, encoding="utf-8")
    with pytest.raises(DraftGenerationError):
        _check_generated_module_imports(
            syntax_error,
            expected_names=["FooRecord", "OperationalEntity"],
            top_class="TopRecordSet",
        )

    missing_name = tmp_path / "missing_name_module.py"
    missing_name.write_text(_MISSING_NAME_MODULE_SOURCE, encoding="utf-8")
    with pytest.raises(DraftGenerationError):
        _check_generated_module_imports(
            missing_name,
            expected_names=["FooRecord", "OperationalEntity"],
            top_class="TopRecordSet",
        )

    # 2026-07-28 P3-2: the prior three controls above all pass without the
    # `cls()` instantiation line in `_IMPORT_CHECK_SCRIPT` at all — none of
    # them exercise instantiation FAILING. This control's `top_class`
    # getattr succeeds (the class exists, importable, matches expected
    # names) but ITS INSTANTIATION raises. Without the `cls()` line in
    # `_IMPORT_CHECK_SCRIPT`, this block goes green for the wrong reason —
    # this is what makes deleting that line detectable (planner-verified the
    # prior three controls stay green either way).
    instantiation_raises = tmp_path / "instantiation_raises_module.py"
    instantiation_raises.write_text(_INSTANTIATION_RAISES_SOURCE, encoding="utf-8")
    with pytest.raises(DraftGenerationError) as exc_instantiation:
        _check_generated_module_imports(
            instantiation_raises,
            expected_names=["FooRecord", "OperationalEntity"],
            top_class="TopRecordSet",
        )
    assert "RuntimeError" in str(exc_instantiation.value)


def _edge(name: str, source: str, target: str) -> CandidateEdge:
    return CandidateEdge(
        name=name,
        source_class=source,
        target_class=target,
        containment=1.0,
        via_name_heuristic=True,
    )


def _edge_def_line(name: str, source: str, target: str) -> str:
    return f'EdgeDef("{name.upper()}", "{source}", "{name}", "{target}")'


def test_verify_edge_defs_bidirectional() -> None:
    # Non-vacuity control for `_verify_edge_defs` itself, mutation-CONFIRMED
    # against Argus round-1 finding: a no-op `_verify_edge_defs` (just
    # `return`) makes this test fail, where before this test existed it
    # would not — the happy-path round-trip test only ever proves
    # membership (each expected EdgeDef substring present), never the
    # missing- or extra-edge directions, and never exercises the raise at
    # all when `_verify_edge_defs` is replaced with a no-op.
    expected = [_edge("customer_id", "OrdersRecord", "CustomersRecord")]

    # Happy path: graph text contains exactly the expected EdgeDef row.
    good_graph = (
        "EDGE_DEFS = [\n    "
        + _edge_def_line("customer_id", "OrdersRecord", "CustomersRecord")
        + ",\n]\n"
    )
    _verify_edge_defs(good_graph, expected)  # must not raise

    # (a) missing: graph text has NO EdgeDef rows at all -> raises, message
    # names the missing edge.
    empty_graph = "EDGE_DEFS = []\n"
    with pytest.raises(DraftGenerationError) as exc_missing:
        _verify_edge_defs(empty_graph, expected)
    assert "missing" in str(exc_missing.value)
    assert "customer_id" in str(exc_missing.value)

    # (b) extra: graph text has the expected row PLUS an unexpected one ->
    # raises, message names the extra edge. This is the direction a
    # membership-only check (e.g. "each expected substring present") can
    # never catch, which is exactly why `_verify_edge_defs` compares sets
    # rather than checking containment.
    extra_graph = (
        "EDGE_DEFS = [\n    "
        + _edge_def_line("customer_id", "OrdersRecord", "CustomersRecord")
        + ",\n    "
        + _edge_def_line("author_id", "DocsRecord", "PeopleRecord")
        + ",\n]\n"
    )
    with pytest.raises(DraftGenerationError) as exc_extra:
        _verify_edge_defs(extra_graph, expected)
    assert "extra" in str(exc_extra.value)
    assert "author_id" in str(exc_extra.value)


_CODEGEN_REL = Path("packages") / "ne-schema" / "codegen"
_VENV_REL = Path("packages") / "ne-schema" / ".codegen-venv"


def test_air_gap_fail_closed_before_any_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codegen_dir = tmp_path / _CODEGEN_REL
    codegen_dir.mkdir(parents=True, exist_ok=True)
    (codegen_dir / "requirements.txt").write_text("linkml==1.8.0\n", encoding="utf-8")
    # No venv directory created at all.

    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(ne_codegen, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(AirGapViolationError):
        validate_draft_generates(
            "id: https://example/schema\nname: x\n",
            repo_root=tmp_path,
            top_class="TopRecordSet",
            expected_classes=[],
            expected_enums=[],
        )

    assert calls == [], "no subprocess/_run call should happen before the AIR_GAP check fires"


def test_codegen_layout_constants_match_ne_schema() -> None:
    assert _CODEGEN_VENV_DIR == ne_codegen._VENV_DIR
    assert _GENERATE_SCRIPT == ne_codegen._GENERATE_SCRIPT
