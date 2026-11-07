# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-HGATE-1 RED-TEAM acceptance suite (the §8 matrix) — WRITTEN FIRST, the bar.

The load-bearing law under test: **facts are verified at EVERY strictness tier;
the knob only moves non-factual vocabulary** (design §0-law-5 / §2.1). So for
every LEGAL (tier x profile) combination a fabricated fact — a fabricated
entity in a fact sentence, a fabricated/wrong/pooled/foreign numeral (digit OR
spelled), an invented IRI, an out-of-range citation, a name-fragment
recombination — is WITHHELD. Only the live-GPU natural narration and a
true spelled count PASS at STANDARD, and the number-word mapping is DERIVED
through the same NumberFact/F3 machinery as digits (a verdict-parity property,
not a transliterated list).

Fixture discipline (brief risk #1 — the gate's derived facts silently ground
numerals a naive fixture author thinks are fabricated): every withhold fixture
is traced against grounding.py's fact inventory. The spelled-wrong-count case
uses a TRUNCATED fixture (aggregates/freq excluded; cells/ordinals/envelope
still mint), puts the target row at index >= 1 (so ordinal-1 anchors a disjoint
row), names the count column so no column token appears in the draft, and
supplies neutral vocab via the prompt — otherwise freq=1 / ordinal-1 would
falsely ground the spelled '1'.

Tests are not a package: ``make_answer`` is a local copy (never imported from a
sibling test module), and ``_EXCLUDED_COMPARATIVES`` is copied verbatim from
test_grounding.py rather than imported.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path

import pytest

from ne.govern import grounding_policy
from ne.govern.grounding import _RULE_ID_NG2 as RULE_ID_NG2
from ne.govern.grounding import (
    CONNECTIVE_ALLOWLIST,
    GATE_VERSION,
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    GateProfile,
    GroundingCorpus,
    GroundingFinding,
    GroundingVerdict,
    check_grounding,
    normalize,
)
from ne.govern.grounding import _parse_number_words as parse_number_words
from ne.govern.grounding_policy import (
    ENFORCEMENT,
    StrictnessDecision,
    StrictnessTier,
    apply_strictness,
)

STRICT = StrictnessTier.STRICT
STANDARD = StrictnessTier.STANDARD

#: The three LEGAL (tier x profile) combinations. STANDARD + ng1-frozen is
#: illegal by design (D-P1-1: ng-1 rules cannot classify fact-sentences); the
#: boot ValueError is NE-HGATE-3's concern, not ne-govern's.
LEGAL_COMBOS: list[tuple[StrictnessTier, GateProfile]] = [
    (STRICT, PROFILE_NG1_FROZEN),
    (STRICT, PROFILE_NG2),
    (STANDARD, PROFILE_NG2),
]
NG2_COMBOS: list[tuple[StrictnessTier, GateProfile]] = [
    (STRICT, PROFILE_NG2),
    (STANDARD, PROFILE_NG2),
]


def _combo_id(combo: tuple[StrictnessTier, GateProfile]) -> str:
    tier, profile = combo
    return f"{tier.value}-{profile.name}"


def make_answer(
    *,
    template_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    citations: Sequence[str] = (),
    truncated: bool = False,
    args: Mapping[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    """Local ``VerifiedAnswer``-shaped builder (tests are not a package)."""
    payload: dict[str, object] = {
        "template_id": template_id,
        "template_version": 1,
        "config_hash": "cfg",
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "citations": list(citations),
        "truncated": truncated,
        "args": dict(args) if args is not None else {},
    }
    payload.update(extra)
    return payload


def _decision(
    draft: str,
    corpus: GroundingCorpus,
    prompt: str,
    tier: StrictnessTier,
    profile: GateProfile,
) -> StrictnessDecision:
    return apply_strictness(check_grounding(draft, corpus, prompt, profile=profile), tier)


def _enforced(decision: StrictnessDecision) -> set[str]:
    return {finding.reason for finding in decision.enforced}


# ---------------------------------------------------------------------------
# Fixtures (traced against grounding.py's fact inventory).
# ---------------------------------------------------------------------------

_U = [f"urn:ne:entity:acct-{s}" for s in ("acme", "beta", "gamma", "delta")]


def _env_corpus() -> GroundingCorpus:
    """Untruncated aggregate: count=3 grounds F3-style (template/column vocab
    'orders'); orders cells 7/4/9. Used for the A-P0-1 fabricated-name case,
    fabricated numeral, invented IRI, out-of-range citation, spelled count."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-orders-count",
                columns=["account", "name", "orders"],
                rows=[
                    [_U[0], "Acme Corp", "7"],
                    [_U[1], "Beta Group", "4"],
                    [_U[2], "Gamma Systems", "9"],
                ],
                citations=[_U[0], _U[1], _U[2]],
            )
        ]
    )


def _env_truncated_corpus() -> GroundingCorpus:
    """Truncated twin of _env_corpus: aggregates (incl. min=4) EXCLUDED so the
    sibling cell '4' is the ONLY source of '4' — the wrong-row refusal is the
    point (an untruncated fixture's min would falsely ground it)."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-orders-count",
                columns=["account", "name", "orders"],
                rows=[
                    [_U[0], "Acme Corp", "7"],
                    [_U[1], "Beta Group", "4"],
                    [_U[2], "Gamma Systems", "9"],
                ],
                citations=[_U[0], _U[1], _U[2]],
                truncated=True,
            )
        ]
    )


def _dir_truncated_corpus() -> GroundingCorpus:
    """Truncated directory with big envelope numerals (total_rows 5,000,500;
    row_limit 500) anchored ONLY to template/column vocab — so those real
    numerals REFUSE when spoken in a per-row identity context (foreign)."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "name"],
                rows=[
                    [_U[0], "Acme Corp"],
                    [_U[1], "Beta Group"],
                    [_U[2], "Gamma Systems"],
                ],
                citations=[_U[0], _U[1], _U[2]],
                truncated=True,
                total_rows=5000500,
                row_limit=500,
            )
        ]
    )


def _spell_corpus(acme_cnt: str) -> GroundingCorpus:
    """Truncated fixture for the spelled-count landmine (brief 1b): Acme at row
    index 1 (ordinal-1 anchors the DISJOINT row-0 name 'Beta Industries'), the
    count column named 'cnt' (no column token appears in the draft). 'open
    order' is supplied via the prompt at call sites; the Acme count cell is
    ``acme_cnt``."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-cnt",
                columns=["account", "name", "cnt"],
                rows=[
                    [_U[1], "Beta Industries", "5"],
                    [_U[0], "Acme Corp", acme_cnt],
                ],
                citations=[_U[1], _U[0]],
                truncated=True,
            )
        ]
    )


def _recomb_corpus() -> GroundingCorpus:
    """Two rows whose names share no token but whose fragments recombine:
    'Thomas More Ltd' + 'Grant Industries Inc' → 'Thomas More Industries'."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="vendors",
                columns=["account", "name"],
                rows=[
                    [_U[0], "Thomas More Ltd"],
                    [_U[1], "Grant Industries Inc"],
                ],
                citations=[_U[0], _U[1]],
            )
        ]
    )


def _collision_corpus() -> GroundingCorpus:
    """'One World Corp' — the number-word 'one' collides with a corpus name
    token (A-P2-8): corpus-prose precedence consumes 'one'."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "name"],
                rows=[
                    [_U[0], "One World Corp"],
                    [_U[1], "Beta Group"],
                ],
                citations=[_U[0], _U[1]],
            )
        ]
    )


def _compound_corpus() -> GroundingCorpus:
    """C1 regression: a corpus where '20' grounds (envelope total_rows=20,
    anchored to the 'accounts' template token) AND '1' grounds (ordinal-1,
    anchored to the 'account' column token) INDEPENDENTLY. A per-token compound
    mapping of 'twenty one' would therefore FALSE-PASS the fabricated value 21 —
    the adjacent-number-word run must be refused whole (numeral-unparseable)."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts",
                columns=["account", "name"],
                rows=[
                    [_U[0], "Acme Corp"],
                    [_U[1], "Beta Group"],
                ],
                citations=[_U[0], _U[1]],
                truncated=True,
                total_rows=20,
            )
        ]
    )


def _narration_corpus() -> GroundingCorpus:
    """The live-GPU-narration acceptance fixture (§1 reproduction): 3 rows each
    with open_orders='1', truncated, returned_rows=3, total_rows=28,
    row_limit=25. Envelope 25/28 anchor to template/column vocab; '1' grounds
    per row via the name identity_cell."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "name", "open_orders"],
                rows=[
                    [_U[0], "Acme Corp", "1"],
                    [_U[1], "Beta Group", "1"],
                    [_U[2], "Gamma Systems", "1"],
                ],
                citations=[_U[0], _U[1], _U[2]],
                truncated=True,
                returned_rows=3,
                total_rows=28,
                row_limit=25,
            )
        ]
    )


#: The §1 live narration, elision expanded (brief 1c). 'open orders'/'order'
#: are user-domain vocabulary supplied via the prompt below.
_NARRATION = (
    "You are cleared to see the following accounts and their respective open "
    "orders: Acme Corp has 1 open order; Beta Group has 1 open order; Gamma "
    "Systems has 1 open order. Additionally, there are 25 more accounts, "
    "making a total of 28 accounts."
)
_NARRATION_PROMPT = "open orders order"


# ---------------------------------------------------------------------------
# §8 red-team matrix: WITHHELD at every legal combo.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_fabricated_name_true_envelope_numeral_withheld(
    combo: tuple[StrictnessTier, GateProfile],
) -> None:
    tier, profile = combo
    corpus = _env_corpus()
    decision = _decision("Initech has 3 open orders", corpus, "", tier, profile)
    assert decision.withhold, _combo_id(combo)
    # 3 is a TRUE numeral (count/distinct-count/envelope) — it GROUNDS; the
    # withhold comes purely from the fabricated entity in a fact sentence.
    assert "numeral-ungrounded" not in _enforced(decision)
    if profile is PROFILE_NG2:
        assert "vocab-ungrounded-factual" in _enforced(decision)
    else:
        assert "vocab-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_fabricated_name_fabricated_numeral_withheld(
    combo: tuple[StrictnessTier, GateProfile],
) -> None:
    tier, profile = combo
    decision = _decision("Initech Global has 73 open orders", _env_corpus(), "", tier, profile)
    assert decision.withhold
    assert "numeral-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_wrong_count_sibling_cell_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    # 4 is Beta's cell; the sentence names Acme -> wrong-row F3 refusal.
    decision = _decision(
        "Acme Corp has 4 open orders", _env_truncated_corpus(), "open", tier, profile
    )
    assert decision.withhold
    assert "numeral-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_pooled_and_foreign_numeral_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    # 5,000,500 and 500 are REAL envelope numerals but anchored to template/
    # column vocab; spoken in a per-row identity ("Acme Corp alone ...") they
    # refuse. 8 has no fact at all.
    draft = "Acme Corp alone placed 5000500 orders 500 shipments and 8 refunds"
    decision = _decision(
        draft, _dir_truncated_corpus(), "placed orders shipments refunds", tier, profile
    )
    assert decision.withhold
    assert "numeral-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_invented_iri_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    decision = _decision("See urn:ne:entity:fake-1", _env_corpus(), "", tier, profile)
    assert decision.withhold
    assert "iri-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_out_of_range_citation_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    decision = _decision("See the directory [9]", _env_corpus(), "", tier, profile)
    assert decision.withhold
    assert "citation-out-of-range" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_spelled_count_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    decision = _decision("You have twenty accounts", _env_corpus(), "", tier, profile)
    assert decision.withhold
    if profile is PROFILE_NG2:
        assert "numeral-ungrounded" in _enforced(decision)  # 'twenty' -> '20', no fact
    else:
        assert "vocab-ungrounded" in _enforced(decision)  # ng1: 'twenty' is prose


@pytest.mark.parametrize("combo", NG2_COMBOS, ids=_combo_id)
def test_spelled_wrong_count_withheld(combo: tuple[StrictnessTier, GateProfile]) -> None:
    tier, profile = combo
    # Acme's cell is 3; 'one' -> '1' has no grounding fact (ordinal-1 anchors
    # the disjoint Beta Industries row) -> numeral-ungrounded.
    decision = _decision(
        "Acme Corp has one open order", _spell_corpus("3"), "open order", tier, profile
    )
    assert decision.withhold
    assert "numeral-ungrounded" in _enforced(decision)


def test_spelled_true_count_passes_at_standard() -> None:
    # Same shape, Acme's cell IS 1 -> 'one' -> '1' grounds via the name
    # identity_cell -> passes at STANDARD.
    decision = _decision(
        "Acme Corp has one open order", _spell_corpus("1"), "open order", STANDARD, PROFILE_NG2
    )
    assert decision.withhold is False
    assert decision.enforced == ()


@pytest.mark.parametrize("corpus_cnt", ["1", "3"], ids=["true-count", "wrong-count"])
def test_number_word_digit_parity_property(corpus_cnt: str) -> None:
    # DERIVED-not-transliterated proof: the word form and the digit form yield
    # IDENTICAL verdicts under ng-2, for both a grounding and a refusing count.
    corpus = _spell_corpus(corpus_cnt)
    prompt = "open order"
    word = check_grounding("Acme Corp has one open order", corpus, prompt, profile=PROFILE_NG2)
    digit = check_grounding("Acme Corp has 1 open order", corpus, prompt, profile=PROFILE_NG2)
    assert word == digit


@pytest.mark.parametrize("combo", NG2_COMBOS, ids=_combo_id)
def test_spelled_compound_fabrication_refused_ng2(
    combo: tuple[StrictnessTier, GateProfile],
) -> None:
    # C1: per-token spelled-compound mapping is a fact-fabrication bypass —
    # 'twenty' (->20) and 'one' (->1) each ground on their own in _compound_corpus,
    # yet the ASSERTED compound 'twenty one' (=21) is fabricated. The adjacent
    # number-word run must be refused whole as numeral-unparseable at BOTH ng-2
    # tiers (design §2.4/§5.4: compounds refuse, conservative-safe).
    tier, profile = combo
    corpus = _compound_corpus()
    prompt = "accounts"
    # Load-bearing sanity: each half grounds independently, so the compound
    # refusal is the ONLY thing standing between the fabricated 21 and a pass.
    assert check_grounding("There are twenty accounts", corpus, prompt, profile=profile).passed
    assert check_grounding("There is one account", corpus, prompt, profile=profile).passed
    decision = _decision("There are twenty one accounts", corpus, prompt, tier, profile)
    assert decision.withhold
    assert "numeral-unparseable" in _enforced(decision)


@pytest.mark.parametrize("combo", NG2_COMBOS, ids=_combo_id)
def test_prompt_echoed_spelled_count_withheld_ng2(
    combo: tuple[StrictnessTier, GateProfile],
) -> None:
    tier, profile = combo
    # The prompt echoes 'twenty' — number-words are NEVER prompt-exempt, so the
    # spelled count still refuses (this closes today's prompt-echo hole; ng1
    # deliberately keeps it and is excluded from this parametrization).
    decision = _decision("You have twenty accounts", _env_corpus(), "twenty", tier, profile)
    assert decision.withhold
    assert "numeral-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
@pytest.mark.parametrize("draft", ["a dozen accounts", "there are millions of orders"])
def test_magnitude_words_refuse_every_combo(
    draft: str, combo: tuple[StrictnessTier, GateProfile]
) -> None:
    tier, profile = combo
    decision = _decision(draft, _env_corpus(), "", tier, profile)
    assert decision.withhold
    if profile is PROFILE_NG2:
        assert "numeral-unparseable" in _enforced(decision)
    else:
        assert "vocab-ungrounded" in _enforced(decision)


@pytest.mark.parametrize("combo", NG2_COMBOS, ids=_combo_id)
def test_name_fragment_recombination_via_allowlist_word_withheld_ng2(
    combo: tuple[StrictnessTier, GateProfile],
) -> None:
    tier, profile = combo
    # 'more' is in the ng-2 EXTENDED lexicon (a "new allowlist word") yet the
    # corpus-precedence run rule keeps [thomas, more, industries] intact so the
    # cross-row recombination still fires.
    decision = _decision("Thomas More Industries", _recomb_corpus(), "", tier, profile)
    assert decision.withhold
    assert "phrase-recombination" in _enforced(decision)


def test_live_gpu_narration_passes_at_standard() -> None:
    corpus = _narration_corpus()
    standard = _decision(_NARRATION, corpus, _NARRATION_PROMPT, STANDARD, PROFILE_NG2)
    assert standard.withhold is False, standard.enforced
    # The SAME draft withholds at STRICT/ng-2 — 'more' is a relational word the
    # STRICT posture refuses (vocab-extended enforced).
    strict = _decision(_NARRATION, corpus, _NARRATION_PROMPT, STRICT, PROFILE_NG2)
    assert strict.withhold
    assert "vocab-extended" in _enforced(strict)
    # 'more' is carried as advisory at STANDARD (recorded, never dropped).
    assert any(f.reason == "vocab-extended" for f in standard.advisory)


# ---------------------------------------------------------------------------
# Behavioral pins.
# ---------------------------------------------------------------------------

_IDENTITY_BATTERY: list[tuple[str, GroundingCorpus, str]] = [
    ("Acme Corp has 7 open orders", _env_corpus(), ""),
    ("Initech has 3 open orders", _env_corpus(), ""),
    ("You have twenty accounts", _env_corpus(), "twenty"),
    ("Thomas More Industries", _recomb_corpus(), ""),
    ("See urn:ne:entity:fake-1 [9]", _env_corpus(), ""),
    (_NARRATION, _narration_corpus(), _NARRATION_PROMPT),
    ("", _env_corpus(), ""),
]


@pytest.mark.parametrize("draft,corpus,prompt", _IDENTITY_BATTERY)
def test_default_profile_identity(draft: str, corpus: GroundingCorpus, prompt: str) -> None:
    # Zero-behavior-change pin: the default is byte-identical to ng1-frozen.
    assert check_grounding(draft, corpus, prompt) == check_grounding(
        draft, corpus, prompt, profile=PROFILE_NG1_FROZEN
    )


def test_profile_ng1_frozen_pins() -> None:
    assert PROFILE_NG1_FROZEN.name == "ng1-frozen"
    assert PROFILE_NG1_FROZEN.version == GATE_VERSION
    assert PROFILE_NG1_FROZEN.core_allowlist == CONNECTIVE_ALLOWLIST
    assert PROFILE_NG1_FROZEN.extended_lexicon == frozenset()
    assert dict(PROFILE_NG1_FROZEN.number_words) == {}
    assert PROFILE_NG1_FROZEN.corpus_precedence_runs is False
    assert PROFILE_NG1_FROZEN.fact_sentence_classes is False
    assert PROFILE_NG1_FROZEN.verbatim_compound_numerals is False
    assert PROFILE_NG2.verbatim_compound_numerals is True


def test_profile_ng2_version_shape_and_recompute() -> None:
    import re

    assert re.fullmatch(r"ng-2/[0-9a-f]{12}", PROFILE_NG2.version)
    core_b = files("ne.govern").joinpath("grounding_allowlist_ng2.txt").read_bytes()
    ext_b = files("ne.govern").joinpath("grounding_allowlist_extended.txt").read_bytes()
    nw_b = files("ne.govern").joinpath("grounding_number_words.txt").read_bytes()
    expected = (
        "ng-2/"
        + hashlib.sha256(
            core_b + b"\x00" + ext_b + b"\x00" + nw_b + b"\x00" + RULE_ID_NG2.encode("utf-8")
        ).hexdigest()[:12]
    )
    assert expected == PROFILE_NG2.version


# The guardian INDEPENDENTLY defines the expected class sets (never imports the
# impl's), so tiering a fact class is a CI failure.
_FACT_CLASSES = {
    "citation-out-of-range",
    "iri-ungrounded",
    "numeral-ungrounded",
    "numeral-unparseable",
    "phrase-recombination",
    # NE-GROUND-REL: semantic row binding is a FACT class — a relation the
    # fetched rows do not support withholds at EVERY tier, never advisory.
    "relation-ungrounded",
    "vocab-ungrounded-factual",
}
_ALL_CLASSES = _FACT_CLASSES | {"vocab-ungrounded", "vocab-extended"}


def test_enforcement_matrix_guardian() -> None:
    for tier in list(StrictnessTier):
        assert ENFORCEMENT[tier] >= _FACT_CLASSES, tier
    assert set(ENFORCEMENT[STANDARD]) == _FACT_CLASSES
    assert set(ENFORCEMENT[STRICT]) == _ALL_CLASSES
    assert len(_ALL_CLASSES) == 9


def test_apply_strictness_partition_properties() -> None:
    verdict = GroundingVerdict(
        passed=False,
        findings=(
            GroundingFinding("numeral-ungrounded", 1),
            GroundingFinding("vocab-extended", 2),
            GroundingFinding("vocab-ungrounded", 3),
        ),
        prompt_echo_count=0,
    )
    for tier in list(StrictnessTier):
        decision = apply_strictness(verdict, tier)
        # Complete partition: every finding lands in exactly one bucket, none
        # invented or duplicated (multiset equality via a sorted materialization).
        combined = decision.enforced + decision.advisory
        assert len(combined) == len(verdict.findings)
        assert sorted(combined, key=lambda f: f.reason) == sorted(
            verdict.findings, key=lambda f: f.reason
        )
        # Strict order preservation WITHIN each bucket — enforced is exactly the
        # in-order subsequence of findings whose reason the tier enforces, and
        # advisory is exactly the complementary in-order subsequence (A1: pins
        # order directly, no set-equality fallback).
        assert decision.enforced == tuple(
            f for f in verdict.findings if f.reason in ENFORCEMENT[tier]
        )
        assert decision.advisory == tuple(
            f for f in verdict.findings if f.reason not in ENFORCEMENT[tier]
        )
        assert decision.withhold is bool(decision.enforced)

    # advisory-only verdict at STANDARD -> withhold False, findings fully carried
    advisory_only = GroundingVerdict(
        passed=False,
        findings=(GroundingFinding("vocab-ungrounded", 1), GroundingFinding("vocab-extended", 1)),
        prompt_echo_count=0,
    )
    decision = apply_strictness(advisory_only, STANDARD)
    assert decision.withhold is False
    assert decision.advisory == advisory_only.findings
    assert decision.enforced == ()


def test_finding_shape_counts_only() -> None:
    # C12: a finding carries ONLY reason + count (no text, no positions).
    fields = {f.name for f in GroundingFinding.__dataclass_fields__.values()}
    assert fields == {"reason", "count"}


def test_ng2_determinism() -> None:
    corpus = _narration_corpus()
    first = check_grounding(_NARRATION, corpus, _NARRATION_PROMPT, profile=PROFILE_NG2)
    second = check_grounding(_NARRATION, corpus, _NARRATION_PROMPT, profile=PROFILE_NG2)
    assert first == second


# ---------------------------------------------------------------------------
# Hygiene screens (§4.4) — parse the packaged resources.
# ---------------------------------------------------------------------------

# Copied verbatim from test_grounding.py:928-965 (tests are not a package).
_EXCLUDED_COMPARATIVES = [
    "more",
    "most",
    "less",
    "least",
    "fewer",
    "than",
    "higher",
    "lower",
    "larger",
    "largest",
    "smaller",
    "smallest",
    "bigger",
    "biggest",
    "best",
    "worst",
    "top",
    "better",
    "worse",
    "greater",
    "greatest",
    "must",
    "all",
    "every",
    "only",
    "many",
    "few",
    "several",
    "nearly",
    "almost",
    "roughly",
    "approximately",
    "over",
    "under",
    "above",
    "below",
]


def _read_tokens(name: str) -> set[str]:
    raw = files("ne.govern").joinpath(name).read_bytes().decode("utf-8")
    tokens: set[str] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tokens.add(s.split()[0])  # number-words are 'word canonical'
    return tokens


def test_hygiene_screens() -> None:
    ng1 = _read_tokens("grounding_allowlist.txt")
    core = set(PROFILE_NG2.core_allowlist)
    extended = set(PROFILE_NG2.extended_lexicon)
    number_words = set(PROFILE_NG2.number_words.keys())

    assert ng1 <= core, "ng2 core must be a superset of ng1 core"
    assert core.isdisjoint(_EXCLUDED_COMPARATIVES), core & set(_EXCLUDED_COMPARATIVES)
    assert core.isdisjoint(extended), core & extended
    assert (core | extended).isdisjoint(number_words), (core | extended) & number_words

    for token in core | extended | number_words:
        assert normalize(token) == (token,), token

    # identity-shaped words never in any list
    assert {"initech", "globex", "acme", "corp", "urn", "ne"}.isdisjoint(
        core | extended | number_words
    )

    # the five live-failure words resolve correctly
    assert {"additionally", "following", "making", "respective"} <= core
    assert "more" in extended
    assert "more" not in core


def test_number_words_parser_fail_loud() -> None:
    with pytest.raises(ValueError):
        parse_number_words(b"One 1\n")  # unnormalized word
    with pytest.raises(ValueError):
        parse_number_words(b"one 1\none 2\n")  # duplicate word
    with pytest.raises(ValueError):
        parse_number_words(b"7 7\n")  # numeral-classified word
    with pytest.raises(ValueError):
        parse_number_words(b"one x\n")  # non-digit canonical
    with pytest.raises(ValueError):
        parse_number_words(b"one two three\n")  # malformed arity
    # a well-formed mix parses
    parsed = parse_number_words(b"one 1\ndozen\n")
    assert parsed == {"one": "1", "dozen": None}


# ---------------------------------------------------------------------------
# Residual pins (A-P2-7 / A-P2-8) — conscious posture, flips in future hardening.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", LEGAL_COMBOS, ids=_combo_id)
def test_zero_claim_residual_pinned(combo: tuple[StrictnessTier, GateProfile]) -> None:
    # A-P2-7: a FALSE zero-claim (real count is 3) passes at EVERY legal combo
    # today — 'no'/'none' are not fact events. Pinned so future hardening flips
    # it consciously.
    tier, profile = combo
    decision = _decision("Acme Corp has no open orders", _env_corpus(), "open", tier, profile)
    assert decision.withhold is False


def test_number_word_corpus_name_collision_residual() -> None:
    # A-P2-8: 'One World Corp' in the corpus -> corpus-prose precedence consumes
    # 'one' (no fact event) -> passes at STANDARD; 'more' still refuses at STRICT.
    corpus = _collision_corpus()
    standard = _decision("there is one more account", corpus, "", STANDARD, PROFILE_NG2)
    assert standard.withhold is False
    strict = _decision("there is one more account", corpus, "", STRICT, PROFILE_NG2)
    assert strict.withhold
    assert "vocab-extended" in _enforced(strict)


# ---------------------------------------------------------------------------
# AST purity guardian for the new module.
# ---------------------------------------------------------------------------

_POLICY_ALLOWED_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "enum",
    "types",
    "typing",
}


def test_grounding_policy_import_guardian() -> None:
    source = Path(grounding_policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in _POLICY_ALLOWED_ROOTS, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            if node.module == "ne.govern.grounding":
                continue
            root = node.module.split(".")[0]
            # the ONLY permitted ne.* import is ne.govern.grounding
            assert not root.startswith("ne") or node.module == "ne.govern.grounding", node.module
            assert root in _POLICY_ALLOWED_ROOTS, node.module
