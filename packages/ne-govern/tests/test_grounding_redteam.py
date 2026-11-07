# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-4 RED-TEAM acceptance suite (the truncation-honesty enabler).

This is the acceptance bar Layer 3 / NE-CTXB-7 is blocked on. It is written
FIRST (TDD): before the ``grounding.py`` extension lands, the intended-PASS
tests are RED. After it lands they are GREEN, while every wrong-row,
wrong-count, pooled-total, foreign-envelope, unseen-row and degenerate-
identity sentence stays REFUSED with ``numeral-ungrounded``.

Two grounding surfaces are exercised on realistic, URN-bearing, shared-suffix
(Corp/Inc/Ltd) data:

* **Envelope facts** — ``returned_rows``/``total_rows``/``row_limit`` mint a
  ``NumberFact(kind="envelope")`` ALWAYS (including when the answer is
  truncated — that is the point), anchored F3-style to the template-id +
  kept-column vocabulary, so ``"Returned 37 of 5,000,000 accounts"`` grounds
  while a pooled/foreign use of the same numeral does not.
* **Aggregate-cell identity** — a ``cell`` numeral's own-row identity can now
  be satisfied by full containment of ANY single non-numeric, non-IRI sibling
  cell's COMPLETE token set (e.g. the name cell ``{acme, corp}``), so the
  Layer-3 count sentence ``"Acme Corp has 17 placed orders"`` grounds even
  though its URN citation cell tokens are never spoken. The preserved Codex-P1
  hardening (wrong-row full-containment + foreign-sibling refusal) still fires.

Sentence-wording discipline (brief risk #3): every draft is worded ONLY from
corpus tokens + the ``CONNECTIVE_ALLOWLIST`` + the test's prompt.
``"returned"``/``"found"``/``"of"``/``"has"``/``"directory"`` are on the
allowlist; ``"placed"``/``"limit"`` come from the prompt; account/name/orders
vocabulary comes from the fixtures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ne.govern.grounding import GroundingCorpus, check_grounding


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
    """Local ``VerifiedAnswer``-shaped builder (tests are not a package, so we
    do NOT import ``test_grounding.make_answer``). Deliberately does NOT inject
    a default ``row_limit`` — envelope keys are opt-in per fixture so each
    test's derived-fact inventory stays exactly what its comment states."""
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


# --- FIXTURE A: Layer-3 aggregate shape (untruncated) ----------------------
# columns = account (URN cell + citation), name (shared Corp/Inc/Ltd suffix),
# n_orders (the aggregate cell). Derived-fact inventory the tests dodge:
#   cells {17, 9, 23, 6, 12}; ordinals 1..5; count 5; distinct-count 5;
#   freq/pairfreq 1; sum 67; min 6; max 23; envelope returned_rows 5.
_ACCT_URNS_A = [
    "urn:ne:entity:acct-acme",
    "urn:ne:entity:acct-beta",
    "urn:ne:entity:acct-gamma",
    "urn:ne:entity:acct-delta",
    "urn:ne:entity:acct-omega",
]


def _fixture_a() -> dict[str, object]:
    return make_answer(
        template_id="accounts-orders-count",
        columns=["account", "name", "n_orders"],
        rows=[
            [_ACCT_URNS_A[0], "Acme Corp", "17"],
            [_ACCT_URNS_A[1], "Beta Corp", "9"],
            [_ACCT_URNS_A[2], "Gamma Inc", "23"],
            [_ACCT_URNS_A[3], "Delta Inc", "6"],
            [_ACCT_URNS_A[4], "Omega Ltd", "12"],
        ],
        citations=list(_ACCT_URNS_A),
    )


# --- FIXTURE B: truncated directory ----------------------------------------
# 3 returned rows out of 5,000,000; row_limit 500. Envelope facts mint ALWAYS
# despite truncated=True (aggregates are excluded, envelope numerals are not).
_ACCT_URNS_B = [
    "urn:ne:entity:acct-acme",
    "urn:ne:entity:acct-beta",
    "urn:ne:entity:acct-gamma",
]


def _fixture_b() -> dict[str, object]:
    return make_answer(
        template_id="accounts-directory",
        columns=["account", "name"],
        rows=[
            [_ACCT_URNS_B[0], "Acme Corp"],
            [_ACCT_URNS_B[1], "Beta Corp"],
            [_ACCT_URNS_B[2], "Gamma Inc"],
        ],
        citations=list(_ACCT_URNS_B),
        truncated=True,
        returned_rows=3,
        total_rows=5000000,
        row_limit=500,
    )


# ===========================================================================
# (1) Layer-3 count sentence PASSES via single-cell identity.
# ===========================================================================


def test_layer3_count_sentence_passes() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_a()])
    prompt = "how many placed orders does acme corp have"

    # 'Acme Corp has 17 placed orders': own URN-inclusive row identity is NOT
    # fully contained (urn/ne/entity/acct are never spoken), but the single
    # name-cell identity {acme, corp} IS -> the numeral 17 grounds.
    verdict = check_grounding("Acme Corp has 17 placed orders", corpus, prompt)
    assert verdict.passed
    assert verdict.findings == ()

    # 'Gamma Inc has 23 placed orders' grounds via the {gamma, inc} single-cell
    # identity. (23 also equals max(n_orders); the max fact would ground it too
    # via the shared 'orders' anchor, but the single-cell identity is the rule
    # under test and is sufficient on its own.)
    gamma = check_grounding("Gamma Inc has 23 placed orders", corpus, prompt)
    assert gamma.passed
    assert gamma.findings == ()


# ===========================================================================
# (2) Wrong-row count REFUSES (Codex-P1 trace preserved on URN+suffix data).
# ===========================================================================


def test_wrong_row_count_refuses() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_a()])
    prompt = "how many placed orders does each account have"

    # 'Beta Corp has 17 placed orders': 17 is Acme's cell. Own row identity not
    # contained; the single-cell identity {acme, corp} is not contained (acme
    # absent); the foreign-sibling scan fires on Beta's identity sharing 'corp'
    # -> refuse.
    beta17 = check_grounding("Beta Corp has 17 placed orders", corpus, prompt)
    assert not beta17.passed
    assert any(f.reason == "numeral-ungrounded" for f in beta17.findings)

    # 'Acme Corp has 9 placed orders': 9 is Beta's cell. Same trace, mirrored.
    acme9 = check_grounding("Acme Corp has 9 placed orders", corpus, prompt)
    assert not acme9.passed
    assert any(f.reason == "numeral-ungrounded" for f in acme9.findings)


# ===========================================================================
# (3) Wrong-count REFUSES (numeral matches no fact).
# ===========================================================================


def test_wrong_count_refuses() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_a()])
    prompt = "how many placed orders does acme corp have"
    # 18 is neither any cell, ordinal, count, sum, min, max, nor envelope.
    verdict = check_grounding("Acme Corp has 18 placed orders", corpus, prompt)
    assert not verdict.passed
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)


# ===========================================================================
# (4) Unseen row REFUSES (vocab- AND numeral-ungrounded).
# ===========================================================================


def test_unseen_row_refuses() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_a()])
    prompt = "how many placed orders does each account have"
    verdict = check_grounding("Epsilon Corp has 11 placed orders", corpus, prompt)
    assert not verdict.passed
    reasons = {f.reason for f in verdict.findings}
    assert "vocab-ungrounded" in reasons  # 'epsilon' is unseen
    assert "numeral-ungrounded" in reasons  # 11 matches no fact


# ===========================================================================
# (5) Truncated-envelope sentence PASSES (the sentence class withheld today).
# ===========================================================================


def test_envelope_truncated_pass() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_b()])

    # comma-separated form (separator canonicalization) ...
    commas = check_grounding("Returned 3 of 5,000,000 accounts", corpus, prompt="")
    assert commas.passed
    assert commas.findings == ()

    # ... and the bare form both ground (3 = returned_rows, 5000000 = total_rows).
    plain = check_grounding("returned 3 of 5000000 accounts", corpus, prompt="")
    assert plain.passed
    assert plain.findings == ()

    # row_limit grounds with 'limit' supplied by the prompt (numerals are never
    # prompt-exempt; the numeral still needs its envelope anchor in-sentence).
    limit = check_grounding(
        "the accounts directory limit is 500",
        corpus,
        prompt="what limit applies to the accounts directory",
    )
    assert limit.passed
    assert limit.findings == ()


# ===========================================================================
# (6) Pooled / foreign envelope numerals REFUSE.
# ===========================================================================


def test_envelope_pooled_and_foreign_refuse() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_a(), _fixture_b()])

    # 5 (A returned) + 3 (B returned) pooled -> 8 is no fact.
    pooled_count = check_grounding("returned 8 accounts", corpus, prompt="")
    assert not pooled_count.passed
    assert any(f.reason == "numeral-ungrounded" for f in pooled_count.findings)

    # 5,000,000 (B total) + 500 (B limit) pooled -> 5000500 is no fact.
    pooled_sum = check_grounding("found 5000500 accounts", corpus, prompt="")
    assert not pooled_sum.passed
    assert any(f.reason == "numeral-ungrounded" for f in pooled_sum.findings)

    # 500 is B's row_limit; spoken in A's vocabulary ('orders'), the envelope
    # anchor {accounts, directory, account, name} does not intersect {orders}.
    foreign = check_grounding("returned 500 orders", corpus, prompt="")
    assert not foreign.passed
    assert any(f.reason == "numeral-ungrounded" for f in foreign.findings)


# ===========================================================================
# (7) Envelope numeral needs a vocab anchor (allowlist-only prose refuses).
# ===========================================================================


def test_envelope_requires_vocab_anchor() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_b()])
    # 'returned' is allowlist-only (never a corpus-grounded prose token), so
    # the sentence carries no anchor for 500 -> refuse.
    verdict = check_grounding("returned 500", corpus, prompt="")
    assert not verdict.passed
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)


# ===========================================================================
# (8) Degenerate identity REFUSES; the true row still PASSES.
# ===========================================================================
# Realistic shape (URN account column + shared 'Corp' suffix). The URN cell
# keeps the own-row full-containment branch from short-circuiting (its tokens
# are never spoken), so grounding must go through the single-cell identity --
# which is exactly what the degenerate filter governs. Amount values 111/222
# are MIDDLE values (min=50, max=999) so no min/max aggregate collides on the
# numeral under test (mirrors the Codex-P1 middle-value technique).


def _fixture_degenerate() -> dict[str, object]:
    return make_answer(
        template_id="company-amounts",
        columns=["account", "company", "amount"],
        rows=[
            ["urn:ne:entity:acct-corp", "Corp", "111"],  # {corp} subset of {beta,corp}
            ["urn:ne:entity:acct-beta", "Beta Corp", "222"],
            ["urn:ne:entity:acct-alpha", "Alpha Ltd", "50"],  # min
            ["urn:ne:entity:acct-omega", "Omega Ltd", "999"],  # max
        ],
    )


def test_degenerate_identity_cell_refuses() -> None:
    corpus = GroundingCorpus.from_answers([_fixture_degenerate()])
    prompt = "what amount does the company have"

    # 111 is the 'Corp' row's cell. Its single-cell identity {corp} is a subset
    # of {beta, corp} -> filtered as degenerate/ambiguous -> no identity anchor.
    # Naming the WRONG row (beta) then hits the foreign-sibling refusal.
    beta_wrong = check_grounding("beta corp has amount 111", corpus, prompt)
    assert not beta_wrong.passed
    assert any(f.reason == "numeral-ungrounded" for f in beta_wrong.findings)

    # Even naming ONLY 'corp' fails: the degenerate identity gives no anchor,
    # and Beta's sibling identity shares 'corp' -> foreign-sibling refusal.
    corp_only = check_grounding("corp has amount 111", corpus, prompt)
    assert not corp_only.passed
    assert any(f.reason == "numeral-ungrounded" for f in corp_only.findings)

    # The true row/number pairing grounds via the usable {beta, corp} identity.
    beta_true = check_grounding("beta corp has amount 222", corpus, prompt)
    assert beta_true.passed
    assert beta_true.findings == ()


# ===========================================================================
# (9) Declared residual: envelope row-misattribution is NOT detectable.
# ===========================================================================


def test_envelope_misattribution_residual_risk_pinned() -> None:
    """DECLARED ACCEPTED RESIDUAL (pinned so future hardening flips it
    consciously): F3-style template+column anchoring cannot see row
    misattribution of an envelope numeral. 'acme corp returned 500 accounts'
    currently PASSES because 500's envelope anchor intersects the sentence's
    grounded vocabulary regardless of which row identity is named -- the same
    granularity class as the pre-existing count-fact behavior. If a future unit
    hardens envelope row-binding, this test must flip DELIBERATELY."""
    corpus = GroundingCorpus.from_answers([_fixture_b()])
    residual = check_grounding("acme corp returned 500 accounts", corpus, prompt="")
    assert residual.passed
