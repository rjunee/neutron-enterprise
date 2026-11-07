# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The differential fuzz — this round's actual deliverable, and the
replacement for a hand-curated equivalence oracle.

**Why this file exists (2026-07-28, differential-fuzz round).** Every prior
round of ``_enum_decline_reason``/``_member_label`` work proved equivalence
against a HAND-CURATED oracle in ``test_roundtrip.py``: a fixed list of
accepted values, rejected values, and collision families the reviewer
happened to think of. That has a structural ceiling — it can only ever catch
a defect in a family the curator imagined — and in practice did not catch
either of the two real defects fixed this round: the R5 raw-vs-normalized
legality regression (``'m²'``/``'10 m³'`` wrongly ADMITTED) and the
platform/venv Unicode-database skew (``'grade\\U0002EBF0'`` wrongly
ADMITTED). Both escaped a suite that, at the point they were introduced, had
243 green tests and six mutation rows written specifically about this
function.

The fix here is not a bigger curated list — it is a different KIND of
oracle. ``test_differential_fuzz_gate_matches_venv_ground_truth`` computes
GROUND TRUTH by actually invoking the real generator's own
``OOCodeGenerator.generate_enum_label``/``EnumValue`` mangle machinery
INSIDE the pinned ``.codegen-venv`` (the same interpreter/library version
the real generator runs under), over a large deterministically-seeded
corpus, and asserts this package's gate/mirror agrees with that ground
truth. This is the SAME lesson NE-VG-0 hit the same night from the other
direction (see ``docs/design/ne-vacuous-guard-*`` and the README's
"Curated vs. systematic coverage" section): curated coverage does not
converge on correctness, systematic coverage does.

**Scope boundary, stated honestly, not left implicit.** This fuzz proves
three things at the LABEL level, against real ground truth: (1) label
COMPUTATION — ``_member_label`` matches the real generator's bound
identifier name for every value the gate accepts; (2) BINDING LEGALITY —
every gate-accepted value's real label actually binds as a Python
identifier (does not crash the venv's own mangle, does not fail to parse);
(3) COLLISION IDENTITY — R6 agrees with the real generator's bound-name
identity in both directions (values that share a real bound name are
declined, values that do not are not over-declined). It does NOT replace
the curated oracle's other job: proving the FULL generated module actually
compiles/imports/instantiates end to end (the enum-MACHINERY branches —
``Enum.mro``, sunder/dunder collisions, the silent-drop vs. loud-TypeError
distinction) and proving R1-R3's claims about the raw VALUE (not the label)
breaking the generated string literal. Those remain the curated oracle
tests' job in ``test_roundtrip.py``/``test_emit.py`` — this fuzz
SUPPLEMENTS them with systematic coverage of the mangle itself, it does not
replace their documentation value for the specific defects already found.
"""

from __future__ import annotations

import itertools
import json
import keyword
import random
import string
import subprocess
from pathlib import Path

from ne.envbuild import venv_python
from ne.schema import codegen as ne_codegen
from ne.schema_bootstrap import emit as emit_mod
from ne.schema_bootstrap.roundtrip import _CODEGEN_VENV_DIR

# tests/ -> ne-schema-bootstrap -> packages -> <repo root>, same derivation
# as test_roundtrip.py's own `_REPO_ROOT`.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Frozen, deliberate — this is what "reproducible" means for a fuzz. Every
#: assertion failure below reports this seed so a reported failure can be
#: reproduced byte-for-byte by re-running this test.
_FUZZ_SEED = 20260728

#: `_enum_decline_reason` (2026-07-28 Argus P1, fourth review round) is
#: keyed to the enum CLASS name for its private-name check
#: (`_is_enum_private_member_name`) — a value's own shape alone no longer
#: determines the verdict. This fuzz proves label computation/binding/
#: collision identity, which is independent of which class the enum ends up
#: in, so a single representative name (matching the curated oracle's own
#: `WidgetsTagEnum` fixture in `test_roundtrip.py`/`test_emit.py`, for
#: continuity, not because it is special) is enough here. The private-name
#: predicate's OWN boundary — declining `_WidgetsTagEnum__x`-shaped labels —
#: stays the curated oracle's job (this corpus does not draw values shaped
#: to trigger it at scale); see
#: `tests/test_roundtrip.py::test_charset_gate_declines_private_mangled_enum_class_name`.
_FUZZ_ENUM_NAME = "WidgetsTagEnum"


# ---------------------------------------------------------------------------
# The ground-truth script — runs INSIDE the real .codegen-venv, importing
# the REAL generator's own mangle machinery. This is deliberately NOT a
# reimplementation of anything: `generate_enum_label` and `EnumValue` are
# imported directly from linkml, never hand-rolled.
# ---------------------------------------------------------------------------

_GROUND_TRUTH_SCRIPT = r"""
import ast
import json
import sys

from linkml.generators.oocodegen import OOCodeGenerator
from linkml.generators.pydanticgen.template import EnumValue


class _Duck:
    # `generate_enum_label` is a plain instance method on OOCodeGenerator;
    # assigning it as a class attribute here makes it a bound method on
    # `_Duck` instances the normal Python way. Its own body calls
    # `self.replace_invalid_identifier_character`, which IS a staticmethod
    # on the real class (verified: `type(OOCodeGenerator.__dict__[
    # "replace_invalid_identifier_character"]) is staticmethod`) -- so it
    # must be wrapped in `staticmethod(...)` here too, or `self.<name>(...)`
    # would bind `self` as its first positional argument and TypeError.
    generate_enum_label = OOCodeGenerator.generate_enum_label
    replace_invalid_identifier_character = staticmethod(
        OOCodeGenerator.replace_invalid_identifier_character
    )


def _ground_truth(value):
    duck = _Duck()
    try:
        label = duck.generate_enum_label(value)
        # The keyword-masking step lives on EnumValue's pydantic validator,
        # not in generate_enum_label -- this is the REAL masking path, not
        # a hand-rolled `keyword.iskeyword` check.
        label = EnumValue(label=label, value=value).label
    except Exception as exc:  # noqa: BLE001 -- classify every crash, never crash itself
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        tree = ast.parse(f"{label} = 1")
    except SyntaxError as exc:
        return {"label": label, "parse_error": str(exc)}
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    if (
        len(assigns) != 1
        or len(assigns[0].targets) != 1
        or not isinstance(assigns[0].targets[0], ast.Name)
    ):
        return {"label": label, "parse_error": f"unexpected parse shape: {ast.dump(tree)}"}
    return {"label": label, "bound": assigns[0].targets[0].id}


def main():
    corpus = json.loads(sys.stdin.read())
    json.dump([_ground_truth(v) for v in corpus], sys.stdout)


if __name__ == "__main__":
    main()
"""


# ---------------------------------------------------------------------------
# The corpus builder. Deliberately seeded so the corpus is reproducible --
# not time- or os-dependent. Weighted toward the shapes that actually
# exercise this gate's interesting space (random ASCII would mostly land in
# the boring "trivially accepted" bucket); see the module docstring's
# scope-boundary paragraph and the individual pool comments below for why
# each family is here.
# ---------------------------------------------------------------------------

_ASCII_WORDS = [
    "active",
    "pending",
    "closed",
    "draft",
    "class",
    "None",
    "True",
    "False",
    "widget",
    "status",
    "order",
    "customer",
    "value",
    "type",
    "state",
    "mro",
    "in progress",
    "n/a",
    "unknown",
    "other",
    "misc",
    "test",
    "sample",
]

#: NFKC-folding characters: superscripts/fractions (`²³¹½¼¾`), a micro sign
#: (`µ`), an angstrom sign (`Å`), a ligature (`ﬁ`), a squared-unit symbol
#: (`㎡`), a numero sign (`№`), a Roman numeral (`Ⅳ`) -- each NFKC-folds to
#: something else, exactly the class of character the P0 regression
#: (checking legality on the FOLDED form) got wrong. `noqa: RUF001` on every
#: use below -- these are deliberately unusual/ambiguous-looking characters,
#: not typos.
_NFKC_FOLDING_CHARS = "²³¹½¼¾µÅﬁ㎡№Ⅳ"
_FULLWIDTH_UPPER = "".join(chr(0xFF21 + i) for i in range(26))
_FULLWIDTH_LOWER = "".join(chr(0xFF41 + i) for i in range(26))
_FULLWIDTH_DIGITS = "".join(chr(0xFF10 + i) for i in range(10))
_FULLWIDTH_MRO = "ｍｒｏ"  # fullwidth "mro"  # noqa: RUF001
_FOLD_POOL = (
    _NFKC_FOLDING_CHARS + _FULLWIDTH_UPPER + _FULLWIDTH_LOWER + _FULLWIDTH_DIGITS + _FULLWIDTH_MRO
)

_COMBINING_MARKS = "̧̀̈"  # combining grave / diaeresis / cedilla

_WHITESPACE_VARIANTS = [
    " ",
    "\t",
    "\xa0",  # NBSP
    "\x85",  # NEL
    "\u2007",  # FIGURE SPACE
    "\u202f",  # NARROW NO-BREAK SPACE
    "\x0b",  # vertical tab
    "\x0c",  # form feed
    "\n",
    "\r",
]

#: The ten CJK ideographs whose `isnumeric()` classification flipped between
#: Unicode 15.0 (venv) and 15.1 (platform) -- R4b deliberately does NOT
#: decline these (see emit.py's R4b comment); included here so the fuzz
#: proves that deliberate non-decline is genuinely safe, not merely assumed.
_CJK_NUMERIC_CHANGED = "两京俩倆拐洞皕秭鉤钩"

#: Deliberately HARDCODED, not `sorted(emit_mod._UNICODE_DB_SKEW)` (2026-07-28
#: Argus P3, fourth review round). Drawing the corpus pool FROM the constant
#: under test made the fuzz structurally incapable of ever discovering that
#: the constant is wrong or empty -- e.g. mutation M-d (emptying
#: `_UNICODE_DB_SKEW`) previously crashed the CORPUS BUILDER itself
#: (`rng.choice([])` on an empty pool) rather than tripping a gate
#: assertion, which is a weaker RED than it looked. These are the SAME three
#: ranges `_UNICODE_DB_SKEW`'s own derivation comment documents (Unicode
#: 15.0->15.1 delta: 0x2FFC-0x2FFF, 0x31EF, 0x2EBF0-0x2EE5D) -- kept here as
#: an INDEPENDENT literal transcription, not an alias, so a bug in the
#: constant (missing/wrong codepoints) still reaches real corpus values and
#: therefore the gate/ground-truth comparison below, not just the parity
#: digest test.
_DB_SKEW_CPS: list[int] = [*range(0x2FFC, 0x3000), 0x31EF, *range(0x2EBF0, 0x2EE5E)]
#: Same independence rationale, for the four measured XID-skew codepoints
#: (U+200C, U+200D, U+30FB, U+FF65) -- not derived from `emit_mod._XID_SKEW`.
_XID_SKEW_CHARS: list[str] = [chr(cp) for cp in (0x200C, 0x200D, 0x30FB, 0xFF65)]

_ASCII_PUNCT = "!@#$%^&*()_+-=[]{}|;:'\",.<>/?`~"


def _build_corpus(seed: int) -> list[str]:
    """Build a deterministic, seed-reproducible corpus targeting >= 4000
    values, weighted toward the shapes this gate's checks actually care
    about (see the pool comments above and inline below) plus a deliberate
    collision-mutant tail that guarantees the fuzz reaches the collision
    space, not just the accept/decline space.

    Never draws a codepoint in the UTF-16 surrogate range (0xD800-0xDFFF,
    not independently representable as a Python `str` character, and not
    JSON-transportable) -- every pool below draws from ranges that exclude
    it by construction.
    """
    rng = random.Random(seed)
    corpus: list[str] = []

    def add(v: str) -> None:
        corpus.append(v)

    # ASCII words, punctuation, digit suffixes, and short random mixes --
    # the ordinary, boring bulk of real customer data.
    for _ in range(600):
        word = rng.choice(_ASCII_WORDS)
        variant = rng.choice(["plain", "punct", "digit", "mixed"])
        if variant == "plain":
            add(word)
        elif variant == "punct":
            add(word + rng.choice(_ASCII_PUNCT))
        elif variant == "digit":
            add(f"{word}{rng.randint(0, 9999)}")
        else:
            pool = word + _ASCII_PUNCT + string.digits
            add("".join(rng.choice(pool) for _ in range(rng.randint(1, 12))))

    # Whitespace variants -- NBSP/NEL/figure-space/tabs/newlines interposed
    # between two words, the family R1/R2/underscore()'s whitespace-collapse
    # step exist for.
    for _ in range(200):
        ws = rng.choice(_WHITESPACE_VARIANTS)
        add(f"{rng.choice(_ASCII_WORDS)}{ws}{rng.choice(_ASCII_WORDS)}")

    # Hyphen/comma/underscore-heavy strings -- the collision-family shapes
    # (hyphen-vs-space, hyphen-vs-underscore, comma-elision) most directly.
    for _ in range(200):
        parts = [rng.choice(_ASCII_WORDS) for _ in range(rng.randint(1, 4))]
        sep = rng.choice(["-", ",", "_", ", ", " - "])
        add(sep.join(parts))

    # NFKC-folding characters, leading/trailing/standalone -- the P0's own
    # failure class (trailing-position superscripts/fractions) plus the
    # fullwidth-Latin/fullwidth-mro identity-collision class.
    for _ in range(300):
        word = rng.choice(_ASCII_WORDS)
        position = rng.choice(["prefix", "suffix", "only"])
        if position == "prefix":
            add(rng.choice(_FOLD_POOL) + word)
        elif position == "suffix":
            add(word + rng.choice(_FOLD_POOL))
        else:
            add("".join(rng.choice(_FOLD_POOL) for _ in range(rng.randint(1, 6))))

    # Combining marks stacked onto ordinary letters.
    for _ in range(100):
        base = rng.choice("aeiouAEIOU")
        mark = rng.choice(_COMBINING_MARKS)
        add(f"{rng.choice(_ASCII_WORDS)}{base}{mark}")

    # CJK -- random draws from the CJK Unified Ideographs block, plus the
    # ten deliberately-unchanged-decline isnumeric-skew ideographs standalone
    # and word-adjacent.
    for _ in range(300):
        add("".join(chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(rng.randint(1, 6))))
    for ch in _CJK_NUMERIC_CHANGED:
        add(ch)
        add(f"{ch}x")
        add(f"x{ch}")

    # Astral-plane: emoji block and CJK Extension B, standalone and
    # multi-codepoint sequences.
    for _ in range(150):
        add("".join(chr(rng.randint(0x1F300, 0x1F6FF)) for _ in range(rng.randint(1, 3))))
    for _ in range(150):
        add("".join(chr(rng.randint(0x20000, 0x2A6DF)) for _ in range(rng.randint(1, 3))))

    # Unicode-database-skew codepoints (assigned on the platform, unassigned
    # in the venv) -- the corpus's dedicated draw into the venv-crash space
    # R4b/the ground-truth error class exists to prove.
    for _ in range(200):
        ch = chr(rng.choice(_DB_SKEW_CPS))
        word = rng.choice(_ASCII_WORDS)
        position = rng.choice(["prefix", "suffix", "only"])
        if position == "prefix":
            add(ch + word)
        elif position == "suffix":
            add(word + ch)
        else:
            add(ch)

    # XID-skew characters (identifier-continue-legal on the platform, not
    # on the venv) embedded inside otherwise-ASCII words -- the corpus's
    # dedicated draw into `_venv_isidentifier`'s emulation space.
    for _ in range(200):
        ch = rng.choice(_XID_SKEW_CHARS)
        add(f"{rng.choice(_ASCII_WORDS)}{ch}{rng.choice(_ASCII_WORDS)}")
        add(ch + rng.choice(_ASCII_WORDS))

    # Control characters and DEL, word-interposed.
    control_chars = [chr(c) for c in range(0x20)] + [chr(0x7F)]
    for _ in range(150):
        ch = rng.choice(control_chars)
        add(f"{rng.choice(_ASCII_WORDS)}{ch}{rng.choice(_ASCII_WORDS)}")

    # Backslash/quotes/backticks -- R3's own family, plus escape-sequence
    # look-alikes.
    for _ in range(150):
        special = rng.choice(["\\", "'", '"', "`", "\\\\", "\\n", "\\t"])
        add(f"{rng.choice(_ASCII_WORDS)}{special}{rng.choice(_ASCII_WORDS)}")

    # Every Python keyword, its underscore-suffixed near-miss, and the
    # mro/dunder/sunder shapes.
    for kw in keyword.kwlist:
        add(kw)
        add(kw + "_")
    for extra in ["mro", "mro_", "_name_", "__init__", "number_", "number_1"]:
        add(extra)

    # Digit-leading values.
    for _ in range(150):
        n_digits = rng.randint(1, 5)
        digits = "".join(str(rng.randint(0, 9)) for _ in range(n_digits))
        add(digits + rng.choice(_ASCII_WORDS))

    # Empty and whitespace-only.
    add("")
    add(" ")
    add("\t")
    add("\n")
    add("   ")

    # Long values.
    for _ in range(30):
        n = rng.randint(200, 1000)
        pool = string.ascii_letters + string.digits + " -_,"
        add("".join(rng.choice(pool) for _ in range(n)))

    # Deliberate collision mutants: for a deterministic sample of base
    # values already built, add shape variants that are DESIGNED to land on
    # the same real generated bound name -- this is what guarantees the
    # corpus reaches the collision space rather than relying on chance
    # collisions among the pools above.
    base_sample = rng.sample(corpus, min(300, len(corpus)))
    for base in base_sample:
        add(base.replace(" ", "-"))
        add(base.replace("-", "_"))
        add(base.replace(" ", "_"))
        add(base + ",")
        add("1" + base)
        add(base + rng.choice(_NFKC_FOLDING_CHARS))
        add(base.replace(" ", " "))  # noqa: RUF001 -- deliberately NBSP-for-space

    return corpus


def _digest_ground_truth(corpus: list[str]) -> list[dict[str, str]]:
    ne_codegen.ensure_env(_REPO_ROOT)
    venv_py = venv_python(_REPO_ROOT / _CODEGEN_VENV_DIR)
    proc = subprocess.run(
        [str(venv_py), "-c", _GROUND_TRUTH_SCRIPT],
        input=json.dumps(corpus),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result: list[dict[str, str]] = json.loads(proc.stdout)
    return result


# Stats floors, frozen at <= half the counts actually observed running THIS
# corpus/seed against the real venv on 2026-07-28: corpus=5291, accepted=4409,
# declined=882, ground_truth_errors=499, multi_value_bound_groups=313,
# over_decline_pairs_checked=300 (capped by construction, not halved). Each
# floor below is comfortably under half its observed counterpart -- margin
# against a future, legitimate, small corpus-mix rebalance not being treated
# as a regression, while still proving the corpus reaches every space this
# test claims to cover (the fuzz is fully seeded, so there is no run-to-run
# measurement noise to buffer against; the margin is purely for intentional
# future corpus edits).
_MIN_CORPUS = 4000
_MIN_ACCEPTED = 2000
_MIN_DECLINED = 400
_MIN_GROUND_TRUTH_ERRORS = 200
_MIN_COLLISION_GROUPS = 150
_MIN_OVER_DECLINE_PAIRS = 300


def test_differential_fuzz_gate_matches_venv_ground_truth() -> None:
    """THE BAR for this round: for every value the gate ACCEPTS, the real
    venv's own mangle machinery (a) does not crash and (b) binds to exactly
    the identifier :func:`emit_mod._member_label` predicts. For every group
    of accepted values sharing a real bound name, the gate DECLINES the
    collision; for a large sample of accepted-value pairs with DIFFERENT
    real bound names, the gate does NOT over-decline. See the module
    docstring for the corpus design, the scope boundary, and why this
    supplements rather than replaces the curated oracle.
    """
    corpus = _build_corpus(_FUZZ_SEED)
    assert len(corpus) >= _MIN_CORPUS, (
        f"corpus too small ({len(corpus)} < {_MIN_CORPUS}) -- the fuzz's own non-vacuity "
        "floor was not met before even reaching the venv"
    )

    ground_truth = _digest_ground_truth(corpus)
    assert len(ground_truth) == len(corpus)

    accepted_count = 0
    declined_count = 0
    error_count = 0
    bound_groups_raw: dict[str, set[str]] = {}

    for value, gt in zip(corpus, ground_truth, strict=True):
        if "error" in gt:
            error_count += 1
        verdict = emit_mod._enum_decline_reason([value], enum_name=_FUZZ_ENUM_NAME)
        if verdict is None:
            accepted_count += 1
            assert "error" not in gt, (
                f"gate ACCEPTED {value!r} (seed {_FUZZ_SEED}) but the real venv's own "
                f"mangle machinery crashed computing its label: {gt.get('error')}"
            )
            assert "bound" in gt, (
                f"gate ACCEPTED {value!r} (seed {_FUZZ_SEED}) but the real generated "
                f"label {gt.get('label')!r} does not bind as a Python identifier: "
                f"{gt.get('parse_error')}"
            )
            mirror_label = emit_mod._member_label(value)
            assert gt["bound"] == mirror_label, (
                f"gate ACCEPTED {value!r} (seed {_FUZZ_SEED}) but _member_label "
                f"predicts {mirror_label!r} while the real venv generator actually "
                f"binds {gt['bound']!r}"
            )
            bound_groups_raw.setdefault(gt["bound"], set()).add(value)
        else:
            declined_count += 1

    # Collision phase: every REAL bound-name group of >= 2 DISTINCT accepted
    # values must be declined by the gate for each adjacent pair -- proves
    # R6 agrees with the real generator's collision identity, not merely
    # with the mirror's own (possibly self-consistently-wrong) notion of it.
    bound_groups = {k: sorted(v) for k, v in bound_groups_raw.items()}
    multi_groups = {k: v for k, v in bound_groups.items() if len(v) >= 2}
    for bound, values in multi_groups.items():
        for a, b in itertools.pairwise(values):
            assert emit_mod._enum_decline_reason([a, b], enum_name=_FUZZ_ENUM_NAME) is not None, (
                f"gate failed to decline colliding pair {a!r}/{b!r} -> shared real "
                f"bound name {bound!r} (seed {_FUZZ_SEED})"
            )

    # Over-decline control: a large deterministic sample of accepted-value
    # pairs with DIFFERENT real bound names must NOT be declined -- without
    # this, a gate that declined every pair unconditionally would pass the
    # collision phase above vacuously.
    distinct_bounds = sorted(bound_groups)
    rng = random.Random(_FUZZ_SEED)
    shuffled_bounds = distinct_bounds[:]
    rng.shuffle(shuffled_bounds)
    control_pairs = list(zip(shuffled_bounds[0::2], shuffled_bounds[1::2], strict=False))[
        :_MIN_OVER_DECLINE_PAIRS
    ]
    for bound_a, bound_b in control_pairs:
        value_a = bound_groups[bound_a][0]
        value_b = bound_groups[bound_b][0]
        verdict = emit_mod._enum_decline_reason([value_a, value_b], enum_name=_FUZZ_ENUM_NAME)
        assert verdict is None, (
            f"gate OVER-declined non-colliding pair {value_a!r} (bound {bound_a!r}) / "
            f"{value_b!r} (bound {bound_b!r}) (seed {_FUZZ_SEED})"
        )

    print(
        f"[differential fuzz] corpus={len(corpus)} seed={_FUZZ_SEED} "
        f"accepted={accepted_count} declined={declined_count} "
        f"ground_truth_errors={error_count} multi_value_bound_groups={len(multi_groups)} "
        f"over_decline_pairs_checked={len(control_pairs)}"
    )

    # Non-vacuity floors: prove the corpus actually reaches every space this
    # test claims to cover, not merely that nothing crashed.
    assert accepted_count >= _MIN_ACCEPTED, (
        f"only {accepted_count} accepted values (< {_MIN_ACCEPTED}) -- corpus mix drifted "
        "away from the accept space"
    )
    assert declined_count >= _MIN_DECLINED, (
        f"only {declined_count} declined values (< {_MIN_DECLINED}) -- corpus mix drifted "
        "away from the decline space"
    )
    assert error_count >= _MIN_GROUND_TRUTH_ERRORS, (
        f"only {error_count} ground-truth errors (< {_MIN_GROUND_TRUTH_ERRORS}) -- corpus "
        "no longer reaches the venv-crash space (e.g. the DB-skew draws)"
    )
    assert len(multi_groups) >= _MIN_COLLISION_GROUPS, (
        f"only {len(multi_groups)} multi-value collision groups (< {_MIN_COLLISION_GROUPS}) "
        "-- corpus no longer reaches the collision space"
    )
    assert len(control_pairs) >= _MIN_OVER_DECLINE_PAIRS, (
        f"only {len(control_pairs)} over-decline control pairs available "
        f"(< {_MIN_OVER_DECLINE_PAIRS}) -- too few distinct bound names in this run"
    )


def test_fuzz_corpus_reproducible_for_same_seed() -> None:
    """Determinism control (invariant: the fuzz is seeded, not
    time/os-dependent): rebuilding the corpus from the same seed produces a
    byte-for-byte identical list -- a failure reported against this seed can
    always be reproduced.
    """
    first = _build_corpus(_FUZZ_SEED)
    second = _build_corpus(_FUZZ_SEED)
    assert first == second


def test_fuzz_corpus_never_contains_surrogate_codepoints() -> None:
    """Non-vacuity/safety control: every value in the corpus is
    JSON-transportable (no lone UTF-16 surrogate codepoints, which Python
    `str` values built from valid `chr()` calls can never contain anyway --
    this test pins that invariant so a future pool change that introduces
    one is caught immediately rather than surfacing as an opaque JSON
    encode/decode failure inside the venv subprocess).
    """
    corpus = _build_corpus(_FUZZ_SEED)
    for value in corpus:
        for ch in value:
            assert not (0xD800 <= ord(ch) <= 0xDFFF), (
                f"corpus value {value!r} contains a lone surrogate codepoint U+{ord(ch):04X}"
            )
