# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GA-G1: the grounding-gate core (``ne.govern.grounding``) — pure,
deterministic, model-free. This is the guarantee unit: precision + fail-
closed over everything. See ``docs/architecture/anti-hallucination-grounding.md`` for the
algorithm and the BINDING red-team hardening (F1 arg-echo poisoning, F2
percent/currency laundering, F3 aggregate-provenance adjacency).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import ne.govern.grounding as grounding
from ne.govern.grounding import (
    CONNECTIVE_ALLOWLIST,
    GATE_VERSION,
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    GroundingCorpus,
    GroundingFinding,
    GroundingVerdict,
    NumberFact,
    canonical_numeral,
    check_grounding,
    is_numeral,
    normalize,
)

_VECTORS_PATH = Path(__file__).parent / "vectors" / "normalize_parity_vectors.json"


def make_answer(
    *,
    template_id: str = "accounts-directory",
    template_version: int = 1,
    config_hash: str = "cfg",
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    citations: Sequence[str] = (),
    truncated: bool = False,
    args: Mapping[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    """The ``VerifiedAnswer`` JSON shape (``answers.py:85-102``) — extra keys
    (``row_limit``/``ontology_version``/``executed_at``) are always present
    to prove they're tolerated."""
    payload: dict[str, object] = {
        "template_id": template_id,
        "template_version": template_version,
        "config_hash": config_hash,
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "citations": list(citations),
        "truncated": truncated,
        "args": dict(args) if args is not None else {},
        "row_limit": 100,
        "ontology_version": "ov-1",
        "executed_at": "2026-07-16T00:00:00Z",
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# normalize — U1 parity + determinism
# ---------------------------------------------------------------------------


def test_normalize_parity_vectors() -> None:
    data = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    vectors = data["vectors"]
    assert vectors, "vectors fixture must be non-empty"
    for vector in vectors:
        assert normalize(vector["text"]) == tuple(vector["tokens"]), vector["text"]


def test_normalize_deterministic() -> None:
    data = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    for vector in data["vectors"]:
        first = normalize(vector["text"])
        second = normalize(vector["text"])
        assert first == second


# ---------------------------------------------------------------------------
# canonical_numeral / is_numeral
# ---------------------------------------------------------------------------


def test_canonical_numeral_plain() -> None:
    assert canonical_numeral("42") == "42"
    assert canonical_numeral("4200") == "4200"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("1,234.50", "1234.5"),
        ("1_234", "1234"),
        ("1 234", "1234"),  # thin space  # noqa: RUF001
        ("1 234", "1234"),  # narrow no-break space  # noqa: RUF001
        ("1 234", "1234"),  # NBSP  # noqa: RUF001
        ("+50", "50"),
        ("-0", "0"),
        ("4200", "4200"),  # never renders as E-notation
    ],
)
def test_canonical_numeral_separators(token: str, expected: str) -> None:
    assert canonical_numeral(token) == expected


def test_canonical_numeral_non_ascii_digits() -> None:
    assert canonical_numeral("٤٢") == "42"
    assert canonical_numeral("４２") == "42"  # noqa: RUF001 - full-width digits (NFKC upstream)


def test_canonical_numeral_percent_currency_distinct() -> None:
    import unicodedata

    assert canonical_numeral("50%") == "50%"
    assert canonical_numeral("50%") != canonical_numeral("50")
    assert canonical_numeral("$50") == "$50"
    assert canonical_numeral("50€") == "€50"  # 50€ -> €50 (prefix canonical)
    # NFKC folding happens upstream in the shared tokenizer (§2c), not inside
    # canonical_numeral itself -- pre-fold here, mirroring the non-ASCII-
    # digits test's "call canonical on pre-folded token" contract.
    fullwidth_percent = unicodedata.normalize(
        "NFKC",
        "５０％",  # noqa: RUF001 - full-width digits/percent
    )
    assert canonical_numeral(fullwidth_percent) == "50%"
    assert canonical_numeral("50%%") is None
    assert canonical_numeral("$50%") is None


def test_canonical_numeral_unparseable() -> None:
    for token in ("3:1", "o-1", "1e999999"):
        assert is_numeral(token) is True
        assert canonical_numeral(token) is None


# ---------------------------------------------------------------------------
# GroundingCorpus.from_answers — shape gate + basic products
# ---------------------------------------------------------------------------


def test_from_answers_rejects_non_artifacts() -> None:
    missing_fields = {
        "template_id": "t",
        "columns": ["a"],
        "rows": [["1"]],
        "citations": [],
        "truncated": False,
    }
    row_mismatch = make_answer(columns=["a", "b"], rows=[["1"]])
    bad_truncated = {**make_answer(columns=["a"], rows=[["1"]]), "truncated": "yes"}
    corpus = GroundingCorpus.from_answers(
        ["BLOCKED: policy denied", missing_fields, row_mismatch, bad_truncated]
    )
    assert corpus.phrases == frozenset()
    assert corpus.tokens == frozenset()
    assert corpus.iris == frozenset()
    assert corpus.numbers == {}
    assert corpus.citation_count == 0


def test_from_answers_extra_keys_tolerated() -> None:
    answer = make_answer(
        columns=["name"],
        rows=[["acme"]],
        row_limit=50,
        ontology_version="ov-9",
        executed_at="2026-07-16T00:00:00Z",
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert ("acme",) in corpus.phrases
    assert "acme" in corpus.tokens


def test_corpus_phrases_tokens_iris() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["name", "iri"],
        rows=[["Acme Corp", "urn:ne:entity:acme"]],
        citations=["urn:ne:entity:cit1"],
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert ("acme", "corp") in corpus.phrases
    assert ("name",) in corpus.phrases
    assert ("accounts", "directory") in corpus.phrases  # template_id
    assert ("urn", "ne", "entity", "cit1") in corpus.phrases  # normalized citation
    assert ("urn:ne:entity:cit1",) in corpus.phrases  # raw citation 1-tuple
    assert {"acme", "corp", "name", "iri", "accounts", "directory"} <= corpus.tokens
    assert "urn:ne:entity:acme" in corpus.iris  # IRI-shaped cell
    assert "urn:ne:entity:cit1" in corpus.iris  # raw citation
    assert corpus.citation_count == 1


# ---------------------------------------------------------------------------
# F1 — arg-echo corpus poisoning (REQUIRED)
# ---------------------------------------------------------------------------


def test_f1_arg_echo_cell_excluded() -> None:
    echoed = "initech has 73 open orders"
    answer = make_answer(
        template_id="accounts-directory",
        columns=["Q", "account"],  # casefold("Q") == casefold(arg key "q")
        rows=[[echoed, "globex-holdings"]],
        args={"q": echoed},
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert "initech" not in corpus.tokens
    assert "73" not in corpus.numbers
    assert not any("initech" in phrase for phrase in corpus.phrases)

    verdict = check_grounding("Initech has 73 open orders", corpus, prompt="")
    assert not verdict.passed
    reasons = {finding.reason for finding in verdict.findings}
    assert "vocab-ungrounded" in reasons
    assert "numeral-ungrounded" in reasons


# ---------------------------------------------------------------------------
# F2 — percent/currency laundering (REQUIRED)
# ---------------------------------------------------------------------------


def test_f2_percent_does_not_ground_off_plain_number() -> None:
    plain = GroundingCorpus.from_answers([make_answer(columns=["metric"], rows=[["50"]])])
    verdict = check_grounding("the share is 50%", plain, prompt="")
    assert not verdict.passed
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)

    literal_percent = GroundingCorpus.from_answers([make_answer(columns=["share"], rows=[["50%"]])])
    verdict2 = check_grounding("the share is 50%", literal_percent, prompt="")
    assert verdict2.passed


def test_f2_detached_percent_currency_refuse_against_plain_cell() -> None:
    """Codex P2 repro, flipped: a percent/currency symbol DETACHED from its
    numeral by whitespace must refuse identically to the attached form --
    both must fail to ground against a corpus whose cell is the bare
    numeral ``"50"``."""
    plain = GroundingCorpus.from_answers(
        [make_answer(columns=["order", "amount"], rows=[["o1", "50"]])]
    )
    for draft in (
        "the amount is 50 %",
        "the amount is 50%",
        "the amount is $ 50",
        "the amount is $50",
    ):
        verdict = check_grounding(draft, plain, prompt="")
        assert not verdict.passed, draft
        assert any(f.reason == "numeral-ungrounded" for f in verdict.findings), draft


def test_f2_literal_percent_cell_grounds_detached_and_attached() -> None:
    """Symmetry: a literal ``"50%"`` cell grounds BOTH the detached and
    attached draft forms, and a corpus cell that is ITSELF detached
    (``"50 %"``) grounds an attached draft -- the shared tokenizer binds
    both sides identically."""
    literal = GroundingCorpus.from_answers([make_answer(columns=["share"], rows=[["50%"]])])
    assert check_grounding("the share is 50 %", literal, prompt="").passed
    assert check_grounding("the share is 50%", literal, prompt="").passed

    detached_cell = GroundingCorpus.from_answers([make_answer(columns=["share"], rows=[["50 %"]])])
    assert check_grounding("the share is 50%", detached_cell, prompt="").passed


def test_f2_plain_number_unaffected_by_binding() -> None:
    """The binding pass must not perturb an ordinary numeral with no
    adjacent symbol -- 'the amount is 50' still grounds via the F3 column
    anchor ('amount')."""
    plain = GroundingCorpus.from_answers(
        [make_answer(columns=["order", "amount"], rows=[["o1", "50"]])]
    )
    assert check_grounding("the amount is 50", plain, prompt="").passed


def test_f2_detached_symbol_variants_fail_closed() -> None:
    """Adjacent-form variants that must all fail closed: a leading '%'
    (fails to parse -- '%50' still carries a bare '%' after prefix/suffix
    stripping, so it is numeral-unparseable), a trailing '$' (canonicalizes
    to '$50', refuses as ungrounded), a punctuation-interposed '50 , %'
    (the stripped comma must not break adjacency), and a citation-
    interposed '50 [1] %' with an IN-RANGE citation (the refusal must be the
    bound numeral, not a citation-out-of-range finding)."""
    plain = GroundingCorpus.from_answers(
        [make_answer(columns=["order", "amount"], rows=[["o1", "50"]])]
    )
    leading_percent = check_grounding("the amount is % 50", plain, prompt="")
    assert not leading_percent.passed
    assert any(f.reason == "numeral-unparseable" for f in leading_percent.findings)

    trailing_currency = check_grounding("the amount is 50 $", plain, prompt="")
    assert not trailing_currency.passed
    assert any(f.reason == "numeral-ungrounded" for f in trailing_currency.findings)

    comma_interposed = check_grounding("the amount is 50 , %", plain, prompt="")
    assert not comma_interposed.passed
    assert any(f.reason == "numeral-ungrounded" for f in comma_interposed.findings)

    cited = GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["order", "amount"],
                rows=[["o1", "50"]],
                citations=["urn:ne:src:1"],
            )
        ]
    )
    citation_interposed = check_grounding("the amount is 50 [1] %", cited, prompt="")
    assert not citation_interposed.passed
    assert any(f.reason == "numeral-ungrounded" for f in citation_interposed.findings)
    assert not any(f.reason == "citation-out-of-range" for f in citation_interposed.findings)


def test_f2_detached_unicode_currency_and_fullwidth_percent() -> None:
    """Non-ASCII symbol-binding legs: a Unicode currency char ('€') binds
    whether leading or trailing, and a FULLWIDTH percent sign (U+FF05, which
    NFKC-folds to ASCII '%' before the bind pass runs) binds too."""
    plain = GroundingCorpus.from_answers(
        [make_answer(columns=["order", "amount"], rows=[["o1", "50"]])]
    )
    for draft in (
        "the amount is € 50",
        "the amount is 50 €",
        "the amount is 50 ％",  # noqa: RUF001 - full-width percent sign
    ):
        verdict = check_grounding(draft, plain, prompt="")
        assert not verdict.passed, draft
        assert any(f.reason == "numeral-ungrounded" for f in verdict.findings), draft


def test_f2_symbol_not_adjacent_to_numeral_keeps_prose_behavior() -> None:
    """DECIDED BOUNDARY (not an oversight): binding is adjacency-only. A
    symbol whose nearest content neighbors on BOTH sides are prose (not a
    numeral) is left unbound and simply dropped by the punctuation fold,
    exactly as before this fix -- 'the amount % is 50' still grounds via
    the plain numeral '50' and the F3 column anchor 'amount'. Extending
    binding across a prose word would be semantic parsing, which is
    explicitly out of scope for this deterministic, pure gate."""
    plain = GroundingCorpus.from_answers(
        [make_answer(columns=["order", "amount"], rows=[["o1", "50"]])]
    )
    assert check_grounding("the amount % is 50", plain, prompt="").passed


# ---------------------------------------------------------------------------
# F3 — aggregate-provenance adjacency + cell/row anchors (REQUIRED)
# ---------------------------------------------------------------------------


def test_f3_aggregate_provenance_adjacency() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["order", "amount"],
        rows=[["o-1", "1000"], ["o-2", "3200"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    refused = check_grounding("4200 orders", corpus, prompt="")
    assert not refused.passed
    assert any(f.reason == "numeral-ungrounded" for f in refused.findings)

    passed = check_grounding("the amount total is 4200", corpus, prompt="")
    assert passed.passed


def test_f3_cell_numeral_row_anchors() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["order", "amount"],
        rows=[["alpha", "1000"], ["beta", "3200"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert check_grounding("alpha 1000", corpus, prompt="").passed
    wrong_row = check_grounding("beta 1000", corpus, prompt="")
    assert not wrong_row.passed
    assert any(f.reason == "numeral-ungrounded" for f in wrong_row.findings)

    # bare column name grounds a cell numeral when no OTHER row is named.
    assert check_grounding("amount 1000", corpus, prompt="").passed


def test_f3_cell_numeral_column_name_does_not_rescue_wrong_row() -> None:
    """Codex P1 (post-review): a bare column-name match must not rescue a
    ``cell`` numeral when the sentence ALSO names a SIBLING row's identity
    for that same column -- otherwise a true column label ("amount") could
    launder a false row/number pairing. Three rows with a MIDDLE value
    (2000, neither the column's min nor its max) isolate the ``cell`` fact
    from any coincidental MIN/MAX aggregate collision on the same canonical.
    """
    answer = make_answer(
        template_id="orders-directory",
        columns=["order", "amount"],
        rows=[["alpha", "1000"], ["beta", "3200"], ["gamma", "2000"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    # "beta" grounds via corpus.tokens but is a single token (run length 1,
    # exempt from phrase-recombination), so only the F3 row-binding check
    # can catch this false pairing.
    wrong_row_with_column = check_grounding("beta has amount 2000", corpus, prompt="")
    assert not wrong_row_with_column.passed
    assert any(f.reason == "numeral-ungrounded" for f in wrong_row_with_column.findings)
    # the TRUE row/column/number pairing still grounds with the same shape.
    assert check_grounding("gamma has amount 2000", corpus, prompt="").passed
    # and the bare column-name form (no sibling row named) keeps working.
    assert check_grounding("amount 2000", corpus, prompt="").passed


def test_f3_cell_numeral_shared_boilerplate_row_token_does_not_rescue_wrong_row() -> None:
    """Codex P1 (post-fix regression): the OWN-row-identity match must
    require the fact's ENTIRE row-identity token set to be present, not
    merely SOME overlapping token. Two row identities that share a
    boilerplate word ("corp") must not let that shared word alone satisfy
    the own-row check for a sentence that actually names the OTHER row --
    otherwise "Beta Corp has amount 2000" (2000 is Alpha's cell) would
    ground via "corp" as if it were unambiguous own-row identity. A MIDDLE
    value again isolates the ``cell`` fact from a coincidental MIN/MAX
    aggregate-fact collision on the same canonical numeral."""
    answer = make_answer(
        template_id="orders-directory",
        columns=["company", "amount"],
        rows=[["alpha corp", "2000"], ["beta corp", "500"], ["gamma corp", "3000"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    wrong_row_shared_boilerplate = check_grounding("beta corp has amount 2000", corpus, prompt="")
    assert not wrong_row_shared_boilerplate.passed
    assert any(f.reason == "numeral-ungrounded" for f in wrong_row_shared_boilerplate.findings)
    # the TRUE row/column/number pairing (full identity present) still grounds.
    assert check_grounding("alpha corp has amount 2000", corpus, prompt="").passed
    assert check_grounding("gamma corp has amount 3000", corpus, prompt="").passed
    # bare column name (no sibling row identity named) keeps working.
    assert check_grounding("amount 2000", corpus, prompt="").passed


# ---------------------------------------------------------------------------
# Ordinals / count / distinct-count / frequency / pair-frequency
# ---------------------------------------------------------------------------


def test_count_ordinals_ground() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["name"],
        rows=[["john doe"], ["anna smith"], ["jane roe"]],
    )
    corpus = GroundingCorpus.from_answers([answer])

    listing = check_grounding("1. john doe\n2. anna smith\n3. jane roe", corpus, prompt="")
    assert listing.passed

    count_via_tid = check_grounding("found 3 accounts", corpus, prompt="")
    assert count_via_tid.passed

    wrong_count = check_grounding("found 4 accounts", corpus, prompt="")
    assert not wrong_count.passed
    assert any(f.reason == "numeral-ungrounded" for f in wrong_count.findings)


def test_distinct_count_and_frequency_ground() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["status"],
        rows=[["open"], ["open"], ["closed"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert check_grounding("status has 2", corpus, prompt="").passed
    assert check_grounding("the status value open has 2", corpus, prompt="").passed


def test_pair_frequency_grounds_join_counts() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["account", "status"],
        rows=[["acme", "open"], ["acme", "open"], ["acme", "open"], ["globex", "open"]],
    )
    corpus = GroundingCorpus.from_answers([answer])
    verdict = check_grounding(
        "acme: 3 open orders; globex: 1",
        corpus,
        prompt="how many open orders does each account have",
    )
    assert verdict.passed


def test_pair_frequency_column_cap() -> None:
    columns = ["cola", "colb", "colc", "cold", "cole", "colf", "colg", "colh", "coli", "colj"]
    rows = [[f"{col}1" for col in columns], [f"{col}2" for col in columns]]
    answer = make_answer(template_id="wide", columns=columns, rows=rows)
    corpus = GroundingCorpus.from_answers([answer])

    pairfreq_columns = {
        fact.column
        for facts in corpus.numbers.values()
        for fact in facts
        if fact.kind == "pairfreq"
    }
    assert pairfreq_columns, "pairfreq facts should exist for in-cap column pairs"
    for pair in pairfreq_columns:
        assert "coli" not in pair
        assert "colj" not in pair


# ---------------------------------------------------------------------------
# Truncation fail-closed
# ---------------------------------------------------------------------------


def test_truncated_answer_aggregates_excluded() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["order", "amount"],
        rows=[["o-1", "1000"], ["o-2", "3200"]],
        truncated=True,
    )
    corpus = GroundingCorpus.from_answers([answer])

    # The DERIVED aggregate 4200 (= sum of amounts) is still excluded under
    # truncation -- it grounds against no fact.
    assert not check_grounding("the amount total is 4200", corpus, prompt="").passed
    # raw cell numerals + ordinals still ground even when truncated.
    assert check_grounding("amount 1000", corpus, prompt="").passed

    # NE-CTXB-4: the returned-count numeral now LEGITIMATELY grounds under
    # truncation via the envelope returned_rows fact (that is the unit's
    # purpose -- honest truncation must be speakable). So prove the aggregate
    # exclusion STRUCTURALLY instead of via the old 'found 2 directory'
    # refusal: no DERIVED-aggregate fact of any kind exists under truncation,
    # while envelope facts DO exist.
    kinds = {fact.kind for facts in corpus.numbers.values() for fact in facts}
    derived_aggregate_kinds = {"count", "distinct-count", "freq", "pairfreq", "sum", "min", "max"}
    assert not (kinds & derived_aggregate_kinds), kinds
    assert "envelope" in kinds
    # returned_rows == len(rows) == 2 is minted even though truncated=True.
    assert check_grounding("returned 2 orders", corpus, prompt="").passed


# ---------------------------------------------------------------------------
# envelope facts — parser gate + minting (NE-CTXB-4 G1 parser tests)
# ---------------------------------------------------------------------------


def _facts_of_kind(corpus: GroundingCorpus, kind: str) -> list[NumberFact]:
    return [fact for facts in corpus.numbers.values() for fact in facts if fact.kind == kind]


def _canonicals_for_kind_column(corpus: GroundingCorpus, kind: str, column: str) -> set[str]:
    return {
        canonical
        for canonical, facts in corpus.numbers.items()
        for fact in facts
        if fact.kind == kind and fact.column == column
    }


def test_envelope_returned_rows_minted_without_key() -> None:
    # No returned_rows key -> returned_rows is minted from len(rows) == 3.
    answer = make_answer(
        template_id="accounts-directory",
        columns=["account", "name"],
        rows=[["a1", "Acme"], ["a2", "Beta"], ["a3", "Gamma"]],
    )
    # make_answer always injects row_limit=100, so a row_limit envelope exists
    # too; strip returned_rows to prove the len(rows) fallback specifically.
    answer.pop("returned_rows", None)
    corpus = GroundingCorpus.from_answers([answer])

    assert _canonicals_for_kind_column(corpus, "envelope", "returned_rows") == {"3"}
    fact = next(
        fact
        for fact in corpus.numbers["3"]
        if fact.kind == "envelope" and fact.column == "returned_rows"
    )
    # anchor == template-id tokens + kept-column tokens.
    assert fact.anchors == frozenset({"accounts", "directory", "account", "name"})


def test_envelope_optional_key_type_gate() -> None:
    # returned_rows present but ill-typed / mismatched -> no returned_rows fact,
    # yet cell + ordinal facts remain (per-field fail-closed, answer accepted).
    for bad_returned in (True, len([["a1", "1"]]) + 1):
        answer = make_answer(
            template_id="t",
            columns=["name", "n"],
            rows=[["acme", "5"]],
            returned_rows=bad_returned,
        )
        corpus = GroundingCorpus.from_answers([answer])
        assert not _canonicals_for_kind_column(corpus, "envelope", "returned_rows")
        assert _facts_of_kind(corpus, "cell"), "cell facts must survive a bad envelope field"
        assert _facts_of_kind(corpus, "ordinal")

    # total_rows: None / '500' / True are all rejected.
    for bad_total in (None, "500", True):
        answer = make_answer(columns=["name"], rows=[["acme"]], total_rows=bad_total)
        corpus = GroundingCorpus.from_answers([answer])
        assert not _canonicals_for_kind_column(corpus, "envelope", "total_rows")
    # C1: a total BELOW the verified returned row count is internally
    # contradictory ("returned 2 of 1") and is muted, never minted.
    contradictory = make_answer(columns=["name"], rows=[["acme"], ["beta"]], total_rows=1)
    corpus = GroundingCorpus.from_answers([contradictory])
    assert not _canonicals_for_kind_column(corpus, "envelope", "total_rows")
    # total_rows == returned row count is the boundary that mints.
    equal_total = make_answer(columns=["name"], rows=[["acme"]], total_rows=1)
    corpus = GroundingCorpus.from_answers([equal_total])
    assert _canonicals_for_kind_column(corpus, "envelope", "total_rows") == {"1"}

    # row_limit >= 1 required: 0 -> no fact; 1 -> minted.
    zero_limit = make_answer(columns=["name"], rows=[["acme"]], row_limit=0)
    corpus = GroundingCorpus.from_answers([zero_limit])
    assert not _canonicals_for_kind_column(corpus, "envelope", "row_limit")
    one_limit = make_answer(columns=["name"], rows=[["acme"]], row_limit=1)
    corpus = GroundingCorpus.from_answers([one_limit])
    assert _canonicals_for_kind_column(corpus, "envelope", "row_limit") == {"1"}


def test_envelope_minted_when_truncated() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["account", "name"],
        rows=[["a1", "Acme"]],
        truncated=True,
        total_rows=5000000,
        row_limit=500,
    )
    corpus = GroundingCorpus.from_answers([answer])
    assert _canonicals_for_kind_column(corpus, "envelope", "returned_rows") == {"1"}
    assert _canonicals_for_kind_column(corpus, "envelope", "total_rows") == {"5000000"}
    assert _canonicals_for_kind_column(corpus, "envelope", "row_limit") == {"500"}
    # ... while every DERIVED aggregate stays excluded under truncation.
    kinds = {fact.kind for facts in corpus.numbers.values() for fact in facts}
    assert not (kinds & {"count", "distinct-count", "freq", "pairfreq", "sum", "min", "max"})


def test_envelope_args_echoed_columns_excluded_from_anchor() -> None:
    # A column dropped by the F1 arg-echo rule contributes NO anchor tokens:
    # the envelope anchor is built from kept_columns only.
    echoed = "acme has 7 open orders"
    answer = make_answer(
        template_id="accounts-directory",
        columns=["Q", "account"],  # casefold("Q") == arg key "q" -> dropped
        rows=[[echoed, "globex"]],
        args={"q": echoed},
        total_rows=42,
    )
    corpus = GroundingCorpus.from_answers([answer])
    fact = next(
        fact
        for fact in corpus.numbers["42"]
        if fact.kind == "envelope" and fact.column == "total_rows"
    )
    # 'account' (kept) is present; the echoed column's tokens are not.
    assert "account" in fact.anchors
    assert fact.anchors == frozenset({"accounts", "directory", "account"})
    assert "acme" not in fact.anchors
    assert "orders" not in fact.anchors


def test_sum_min_max_numeric_columns_only() -> None:
    answer = make_answer(
        template_id="orders-directory",
        columns=["order", "amount", "share"],
        rows=[["o-1", "1000", "50%"], ["o-2", "3200", "n/a"]],
    )
    corpus = GroundingCorpus.from_answers([answer])

    def kinds_for(column: str) -> set[str]:
        return {
            fact.kind
            for facts in corpus.numbers.values()
            for fact in facts
            if fact.column == column
        }

    assert {"sum", "min", "max"} <= kinds_for("amount")
    assert not ({"sum", "min", "max"} & kinds_for("share"))
    assert not ({"sum", "min", "max"} & kinds_for("order"))


# ---------------------------------------------------------------------------
# Anti-recombination phrase rule
# ---------------------------------------------------------------------------


def test_phrase_recombination_blocked() -> None:
    answer = make_answer(template_id="names", columns=["name"], rows=[["john doe"], ["anna smith"]])
    corpus = GroundingCorpus.from_answers([answer])

    minted = check_grounding("john smith", corpus, prompt="")
    assert not minted.passed
    assert any(f.reason == "phrase-recombination" for f in minted.findings)

    real = check_grounding("john doe", corpus, prompt="")
    assert real.passed

    single = check_grounding("john", corpus, prompt="")
    assert single.passed


def test_phrase_run_breaks_on_prompt_and_allowlist() -> None:
    answer = make_answer(template_id="names", columns=["name"], rows=[["john doe"], ["anna smith"]])
    corpus = GroundingCorpus.from_answers([answer])

    # 'and' (allowlist) breaks the run between 'john' and 'anna'.
    via_allowlist = check_grounding("john and anna", corpus, prompt="")
    assert via_allowlist.passed

    # without a break, the same tokens WOULD be a violation ...
    unbroken = check_grounding("john smith", corpus, prompt="")
    assert not unbroken.passed

    # ... but a prompt-echoed 'smith' removes it from run-membership.
    via_prompt = check_grounding("john smith", corpus, prompt="smith called earlier")
    assert via_prompt.passed


# ---------------------------------------------------------------------------
# Citation markers
# ---------------------------------------------------------------------------


def test_citation_markers() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["name"],
        rows=[["acme"], ["globex"]],
        citations=["urn:ne:entity:a1", "urn:ne:entity:a2"],
    )
    corpus = GroundingCorpus.from_answers([answer])

    ok = check_grounding("acme[1] and globex[2].", corpus, prompt="")
    assert ok.passed

    out_of_range = check_grounding("acme[3] and globex[0].", corpus, prompt="")
    assert not out_of_range.passed
    assert any(f.reason == "citation-out-of-range" and f.count == 2 for f in out_of_range.findings)

    # excised markers never produce stray numerals: a pure "[1][2]" token
    # contributes zero numeral findings.
    both = check_grounding("acme[1][2].", corpus, prompt="")
    assert not any(f.reason.startswith("numeral") for f in both.findings)


# ---------------------------------------------------------------------------
# IRI / URN
# ---------------------------------------------------------------------------


def test_iri_checked_raw_before_fold() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["name"],
        rows=[["acme"], ["globex"]],
        citations=["urn:ne:entity:a1", "urn:ne:entity:a2"],
    )
    corpus = GroundingCorpus.from_answers([answer])

    assert check_grounding("acme urn:ne:entity:a1", corpus, prompt="").passed

    zero_width = check_grounding("acme urn:ne:entity:a1​", corpus, prompt="")
    assert not zero_width.passed
    assert any(f.reason == "iri-ungrounded" for f in zero_width.findings)

    unknown = check_grounding("acme https://example.com/x", corpus, prompt="")
    assert not unknown.passed
    assert any(f.reason == "iri-ungrounded" for f in unknown.findings)

    total_colon = check_grounding("Total: 5", corpus, prompt="")
    assert not any(f.reason == "iri-ungrounded" for f in total_colon.findings)


# ---------------------------------------------------------------------------
# Unicode evasion
# ---------------------------------------------------------------------------


def test_unicode_evasion() -> None:
    answer = make_answer(template_id="names", columns=["name"], rows=[["Acme"]])
    corpus = GroundingCorpus.from_answers([answer])

    cf_stripped = check_grounding("Ac​me is here", corpus, prompt="")
    assert cf_stripped.passed

    homoglyph = check_grounding(
        "Аcme is here",  # noqa: RUF001 - Cyrillic capital A homoglyph
        corpus,
        prompt="",
    )
    assert not homoglyph.passed
    assert any(f.reason == "vocab-ungrounded" for f in homoglyph.findings)

    fullwidth = check_grounding(
        "ＡＣＭＥ is here",  # noqa: RUF001 - full-width Latin letters
        corpus,
        prompt="",
    )
    assert fullwidth.passed


# ---------------------------------------------------------------------------
# Plural fold — check-time only
# ---------------------------------------------------------------------------


def test_plural_fold_check_time_only() -> None:
    singular_corpus = GroundingCorpus.from_answers(
        [make_answer(template_id="t", columns=["account"], rows=[["acme"]])]
    )
    assert check_grounding("accounts here", singular_corpus, prompt="").passed

    plural_corpus = GroundingCorpus.from_answers(
        [make_answer(template_id="t", columns=["accounts"], rows=[["acme"]])]
    )
    assert check_grounding("account here", plural_corpus, prompt="").passed

    # the fold never applies to allowlist membership: 'hass' does not fold
    # to the allowlisted 'has'.
    empty_corpus = GroundingCorpus.from_answers([])
    hass = check_grounding("hass", empty_corpus, prompt="")
    assert not hass.passed
    assert any(f.reason == "vocab-ungrounded" for f in hass.findings)


# ---------------------------------------------------------------------------
# Numerals never prompt-exempt
# ---------------------------------------------------------------------------


def test_numeral_never_prompt_exempt() -> None:
    empty_corpus = GroundingCorpus.from_answers([])
    verdict = check_grounding("yes, 40 seats", empty_corpus, prompt="acme has 40 seats, right?")
    assert not verdict.passed
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)


# ---------------------------------------------------------------------------
# Empty corpus
# ---------------------------------------------------------------------------


def test_empty_corpus_fails_content() -> None:
    empty_corpus = GroundingCorpus.from_answers([])
    assert not check_grounding("acme corp", empty_corpus, prompt="").passed
    assert check_grounding("the data is here", empty_corpus, prompt="").passed
    assert check_grounding("", empty_corpus, prompt="").passed


# ---------------------------------------------------------------------------
# C12 redaction discipline
# ---------------------------------------------------------------------------


def test_verdict_redaction_c12() -> None:
    fields = {f.name for f in dataclasses.fields(GroundingFinding)}
    assert fields == {"reason", "count"}

    answer = make_answer(template_id="names", columns=["name"], rows=[["john doe"]])
    corpus = GroundingCorpus.from_answers([answer])
    verdict = check_grounding("Initech Global has 73 open orders", corpus, prompt="")
    assert not verdict.passed
    for finding in verdict.findings:
        # no draft text anywhere in the finding's repr.
        assert "Initech" not in repr(finding)
        assert "73" not in repr(finding)
    reasons = [f.reason for f in verdict.findings]
    assert reasons == sorted(reasons)
    assert verdict.passed == (verdict.findings == ())


# ---------------------------------------------------------------------------
# prompt_echo_count
# ---------------------------------------------------------------------------


def test_prompt_echo_count() -> None:
    answer = make_answer(
        template_id="accounts-directory",
        columns=["Q", "account"],
        rows=[["initech has 73 open orders", "globex-holdings"]],
        args={"q": "initech has 73 open orders"},
    )
    corpus = GroundingCorpus.from_answers([answer])
    verdict = check_grounding("initech seems nice", corpus, prompt="initech seems nice")
    assert verdict.passed
    assert verdict.prompt_echo_count == 3

    # a corpus- or allowlist-grounded token is NOT counted as a prompt echo.
    corpus2 = GroundingCorpus.from_answers(
        [make_answer(template_id="t", columns=["name"], rows=[["initech"]])]
    )
    verdict2 = check_grounding("initech", corpus2, prompt="initech")
    assert verdict2.prompt_echo_count == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_property() -> None:
    answer = make_answer(columns=["name"], rows=[["acme"], ["globex"]])
    reordered = json.loads(json.dumps({key: answer[key] for key in reversed(list(answer))}))

    corpus_a = GroundingCorpus.from_answers([answer])
    corpus_b = GroundingCorpus.from_answers([reordered])
    assert corpus_a == corpus_b

    verdict_a = check_grounding("found acme and globex", corpus_a, prompt="")
    verdict_b = check_grounding("found acme and globex", corpus_b, prompt="")
    assert verdict_a == verdict_b
    assert check_grounding("found acme and globex", corpus_a, prompt="") == verdict_a


# ---------------------------------------------------------------------------
# CONNECTIVE_ALLOWLIST + GATE_VERSION
# ---------------------------------------------------------------------------

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


def test_allowlist_no_comparatives() -> None:
    assert CONNECTIVE_ALLOWLIST.isdisjoint(_EXCLUDED_COMPARATIVES)


def test_allowlist_normalized_and_resource_loaded() -> None:
    assert CONNECTIVE_ALLOWLIST, "allowlist must be non-empty"
    for token in CONNECTIVE_ALLOWLIST:
        assert normalize(token) == (token,)
    reporting_words = {
        "found",
        "returned",
        "listed",
        "shows",
        "matched",
        "fetched",
        "see",
        "data",
        "results",
        "rows",
        "verified",
        "directory",
        "table",
    }
    assert reporting_words <= CONNECTIVE_ALLOWLIST

    from importlib.resources import files

    resource = files("ne.govern").joinpath("grounding_allowlist.txt")
    assert resource.is_file()


def test_gate_version_pinned() -> None:
    assert re.fullmatch(r"ng-1/[0-9a-f]{12}", GATE_VERSION)

    from importlib.resources import files

    allowlist_bytes = files("ne.govern").joinpath("grounding_allowlist.txt").read_bytes()
    # NE-CTXB-4 moved GATE_VERSION by extending the pinned recompute-list with
    # the envelope facts + the single-sibling-cell identity extension.
    recompute_id = (
        b"count|ordinals|distinct-count|freq|pairfreq(cap=8)|sum|min|max|"
        b"envelope(returned_rows,total_rows,row_limit)|"
        b"cell-identity-v2(single-sibling-cell,degenerate-subset-refuse)"
    )
    expected = "ng-1/" + hashlib.sha256(allowlist_bytes + b"\x00" + recompute_id).hexdigest()[:12]
    assert expected == GATE_VERSION


# ---------------------------------------------------------------------------
# C3 guardian: stdlib-only imports (positive allowlist)
# ---------------------------------------------------------------------------

_ALLOWED_ROOT_MODULES = {
    "__future__",
    "collections",
    "dataclasses",
    "decimal",
    "hashlib",
    "importlib",
    "re",
    "typing",
    "unicodedata",
}


def test_grounding_module_stdlib_only_guardian() -> None:
    source = Path(grounding.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in _ALLOWED_ROOT_MODULES, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            root = node.module.split(".")[0]
            assert root in _ALLOWED_ROOT_MODULES, node.module


# ---------------------------------------------------------------------------
# GroundingVerdict / NumberFact structural sanity
# ---------------------------------------------------------------------------


def test_grounding_verdict_and_number_fact_shapes() -> None:
    verdict_fields = {f.name for f in dataclasses.fields(GroundingVerdict)}
    assert verdict_fields == {"passed", "findings", "prompt_echo_count"}
    fact_fields = {f.name for f in dataclasses.fields(NumberFact)}
    # NE-CTXB-4 adds the defaulted ``identity_cells`` field (single-cell own-row
    # identity for ``cell`` facts); NE-GROUND-ROWID adds the defaulted
    # ``row_tokens`` field (the fact's own row's ATOMIC token set, which is what
    # a sentence's QUOTED identifiers are checked against).
    assert fact_fields == {"kind", "column", "anchors", "identity_cells", "row_tokens"}


# ---------------------------------------------------------------------------
# ng-2 verbatim COMPOUND numerals (NE-GROUND-COMPOUND)
#
# ``canonical_numeral`` returns None for every COMPOUND numeral -- an ISO date
# (2026-05-15), a hyphenated case id (DR-4830), a dotted version (1.2.3), a
# range, a time. Under ng-1 that is ALWAYS a ``numeral-unparseable`` finding,
# regardless of provenance -- so a sentence refuses even when the token was
# copied VERBATIM out of the very chunk being checked against, and no claim
# about a dated record can ever ground. ng-2 grounds such a token IFF it
# appears verbatim in the corpus (a QUOTATION, not an assertion) AND it JOINS
# the anti-recombination run, so contiguity still binds it to its neighbours.
# ---------------------------------------------------------------------------


def _compound_corpus() -> GroundingCorpus:
    """One row whose cells carry the three compound shapes, each contiguous
    with its own prose ("filed 2026-05-15"), plus an unrelated "open" cell to
    recombine against."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["case_id", "summary", "schema", "status"],
                rows=[["DR-4830", "filed 2026-05-15", "schema 1.2.3", "open"]],
            )
        ]
    )


def test_corpus_build_retains_compound_numerals_verbatim() -> None:
    """The BUILD-side prerequisite: ``normalize`` shreds a compound numeral
    ("2026-05-15" -> 2026/05/15), so containment could never match. The corpus
    now ALSO keeps the numeral-preserving phrase variant -- and keeps the
    normalize phrase alongside it (no regression)."""
    corpus = _compound_corpus()
    assert "2026-05-15" in corpus.tokens
    assert "dr-4830" in corpus.tokens
    assert "1.2.3" in corpus.tokens
    # the normalize-shredded tokens/phrases are STILL there.
    assert {"2026", "05", "15", "dr", "4830"} <= corpus.tokens
    assert ("filed", "2026", "05", "15") in corpus.phrases
    assert ("filed", "2026-05-15") in corpus.phrases


@pytest.mark.parametrize("draft", ["DR-4830", "filed 2026-05-15", "schema 1.2.3"])
def test_ng2_verbatim_compound_numeral_grounds(draft: str) -> None:
    verdict = check_grounding(draft, _compound_corpus(), prompt="", profile=PROFILE_NG2)
    assert verdict.passed, verdict.findings


@pytest.mark.parametrize("draft", ["DR-4830", "filed 2026-05-15", "schema 1.2.3"])
def test_ng1_compound_numeral_still_unparseable(draft: str) -> None:
    """Byte-parity: the ng-1 rulebook is untouched -- a compound numeral is a
    finding there whatever its provenance."""
    verdict = check_grounding(draft, _compound_corpus(), prompt="", profile=PROFILE_NG1_FROZEN)
    assert not verdict.passed
    assert any(f.reason == "numeral-unparseable" for f in verdict.findings)


@pytest.mark.parametrize("draft", ["2026-05-15", "DR-4830", "1.2.3"])
def test_ng2_compound_numeral_absent_from_corpus_fails_closed(draft: str) -> None:
    """Fail-closed intact: the SAME shapes, absent from the corpus, are still
    ``numeral-unparseable`` under ng-2."""
    corpus = GroundingCorpus.from_answers(
        [make_answer(columns=["name", "amount"], rows=[["acme corp", "50"]])]
    )
    verdict = check_grounding(draft, corpus, prompt="", profile=PROFILE_NG2)
    assert not verdict.passed
    assert GroundingFinding("numeral-unparseable", 1) in verdict.findings


@pytest.mark.parametrize("draft", ["filed 2026-05-14", "DR-4831", "schema 1.2.4"])
def test_ng2_compound_numeral_near_miss_refuses(draft: str) -> None:
    """The anti-fabrication core: EXACT containment. A corpus that says
    2026-05-15 can never ground an answer that says 2026-05-14."""
    verdict = check_grounding(draft, _compound_corpus(), prompt="", profile=PROFILE_NG2)
    assert not verdict.passed
    assert any(f.reason == "numeral-unparseable" for f in verdict.findings)


def test_ng2_compound_numeral_participates_in_recombination_run() -> None:
    """The design point: a corpus-contained compound numeral is not waved
    through -- it JOINS the run, so a date lifted from one cell and pasted next
    to prose from a DIFFERENT cell is still ``phrase-recombination``."""
    verdict = check_grounding("open 2026-05-15", _compound_corpus(), prompt="", profile=PROFILE_NG2)
    assert not verdict.passed
    assert GroundingFinding("phrase-recombination", 1) in verdict.findings

    # Same rule ACROSS cells: the case_id cell's id spliced onto the summary
    # cell's "filed <date>" is a recombination, even though every token of it
    # is corpus-verbatim.
    cross = check_grounding(
        "DR-4830 filed 2026-05-15", _compound_corpus(), prompt="", profile=PROFILE_NG2
    )
    assert not cross.passed
    assert GroundingFinding("phrase-recombination", 1) in cross.findings


def test_ng2_containment_is_exact_never_plural_folded() -> None:
    """EXACT membership (``token in corpus.tokens``), never
    ``_prose_in_corpus`` -- the plural fold is meaningless for a numeral and
    would only widen the rule."""
    corpus = GroundingCorpus.from_answers([make_answer(columns=["build"], rows=[["build 1.2.3s"]])])
    verdict = check_grounding("build 1.2.3", corpus, prompt="", profile=PROFILE_NG2)
    assert not verdict.passed
    assert any(f.reason == "numeral-unparseable" for f in verdict.findings)


def test_ng2_parseable_numerals_still_take_the_fact_path() -> None:
    """Plain/percent/currency numerals are PARSEABLE, so the containment rule
    never sees them -- they keep grounding (or refusing) through the NumberFact
    / F3 machinery exactly as before, even in a corpus that now carries
    compound tokens."""
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["case_id", "amount", "share"],
                rows=[["DR-4830", "50", "12%"]],
            )
        ]
    )
    assert check_grounding("the amount is 50", corpus, prompt="", profile=PROFILE_NG2).passed
    assert check_grounding("the share is 12%", corpus, prompt="", profile=PROFILE_NG2).passed
    wrong = check_grounding("the amount is 51", corpus, prompt="", profile=PROFILE_NG2)
    assert GroundingFinding("numeral-ungrounded", 1) in wrong.findings
    laundered = check_grounding("the share is 12", corpus, prompt="", profile=PROFILE_NG2)
    assert GroundingFinding("numeral-ungrounded", 1) in laundered.findings


# ---------------------------------------------------------------------------
# NE-GROUND-ROWID: a QUOTED identifier binds the numeral to ITS OWN row
#
# An entity identifier ("mer-dsp-003") is numeral-classified, so it is never a
# prose token and can never enter F3's ``anchor_universe`` -- both the own-row
# and the foreign-row routes are BLIND to it. The verbatim-compound-numeral rule
# made such identifiers speakable, which turned that blindness into a live hole:
# a sentence could attribute one row's cell numeral to a DIFFERENT row and pass
# on the shared column name alone. The quoted identifiers of a sentence are now
# checked against the fact's own row.
# ---------------------------------------------------------------------------


def _rowid_corpus() -> GroundingCorpus:
    """The shape that exposed the hole: mer-dsp-001 has NO credit_amount_cents at
    all, and 20000 is mer-dsp-003's. ``credit_amount_cents`` is only partly
    populated, so it mints no sum/min/max -- these cases therefore exercise the
    ``cell``-fact path itself, not an aggregate that happens to share the value
    (that residual is pinned separately below)."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["dispute_id", "status", "amount_cents", "credit_amount_cents"],
                rows=[
                    ["mer-dsp-001", "open", "65000", ""],
                    ["mer-dsp-003", "open", "20000", "20000"],
                    ["mer-dsp-005", "closed", "90000", "90000"],
                ],
            )
        ]
    )


def test_ng2_quoted_identifier_binds_a_cell_numeral_to_its_own_row() -> None:
    """The true attribution still grounds -- the rule must not be a blanket ban."""
    verdict = check_grounding(
        "mer-dsp-003 has credit_amount_cents 20000", _rowid_corpus(), "", profile=PROFILE_NG2
    )
    assert verdict.passed, verdict.findings


@pytest.mark.parametrize(
    "draft",
    [
        # the numeral is real, the column is real, the ROW is wrong -- and the
        # named row has no credit cell at all.
        "mer-dsp-001 has credit_amount_cents 20000",
        # a sibling row's value for a column the named row DOES carry.
        "mer-dsp-003 has credit_amount_cents 90000",
        "mer-dsp-005 has credit_amount_cents 20000",
    ],
)
def test_ng2_a_numeral_attributed_to_a_row_that_does_not_carry_it_refuses(draft: str) -> None:
    verdict = check_grounding(draft, _rowid_corpus(), "", profile=PROFILE_NG2)
    assert not verdict.passed
    assert GroundingFinding("numeral-ungrounded", 1) in verdict.findings


def test_ng2_a_sentence_naming_two_rows_refuses_both_their_numerals() -> None:
    """Conservative-safe, mirroring the compound spelled-number rule: when a
    sentence names two rows the pairing is unverifiable, so neither grounds."""
    verdict = check_grounding(
        "mer-dsp-003 and mer-dsp-005 have credit_amount_cents 20000 and 90000",
        _rowid_corpus(),
        "",
        profile=PROFILE_NG2,
    )
    assert not verdict.passed
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)


def test_ng2_a_row_agnostic_column_statement_still_grounds() -> None:
    """No identifier quoted -> no row to conflict with. This is the honest
    row-agnostic shape ("the column holds these values"), and it must keep
    working: it is what an answer falls back to when it cannot bind a row."""
    verdict = check_grounding("credit_amount_cents 20000", _rowid_corpus(), "", profile=PROFILE_NG2)
    assert verdict.passed, verdict.findings


def test_ng1_is_untouched_by_the_row_identity_rule() -> None:
    """ng-1 never grounds a compound numeral, so it quotes no identifiers and the
    rule is unreachable there -- the frozen profile stays byte-identical."""
    corpus = _rowid_corpus()
    for draft in ("mer-dsp-003 has credit_amount_cents 20000", "credit_amount_cents 20000"):
        assert check_grounding(draft, corpus, "", profile=PROFILE_NG1_FROZEN) == check_grounding(
            draft, corpus, ""
        )


# ---------------------------------------------------------------------------
# Measured product limits, pinned so they are not "fixed" by accident.
# ---------------------------------------------------------------------------


def test_a_partially_empty_numeric_column_mints_no_sum() -> None:
    """A TRUE total over a column that is only PARTLY populated cannot ground.

    ``sum``/``min``/``max`` are minted only when EVERY cell of the column parses
    as a plain numeral, so a column with blank cells (an unbound OPTIONAL in the
    served table) mints no aggregate at all -- 20000 + 90000 = 110000 is a true
    fact about the served rows that the gate has no fact for. Pinned as a
    DECLARED limit: widening the recompute to "sum over present values" is a
    deliberate rulebook change (it moves the profile version), never a quiet one.
    """
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["dispute_id", "credit_amount_cents"],
                rows=[["mer-dsp-001", ""], ["mer-dsp-003", "20000"], ["mer-dsp-005", "90000"]],
            )
        ]
    )
    assert not any(
        fact.kind in {"sum", "min", "max"} for facts in corpus.numbers.values() for fact in facts
    )
    assert "110000" not in corpus.numbers
    verdict = check_grounding("the total is 110000", corpus, "", profile=PROFILE_NG2)
    assert GroundingFinding("numeral-ungrounded", 1) in verdict.findings


def test_normalize_shards_of_an_identifier_are_still_corpus_tokens() -> None:
    """A DECLARED residual, measured and pinned rather than fixed.

    ``normalize`` shreds ``mer-dsp-003`` into ``mer``/``dsp``/``003`` and those
    fragments remain free-standing corpus vocabulary alongside the atomic form.
    Removing them was measured to be a LATERAL move at best: the words that
    actually drive real refusals ("dispute", "disputes") are a genuine column
    name and a genuine template-id word, not shards, so dropping the shards only
    converts one finding class into another. Pinned so the next person to
    consider it starts from the measurement.
    """
    corpus = _rowid_corpus()
    assert "mer-dsp-003" in corpus.tokens  # the atomic form (NE-GROUND-NUMERAL)
    assert {"mer", "dsp", "003"} <= corpus.tokens  # ... and its shards, still


def test_an_extremum_no_longer_rescues_a_row_scoped_sentence() -> None:
    """NE-GROUND-AGGRESCUE. **This test formerly pinned the OPPOSITE**, as a
    declared residual named ``test_a_table_level_aggregate_still_rescues_a_row_
    scoped_sentence``. Its note said closing the gap "means giving aggregates a
    row-scope notion, which would also refuse legitimate 'row X is one of N'
    sentences -- a deliberate call, not a quiet one."

    The call is now made the other way, for two reasons the original note did not
    have in front of it.

    **One: the cost it feared does not apply to a fix scoped to the EXTREMA.**
    ``min`` and ``max`` are, by construction, some row's cell in that column, so
    an extremum rescue of a value whose every ``cell`` fact was refused is ALWAYS
    that same refused pairing arriving through a fact with no row. Nothing else is
    suppressed: ``count``, ``distinct-count``, ``sum``, ``freq``, ``pairfreq`` and
    ``envelope`` are DERIVED and only coincide with a cell by accident, so the
    "row X is one of N" sentence is untouched -- pinned by the test below.

    **Two: what a pass here BUYS.** ``run_claim_verifier`` raises
    ``ClaimVerificationUnavailable`` on a failed verdict and mints a
    ``DeterministicClaimVerification`` receipt on a passed one. So this residual
    did not merely read oddly in a diagnostic; it ATTESTED a false row/value
    attribution. And it was not a corner: in a TWO-row answer every value is the
    min or the max, so for two-row evidence the row binding protected nothing.

    Here 20000 is mer-dsp-003's ``amount_cents`` and also ``min(amount_cents)``,
    and the sentence names mer-dsp-001, which has 65000.
    """
    verdict = check_grounding(
        "mer-dsp-001 has amount_cents 20000", _rowid_corpus(), "", profile=PROFILE_NG2
    )
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)
    # Anti-vacuity: the min fact IS present, so the refusal is the new rule and
    # not an absent aggregate.
    assert any(
        fact.kind == "min" and fact.column == "amount_cents"
        for fact in _rowid_corpus().numbers["20000"]
    )


def test_the_row_that_owns_the_extremum_still_states_it() -> None:
    """The other half of the pair, and the one that makes the fix a fix rather
    than a blanket refusal: the row the extremum BELONGS to may still say it,
    because its own ``cell`` fact grounds and is never suppressed."""
    verdict = check_grounding(
        "mer-dsp-003 has amount_cents 20000", _rowid_corpus(), "", profile=PROFILE_NG2
    )
    assert verdict.passed


def test_an_extremum_with_no_row_named_still_grounds() -> None:
    """And the genuine aggregate reading survives. With no row identity quoted
    the ``cell`` fact itself grounds -- its sibling-conflict check passes -- so
    ``passing`` still holds a cell fact and the suppression never triggers."""
    verdict = check_grounding("the amount_cents is 20000", _rowid_corpus(), "", profile=PROFILE_NG2)
    assert verdict.passed


def test_a_derived_aggregate_can_still_rescue_when_its_value_collides_with_a_cell() -> None:
    """THE RESIDUAL THAT SURVIVES, narrowed rather than removed, and pinned here
    so it is not mistaken for closure.

    NE-GROUND-AGGRESCUE suppresses only ``min``/``max``. A DERIVED aggregate --
    here the row ``count`` -- whose value happens to equal a cell in a column
    where the cell fact was refused will still rescue it. Below, 2 is
    mer-dsp-001's ``open_disputes`` AND the row count, and the sentence
    attributes it to mer-dsp-002, whose value is 5.

    This is deliberate and it is the ORIGINAL residual's trade, kept: suppressing
    the derived kinds too would refuse the legitimate "row X is one of N"
    sentence whenever N collided with a cell, which is a false REFUSAL traded for
    a false acceptance. Verified unchanged from before this rule -- the same
    draft passes on the pre-change gate.
    """
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["merchant_id", "open_disputes"],
                rows=[["mer-dsp-001", "2"], ["mer-dsp-002", "5"]],
            )
        ]
    )
    verdict = check_grounding("mer-dsp-002 has open_disputes 2", corpus, "", profile=PROFILE_NG2)
    assert verdict.passed
    assert {"cell", "count"} <= {fact.kind for fact in corpus.numbers["2"]}


# ---------------------------------------------------------------------------
# NE-GROUND-AGGRESCUE, SPELLED: the number-word site obeys the SAME rule
#
# ``check_grounding`` grounds a value at two sites -- the digit ``_NumeralToken``
# branch and the spelled number-word ``_ProseToken`` branch (§2.4) -- which held
# byte-identical ``passing`` comprehensions. AGGRESCUE landed at the digit site
# ALONE, so the identical claim passed in words and refused in digits, and
# ``run_claim_verifier`` (hardcoded to ``PROFILE_NG2``) minted a
# ``DeterministicClaimVerification`` receipt on the spelled form. Both sites now
# call ``grounding._passing_number_facts``; these cases are parametrized over
# ("5", "five") so a future rule landing at one site only fails here.
#
# 5 is deliberately OUTSIDE ``1..len(rows)``: an in-range value is also a row
# ``ordinal`` (``column='*'``, whole-row anchors), and ``ordinal`` is not in
# ``_EXTREMUM_KINDS``, so it would rescue the claim and the refusal cases would
# silently prove nothing. It is also neither the row count (3) nor the column
# sum (21).
# ---------------------------------------------------------------------------


def _spelled_extremum_corpus() -> GroundingCorpus:
    """``open_disputes`` = 7 / 5 / 9 over mer-dsp-001 / -003 / -005, so 5 is
    mer-dsp-003's cell AND ``min(open_disputes)`` -- the AGGRESCUE shape, with a
    value every number-word in ``grounding_number_words.txt`` can spell."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["dispute_id", "status", "open_disputes"],
                rows=[
                    ["mer-dsp-001", "open", "7"],
                    ["mer-dsp-003", "open", "5"],
                    ["mer-dsp-005", "closed", "9"],
                ],
            )
        ]
    )


def test_the_spelled_corpus_is_the_aggrescue_shape_and_traps_nothing() -> None:
    """Anti-vacuity for the four cases below: the ``min`` fact IS minted (so a
    refusal is the rule biting, not an absent aggregate), and 5 is NOT a row
    ordinal, the row count, or the column sum -- any of which would ground the
    claim through a kind AGGRESCUE deliberately does not suppress."""
    corpus = _spelled_extremum_corpus()
    kinds = {(fact.kind, fact.column) for fact in corpus.numbers["5"]}
    assert ("min", "open_disputes") in kinds
    assert ("cell", "open_disputes") in kinds
    assert not any(fact.kind not in {"cell", "min"} for fact in corpus.numbers["5"])
    assert frozenset({"min", "max"}) == grounding._EXTREMUM_KINDS
    assert "five" in PROFILE_NG2.number_words and PROFILE_NG2.number_words["five"] == "5"


@pytest.mark.parametrize("value", ["5", "five"])
def test_an_extremum_no_longer_rescues_a_row_scoped_sentence_spelled(value: str) -> None:
    """THE P1. ``"mer-dsp-001 has open_disputes five"`` used to PASS while the
    digit form was correctly refused, because the number-word site ran the
    ``passing`` comprehension with no AGGRESCUE suppression after it. mer-dsp-001
    has 7; 5 is mer-dsp-003's cell and also ``min(open_disputes)``."""
    verdict = check_grounding(
        f"mer-dsp-001 has open_disputes {value}",
        _spelled_extremum_corpus(),
        "",
        profile=PROFILE_NG2,
    )
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)


@pytest.mark.parametrize("value", ["5", "five"])
def test_the_row_that_owns_the_extremum_still_states_it_spelled(value: str) -> None:
    """CONTROL: a TRUE attribution still grounds in spelled form. The fix must
    not be a blanket refusal of spelled numbers -- mer-dsp-003's own ``cell``
    fact grounds and is never suppressed."""
    verdict = check_grounding(
        f"mer-dsp-003 has open_disputes {value}",
        _spelled_extremum_corpus(),
        "",
        profile=PROFILE_NG2,
    )
    assert verdict.passed


@pytest.mark.parametrize("value", ["5", "five"])
def test_an_extremum_with_no_row_named_still_grounds_spelled(value: str) -> None:
    """CONTROL: the genuine ROWLESS aggregate reading survives in spelled form.
    With no row identity quoted the ``cell`` fact itself grounds -- its
    sibling-conflict check passes -- so ``passing`` still holds a cell fact and
    the suppression never triggers."""
    verdict = check_grounding(
        f"the open_disputes is {value}", _spelled_extremum_corpus(), "", profile=PROFILE_NG2
    )
    assert verdict.passed


@pytest.mark.parametrize("value", ["5", "five"])
def test_a_derived_aggregate_still_rescues_in_spelled_form(value: str) -> None:
    """THE RESIDUAL, mirrored: AGGRESCUE suppresses only ``min``/``max``, so a
    DERIVED aggregate whose value collides with a refused cell still rescues --
    identically in digits and in words. Here ``refund_count`` is 0 / 5 / 0, so 5
    is mer-dsp-003's cell, ``max(refund_count)`` AND ``sum(refund_count)``; the
    sentence names mer-dsp-001 (0), the cell fact is refused and the ``max`` is
    suppressed, but the ``sum`` grounds it.

    Deliberate and unchanged -- suppressing the derived kinds too would refuse
    the legitimate "row X is one of N" sentence. Pinned in spelled form so the
    shared helper is shown to preserve the residual rather than quietly widen the
    suppression at the newly-covered site."""
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                columns=["dispute_id", "refund_count"],
                rows=[["mer-dsp-001", "0"], ["mer-dsp-003", "5"], ["mer-dsp-005", "0"]],
            )
        ]
    )
    assert {fact.kind for fact in corpus.numbers["5"]} == {"cell", "sum", "max"}
    verdict = check_grounding(
        f"mer-dsp-001 has refund_count {value}", corpus, "", profile=PROFILE_NG2
    )
    assert verdict.passed


def test_ng1_frozen_never_reaches_the_number_word_site() -> None:
    """Why sharing the AGGRESCUE-bearing helper between both sites cannot move
    the FROZEN profile: ``PROFILE_NG1_FROZEN.number_words`` is empty, so
    ``nw_indices`` is always empty on ng1 and the number-word branch is dead
    code there. Its version therefore stays byte-identical to ``GATE_VERSION``
    (C10: a frozen profile's behaviour may not move under an unchanged version).
    """
    assert dict(PROFILE_NG1_FROZEN.number_words) == {}
    assert PROFILE_NG1_FROZEN.version == GATE_VERSION
    corpus = _spelled_extremum_corpus()
    # On ng1 "five" is plain prose, never a numeral: it is ``vocab-ungrounded``,
    # and no numeral GROUNDING decision is taken for it -- so the shared helper,
    # AGGRESCUE and all, is never called on its behalf. (The lone
    # ``numeral-unparseable`` is "mer-dsp-001": ng1 has no verbatim-compound
    # rule. Dropping the identifier leaves the vocab finding alone.)
    verdict = check_grounding(
        "mer-dsp-001 has open_disputes five", corpus, "", profile=PROFILE_NG1_FROZEN
    )
    assert not any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)
    assert check_grounding(
        "open_disputes five", corpus, "", profile=PROFILE_NG1_FROZEN
    ).findings == (GroundingFinding("vocab-ungrounded", 1),)


# ---------------------------------------------------------------------------
# NE-MERIDIAN-ASOF: a served as-of anchor is speakable; an unserved one is not
#
# The Meridian servicing templates carry the snapshot's ``as_of_date`` on every
# row, so a window's anchor date arrives as a served cell in the SAME row as
# the loan and payment ids it is asserted with. At THIS gate a date is not
# numeral-classified (``canonical_numeral`` -> None), so it grounds by verbatim
# containment: served -> speakable, unserved -> refused — even when the model
# itself SUPPLIED the very value as a template argument, because the arg-echo
# rule keeps model input from self-grounding. The in-row placement is for the
# citation side (one cited contributor row covers the anchor AND the facts
# asserted with it); the gate-side residual is pinned below. The dates here are
# arbitrary — the mechanism, not any one fixture's calendar, is what is pinned.
# ---------------------------------------------------------------------------


def _payments_window_answer(*, with_as_of: bool) -> dict[str, object]:
    columns = ["payment_id", "loan_id", "due_date", "amount_cents", "status"]
    row = ["mer-pay-007", "mer-loan-007", "2031-04-24", "35250", "scheduled"]
    if with_as_of:
        columns = [*columns, "as_of_date"]
        row = [*row, "2031-04-10"]
    return make_answer(
        template_id="payments-due-window",
        columns=columns,
        rows=[row],
        args={
            "q_loan_id": "mer-loan-007",
            "window_start": "2031-04-10",
            "window_end": "2031-04-24",
        },
    )


def test_a_served_as_of_anchor_is_speakable_in_a_row_claim() -> None:
    corpus = GroundingCorpus.from_answers([_payments_window_answer(with_as_of=True)])
    verdict = check_grounding(
        "the as_of_date for mer-loan-007 is 2031-04-10", corpus, "", profile=PROFILE_NG2
    )
    assert verdict.passed, verdict.findings


def test_an_unserved_as_of_anchor_refuses_even_when_the_model_supplied_it() -> None:
    """window_start/window_end are MODEL-supplied args, and an argument echo is
    dropped from the corpus — so before the anchor column existed, no sentence
    could state the window's anchor date at all (this is the exact live failure
    shape: a model that must invent the window's start can never say why). That
    refusal must stay a refusal whenever the served column is absent."""
    corpus = GroundingCorpus.from_answers([_payments_window_answer(with_as_of=False)])
    verdict = check_grounding(
        "the as_of_date for mer-loan-007 is 2031-04-10", corpus, "", profile=PROFILE_NG2
    )
    assert not verdict.passed
    assert {finding.reason for finding in verdict.findings} & {
        "numeral-unparseable",
        "vocab-ungrounded-factual",
    }


def test_a_date_cell_is_not_row_bound_at_this_gate_a_declared_residual() -> None:
    """MEASURED and pinned rather than assumed: a date is not numeral-classified,
    so NE-GROUND-ROWID's row binding never applies to it — an as-of date served
    on a DEDICATED snapshot row still grounds when spoken next to a loan id.
    The gate alone therefore cannot force an anchor into the loan's own row;
    what does is the CITATION side (a claim's cited contributor row should
    carry the values asserted with it), which is why the Meridian projection
    puts ``as_of_date`` on every servicing row rather than on a snapshot row
    nothing else shares. Closing this gate-side residual means
    numeral-classifying dates — a deliberate rulebook change, never a quiet
    one."""
    snapshot_answer = make_answer(
        template_id="servicing-snapshot",
        columns=["snapshot_scope", "as_of_date"],
        rows=[["servicing", "2031-04-10"]],
    )
    corpus = GroundingCorpus.from_answers(
        [_payments_window_answer(with_as_of=False), snapshot_answer]
    )
    verdict = check_grounding(
        "the as_of_date for mer-loan-007 is 2031-04-10", corpus, "", profile=PROFILE_NG2
    )
    assert verdict.passed, verdict.findings


# ---------------------------------------------------------------------------
# NE-MERIDIAN-DOLLARS: a served dollars cell makes a dollar phrase speakable
#
# F2 makes a currency symbol MEANING-BEARING: ``$352.50`` canonicalizes to the
# DISTINCT token ``$352.5``, which grounds ONLY against a corpus cell that
# itself canonicalizes to it — and the gate has, BY DESIGN, no currency
# recompute (a recompute would let a model relabel any plain grounded number as
# money). The Meridian monetary templates therefore serve a ``*_dollars``
# column beside each ``*_cents`` system-of-record column: the exact fixed-point
# rendering (``$X.YY``) of the same integer. These tests run the REAL gate over
# that construction: served -> speakable (in any equal-valued ``$`` spelling,
# since both sides pass through ``canonical_numeral``); unserved, wrong-figure,
# near-miss and wrong-row all refuse. The amounts here are arbitrary — the
# mechanism, not any one fixture's balances, is what is pinned.
# ---------------------------------------------------------------------------


def _payments_dollars_answer(*, with_dollars: bool) -> dict[str, object]:
    columns = ["payment_id", "loan_id", "due_date", "amount_cents", "status"]
    rows = [
        ["mer-pay-007", "mer-loan-007", "2031-04-24", "35250", "scheduled"],
        ["mer-pay-008", "mer-loan-008", "2031-04-25", "41500", "scheduled"],
    ]
    if with_dollars:
        columns = [*columns, "amount_dollars"]
        rows = [
            [*rows[0], "$352.50"],
            [*rows[1], "$415.00"],
        ]
    return make_answer(
        template_id="payments-due-window",
        columns=columns,
        rows=rows,
        args={
            "q_loan_id": "mer-loan-007",
            "window_start": "2031-04-10",
            "window_end": "2031-04-30",
        },
    )


def test_a_served_dollars_cell_grounds_the_dollar_phrase_in_a_row_claim() -> None:
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=True)])
    verdict = check_grounding(
        "the amount_dollars for mer-pay-007 is $352.50", corpus, "", profile=PROFILE_NG2
    )
    assert verdict.passed, verdict.findings


def test_equal_valued_dollar_spellings_ground_against_the_same_served_cell() -> None:
    """Both sides pass through ``canonical_numeral`` (``$352.50`` and ``$352.5``
    -> ``$352.5``; ``$415.00``/``$415`` -> ``$415``), so grounding is by exact
    VALUE-plus-symbol, not by byte spelling — trailing zeros and digit-group
    separators never decide."""
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=True)])
    for draft in (
        "the amount_dollars for mer-pay-007 is $352.5",
        "the amount_dollars for mer-pay-008 is $415",
        "the amount_dollars for mer-pay-008 is $415.00",
    ):
        verdict = check_grounding(draft, corpus, "", profile=PROFILE_NG2)
        assert verdict.passed, (draft, verdict.findings)


def test_without_the_served_dollars_cell_a_dollar_rendering_of_cents_refuses() -> None:
    """THE constraint this whole feature answers, pinned so nobody 'fixes' it by
    teaching the gate to convert: the cents cell serves plain ``35250``, and no
    currency recompute exists, so ``$352.50`` — the TRUE dollar value of that
    very cell — refuses whenever no served cell says it. The fix is serving a
    real cell, never a gate-side conversion."""
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=False)])
    verdict = check_grounding(
        "the amount_dollars for mer-pay-007 is $352.50", corpus, "", profile=PROFILE_NG2
    )
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)


@pytest.mark.parametrize(
    "draft",
    [
        # a dollar figure the table nowhere carries.
        "the amount_dollars for mer-pay-007 is $999.00",
        # near-miss: one cent off the served $352.50.
        "the amount_dollars for mer-pay-007 is $352.51",
        # relabeling: the served CENTS integer spoken as dollars.
        "the amount_dollars for mer-pay-007 is $35250",
    ],
)
def test_a_wrong_or_near_miss_dollar_figure_still_refuses(draft: str) -> None:
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=True)])
    verdict = check_grounding(draft, corpus, "", profile=PROFILE_NG2)
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)


def test_a_dollars_cell_is_row_bound_unlike_a_date_cell() -> None:
    """A currency token IS numeral-classified, so — unlike the as-of date's
    declared residual above — NE-GROUND-ROWID binds a served dollars cell to
    its own row: another row's true amount attributed to the quoted row
    refuses at the GATE, not merely citation-side."""
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=True)])
    verdict = check_grounding(
        "the amount_dollars for mer-pay-008 is $352.50", corpus, "", profile=PROFILE_NG2
    )
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)


def test_a_served_dollars_column_mints_no_currency_aggregate() -> None:
    """$352.50 + $415.00 = $767.50 is a true fact about the served rows that the
    gate deliberately has no fact for: sum/min/max mint only over columns whose
    every cell parses as a PLAIN numeral, and a currency cell is not plain.
    Pinned as a declared limit — widening it is a rulebook change, never a
    quiet one."""
    corpus = GroundingCorpus.from_answers([_payments_dollars_answer(with_dollars=True)])
    verdict = check_grounding(
        "the amount_dollars total is $767.50", corpus, "", profile=PROFILE_NG2
    )
    assert not verdict.passed
    assert any(finding.reason == "numeral-ungrounded" for finding in verdict.findings)
