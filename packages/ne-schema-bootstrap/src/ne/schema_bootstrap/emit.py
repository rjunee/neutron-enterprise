# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The house-dialect LinkML emitter: :class:`CandidateModel` -> YAML text.

``emit_linkml`` is a PURE function of ``(model, engagement, config)`` — no
I/O, no clock, no randomness — mirroring the determinism discipline of
:mod:`ne.schema_bootstrap.infer`. The emitted YAML follows the
``operational.yaml`` idioms (abstract provenance base, ``is_a`` classes,
``inlined: false`` FK relations, a ``tree_root`` record-set container) so the
draft is structurally identical in SHAPE to the platform's hand-authored
seed schema — only the CONTENT differs.

**Decided deviation from design §4.1's prose — read before touching this
module.** The abstract provenance base class emitted here is named LITERALLY
``OperationalEntity``, not ``<Engagement>Entity`` as the design doc's prose
states. The real generator's Tier-2 entity-graph emitter hardwires that exact
class name when it decides which classes are concrete operational entities
(``packages/ne-schema/codegen/generate.py:211`` — ``cdef.is_a ==
"OperationalEntity"`` — and ``:215`` — ``all_classes["OperationalEntity"]``,
both outside this unit's ``mayTouch``). Naming the base class anything else
would silently produce a draft whose Tier-2 graph contracts never generate
(``entity_classes`` would be empty). Any landed engagement schema wanting
graph contracts needs this literal name today; changing it is a follow-up
unit's job (editing ``packages/ne-schema``), not this emitter's.

**Residual #1 (inherited from U1), decided here.** ``ColumnProfile`` carries
both ``distinct_count`` (the RAW, pre-scrub cardinality) and
``distinct_values`` (the RETAINED, post-scrub-and-dedupe list, which can be
SMALLER when two raw-distinct values collapse to the same value after
scrub/truncate — see :mod:`ne.schema_bootstrap.profiles`'s
``_profile_column`` docstring). The emitted ``permissible_values`` are always
the RETAINED list (``CandidateEnum.permissible_values``, already what
:mod:`ne.schema_bootstrap.infer` populates) — it is the only PII-safe
artifact; the raw ``distinct_count`` never reaches the YAML at all, so the
emitted schema can never state an inconsistent count/value-list pair. The
divergence itself (when it exists) is disclosed in the traceability report
(:mod:`ne.schema_bootstrap.report`), not silently dropped. The enum GATE in
:mod:`ne.schema_bootstrap.infer` (``distinct_count <= config.enum_max_distinct``)
is deliberately left untouched — it stays conservative (gated on the LARGER
raw count), so it can only ever REJECT more candidates than a
retained-count gate would, never accept a candidate a retained-count gate
would have rejected.

Seed anchors (design §4.1's "assemble, don't invent") are surfaced only in
the report, never as ``is_a: <SeedClass>`` here — adopting the platform's
seed model would require importing ``operational.yaml`` into a standalone
draft, breaking the "generates on its own" property this whole unit proves.
"""

from __future__ import annotations

import re
import unicodedata
from keyword import iskeyword

import yaml
from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_CONFIG, BootstrapConfig
from .errors import (
    DuplicateClassNameError,
    DuplicateRecordSetSlotNameError,
    InvalidClassNameError,
    InvalidEngagementNameError,
)
from .model import CandidateClass, CandidateModel, CandidateSlot, to_camel_case, to_snake_case
from .seeds import PROVENANCE_SLOTS

_ENGAGEMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

#: A member label that collides with ``enum.Enum``'s own ``mro`` class
#: attribute. Probe-proven fatal (2026-07-28 revision): a permissible value
#: whose generator member-name mangle produces exactly this string breaks
#: the real generator — see ``tests/test_roundtrip.py::
#: test_charset_gate_rejected_values_actually_break_the_real_generator``.
_FATAL_MEMBER_LABEL = "mro"


def _is_enum_private_member_name(cls_name: str, label: str) -> bool:
    """Verbatim-derived mirror of CPython's own ``enum._is_private`` — the
    predicate ``enum._EnumDict.__setitem__`` calls, while the generated
    module's class body executes, to decide whether an assignment target is
    a Python-mangled PRIVATE name relative to the enum class currently being
    defined (2026-07-28 Argus P1, fourth review round).

    Read directly from ``Lib/enum.py`` (identical on the platform's 3.13.5
    and the pinned ``.codegen-venv``'s 3.12.11, probe-verified — this
    predicate is pure string logic with no Unicode-database dependency, so
    it needs no interpreter-skew treatment the way :func:`_venv_isidentifier`
    does)::

        def _is_private(cls_name, name):
            pattern = '_%s__' % (cls_name, )
            pat_len = len(pattern)
            if (
                    len(name) > pat_len
                    and name.startswith(pattern)
                    and name[pat_len:pat_len+1] != ['_']
                    and (name[-1] != '_' or name[-2] != '_')
                ):
                return True
            else:
                return False

    ``name[pat_len:pat_len+1] != ['_']`` compares a STRING slice (length 0
    or 1) to the LIST ``['_']`` — a ``str`` can never equal a ``list``, so
    that clause is always ``True`` and contributes nothing to the real
    condition. The real condition reduces to: longer than the pattern,
    starts with the pattern, and does not itself end in ``'__'`` (which
    would make it a DUNDER, not a private name — dunders are handled by a
    separate branch in the real ``enum.py`` this gate does not need to
    mirror, since R5's ``mro``/underscore-bookend checks already cover the
    dunder-shaped member names this gate cares about). Implemented against
    that reduced, equivalent form here — proven equivalent to the literal
    source above by exhaustive comparison against the real
    ``enum._is_private`` in
    ``tests/test_emit.py::test_is_enum_private_member_name_matches_stdlib``.

    A permissible value whose member label matches this predicate is a
    silent-drop hazard distinct from every other R5 check: ``enum.
    _EnumDict.__setitem__`` does not raise for a private-mangled name — it
    simply never records the assignment as a member at all (probe-verified
    live on both interpreters under ``-W error``: no warning either), so the
    generated module imports fine and the class LOOKS complete, but the
    member is gone — no ingested row carrying this permissible value could
    ever validate against the generated schema. See
    ``tests/test_roundtrip.py::
    test_charset_gate_declines_private_mangled_enum_class_name`` for the
    live end-to-end proof (forced through, the generated source contains
    the assignment textually, but ``Enum.__members__`` omits it at
    runtime).
    """
    pattern = f"_{cls_name}__"
    return len(label) > len(pattern) and label.startswith(pattern) and not label.endswith("__")


def _underscore(txt: str) -> str:
    """Verbatim mirror of ``linkml_runtime.utils.formatutils.underscore()``.

    ``packages/ne-schema/.codegen-venv/lib/python3.12/site-packages/
    linkml_runtime/utils/formatutils.py:29-30``::

        def underscore(txt: str) -> str:
            return ws_pattern.sub("_", txt.strip()).replace(",", "").replace("-", "_")

    where ``ws_pattern = re.compile(r"\\s+")``. Three steps, in this exact
    order: strip, collapse whitespace runs to ``_``, drop commas, turn
    hyphens into ``_``. Read directly from the real source above — not
    derived from symptom examples.
    """
    return re.sub(r"\s+", "_", txt.strip()).replace(",", "").replace("-", "_")


#: Codepoints assigned a Unicode NAME in the PLATFORM interpreter's Unicode
#: database (15.1.0, python 3.13.5 today) but UNASSIGNED — ``unicodedata.
#: name()`` raises ``ValueError: no such name`` — in the pinned
#: ``.codegen-venv``'s database (15.0.0, python 3.12.11 today). Measured
#: directly, not estimated: the full 15.0->15.1 delta is exactly three
#: contiguous-or-singleton ranges, ``0x2FFC-0x2FFF`` (3 new Ideographic
#: Description Sequence characters), ``0x31EF`` (1 new Bopomofo-extension
#: character), and ``0x2EBF0-0x2EE5D`` (623 new CJK Unified Ideographs,
#: Extension I) — 627 codepoints total, probe-verified
#: ``named-on-platform == 627, named-on-venv == 0`` for exactly this set.
#: The real generator's member-name mangle
#: (``OOCodeGenerator.replace_invalid_identifier_character``) calls
#: ``unicodedata.name()`` INSIDE the venv, so any of these codepoints
#: reaching that call crashes generation outright — R4b below declines them
#: before ``_member_label_raw`` ever runs. A separate, SMALLER divergence —
#: ten already-named-in-both CJK ideographs whose ``isnumeric()``
#: classification flipped between the two databases (e.g. U+4E24) — is
#: deliberately NOT included here: every mirror predicate that matters
#: (``_member_label_raw``'s transliteration test, this gate's own checks)
#: consults ``isalpha() OR isnumeric()``, and those ten codepoints are
#: ``isalpha() == True`` in BOTH databases, so the combined predicate never
#: diverges for them (see R4b's own comment and
#: ``tests/test_roundtrip.py::
#: test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew``,
#: which hashes exactly that combined predicate and proves it byte-identical
#: across both interpreters outside this set).
#:
#: Re-derivation instruction: if the ``.codegen-venv`` pin bumps to a newer
#: ``unicodedata`` database, or the platform's own Python minor version
#: changes its bundled database, re-run
#: ``test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew``
#: — a stale set here turns that test RED rather than leaving this comment's
#: claim to silently go wrong.
_UNICODE_DB_SKEW: frozenset[int] = (
    frozenset(range(0x2FFC, 0x3000)) | frozenset({0x31EF}) | frozenset(range(0x2EBF0, 0x2EE5E))
)

#: Codepoints that CPython 3.13 (the PLATFORM interpreter this gate runs on)
#: classifies as ``XID_Continue`` (legal inside, though not necessarily at
#: the start of, an identifier) but CPython 3.12 (the pinned ``.codegen-
#: venv``'s interpreter, where the real generator's
#: ``generate_enum_label``/``str.isidentifier()`` branch decision actually
#: runs) does NOT. Measured directly: ``('_' + chr(cp)).isidentifier()`` is
#: ``True`` on the platform interpreter and ``False`` on the venv interpreter
#: for exactly these four codepoints — U+200C ZERO WIDTH NON-JOINER, U+200D
#: ZERO WIDTH JOINER, U+30FB KATAKANA MIDDLE DOT, U+FF65 HALFWIDTH KATAKANA
#: MIDDLE DOT — and byte-identical on both interpreters for every other
#: codepoint (proven by the same parity-digest test cited above). This
#: matters because the branch these four codepoints affect — "is the
#: ``underscore()``-processed label ALREADY a valid identifier, or does it
#: need per-character transliteration" — is decided INSIDE the venv in the
#: real generator; a mirror that only consults the platform's own
#: ``str.isidentifier()`` would silently take the wrong branch for a label
#: containing one of these four characters (see :func:`_venv_isidentifier`).
#: Re-derivation instruction: same as :data:`_UNICODE_DB_SKEW` above — a
#: venv or platform Python minor-version bump must re-run the parity-digest
#: test before this set can be trusted.
_XID_SKEW: frozenset[int] = frozenset({0x200C, 0x200D, 0x30FB, 0xFF65})


def _venv_isidentifier(label: str) -> bool:
    """Emulate ``str.isidentifier()`` AS THE PINNED ``.codegen-venv``'s
    CPython 3.12 interpreter would evaluate it, not as the platform's
    CPython 3.13 interpreter evaluates it directly.

    The real generator's ``generate_enum_label`` branch — "is the
    ``underscore()``-processed label already a valid Python identifier, or
    does every illegal character need per-character transliteration" — is
    decided by calling ``str.isidentifier()`` INSIDE the venv interpreter.
    Every codepoint's ``isidentifier``-relevant classification is identical
    across the two interpreters EXCEPT the four in :data:`_XID_SKEW`
    (proven exhaustively, not assumed, by
    ``tests/test_roundtrip.py::
    test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew``),
    so subtracting exactly those four from the platform's own
    ``str.isidentifier()`` result is a complete, not approximate, emulation
    of the venv's answer for any post-R4b input (R4b having already removed
    every :data:`_UNICODE_DB_SKEW` codepoint, the other axis of skew, before
    this function is ever called).
    """
    return label.isidentifier() and all(ord(ch) not in _XID_SKEW for ch in label)


def _member_label_raw(value: str) -> str:
    """The real generator's OWN two-step member-name mangle — the literal
    source TOKEN it writes into the generated module — with NO CPython-side
    normalization applied. Derived from the real ``.codegen-venv`` (linkml
    1.11.1), read from source, not transliterated from examples:

    1. ``linkml.generators.oocodegen.OOCodeGenerator.generate_enum_label``
       (``oocodegen.py:184-196``)::

           def generate_enum_label(self, value: str) -> str:
               label = underscore(value)
               if label.isidentifier():
                   return label
               else:
                   label = re.sub(r"(?=^\\d)", "number_", label)
                   safe_label = ""
                   for character in label:
                       safe_label += self.replace_invalid_identifier_character(character)
                   return safe_label

       — run ``_underscore`` first; if the result is already a valid Python
       identifier, stop there. **The ``isidentifier()`` test in that branch
       runs INSIDE the venv interpreter** (CPython 3.12 today) — mirrored
       here by :func:`_venv_isidentifier`, NOT plain ``str.isidentifier()``
       (2026-07-28 Argus P1, fourth review round / differential-fuzz round:
       plain platform ``isidentifier()`` disagrees with the venv on exactly
       the four :data:`_XID_SKEW` codepoints, e.g. U+200D ZERO WIDTH JOINER
       — a platform-only mirror predicts label ``'a\\u200db'`` unchanged,
       where the real venv-side generator takes the transliteration branch
       and produces ``'aZERO_WIDTH_JOINERb'``). Otherwise prefix ``number_``
       if the label starts with a digit (``oocodegen.py``'s own
       ``re.sub(r"(?=^\\d)", ...)`` — a zero-width match at the start, only
       when the first character is a digit), then transliterate every
       character that is still illegal in a Python identifier via
       ``replace_invalid_identifier_character`` (``oocodegen.py:177-182``)::

           if char.isalpha() or char.isnumeric() or char == "_":
               return char
           else:
               return underscore(unicodedata.name(char))

       (the SAME ``unicodedata.name()`` call R4/R4b above already rely on —
       this is why R1-R4b MUST run, and their order must NOT change, before
       any label is computed here: an unnamed-in-the-venv-database
       character reaching this function would raise the same ``ValueError``
       the real generator raises).
    2. ``linkml.generators.pydanticgen.template.EnumValue.alias_python_
       keywords`` (``pydanticgen/template.py:126-131``)::

           @model_validator(mode="after")
           def alias_python_keywords(self) -> Self:
               if iskeyword(self.label):
                   ...
                   self.label = self.label + "_"
               return self

       — if the label so far is a Python keyword (``class``, ``None``,
       ...), the real generator masks it by appending ``_``. This step also
       runs on the label's un-normalized (pre-NFKC) spelling — it has no
       NFKC awareness either — which is why it belongs in THIS function
       (the raw/pre-normalization half), not in :func:`_member_label`.

    This is the literal string the real generator writes as a Python
    assignment target — used by :func:`_enum_decline_reason`'s R5 legality
    check, which must validate the SAME raw token CPython's own tokenizer
    validates (see that function's docstring for why legality and identity
    are checked at different points in this split).

    **PRECONDITION**: only call this on a value that has already passed
    R1-R4b in :func:`_enum_decline_reason` — a character unnamed in the
    venv's Unicode database reaching ``unicodedata.name()`` here raises
    ``ValueError``, mirroring the real generator's own crash on the same
    input. Do not reorder the gate's rules.
    """
    label = _underscore(value)
    if not _venv_isidentifier(label):
        label = re.sub(r"(?=^\d)", "number_", label)
        label = "".join(
            ch
            if (ch.isalpha() or ch.isnumeric() or ch == "_")
            else _underscore(unicodedata.name(ch))
            for ch in label
        )
    if iskeyword(label):
        label += "_"
    return label


def _member_label(value: str) -> str:
    """The identifier IDENTITY CPython assigns to :func:`_member_label_raw`'s
    output — used for the mro/underscore-bookend checks and R6's
    cross-value collision check, never for R5's legality check.

    **Legality vs. identity, and why the split matters (2026-07-28 Argus
    P0, differential-fuzz round — the regression this split fixes).**
    CPython's own identifier handling has TWO separate rules, applied at
    two separate times, and conflating them is exactly the defect this
    function used to have:

    - **Legality** (does the raw source token even parse as an identifier
      at all) is checked by the tokenizer against the raw characters
      actually written into the source file, BEFORE any normalization.
    - **Identity** (are two syntactically-legal identifiers "the same
      name") is what NFKC normalization governs — the Python Language
      Reference, "Identifiers and keywords": "Identifiers are compared by
      their NFKC normal form" — applied AFTER legality has already been
      decided, purely to decide whether two already-legal spellings
      collide in the symbol table.

    NFKC-normalizing a label BEFORE checking legality can make an illegal
    raw token look legal: ``unicodedata.normalize("NFKC", "m²")`` folds the
    SUPERSCRIPT TWO into an ordinary digit, yielding ``"m2"`` — a valid
    identifier — even though the raw token the generator actually writes is
    ``m² = "m²"``, which CPython's tokenizer rejects outright
    (``SyntaxError: invalid character '²' (U+00B2)``). Checking legality on
    the NFKC form was exactly this bug: it made the gate ACCEPT ``'m²'``/
    ``'10 m³'`` (both correctly declined before this bug was introduced),
    silently shipping a draft that hard-breaks real generation. See
    ``tests/test_roundtrip.py``'s ``trailing-superscript``/
    ``trailing-superscript-spaced`` regression pins and
    ``tests/test_differential_fuzz.py`` (mutation M-a reproduces this exact
    diff and turns both RED).

    Identity, by contrast, genuinely needs NFKC: two members whose
    :func:`_member_label_raw` outputs are merely NFKC-equivalent — not
    textually identical — collide anyway once CPython's tokenizer builds
    the module's symbol table, because identifier IDENTITY is what
    NFKC-compares, e.g. ``'µm'`` (U+00B5 MICRO SIGN) and ``'μm'`` (U+03BC
    GREEK SMALL LETTER MU) are textually distinct raw labels (neither needs
    transliteration; both are already legal, distinct-looking identifiers),
    yet both compile to the SAME symbol table entry — reproduced live:
    ``TypeError: 'μm' already defined as 'µm'`` at ``enum.py:403``. NFKC is
    applied LAST here, after :func:`_member_label_raw`'s keyword-masking —
    not before — because keyword-masking itself runs on the generator's own
    unnormalized label (it has no NFKC awareness either); a label that is
    NFKC-equivalent to a keyword but not textually one (the FULLWIDTH LATIN
    spelling of "mro", three characters U+FF4D/U+FF52/U+FF4F) is masked by
    nothing upstream and would otherwise silently bypass R5's own
    ``Enum.mro``/keyword-shaped checks, which is exactly what applying NFKC
    here (for the mro/bookend/R6 IDENTITY checks) closes.

    **Interpreter-version dependency, named not hidden (2026-07-28 Argus
    P3).** :func:`_member_label_raw`'s keyword-masking step calls
    ``keyword.iskeyword`` on the PLATFORM interpreter, while the real
    generator's ``alias_python_keywords`` runs INSIDE the ``.codegen-
    venv``'s interpreter — a different Python minor version today (platform
    3.13, venv 3.12). Correctness of this step depends on ``keyword.kwlist``
    being identical across the two interpreters; that parity is not
    assumed, it is PINNED by ``tests/test_roundtrip.py::
    test_platform_keyword_list_matches_codegen_venv_keyword_list``, which
    subprocesses the real venv interpreter and asserts set-equality. If a
    future ``.codegen-venv`` pin bump ever changes its keyword list, that
    test goes RED — not this docstring's premise silently going stale.
    Likewise, this NFKC step and :func:`_venv_isidentifier`'s XID emulation
    both depend on the platform/venv Unicode-database parity proven by
    ``tests/test_roundtrip.py::
    test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew``.

    Equivalence is pinned by TWO oracles, not prose: the curated
    ``tests/test_roundtrip.py`` oracle (named regression pins for every
    defect found so far — see its module docstring) and the SYSTEMATIC
    ``tests/test_differential_fuzz.py`` oracle, which computes ground truth
    by actually importing the real generator's own
    ``generate_enum_label``/``EnumValue`` inside the venv over a large
    seeded corpus rather than a hand-curated example set. A future
    ``.codegen-venv`` pin bump that changes the real mangle turns both RED —
    drift is caught by TEST, not by this docstring's prose.
    """
    return unicodedata.normalize("NFKC", _member_label_raw(value))


#: The five provenance slots, verbatim from ``operational.yaml:29-48``. This
#: is a deliberate literal transcription (like ``seeds.py``'s own
#: transcription of the seed class signatures) — see
#: ``tests/test_emit.py::test_emitted_yaml_is_house_dialect`` for the parity
#: proof against the actual generated shape.
_PROVENANCE_SLOT_DEFS: dict[str, dict[str, object]] = {
    "id": {
        "description": "Platform-canonical entity id (stable, unique within the platform).",
        "identifier": True,
        "required": True,
        "range": "string",
    },
    "source_system": {
        "description": (
            "Name of the upstream system of record this entity was ingested from "
            '(e.g. "salesforce", "zendesk", "netsuite").'
        ),
        "range": "string",
    },
    "source_record_id": {
        "description": "The entity's native primary key in its source system.",
        "range": "string",
    },
    "created_at": {
        "description": "Timestamp the entity was first created in its source system.",
        "range": "datetime",
    },
    "updated_at": {
        "description": "Timestamp the entity was last modified in its source system.",
        "range": "datetime",
    },
}


class EmittedSchema(BaseModel):
    """The emitted YAML text plus the bookkeeping needed by the report/round-trip."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    yaml_text: str
    top_class: str
    schema_name: str
    emitted_enums: list[str]
    declined_enums: dict[str, str]
    provenance_mapped: dict[str, list[str]]


class _NoAliasSafeDumper(yaml.SafeDumper):
    """A ``SafeDumper`` that NEVER emits anchors/aliases.

    ``yaml.SafeDumper`` already refuses arbitrary Python objects (the "safe"
    in the name), but it still emits ``&anchor``/``*alias`` when the SAME
    Python object is referenced twice in the data structure being dumped —
    structurally impossible with this emitter's construction (every value is
    freshly built), but overridden here belt-and-suspenders so the guarantee
    holds even if a future edit accidentally reuses a list/dict object.
    """

    def ignore_aliases(self, data: object) -> bool:
        return True


def _represent_none(dumper: yaml.SafeDumper, _data: None) -> yaml.Node:
    # Bare-key rendering for enum permissible values (house dialect: `value:`
    # with nothing after the colon, not the default `value: null`).
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


_NoAliasSafeDumper.add_representer(type(None), _represent_none)


def _dump_yaml(data: dict[str, object]) -> str:
    return yaml.dump(
        data,
        Dumper=_NoAliasSafeDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def _validate_engagement(engagement: str) -> None:
    if not _ENGAGEMENT_PATTERN.match(engagement):
        raise InvalidEngagementNameError(engagement)


def _prefix_key(engagement: str) -> str:
    return "ne" + engagement.replace("-", "")


def _validate_class_names(model: CandidateModel) -> None:
    """Raise :class:`InvalidClassNameError` for any mechanically-derived class
    name that is not a valid Python identifier (2026-07-28 Argus P2).

    ``CandidateClass.name`` is produced by ``class_name_for_table`` — a
    mechanical ``CamelCase(stem) + "Record"`` transform with no leading-digit
    guard. A sample stem that is entirely digits (e.g. ``2023.csv``) yields
    a class name starting with a digit (``2023Record``), which the real
    codegen venv's ``PydanticGenerator`` rejects with ``ValueError: ... is
    not a valid Python identifier`` deep inside generation — verified live.
    Checked here so that failure surfaces immediately, before any subprocess,
    naming the offending sample file. The ``RecordSet`` container name needs
    no equivalent check: it is derived from the OPERATOR-supplied
    ``engagement`` slug, and ``_ENGAGEMENT_PATTERN`` already forces a leading
    letter (``^[a-z]...``), so ``to_camel_case(engagement)`` can never start
    with a digit.
    """
    for cls in model.classes:
        if not cls.name.isidentifier():
            raise InvalidClassNameError(cls.name, cls.source_table)


def _validate_no_derived_name_collisions(model: CandidateModel) -> None:
    """Raise for any pair of classes whose mechanically-derived EMIT-TIME
    dictionary keys collide (Codex C1, 2026-07-28).

    ``emit_linkml`` (below) builds two dictionaries keyed by values derived
    from ``CandidateClass`` fields the type system does not itself keep
    unique: ``classes[cls.name]`` (one entry per emitted LinkML class) and
    ``record_set_attrs[to_snake_case(cls.source_table)]`` (one entry per
    class on the top-level record-set container). ``infer_candidate_model``'s
    own ``_check_no_duplicate_class_names`` already refuses a colliding
    ``class_name_for_table`` result for models IT builds — but ``emit_linkml``
    is a public, PURE function of ANY caller-constructed
    :class:`CandidateModel` (every ``tests/test_*.py`` module in this package
    constructs one by hand for at least some tests, entirely bypassing
    ``infer_candidate_model``), and nothing in the Pydantic model couples one
    class's ``name`` to another's, or one class's ``source_table`` to
    another's. Two DISTINCT failure modes follow from that, checked
    independently here because neither implies the other:

    - Two classes sharing the same ``name`` (e.g. two source tables whose
      stems normalize identically under ``class_name_for_table``, such as
      ``foo-bar.csv``/``foo_bar.csv`` both yielding ``FooBarRecord``) make
      the second ``classes[cls.name] = ...`` assignment in
      :func:`emit_linkml` silently overwrite the first — the emitted YAML
      keeps only the second table's class, with no error and no trace in
      ``declined_enums``/``provenance_mapped``.
    - Two classes with already-DISTINCT, non-colliding ``name``s can still
      collide on ``to_snake_case(cls.source_table)`` (nothing forces
      ``source_table`` to be the literal stem ``name`` was derived from),
      making the second ``record_set_attrs[...] = ...`` assignment silently
      overwrite the first — the emitted schema keeps both classes, but the
      record-set container that is the schema's own ``top_class`` can only
      ever hold ONE of the two tables' sampled records, again with no error.

    Checked here, eagerly, before either dictionary is built, so the
    operator sees which two source tables collide instead of a draft that
    silently loses a table. See
    ``tests/test_emit.py::test_duplicate_class_name_rejected_before_emit``
    and ``::test_duplicate_record_set_slot_name_rejected_before_emit`` for
    the bidirectional proof (including the "distinct names, distinct
    snake_case keys" control that must still emit fine).
    """
    seen_class_names: dict[str, str] = {}
    seen_slot_names: dict[str, str] = {}
    for cls in model.classes:
        if cls.name in seen_class_names:
            raise DuplicateClassNameError(cls.name, seen_class_names[cls.name], cls.source_table)
        seen_class_names[cls.name] = cls.source_table

        slot_name = to_snake_case(cls.source_table)
        if slot_name in seen_slot_names:
            raise DuplicateRecordSetSlotNameError(
                slot_name, seen_slot_names[slot_name], cls.source_table
            )
        seen_slot_names[slot_name] = cls.source_table


def _enum_decline_reason(values: list[str], *, enum_name: str) -> str | None:
    """Decide whether an inferred enum's permissible values are safe to emit.

    ``enum_name`` is the mechanically-derived enum CLASS name the real
    generator will write (e.g. ``"WidgetsTagEnum"`` — see
    ``infer.py``'s ``f"{class_name}{to_camel_case(col.name)}Enum"``), needed
    by R5's private-name check below: Python's own name-mangling treats a
    member label of the shape ``_<enum_name>__<x>`` as PRIVATE to that
    SPECIFIC class, so the check is inherently keyed to which class the
    enum will end up in, not just the value's own shape (2026-07-28 Argus
    P1, fourth review round).

    Nine checks (R1-R4b, plus R5's four label checks), evaluated in this
    exact order over the (already sorted) ``values`` list — first violation
    wins, so the result is deterministic regardless of dict/set iteration
    order elsewhere in the pipeline. Each check is tagged EMPIRICAL (a real
    permissible-value shape that DOES break the real ``.codegen-venv``
    generator was probed and observed to fail) or CONSERVATIVE (declined out
    of caution though the probed shape actually generates fine) — see
    ``tests/test_roundtrip.py::test_charset_gate_rejected_values_actually_break_the_real_generator``
    and ``::test_charset_gate_conservative_declines_are_labeled_conservative``
    for the oracle proof of each tag, and
    ``::test_charset_gate_accepted_values_roundtrip_through_real_generator``
    for the acceptance half — 19 shapes the PRE-REVISION charset wrongly
    declined (backtick, ``@``, ``%``, quotes, ``<>``, unicode, emoji,
    digit-leading, Python keywords, ...) that this gate now admits.

    A decline is not free: the slot falls back to ``range: string``, silently
    losing the constraint this whole unit exists to infer. **This gate is
    NOT claimed to be 100% empirical** — the underscore-bookended branch of
    R5 is explicitly conservative, and R4b is explicitly conservative in the
    opposite direction (declines some values that would, individually,
    actually generate fine — see R4b below); every other check is tied to
    an observed real-generator failure.

    **How this gate's equivalence to the real generator is now proven
    (2026-07-28, differential-fuzz round — the round's actual deliverable).**
    Every prior round proved equivalence against a HAND-CURATED oracle: a
    fixed list of accepted values, rejected values, and collision families
    the reviewer happened to think of. That approach has a structural
    ceiling — it cannot catch a defect in a family the curator never
    imagined — and in fact did not: the two real defects fixed this round
    (the R5 raw-vs-normalized legality bug below, and the platform/venv
    Unicode-database skew) both escaped a suite that, at the time, had 243
    green tests and six mutation rows written specifically about this
    function. ``tests/test_differential_fuzz.py`` closes that ceiling: it
    computes GROUND TRUTH by actually invoking the real generator's own
    ``generate_enum_label``/``EnumValue`` mangle machinery INSIDE the
    ``.codegen-venv``, over a large deterministically-seeded corpus
    (thousands of values, not dozens), and asserts this gate's ACCEPT/
    DECLINE verdict and :func:`_member_label` output agree with that ground
    truth in both directions. The curated oracle in ``test_roundtrip.py`` is
    kept — every specific defect found gets a permanent named regression pin
    there — but the fuzz, not the curated list, is now the authority for the
    general equivalence claim. See ``tests/test_differential_fuzz.py``'s
    module docstring for the corpus design and scope boundary.

    R1 EMPIRICAL — empty or whitespace-only value (probed ``''``, ``' '``,
       a tab-only value, each paired with a second, non-empty PV so the
       enum itself stays non-trivial): produces an empty enum MEMBER NAME,
       a ``SyntaxError`` in the generated module.
    R2 EMPIRICAL — a raw newline/carriage-return inside the value (probed
       ``'line1\\nline2'``, ``'a\\rb'``): the character lands inside a
       generated Python string literal and breaks it (``SyntaxError``).
    R3 EMPIRICAL — a backslash anywhere in the value (probed ``'trail\\\\'``):
       a trailing backslash escapes the closing quote of the generated
       string literal (``SyntaxError``); an interior backslash is instead
       reinterpreted as a Python escape sequence (silent value corruption).
       Declined unconditionally rather than trying to distinguish the two
       cases from the value alone.
    R4 EMPIRICAL — an unnamed-on-the-PLATFORM control character (probed
       NUL/BEL/ESC/DEL: ``'a\\x00b'``, ``'a\\x07b'``, an ESC-prefixed value,
       ``'a\\x7fb'``): the generator's member-name mangle falls back to
       ``unicodedata.name(char)`` for any character illegal in a Python
       identifier, and that call raises ``ValueError`` when the character
       has no Unicode name. (Whitespace-classified control characters like
       vertical-tab/form-feed/NEL/NBSP are excluded from this check — probed
       and confirmed to generate fine, since they fall through to R5's
       whitespace-collapse instead.)
    R4b EMPIRICAL, and the P1 fix (2026-07-28, differential-fuzz round) —
       the value contains a codepoint in :data:`_UNICODE_DB_SKEW`: assigned
       a name in the PLATFORM's Unicode database (this gate's own R4 check
       just passed it) but UNASSIGNED in the pinned ``.codegen-venv``'s
       OLDER Unicode database — so R4's own ``unicodedata.name()`` fallback
       raises ``ValueError`` when the REAL generator (running inside the
       venv) reaches it, a failure R4 itself cannot see because R4 queries
       the platform's database, not the venv's. Probed live:
       ``'grade\\U0002EBF0'`` — a codepoint the platform's Unicode 15.1
       assigns a name (so R4 lets it through) but the venv's Unicode 15.0
       has never heard of — reaches the real generator's mangle and raises
       ``ValueError: no such name`` at ``oocodegen.py:182``. This check is
       CONSERVATIVE in ONE narrow, declared sense: ten already-named-in-both
       CJK ideographs (e.g. U+4E24) whose ``isnumeric()`` classification
       merely FLIPPED between the two databases are deliberately NOT
       declined here, because every predicate this gate/mirror actually
       consults tests ``isalpha() OR isnumeric()`` and those ten codepoints
       satisfy ``isalpha()`` in BOTH databases — the flip is invisible to
       every check that matters. See :data:`_UNICODE_DB_SKEW`'s own comment
       for the full derivation and
       ``tests/test_roundtrip.py::
       test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew``
       for the proof this set (plus :data:`_XID_SKEW`) is EXHAUSTIVE — every
       other codepoint's mirror-relevant properties are byte-identical
       across the platform and venv interpreters.
    R5 — the RAW label is computed by :func:`_member_label_raw` (the real
       generator's own two-step mangle: ``underscore()``'s strip/whitespace-
       collapse/comma-drop/hyphen-collapse, the ``number_`` digit-prefix +
       per-char ``unicodedata.name()`` transliteration fallback — branch
       decision made by :func:`_venv_isidentifier`, emulating the VENV
       interpreter, not the platform's own ``isidentifier()`` — and the
       ``iskeyword``-masking step; see that function's docstring), then
       checked in this order:
         - EMPIRICAL: the RAW label is not a valid Python identifier
           (``not raw.isidentifier()`` — checked on the RAW, PRE-NFKC label,
           2026-07-28 differential-fuzz round, the P0 fix). **This is
           deliberately NOT checked on :func:`_member_label`'s NFKC-
           normalized output** — CPython's tokenizer validates identifier
           LEGALITY against the raw source characters it actually reads,
           and applies NFKC only afterwards, purely to decide identifier
           IDENTITY (see :func:`_member_label`'s docstring for the full
           legality-vs-identity argument). Checking the normalized form
           here was exactly the regression this round fixes: NFKC folds the
           SUPERSCRIPT TWO in ``'m²'`` into an ordinary digit
           (``normalize("NFKC", "m²") == "m2"``, a legal identifier), so a
           normalized-form check wrongly ADMITTED ``'m²'``/``'10 m³'`` even
           though the raw token the generator actually writes
           (``m² = "m²"``) makes CPython's real tokenizer raise
           ``SyntaxError: invalid character '²' (U+00B2)``. Checking the
           RAW label closes this: two distinct real failure modes collapse
           into one check — an EMPTY raw label (probed ``',,'``, a value
           that mangles entirely away via comma-elision) produces an
           assignment with an empty left-hand side (``SyntaxError``); a
           NON-EMPTY but illegal raw label (probed ``'½ inch'`` ->
           ``'½_inch'``, and the trailing-position ``'m²'``/``'10 m³'`` —
           ``'½'``/``'²'``/``'³'`` all satisfy ``str.isnumeric()`` so the
           mirror's char-by-char transliteration step, which mirrors
           ``replace_invalid_identifier_character``'s own ``isalpha() or
           isnumeric()`` test, leaves them untouched even though none is
           legal in a Python identifier character) produces the same class
           of ``SyntaxError`` deep in the generated module. All probed live
           against the real ``.codegen-venv`` — see
           ``test_charset_gate_rejected_values_actually_break_the_real_generator``
           (``empty-label-after-mangle``, ``identifier-illegal-char``,
           ``trailing-superscript``, ``trailing-superscript-spaced``).
         - EMPIRICAL: :func:`_member_label`'s NFKC-normalized IDENTITY
           equals ``"mro"`` (probed ``'mro'``, ``'mro '``, ``'mro,'`` —
           comma-elision alone can produce this label too — and the
           FULLWIDTH LATIN spelling of "mro", three ASCII-lookalike
           characters that NFKC-normalize to ``'mro'``) — collides with
           ``Enum.mro``, a real attribute every Python ``Enum`` class
           carries; the generator fails to define the member. This check
           uses the NORMALIZED label (identity), not the raw one — the
           fullwidth spelling's RAW label is a perfectly legal, distinct
           identifier; it is only IDENTICAL to ``mro`` once NFKC is applied,
           which is exactly the collision CPython's own symbol table would
           see.
         - EMPIRICAL, and an accept-side silent drop this round's Argus
           review found (2026-07-28 P1) — :func:`_member_label`'s NORMALIZED
           label is PRIVATE to ``enum_name`` under Python's own name-
           mangling rule (:func:`_is_enum_private_member_name`, a
           verbatim-derived mirror of ``enum._is_private``): probed live,
           ``_EnumDict.__setitem__`` silently refuses to record such a
           label as a member at all — no exception on either interpreter —
           so the generated module imports fine and LOOKS complete while
           the member is simply gone. This class is genuinely fatal (not
           merely conservative) but escaped every prior round's curated
           oracle AND the differential fuzz (the fuzz proves label
           computation only, never the enum-class-construction machinery a
           class-relative predicate like this depends on — see
           ``tests/test_differential_fuzz.py``'s own scope-boundary
           paragraph) because it requires a value textually equal to the
           derived enum class name, adversarial in practice but not
           impossible. See ``tests/test_roundtrip.py::
           test_charset_gate_declines_private_mangled_enum_class_name``.
         - CONSERVATIVE: :func:`_member_label`'s NORMALIZED label is
           underscore-bookended (``len(label) >= 2`` and starts and ends
           with ``_``, e.g. ``'_name_'``, ``'__init__'``, or ``'-name-'``
           once hyphens mangle to underscores). ``_name_``/``_value_``/
           ``_order_``/``__init__`` are probe-proven FATAL (they collide
           with ``enum.Enum`` dunder/sunder machinery); ``_missing_``/
           ``_ignore_``/``__str__`` are probe-proven BENIGN (they generate
           fine) — this rule declines the whole SHAPE anyway rather than
           enumerate the fatal/benign split member-by-member. See
           ``test_charset_gate_conservative_declines_are_labeled_conservative``
           for the living proof this is over-cautious, not generator-derived.
           This is the ONLY remaining CONSERVATIVE-over-declining branch in
           the gate (R4b above is also conservative, but in the sense of
           declining safely rather than risking an accept-side wrongness).
    R6 EMPIRICAL, cross-value — two DIFFERENT permissible values that
       collapse to the SAME :func:`_member_label` NORMALIZED identity
       (probed across nine collision families spanning every mangle step —
       hyphen-vs-space, hyphen-vs-underscore, comma-elision, digit-prefix, a
       three-way family, keyword-suffix, two CPython-NFKC families
       (nfkc-micro-sign, nfkc-fullwidth-letter, e.g. ``'µm'``/``'μm'`` — an
       ordinary uncleaned-units-column pair that neither needs
       transliteration nor collides under the raw mangle alone, but IS the
       same identifier once CPython's own NFKC normalization is applied),
       and xid-skew-transliteration (2026-07-28 differential-fuzz round —
       ``'a・b'`` KATAKANA MIDDLE DOT and its transliterated form both
       reachable via the venv-isidentifier emulation)): the generator
       either silently DROPS one of the two members (most families) or
       fails loud with a Python-level ``TypeError`` at enum-class
       construction (keyword-suffix, nfkc-micro-sign, nfkc-fullwidth-letter
       — every one whose collision is invisible to the generator's OWN
       label-mangle dedup and so leaves two textually-distinct assignment
       lines in the generated source; e.g. keyword-suffix's ``'class'`` +
       ``'class_'`` — the SAME masked label; observed live:
       ``EnumDict.__setitem__`` (``enum.py:403``) raises ``TypeError:
       'class_' already defined as 'class'``). A pass/fail round-trip can
       never catch the silent-drop shape; only a member-count/content
       assertion against the generated module can (see
       ``test_real_mangle_collision_families_decline_and_prove_member_loss``
       and ``test_colliding_member_labels_silently_drop_a_member_when_forced``).
    """
    for value in values:
        stripped = value.strip()
        if stripped == "":
            return (
                f"permissible value {value!r} is empty or whitespace-only, which would "
                "produce an empty enum member name (SyntaxError) in the generated module"
            )
        if "\n" in value or "\r" in value:
            return (
                f"permissible value {value!r} contains a raw newline/carriage return, "
                "which lands inside a generated string literal (SyntaxError)"
            )
        if "\\" in value:
            return (
                f"permissible value {value!r} contains a backslash: a trailing backslash "
                "escapes the closing quote of the generated string literal (SyntaxError), "
                "an interior backslash is reinterpreted as a Python escape sequence "
                "(silent value corruption)"
            )
        if any(not ch.isspace() and unicodedata.name(ch, None) is None for ch in value):
            return (
                f"permissible value {value!r} contains an unnamed control character — the "
                "generator's unicodedata.name() member-name mangle raises ValueError"
            )
        skewed = next((ch for ch in value if ord(ch) in _UNICODE_DB_SKEW), None)
        if skewed is not None:
            return (
                f"permissible value {value!r} contains U+{ord(skewed):04X}, assigned a "
                "name in the platform's Unicode database but UNASSIGNED in the pinned "
                ".codegen-venv's (older) Unicode database — the real generator's "
                "unicodedata.name() member-name mangle raises ValueError ('no such "
                "name') for this character even though this gate's own platform-side "
                "check just passed it (EMPIRICAL, R4b)"
            )
        raw = _member_label_raw(value)
        if not raw.isidentifier():
            return (
                f"permissible value {value!r} mangles to raw member label {raw!r}, which "
                "is not a valid Python identifier — an empty label produces an "
                "assignment with an empty left-hand side (SyntaxError); a non-empty-"
                "but-illegal label (e.g. a trailing character such as '½'/'²'/'³' that "
                "satisfies str.isnumeric() but is not valid in a Python identifier) "
                "breaks the generated module the same way (SyntaxError), both "
                "probe-verified against the real .codegen-venv"
            )
        label = _member_label(value)
        if label == _FATAL_MEMBER_LABEL:
            return (
                f"permissible value {value!r} mangles to the member name {label!r}, which "
                "collides with Python's Enum.mro"
            )
        if _is_enum_private_member_name(enum_name, label):
            return (
                f"permissible value {value!r} mangles to the member name {label!r}, which "
                f"Python's own name-mangling treats as PRIVATE to the generated "
                f"{enum_name!r} class body ('_' + class name + '__' prefix, not ending in "
                "'__') — enum._EnumDict.__setitem__ silently refuses to record it as a "
                "member at all (no exception, probe-verified on both interpreters), so the "
                "generated module imports fine but the member is gone: no ingested row "
                "carrying this value could ever validate"
            )
        if len(label) >= 2 and label.startswith("_") and label.endswith("_"):
            return (
                f"permissible value {value!r} mangles to the underscore-bookended member "
                f"name {label!r} — declined CONSERVATIVELY as a shape, not member-by-member "
                "(_name_/_value_/_order_/__init__ are probe-proven fatal; _missing_/_ignore_/"
                "__str__ are probe-proven benign but declined anyway)"
            )

    labels: dict[str, str] = {}
    for value in values:
        label = _member_label(value)
        if label in labels:
            other = labels[label]
            return (
                f"permissible values {other!r} and {value!r} both mangle to the shared "
                f"member name {label!r} — depending on the collision shape, the generator "
                "either silently drops one member or fails during generation (both modes "
                "probe-proven; see the R6 collision-family oracle)"
            )
        labels[label] = value
    return None


def _slot_attribute(
    slot: CandidateSlot,
    *,
    class_names: frozenset[str],
    enum_pvs: dict[str, list[str]],
    declined_enums: dict[str, str],
    emitted_enum_names: set[str],
) -> dict[str, object]:
    if slot.identifier:
        attr: dict[str, object] = {
            "description": "source natural key (unique in sample)",
            "required": True,
            "range": slot.range,
        }
        if slot.pattern:
            attr["pattern"] = slot.pattern
        return attr

    if slot.range in class_names:
        attr = {"range": slot.range, "inlined": False}
        if slot.required:
            attr["required"] = True
        return attr

    if slot.enum_name is not None:
        reason = _enum_decline_reason(enum_pvs[slot.enum_name], enum_name=slot.enum_name)
        if reason is not None:
            declined_enums[slot.enum_name] = reason
            attr = {"range": "string"}
        else:
            emitted_enum_names.add(slot.enum_name)
            attr = {"range": slot.enum_name}
        if slot.required:
            attr["required"] = True
        return attr

    attr = {"range": slot.range}
    if slot.pattern:
        attr["pattern"] = slot.pattern
    if slot.required:
        attr["required"] = True
    return attr


def _emit_class(
    cls: CandidateClass,
    *,
    class_names: frozenset[str],
    enum_pvs: dict[str, list[str]],
    declined_enums: dict[str, str],
    emitted_enum_names: set[str],
    provenance_mapped: dict[str, list[str]],
) -> dict[str, object]:
    attributes: dict[str, object] = {}
    omitted: list[str] = []
    for slot in cls.slots:
        if slot.mechanical_name in PROVENANCE_SLOTS:
            omitted.append(slot.mechanical_name)
            continue
        attributes[slot.mechanical_name] = _slot_attribute(
            slot,
            class_names=class_names,
            enum_pvs=enum_pvs,
            declined_enums=declined_enums,
            emitted_enum_names=emitted_enum_names,
        )
    if omitted:
        provenance_mapped[cls.name] = sorted(omitted)

    class_dict: dict[str, object] = {
        "is_a": "OperationalEntity",
        "description": (
            f"Draft class inferred from sample table `{cls.source_table}` "
            "(ne-schema-bootstrap, unreviewed)."
        ),
    }
    if attributes:
        class_dict["attributes"] = attributes
    return class_dict


def emit_linkml(
    model: CandidateModel, *, engagement: str, config: BootstrapConfig = DEFAULT_CONFIG
) -> EmittedSchema:
    """Emit a house-dialect LinkML draft for ``model`` under ``engagement``.

    Pure function of ``(model, engagement)`` — see the module docstring for
    the two decided deviations from the design doc's prose (the
    ``OperationalEntity`` base name, residual #1's authoritative
    ``permissible_values`` source).

    **``config`` is currently a NO-OP (2026-07-28 Argus P3).** It is
    accepted, not ``**kwargs``-swallowed, purely for signature parity with
    the rest of this package's public functions (``profile_samples``,
    ``infer_candidate_model``) — every threshold that could plausibly govern
    emission (the enum-value gate) is instead calibrated directly against the
    real codegen venv (see ``_enum_decline_reason``'s docstring and
    ``tests/test_roundtrip.py``'s ``test_charset_gate_*`` tests), not against
    a configurable threshold. A caller that constructs a non-default
    ``BootstrapConfig`` and threads it through ``profile_samples`` ->
    ``infer_candidate_model`` -> ``emit_linkml`` will get byte-identical
    ``emit_linkml`` output regardless of what ``config`` says — do not assume
    passing a tighter/looser config changes what gets emitted.

    Raises :class:`InvalidClassNameError` (2026-07-28 P2) if any class's
    mechanically-derived name is not a valid Python identifier (e.g. a
    digit-leading sample-file stem) — before doing any other work, so the
    operator sees the offending file name immediately rather than a late,
    cryptic failure from the real generator. Raises
    :class:`DuplicateClassNameError` or :class:`DuplicateRecordSetSlotNameError`
    (Codex C1, 2026-07-28) if any two classes collide on their emitted class
    name or their record-set container attribute name respectively — see
    :func:`_validate_no_derived_name_collisions`'s docstring; either
    collision would otherwise silently overwrite one class/table out of the
    emitted schema with no error.
    """
    _validate_engagement(engagement)
    _validate_class_names(model)
    _validate_no_derived_name_collisions(model)
    # `config` is accepted (not `**kwargs`-swallowed) for signature parity with
    # the rest of the package's public functions — every threshold that could
    # plausibly govern emission (the enum charset gate) is calibrated against
    # the real generator instead (see the module docstring), so `config` is
    # currently unused here; kept in the signature so a future threshold-gated
    # emission rule has an obvious, already-wired home.
    _ = config

    prefix_key = _prefix_key(engagement)
    class_names = frozenset(c.name for c in model.classes)
    enum_pvs = {e.name: e.permissible_values for e in model.enums}
    declined_enums: dict[str, str] = {}
    emitted_enum_names: set[str] = set()
    provenance_mapped: dict[str, list[str]] = {}

    classes: dict[str, object] = {
        "OperationalEntity": {
            "abstract": True,
            "description": (
                "Abstract base for every draft operational entity. Carries the "
                "platform-canonical identity and source-provenance slots shared "
                "by all records (ne-schema-bootstrap draft, mirrors "
                "operational.yaml's OperationalEntity)."
            ),
            "slots": list(_PROVENANCE_SLOT_DEFS),
        }
    }
    for cls in model.classes:
        classes[cls.name] = _emit_class(
            cls,
            class_names=class_names,
            enum_pvs=enum_pvs,
            declined_enums=declined_enums,
            emitted_enum_names=emitted_enum_names,
            provenance_mapped=provenance_mapped,
        )

    record_set_name = f"{to_camel_case(engagement)}RecordSet"
    record_set_attrs: dict[str, object] = {}
    for cls in model.classes:
        record_set_attrs[to_snake_case(cls.source_table)] = {
            "range": cls.name,
            "multivalued": True,
            "inlined_as_list": True,
        }
    classes[record_set_name] = {
        "tree_root": True,
        "description": (
            f"Top-level container binding a set of draft {engagement} entities "
            "together (ne-schema-bootstrap draft record set)."
        ),
        "attributes": record_set_attrs,
    }

    enums: dict[str, object] = {}
    emitted_enums: list[str] = []
    for enum in model.enums:
        if enum.name not in emitted_enum_names:
            continue
        emitted_enums.append(enum.name)
        enums[enum.name] = {"permissible_values": {v: None for v in enum.permissible_values}}

    data: dict[str, object] = {
        "id": f"https://neutron.enterprise/schema/{engagement}",
        "name": f"neutron-{engagement}",
        "title": f"Neutron Enterprise {engagement} engagement schema (draft)",
        "description": (
            f"Machine-generated draft schema inferred from a bounded sample of "
            f"{engagement}'s data (ne-schema-bootstrap). Deterministic structure; "
            "NOT reviewed — review before landing."
        ),
        "version": "0.1.0-draft",
        "license": "Apache-2.0",
        "prefixes": {
            prefix_key: f"https://neutron.enterprise/schema/{engagement}/",
            "linkml": "https://w3id.org/linkml/",
        },
        "default_prefix": prefix_key,
        "default_range": "string",
        "imports": ["linkml:types"],
        "slots": _PROVENANCE_SLOT_DEFS,
        "classes": classes,
    }
    if enums:
        data["enums"] = enums

    yaml_text = _dump_yaml(data)

    return EmittedSchema(
        yaml_text=yaml_text,
        top_class=record_set_name,
        schema_name=f"neutron-{engagement}",
        emitted_enums=emitted_enums,
        declined_enums=declined_enums,
        provenance_mapped=provenance_mapped,
    )


__all__ = ["EmittedSchema", "emit_linkml"]
