# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GROUND-REL — semantic row binding for prose relations (red-first suite).

The defect (U11 adversarial review, SPEC open-defect entry): the gate proved
every token's PROVENANCE and bound numerals/IRIs/citations to their own rows,
but a *relation asserted between fetched entities* was never checked against a
fetched row — over rows ``Alice/open`` + ``Bob/closed`` the draft
``"Alice is closed."`` yielded ZERO findings at every tier. These tests were
written RED against that gate: the reproduction must WITHHOLD and the matching
true statement must still PASS, so the guard discriminates rather than
blanket-refusing.

The mechanism under test (``relation-ungrounded``, ng-2 only, a FACT class —
withholds at EVERY tier): a corpus-grounded prose token that comes only from
row cells (not schema vocabulary, not the prompt, not the allowlists) must be
row-supported in its sentence — by a co-row partner token, a named row (quoted
row identifier / spoken row IRI), a same-column enumeration partner, or the
anchor set of ONE fact that grounded a numeral in the same sentence. A lone
row mention asserts no relation and stays free.

Documented residuals pinned here (bag-of-tokens limits, disclosed in SPEC):
a sentence naming BOTH rows and BOTH values passes even when swapped (the
numeral row-binding shares this exact limit), and prompt echo remains
vocabulary-exempt (A-P2-6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from ne.govern.grounding import (
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    GroundingCorpus,
    check_grounding,
)
from ne.govern.grounding_policy import StrictnessTier, apply_strictness

STRICT = StrictnessTier.STRICT
STANDARD = StrictnessTier.STANDARD


def make_answer(
    *,
    template_id: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    citations: Sequence[str] = (),
    truncated: bool = False,
    args: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Local ``VerifiedAnswer``-shaped builder (tests are not a package)."""
    return {
        "template_id": template_id,
        "template_version": 1,
        "config_hash": "cfg",
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "citations": list(citations),
        "truncated": truncated,
        "args": dict(args) if args is not None else {},
    }


def _tickets_corpus() -> GroundingCorpus:
    """THE reproduction corpus: Alice/open + Bob/closed."""
    return GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="tickets-directory",
                columns=["customer", "status"],
                rows=[["Alice", "open"], ["Bob", "closed"]],
                citations=["urn:ne:entity:ticket-a", "urn:ne:entity:ticket-b"],
            )
        ]
    )


def _reasons(draft: str, corpus: GroundingCorpus, prompt: str = "") -> set[str]:
    verdict = check_grounding(draft, corpus, prompt, profile=PROFILE_NG2)
    return {finding.reason for finding in verdict.findings}


def _passes(draft: str, corpus: GroundingCorpus, prompt: str = "") -> bool:
    return check_grounding(draft, corpus, prompt, profile=PROFILE_NG2).passed


# ---------------------------------------------------------------------------
# THE reproduction: red before, green after.
# ---------------------------------------------------------------------------


def test_relational_recombination_withheld_at_every_tier() -> None:
    """``"Alice is closed."`` over Alice/open + Bob/closed: every token has
    provenance, but the asserted relation is supported by NO fetched row —
    the gate must refuse, and the refusal must be a FACT class (withholds at
    STRICT and STANDARD alike)."""
    corpus = _tickets_corpus()
    verdict = check_grounding("Alice is closed.", corpus, "", profile=PROFILE_NG2)
    assert not verdict.passed
    assert {finding.reason for finding in verdict.findings} == {"relation-ungrounded"}
    for tier in (STRICT, STANDARD):
        decision = apply_strictness(verdict, tier)
        assert decision.withhold, tier
        assert any(finding.reason == "relation-ungrounded" for finding in decision.enforced)


def test_matching_true_statement_still_passes() -> None:
    """The guard must discriminate: ``"Alice is open."`` is exactly what the
    rows state and must pass — a blanket refusal would be its own defect."""
    corpus = _tickets_corpus()
    assert _passes("Alice is open.", corpus)
    assert _passes("Bob is closed.", corpus)


def test_ng1_frozen_profile_is_byte_identical() -> None:
    """The frozen profile must NOT change behavior: the reproduction still
    passes under ng1 (the defect is closed in ng-2, the deployed rulebook;
    ng1-frozen is the pinned zero-behavior-change surface)."""
    corpus = _tickets_corpus()
    verdict = check_grounding("Alice is closed.", corpus, "", profile=PROFILE_NG1_FROZEN)
    assert verdict.passed


# ---------------------------------------------------------------------------
# The guard discriminates on realistic prose (must-keep-passing shapes).
# ---------------------------------------------------------------------------


def test_compound_true_sentence_passes() -> None:
    corpus = _tickets_corpus()
    assert _passes("Alice is open and Bob is closed.", corpus)


def test_lone_row_mention_passes() -> None:
    """Mentioning a fetched entity (or a fetched value) alone asserts no
    entity-value relation and must not withhold."""
    corpus = _tickets_corpus()
    assert _passes("Alice is in the directory.", corpus)
    assert _passes("One of the tickets is closed.", corpus)


def test_same_column_enumeration_passes() -> None:
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "segment"],
                rows=[["Acme Corp", "enterprise"], ["Globex Inc", "startup"]],
                citations=["urn:ne:entity:acme", "urn:ne:entity:globex"],
            )
        ]
    )
    # Two rows' identities enumerated in one sentence is not a relation claim.
    assert _passes("The accounts are Acme Corp and Globex Inc.", corpus)


def test_aggregate_anchored_value_passes_and_wrong_value_refuses() -> None:
    """The join shape: an entity from one answer + a status from another,
    glued by an aggregate the gate itself recomputed. ``freq(status=open)=3``
    proves the pairing (open, 3); the same sentence with the WRONG status
    refuses on the numeral AND the relation."""
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "segment"],
                rows=[["Acme Corp", "enterprise"]],
                citations=["urn:ne:entity:acme"],
            ),
            make_answer(
                template_id="tickets-by-account",
                columns=["ticket", "state"],
                rows=[["T-100", "open"], ["T-101", "open"], ["T-102", "open"], ["T-103", "held"]],
                citations=[
                    "urn:ne:entity:t100",
                    "urn:ne:entity:t101",
                    "urn:ne:entity:t102",
                    "urn:ne:entity:t103",
                ],
            ),
        ]
    )
    assert _passes("3 of the tickets for Acme Corp are open.", corpus)
    # The wrong status: the digit may still ground through an unrelated fact
    # (ordinal-3 anchors on the shared 'ticket' vocabulary), but the RELATION
    # (acme/held) is supported by no row and no recomputed aggregate.
    assert "relation-ungrounded" in _reasons("3 of the tickets for Acme Corp are held.", corpus)


def test_list_marker_ordinal_cannot_rescue_the_recombination() -> None:
    """``"1. Alice is closed"`` — the list-marker digit grounds (ordinal /
    freq facts), but no SINGLE grounding fact covers both ``alice`` and
    ``closed``; a stitched cover across facts must not rescue the relation."""
    corpus = _tickets_corpus()
    assert _reasons("1. Alice is closed", corpus) == {"relation-ungrounded"}
    # The honest list items still pass.
    assert _passes("1. Alice is open", corpus)
    assert _passes("2. Bob is closed", corpus)


def test_quoted_identifier_binds_prose_values_to_their_own_row() -> None:
    """NE-GROUND-ROWID generalized to prose: a compound row identifier quoted
    verbatim names its row; a value carried by a DIFFERENT row does not
    attach to it."""
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="merchant-disputes",
                columns=["merchant", "resolution"],
                rows=[["mer-dsp-001", "approved"], ["mer-dsp-003", "rejected"]],
                citations=["urn:ne:entity:m1", "urn:ne:entity:m3"],
            )
        ]
    )
    assert _passes("mer-dsp-003 was rejected.", corpus)
    assert _reasons("mer-dsp-001 was rejected.", corpus) == {"relation-ungrounded"}


def test_cross_answer_recombination_refuses() -> None:
    """An entity fetched by one template + a value fetched by another, with no
    row or recomputed aggregate connecting them, is an unverifiable relation."""
    corpus = GroundingCorpus.from_answers(
        [
            make_answer(
                template_id="accounts-directory",
                columns=["account", "segment"],
                rows=[["Acme Corp", "enterprise"]],
                citations=["urn:ne:entity:acme"],
            ),
            make_answer(
                template_id="billing-flags",
                columns=["invoice", "flag"],
                rows=[["INV-9", "delinquent"]],
                citations=["urn:ne:entity:inv9"],
            ),
        ]
    )
    assert _reasons("Acme Corp is delinquent.", corpus) == {"relation-ungrounded"}


def test_schema_vocabulary_stays_free() -> None:
    """Column/template vocabulary is not a row claim: speaking it next to a
    row identity must not trip the binding."""
    corpus = _tickets_corpus()
    assert _passes("The status for Alice is open.", corpus)
    assert _passes("The customer for this ticket is Alice.", corpus)


def test_prompt_echo_residual_pinned() -> None:
    """A-P2-6 (disclosed, unchanged): prompt vocabulary stays exempt, so a
    question that embeds its own answer is affirmable from the question's
    words. Pinned so any future change to prompt exemption is conscious."""
    corpus = _tickets_corpus()
    assert _passes("Alice is closed.", corpus, prompt="Is Alice closed?")


def test_swapped_compound_residual_pinned() -> None:
    """DISCLOSED residual (shared with the numeral row-binding): a sentence
    naming BOTH rows and BOTH values is a bag-of-tokens fixed point — the
    swapped false compound passes. Pinned so future hardening flips it
    consciously, never silently."""
    corpus = _tickets_corpus()
    assert _passes("Alice is closed and Bob is open.", corpus)


def test_empty_corpus_allowlist_only_assertion_still_passes_gate_level() -> None:
    """Reproduction (2) is mitigated on the RUN PATH by the non-empty-evidence
    invariant (NE-GA-RUN5), not here: an allowlist-only sentence produces no
    findings at gate level, and the connective allowlist is load-bearing and
    not pruned. Pinned so the division of labor stays conscious."""
    corpus = GroundingCorpus.from_answers([])
    assert _passes("The record is verified.", corpus)


@pytest.mark.parametrize(
    "draft",
    [
        "Alice is closed.",
        "Alice is open.",
        "1. Alice is closed",
        "The status for Alice is open.",
    ],
)
def test_relational_binding_is_deterministic(draft: str) -> None:
    corpus = _tickets_corpus()
    first = check_grounding(draft, corpus, "", profile=PROFILE_NG2)
    for _ in range(3):
        assert check_grounding(draft, corpus, "", profile=PROFILE_NG2) == first
