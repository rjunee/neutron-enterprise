# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import ne.runtime.run_claim_verifier as verifier_module
from ne.govern.grounding import (
    GateProfile,
    GroundingCorpus,
    GroundingFinding,
    GroundingVerdict,
)
from ne.runtime.run_claim_verifier import (
    CLAIM_VERIFIER_METHOD,
    CLAIM_VERIFIER_VERSION,
    ClaimSlice,
    ClaimVerificationUnavailable,
    DirectSearchEvidenceObservation,
    InferredGraphPathEvidenceObservation,
    verify_claim,
)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _claim(output: str, text: str, *, start: int | None = None, key: str = "claim-a") -> ClaimSlice:
    raw = output.encode()
    selected = text.encode()
    begin = raw.index(selected) if start is None else start
    return ClaimSlice(
        claim_key=key,
        claim_content_hash=_hash(selected),
        span_start_byte=begin,
        span_end_byte=begin + len(selected),
        served_output_hash=_hash(raw),
        served_output=raw,
    )


def _search(**changes: object) -> DirectSearchEvidenceObservation:
    rendering = (
        b"[showing 2 of 2 retrieved chunks]\n"
        b"chunk-a: Acme balance is 42\n"
        b"chunk-b: Meridian status is current"
    )
    locator = json.dumps(
        {
            "chunk_ids": ["chunk-a", "chunk-b"],
            "contributors": [{"server_stamp": "stamp-a"}],
            "evidence_assurance": "direct",
            "kind": "knowledge_search",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fields: dict[str, object] = {
        "protected_ref": "ne-source-ref:v1:search-a",
        "protected_result_hash": _hash(rendering),
        "evidence_id": "evidence-a",
        "locator_id": "locator-a",
        "locator_hash": _hash(locator),
        "producing_event_id": "event-a",
        "delivery_receipt_id": "delivery-a",
        "delivery_receipt_hash": "d" * 64,
        "protected_rendering": rendering,
        "locator": locator,
    }
    fields.update(changes)
    return DirectSearchEvidenceObservation.model_validate(fields)


def _graph(**changes: object) -> InferredGraphPathEvidenceObservation:
    path = {
        "hops": [
            {
                "assurance": "source",
                "edge_kind": "services",
                "obj": {
                    "assurance": "source",
                    "display": "Meridian",
                    "entity_class": "customer",
                    "entity_key": "meridian",
                    "source_ref": "document-b",
                },
                "source_ref": "edge-document",
                "source_system": "m365",
                "subject": {
                    "assurance": "source",
                    "display": "Acme",
                    "entity_class": "provider",
                    "entity_key": "acme",
                    "source_ref": "document-a",
                },
            }
        ]
    }
    rendering = json.dumps(path, sort_keys=True, separators=(",", ":")).encode()
    locator = json.dumps(
        {
            "evidence_assurance": "inferred",
            "hop_source_stamps": [[{"server_stamp": "stamp-a"}]],
            "kind": "knowledge_graph_path",
            "path": path,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fields: dict[str, object] = {
        "protected_ref": "ne-source-ref:v1:graph-a",
        "protected_result_hash": _hash(rendering),
        "evidence_id": "evidence-graph",
        "locator_id": "locator-graph",
        "locator_hash": _hash(locator),
        "producing_event_id": "event-graph",
        "delivery_receipt_id": "delivery-graph",
        "delivery_receipt_hash": "e" * 64,
        "protected_rendering": rendering,
        "locator": locator,
    }
    fields.update(changes)
    return InferredGraphPathEvidenceObservation.model_validate(fields)


def test_direct_search_verifies_exact_claim_and_binds_every_server_identity() -> None:
    claim = _claim("Summary: Acme balance is 42.", "Acme balance is 42")
    observation = _search()

    first = verify_claim(claim, observation)
    second = verify_claim(claim, observation)

    assert first == second
    assert first.verifier_method == CLAIM_VERIFIER_METHOD
    assert first.verifier_version == CLAIM_VERIFIER_VERSION
    assert first.claim_key == claim.claim_key
    assert first.claim_content_hash == claim.claim_content_hash
    assert first.protected_ref == observation.protected_ref
    assert first.evidence_id == observation.evidence_id
    assert first.locator_id == observation.locator_id
    assert first.producing_event_id == observation.producing_event_id
    assert first.delivery_receipt_id == observation.delivery_receipt_id


def test_graph_path_requires_byte_equal_locator_and_projects_canonical_narration() -> None:
    result = verify_claim(_claim("Acme services Meridian", "Acme services Meridian"), _graph())
    assert result.evidence_id == "evidence-graph"

    observation = _graph()
    locator = json.loads(observation.locator)
    locator["path"]["hops"][0]["edge_kind"] = "owns"
    altered = json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ClaimVerificationUnavailable, match="path and locator mismatch"):
        verify_claim(
            _claim("Acme services Meridian", "Acme services Meridian"),
            observation.model_copy(update={"locator": altered, "locator_hash": _hash(altered)}),
        )


def test_empty_prompt_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    real_check = verifier_module.check_grounding  # pyright: ignore[reportPrivateImportUsage]

    def observed_check(
        draft: str, corpus: GroundingCorpus, prompt: str, *, profile: GateProfile
    ) -> GroundingVerdict:
        prompts.append(prompt)
        return real_check(draft, corpus, prompt, profile=profile)

    monkeypatch.setattr(verifier_module, "check_grounding", observed_check)
    observation = _search()
    verify_claim(_claim("Acme balance is 42", "Acme balance is 42"), observation)
    assert prompts == [""]
    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim("Fabricated statement", "Fabricated statement"), observation)


def test_unsupported_numeral_fact_refuses() -> None:
    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(
            _claim("Acme balance is 99", "Acme balance is 99"),
            _search(),
        )


def test_assurance_and_read_kind_are_closed_and_cannot_be_swapped() -> None:
    with pytest.raises(ValidationError):
        _search(assurance="inferred")
    with pytest.raises(ValidationError):
        _graph(assurance="direct")
    with pytest.raises(ValidationError):
        DirectSearchEvidenceObservation.model_validate(
            {**_search().model_dump(), "unexpected": "field"}
        )


def test_result_hash_binds_evidence_and_delivery_identity() -> None:
    claim = _claim("Acme balance is 42", "Acme balance is 42")
    baseline = verify_claim(claim, _search())
    assert (
        verify_claim(claim, _search(evidence_id="evidence-b")).result_hash != baseline.result_hash
    )
    assert (
        verify_claim(claim, _search(delivery_receipt_id="delivery-b")).result_hash
        != baseline.result_hash
    )


def test_repeated_identical_claims_are_distinguished_by_exact_utf8_span() -> None:
    text = "Acme balance is 42; Acme balance is 42"
    first = _claim(text, "Acme balance is 42")
    second_start = text.encode().rindex(b"Acme balance is 42")
    second = _claim(text, "Acme balance is 42", start=second_start)
    observation = _search()

    assert (
        verify_claim(first, observation).result_hash
        != verify_claim(second, observation).result_hash
    )


def test_hashes_utf8_boundaries_order_and_locator_shape_fail_closed() -> None:
    claim = _claim("Acme balance is 42", "Acme balance is 42")
    observation = _search()
    with pytest.raises(ClaimVerificationUnavailable, match="rendering hash"):
        verify_claim(claim, observation.model_copy(update={"protected_result_hash": "0" * 64}))
    with pytest.raises(ClaimVerificationUnavailable, match="claim content hash"):
        verify_claim(claim.model_copy(update={"claim_content_hash": "0" * 64}), observation)

    reordered = observation.protected_rendering.replace(b"chunk-a", b"chunk-z", 1)
    with pytest.raises(ClaimVerificationUnavailable, match="chunk order"):
        verify_claim(
            claim,
            observation.model_copy(
                update={"protected_rendering": reordered, "protected_result_hash": _hash(reordered)}
            ),
        )

    locator = json.loads(observation.locator)
    locator["extra"] = True
    extra = json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ClaimVerificationUnavailable, match="locator is unavailable"):
        verify_claim(
            claim, observation.model_copy(update={"locator": extra, "locator_hash": _hash(extra)})
        )

    unicode_output = "é Acme balance is 42".encode()
    split = ClaimSlice(
        claim_key="claim-split",
        claim_content_hash=_hash(unicode_output[1:]),
        span_start_byte=1,
        span_end_byte=len(unicode_output),
        served_output_hash=_hash(unicode_output),
        served_output=unicode_output,
    )
    with pytest.raises(ClaimVerificationUnavailable, match="UTF-8"):
        verify_claim(split, observation)


# --- NE-VERIFY-REASON: this failure says WHY, and NE-GROUND-NUMERAL: the
# verbatim-compound-numeral rule (what it grounds, and what it still refuses) ---


def _rendering(*chunks: str) -> tuple[bytes, bytes]:
    """One protected search rendering over ``chunks``, and its exact locator."""
    ids = [f"chunk-{index}" for index in range(len(chunks))]
    lines = [f"[showing {len(chunks)} of {len(chunks)} retrieved chunks]"]
    lines.extend(f"{cid}: {text}" for cid, text in zip(ids, chunks, strict=True))
    rendering = "\n".join(lines).encode()
    locator = json.dumps(
        {
            "chunk_ids": ids,
            "contributors": [{"server_stamp": "stamp-a"}],
            "evidence_assurance": "direct",
            "kind": "knowledge_search",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return rendering, locator


def _observation_over(*chunks: str) -> DirectSearchEvidenceObservation:
    rendering, locator = _rendering(*chunks)
    return _search(
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
        locator=locator,
        locator_hash=_hash(locator),
    )


def test_a_claim_quoting_its_cited_source_verifies_and_binds() -> None:
    """The serving half of the pair: an exact quotation of the source grounds."""
    chunk = "All 11 fees were reversed after the review."
    answer = f"We looked into it. {chunk} That closes the matter."

    verified = verify_claim(_claim(answer, chunk), _observation_over(chunk))

    assert verified.claim_key == "claim-a"
    assert verified.verifier_method == verifier_module.CLAIM_VERIFIER_METHOD


def test_a_claim_that_paraphrases_its_source_still_fails_and_names_the_classes() -> None:
    """The refusing half: the model's own words are not the source's words.

    `claim is not grounded by exact evidence` alone cannot tell a paraphrase from
    a perfect quotation the gate's numeral parser refused — and those are a prompt
    problem and a gate problem. The finding CLASSES separate them, and they are
    publishable exactly as they are: a `GroundingFinding` carries only a class
    from a closed `Literal` and a count, never the ungrounded text.
    """
    chunk = "All 11 fees were reversed after the review."
    answer = "Every charge was refunded to the affected customers."

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, answer), _observation_over(chunk))

    reason = raised.value.safe_reason
    assert "not grounded by exact evidence" in reason
    assert "vocab-ungrounded" in reason
    # No claim text and no evidence text — only classes and counts.
    assert "refunded" not in reason
    assert "reversed" not in reason
    assert 0 < len(reason) <= verifier_module._MAX_SAFE_REASON_CHARS
    assert all(character.isprintable() for character in reason)


_COMPOUND_SENTENCES = [
    "The revision is dated 2026-03.",
    "It was opened on 2026-05-14.",
    "Dispute MER-DSP-003 was opened.",
    "Loan ID 4830-A was affected.",
    "Version 1.2.3 shipped.",
    "The window is 2-3 cycles.",
    "It closed at 09:30 today.",
]


@pytest.mark.parametrize("sentence", _COMPOUND_SENTENCES)
def test_a_source_sentence_carrying_a_compound_numeral_is_citable_when_quoted_verbatim(
    sentence: str,
) -> None:
    """NE-GROUND-NUMERAL: the wall came down, deliberately and by owner approval.

    This test used to pin the opposite — that a chunk carrying an ISO date, a
    hyphenated identifier, a dotted version, a range or a time was NOT GROUNDED
    IN ITSELF, so no claim over it could pass however perfectly the model quoted.
    `ne.govern.grounding` (`GateProfile.verbatim_compound_numerals`, ng-2 only)
    now grounds such a token IFF it appears VERBATIM in `corpus.tokens`: a
    QUOTATION is not an ASSERTION. Each sentence below is quoted BYTE-IDENTICALLY
    out of the chunk the claim cites, so each meets that bar exactly.

    The rule that replaced the wall is narrow, and its two halves are pinned
    separately below rather than assumed here:

    * exact containment — a numeral the source does NOT carry is still
      `numeral-unparseable`, near-misses included
      (`test_a_compound_numeral_the_source_does_not_carry_is_still_refused`);
    * run participation — the contained numeral JOINS the anti-recombination
      run instead of flushing it, so it cannot be lifted out of its chunk and
      pasted next to another chunk's prose
      (`test_a_compound_numeral_spliced_onto_another_chunks_prose_is_refused`).

    A verbatim quotation clears both because the verifier mints ONE grounding
    fact per complete protected chunk (`_search_answers`), so the whole chunk is
    a single corpus phrase and any contiguous quotation of it is a contiguous
    run inside that phrase.
    """
    answer = f"Context first. {sentence} Context after."

    verified = verify_claim(_claim(answer, sentence), _observation_over(sentence))

    assert verified.claim_key == "claim-a"
    assert verified.verifier_method == verifier_module.CLAIM_VERIFIER_METHOD


@pytest.mark.parametrize(
    "source,altered",
    [
        ("The revision is dated 2026-03.", "The revision is dated 2026-04."),
        ("It was opened on 2026-05-14.", "It was opened on 2026-05-15."),
        ("Dispute MER-DSP-003 was opened.", "Dispute MER-DSP-042 was opened."),
        ("Loan ID 4830-A was affected.", "Loan ID 4830-B was affected."),
        ("Version 1.2.3 shipped.", "Version 1.2.4 shipped."),
        ("The window is 2-3 cycles.", "The window is 2-4 cycles."),
        ("It closed at 09:30 today.", "It closed at 09:31 today."),
    ],
)
def test_a_compound_numeral_the_source_does_not_carry_is_still_refused(
    source: str, altered: str
) -> None:
    """The other half of the same rule: containment is EXACT, so every one of
    these NEAR-MISSES still refuses.

    One digit moved in each — a fabricated dispute id, a date off by a day, a
    version off by a patch, a widened range, a minute later. The surrounding
    prose is quoted perfectly, which is precisely what makes these the dangerous
    shape: the sentence LOOKS sourced. Containment is `token in corpus.tokens`
    with no plural fold and no canonicalization, so `2026-05-15` cannot be
    grounded by a source that says `2026-05-14`.
    """
    answer = f"Context first. {altered} Context after."

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, altered), _observation_over(source))

    assert "numeral-unparseable" in raised.value.safe_reason


def test_a_compound_numeral_absent_from_a_numeral_free_source_is_still_refused() -> None:
    """Fail-closed, unchanged: a source with no numeral at all grounds none."""
    chunk = "The refund was approved after review."
    claimed = "The refund was approved on 2026-05-14."
    answer = f"Context first. {claimed} Context after."

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, claimed), _observation_over(chunk))

    assert "numeral-unparseable" in raised.value.safe_reason


def test_a_compound_numeral_spliced_onto_another_chunks_prose_is_refused() -> None:
    """Run participation, pinned: containment alone must NOT be enough.

    Every token of the spliced claim is corpus-verbatim — the dispute id from
    chunk-0, the date and the approval from chunk-1 — so a rule that merely
    waved a contained numeral through would pass this fabricated composite. The
    contained numeral instead JOINS the anti-recombination run, so the run spans
    two chunks, is contiguous in neither, and `phrase-recombination` fires.
    """
    chunk_a = "Dispute MER-DSP-003 was opened."
    chunk_b = "The refund was approved on 2026-05-14."
    spliced = "Dispute MER-DSP-003 was approved on 2026-05-14."
    answer = f"Context first. {spliced} Context after."

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, spliced), _observation_over(chunk_a, chunk_b))

    assert "phrase-recombination" in raised.value.safe_reason


# --- multi-line prose chunks: the body grammar is one id-prefixed BLOCK per
# chunk, not one physical line. Chunk text is free document prose (the corpus
# mapper serves clean multi-line prose, not a single-line envelope), so a chunk
# may legitimately span lines — while every genuine order/count mismatch, and
# every ambiguous attribution, must STILL refuse. ---


_PROSE_CHUNK = (
    "Dispute MER-DSP-003 was opened by Meridian.\n"
    "The adjusted balance is 42 dollars.\n"
    "Escalation review is pending."
)


def test_a_multiline_prose_chunk_verifies_a_verbatim_quotation() -> None:
    """A prose chunk with embedded newlines is one grounding fact: a sentence
    quoted verbatim out of it — numeral included — verifies and binds."""
    quoted = "The adjusted balance is 42 dollars."
    answer = f"Context first. {quoted} Context after."

    verified = verify_claim(_claim(answer, quoted), _observation_over(_PROSE_CHUNK))

    assert verified.claim_key == "claim-a"
    assert verified.verifier_method == verifier_module.CLAIM_VERIFIER_METHOD


def test_a_quotation_spanning_a_prose_line_break_stays_one_contiguous_run() -> None:
    """The rendering's line break is whitespace, not a phrase boundary: a
    quotation crossing it (re-flowed onto one line, as a draft naturally
    would) is still a contiguous run inside the ONE chunk phrase."""
    quoted = "was opened by Meridian. The adjusted balance is 42 dollars."
    answer = f"The record says it {quoted}"

    verified = verify_claim(_claim(answer, quoted), _observation_over(_PROSE_CHUNK))

    assert verified.claim_key == "claim-a"


def test_prose_and_single_line_record_chunks_ground_side_by_side() -> None:
    """The corpus is mixed by design: multi-line prose chunks and single-line
    json/csv record chunks must both verify out of the SAME rendering."""
    record = '{"account": "acme", "balance": 977, "status": "current"}'
    observation = _observation_over(_PROSE_CHUNK, record)

    prose_quote = "Escalation review is pending."
    verified = verify_claim(
        _claim(f"Note: {prose_quote}", prose_quote),
        observation,
    )
    assert verified.claim_key == "claim-a"

    record_answer = f"The served record is {record} exactly."
    verified = verify_claim(_claim(record_answer, record), observation)
    assert verified.claim_key == "claim-a"


def test_a_numeral_spliced_out_of_a_multiline_chunk_is_still_refused() -> None:
    """Anti-recombination survives the block grammar: each multi-line chunk is
    still exactly ONE corpus phrase, so a composite lifting a numeral out of
    one chunk into another chunk's prose still spans two phrases and refuses."""
    chunk_a = "Dispute MER-DSP-003 was opened.\nIt remains under review."
    chunk_b = "The refund was approved on 2026-05-14.\nNo further action was needed."
    spliced = "Dispute MER-DSP-003 was approved on 2026-05-14."
    answer = f"Context first. {spliced} Context after."

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, spliced), _observation_over(chunk_a, chunk_b))

    assert "phrase-recombination" in raised.value.safe_reason


def test_a_missing_chunk_block_is_still_a_count_mismatch() -> None:
    """Header and locator say two chunks; the body renders one block (the
    second chunk's id-prefix line is gone, its text absorbed nowhere)."""
    rendering, locator = _rendering(_PROSE_CHUNK, "Second chunk text.")
    tampered = rendering.replace(b"\nchunk-1: Second chunk text.", b"")
    observation = _search(
        protected_rendering=tampered,
        protected_result_hash=_hash(tampered),
        locator=locator,
        locator_hash=_hash(locator),
    )

    with pytest.raises(ClaimVerificationUnavailable, match="count mismatch"):
        verify_claim(_claim("anything", "anything"), observation)


def test_reordered_chunk_blocks_are_still_an_order_mismatch() -> None:
    """The body must render the locator's chunks in the locator's order."""
    rendering, locator = _rendering("First chunk text.", "Second chunk text.")
    swapped = (
        b"[showing 2 of 2 retrieved chunks]\n"
        b"chunk-1: Second chunk text.\n"
        b"chunk-0: First chunk text."
    )
    assert sorted(swapped.splitlines()) == sorted(rendering.splitlines())
    observation = _search(
        protected_rendering=swapped,
        protected_result_hash=_hash(swapped),
        locator=locator,
        locator_hash=_hash(locator),
    )

    with pytest.raises(ClaimVerificationUnavailable, match="chunk order"):
        verify_claim(_claim("anything", "anything"), observation)


def test_a_duplicated_chunk_block_is_still_refused() -> None:
    """One chunk rendered twice in place of another: the second occurrence is
    a continuation line carrying a served id-prefix — ambiguous, refused."""
    duplicated = (
        b"[showing 2 of 2 retrieved chunks]\nchunk-0: First chunk text.\nchunk-0: First chunk text."
    )
    _, locator = _rendering("First chunk text.", "Second chunk text.")
    observation = _search(
        protected_rendering=duplicated,
        protected_result_hash=_hash(duplicated),
        locator=locator,
        locator_hash=_hash(locator),
    )

    with pytest.raises(ClaimVerificationUnavailable, match="chunk order"):
        verify_claim(_claim("anything", "anything"), observation)


def test_a_prose_line_colliding_with_a_served_chunk_id_refuses_not_guesses() -> None:
    """Fail-closed ambiguity: a continuation line that itself starts with a
    served chunk's id-prefix cannot be attributed with certainty — it could be
    that chunk rendered out of order, or prose colliding with the id. The
    verifier must become UNAVAILABLE, never silently bind the text to either
    chunk."""
    colliding = "First line of prose.\nchunk-1: looks like the next chunk but is prose."
    observation = _observation_over(colliding, "Second chunk text.")

    with pytest.raises(ClaimVerificationUnavailable, match="chunk order"):
        verify_claim(_claim("anything", "anything"), observation)


def test_an_empty_chunk_block_is_still_refused() -> None:
    """A block whose text is empty carries no grounding fact — refused, as the
    one-line grammar always did."""
    rendering, locator = _rendering("")
    observation = _search(
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
        locator=locator,
        locator_hash=_hash(locator),
    )

    with pytest.raises(ClaimVerificationUnavailable, match="chunk order"):
        verify_claim(_claim("anything", "anything"), observation)


def test_every_verifier_refusal_publishes_a_reason_not_a_blank() -> None:
    """Each raise site here used to publish `failure_reason: null`."""
    chunk = "All 11 fees were reversed after the review."
    observation = _observation_over(chunk)
    broken = _claim(chunk, chunk).model_copy(update={"served_output_hash": _hash(b"other")})

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(broken, observation)

    reason = raised.value.safe_reason
    assert reason.startswith("A claim could not be verified against its cited evidence:")
    assert "served output hash mismatch" in reason
    assert 0 < len(reason) <= verifier_module._MAX_SAFE_REASON_CHARS


def test_the_verifier_reason_shares_the_projection_cap() -> None:
    """Two caps that must agree, pinned against each other rather than assumed."""
    import ne.runtime.runner as runner_module

    assert verifier_module._MAX_SAFE_REASON_CHARS == runner_module._MAX_SAFE_REASON_CHARS == 256


def test_the_finding_list_is_trimmed_to_fit_rather_than_dropped_whole() -> None:
    """A reason past the cap is not a longer explanation — it is another null."""
    findings = tuple(
        GroundingFinding(reason="vocab-ungrounded-factual", count=n) for n in range(1, 40)
    )
    detail = verifier_module._grounding_detail(
        GroundingVerdict(passed=False, findings=findings, prompt_echo_count=0)
    )
    published = f"A claim could not be verified against its cited evidence: {detail}."

    assert len(published) <= verifier_module._MAX_SAFE_REASON_CHARS
    assert "vocab-ungrounded-factual x1" in published


# --- NE-QUOTE-THE-SOURCE: one string must satisfy BOTH gates -----------------


def test_a_source_sentence_quoted_inside_the_answer_satisfies_both_gates() -> None:
    """The shape the prompt now teaches, proved against both checks at once.

    Two gates were being satisfied separately and never together:
      * `_validated_agentic_claims` requires the span to occur in `final_text`;
      * `check_grounding` requires its tokens to come from the CITED CHUNK.
    An answer in the model's own voice satisfies the first and fails the second;
    a bare source quotation satisfies the second and fails the first. Measured
    over 21 real claims from 6 model runs on the deployed prompt: 19 were spans
    of the answer that appear nowhere in the source, 2 were verbatim source spans
    absent from the answer, and ZERO satisfied both.

    The rule that satisfies both is one string doing both jobs — the source's own
    sentence, quoted inside the answer, with the model's analysis around it.
    """
    chunk = "All eleven late fees were reversed after the billing review."
    answer = (
        "The record is unambiguous on this point. "
        f'The source states: "{chunk}" '
        "That places the fault with the billing job rather than the policy."
    )

    # Gate 1: the span is located in final_text, exactly once.
    assert answer.count(chunk) == 1
    # Gate 2: the same span is grounded in the cited chunk.
    verified = verify_claim(_claim(answer, chunk), _observation_over(chunk))

    assert verified.claim_key == "claim-a"
    assert verified.span_end_byte - verified.span_start_byte == len(chunk.encode())


def test_the_same_answer_paraphrased_still_fails_both_ways() -> None:
    """The refusing half: rewording the source breaks the binding, both gates.

    The paraphrase is a perfectly good span of the answer — gate 1 is happy — and
    grounding refuses it, which is exactly the split that made this invisible.
    """
    chunk = "All eleven late fees were reversed after the billing review."
    paraphrase = "Every one of the eleven charges was refunded following an audit."
    answer = f"In short: {paraphrase} That is the finding."

    assert answer.count(paraphrase) == 1  # gate 1 would pass

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_claim(answer, paraphrase), _observation_over(chunk))

    reason = raised.value.safe_reason
    assert "not grounded by exact evidence" in reason
    assert "vocab-ungrounded" in reason
    assert "refunded" not in reason and "reversed" not in reason


def _server_bound_slice(answer: str, chunk: str) -> ClaimSlice:
    """A ClaimSlice built EXACTLY as the runner's server-side binder builds it.

    NE-SPAN-BIND: the runner now selects the span itself (longest common verbatim
    word run between the answer and the retained delivered text) instead of
    locating a model quote. This helper runs that real selector, so the tests
    below prove the verifier over the spans the server actually produces.
    """
    from ne.runtime.runner import _server_bound_claims
    from ne.runtime.source_results import ServedTextRetention

    from .test_model_planned_runner import _PROTECTED_REF, _SOURCE_ID

    retention = ServedTextRetention()
    retention.record(_SOURCE_ID, chunk)
    (bound,) = _server_bound_claims(answer, served_refs={_PROTECTED_REF}, retention=retention)
    raw = answer.encode()
    return ClaimSlice(
        claim_key="claim-a",
        claim_content_hash=bound.claim_content_hash,
        span_start_byte=bound.span_start_byte,
        span_end_byte=bound.span_end_byte,
        served_output_hash=_hash(raw),
        served_output=raw,
    )


def test_a_server_bound_span_verifies_against_the_chunk_that_served_it() -> None:
    """The two halves finally meet: the runner's own selection passes both gates.

    The old measurement (21 real claims, zero satisfying both gates) was the
    proof that a model-authored string could not do this job. The server-selected
    span does, BY CONSTRUCTION: it is a span of the answer, verbatim in the
    served chunk, and `check_grounding` here still runs on it with full force.
    """
    chunk = "All eleven late fees were reversed after the billing review."
    answer = f'The source states: "{chunk}" So the billing job is at fault.'

    verified = verify_claim(_server_bound_slice(answer, chunk), _observation_over(chunk))

    assert verified.span_end_byte - verified.span_start_byte == len(chunk.encode())


def test_grounding_still_refuses_a_server_bound_span_over_the_wrong_evidence() -> None:
    """Binding decides WHICH span is checked; it exempts the span from NOTHING.

    The span below is a real span of the answer and verbatim in the chunk the
    RUNNER retained — but the evidence observation loaded for verification
    carries a different chunk (the wrong row, in NE-GROUND-ROWID terms). The
    grounding gate must still refuse it, exactly as it refuses a model-authored
    near-miss: server selection upstream buys no exemption here.
    """
    chunk = "All eleven late fees were reversed after the billing review."
    other_row = "The hardship credit posted to the account on May ninth."
    answer = f'The source states: "{chunk}" So the billing job is at fault.'

    with pytest.raises(ClaimVerificationUnavailable) as raised:
        verify_claim(_server_bound_slice(answer, chunk), _observation_over(other_row))

    reason = raised.value.safe_reason
    assert "not grounded by exact evidence" in reason
    assert "eleven" not in reason and "reversed" not in reason


# --- verified (T3) evidence sets ------------------------------------------------
# NE-CLAIM-VERIFIED-SET. A `read.kg.*` verified read serves ONE protected result
# backed by an evidence SET — one record per (row, contributor) — and the model is
# shown the WHOLE table. These pin the two halves that make binding a claim to that
# set safe: the rendering is parsed back and each row re-hashed exactly as
# `ne.brain_ontology.verified.answers` hashed it when it minted the row receipt, and
# every member of the set is checked before one byte reaches the grounding corpus.

_T3_COLUMNS = ["customer", "provisional_credit_amount", "status"]
_T3_ROWS = [
    ["Meridian Bank", "412.55", "posted"],
    ["Northwind Trust", "88.20", "pending"],
]


def _row_anchor(receipt_hash: str) -> str:
    return _hash(verifier_module._ROW_ANCHOR_DOMAIN + receipt_hash.encode("ascii"))


def _canonical_row_hash(columns: list[str], row: list[str]) -> str:
    return _hash(json.dumps([columns, row], separators=(",", ":")).encode("utf-8"))


def _t3_rendering(columns: list[str], rows: list[list[str]]) -> bytes:
    lines = [
        f"[showing {len(rows)} of {len(rows)} rows]",
        " | ".join(columns),
        "-+-".join("-" * len(column) for column in columns),
        *(" | ".join(row) for row in rows),
    ]
    return "\n".join(lines).encode("utf-8")


def _t3_member(
    *,
    row_index: int,
    contributor_index: int,
    contributors: list[dict[str, str]],
    columns: list[str] | None = None,
    rows: list[list[str]] | None = None,
    canonical_row_hash: str | None = None,
) -> verifier_module.VerifiedRowSetMemberObservation:
    columns = _T3_COLUMNS if columns is None else columns
    rows = _T3_ROWS if rows is None else rows
    receipt_hash = _hash(f"receipt-{row_index}".encode())
    receipt = {
        "receipt_schema_version": 1,
        "template_id": "disaster_declarations",
        "template_version": 1,
        "canonical_row_hash": (
            _canonical_row_hash(columns, rows[row_index])
            if canonical_row_hash is None
            else canonical_row_hash
        ),
        "contributor_schema_version": 1,
        "contributors": contributors,
        "contributor_count": len(contributors),
        "contributor_set_hash": "1" * 64,
        "receipt_hash": receipt_hash,
    }
    locator = json.dumps(
        {
            "kind": "verified_t3",
            "row_anchor": _row_anchor(receipt_hash),
            "receipt": receipt,
            "contributor_index": contributor_index,
            "contributor": contributors[contributor_index],
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return verifier_module.VerifiedRowSetMemberObservation(
        evidence_id=f"evidence-{row_index}-{contributor_index}",
        locator_id=f"locator-{row_index}-{contributor_index}",
        locator_hash=_hash(locator),
        producing_event_id=f"event-{row_index}-{contributor_index}",
        locator=locator,
    )


def _t3_set(
    *,
    contributors_per_row: int = 2,
    columns: list[str] | None = None,
    rows: list[list[str]] | None = None,
    members: tuple[verifier_module.VerifiedRowSetMemberObservation, ...] | None = None,
    **changes: object,
) -> verifier_module.VerifiedRowSetEvidenceObservation:
    columns = _T3_COLUMNS if columns is None else columns
    rows = _T3_ROWS if rows is None else rows
    contributors = [{"server_stamp": f"stamp-{index}"} for index in range(contributors_per_row)]
    rendering = _t3_rendering(columns, rows)
    built = members
    if built is None:
        built = tuple(
            _t3_member(
                row_index=row_index,
                contributor_index=contributor_index,
                contributors=contributors,
                columns=columns,
                rows=rows,
            )
            for row_index in range(len(rows))
            for contributor_index in range(contributors_per_row)
        )
    fields: dict[str, object] = {
        "protected_ref": "pending-result-a",
        "protected_result_hash": _hash(rendering),
        "evidence_set_id": "evidence-set-a",
        "set_key": "9" * 64,
        "delivery_receipt_id": "delivery-t3",
        "delivery_receipt_hash": "f" * 64,
        "protected_rendering": rendering,
        "members": built,
    }
    fields.update(changes)
    return verifier_module.VerifiedRowSetEvidenceObservation.model_validate(fields)


def test_verified_set_binds_a_claim_over_the_whole_served_table() -> None:
    """The regression: this claim used to die as a bare `LookupError`."""

    output = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
    observation = _t3_set()

    first = verify_claim(_claim(output, output), observation)
    second = verify_claim(_claim(output, output), observation)

    assert first == second
    assert first.verifier_method == CLAIM_VERIFIER_METHOD
    assert first.verifier_version == CLAIM_VERIFIER_VERSION
    # The anchor is the set's FIRST member in its server-minted order — never an
    # arbitrary pick — and the receipt names the set and every member it verified.
    assert first.evidence_id == "evidence-0-0"
    assert first.locator_id == "locator-0-0"
    assert first.producing_event_id == "event-0-0"
    assert first.evidence_set_id == "evidence-set-a"
    assert first.member_evidence_ids == tuple(member.evidence_id for member in observation.members)
    assert len(first.member_evidence_ids) == 4


def test_verified_set_result_hash_covers_every_member_not_just_the_anchor() -> None:
    output = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
    baseline = verify_claim(_claim(output, output), _t3_set())

    # Same anchor, same rendering, one FEWER contributor behind the second row: a
    # collapse to the anchor would produce the same receipt. It must not.
    contributors = [{"server_stamp": f"stamp-{index}"} for index in range(2)]
    trimmed = _t3_set(
        members=(
            _t3_member(row_index=0, contributor_index=0, contributors=contributors),
            _t3_member(row_index=0, contributor_index=1, contributors=contributors),
            _t3_member(row_index=1, contributor_index=0, contributors=contributors[:1]),
        )
    )
    assert verify_claim(_claim(output, output), trimmed).result_hash != baseline.result_hash


def test_verified_set_refuses_a_claim_the_served_table_does_not_support() -> None:
    output = "Meridian Bank 999999.99 posted"
    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(output, output), _t3_set())


def test_verified_set_refuses_an_incomplete_contributor_set() -> None:
    contributors = [{"server_stamp": f"stamp-{index}"} for index in range(2)]
    # Row 1 declares two contributors and only one member carries it.
    observation = _t3_set(
        members=(
            _t3_member(row_index=0, contributor_index=0, contributors=contributors),
            _t3_member(row_index=0, contributor_index=1, contributors=contributors),
            _t3_member(row_index=1, contributor_index=0, contributors=contributors),
        )
    )
    with pytest.raises(ClaimVerificationUnavailable, match="contributor set is incomplete"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), observation)


def test_verified_set_refuses_a_rendering_its_receipts_do_not_cover() -> None:
    """The row hash is the parse's own proof: a substituted value cannot pass."""

    tampered = [["Meridian Bank", "999999.99", "posted"], list(_T3_ROWS[1])]
    rendering = _t3_rendering(_T3_COLUMNS, tampered)
    observation = _t3_set(
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
    )
    with pytest.raises(ClaimVerificationUnavailable, match="not the row its receipt covers"):
        verify_claim(_claim("Meridian Bank 999999.99", "Meridian Bank 999999.99"), observation)


def test_verified_set_refuses_a_reordered_table() -> None:
    reordered = [list(_T3_ROWS[1]), list(_T3_ROWS[0])]
    rendering = _t3_rendering(_T3_COLUMNS, reordered)
    observation = _t3_set(
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
    )
    with pytest.raises(ClaimVerificationUnavailable, match="not the row its receipt covers"):
        verify_claim(_claim("Northwind Trust", "Northwind Trust"), observation)


def test_verified_set_refuses_a_row_count_the_receipts_do_not_match() -> None:
    rendering = _t3_rendering(_T3_COLUMNS, [list(_T3_ROWS[0])])
    observation = _t3_set(
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
    )
    with pytest.raises(ClaimVerificationUnavailable, match="row count mismatch"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), observation)


def test_verified_set_refuses_a_member_locator_that_does_not_hash() -> None:
    observation = _t3_set()
    broken = observation.model_copy(
        update={
            "members": (
                observation.members[0].model_copy(update={"locator_hash": "0" * 64}),
                *observation.members[1:],
            )
        }
    )
    with pytest.raises(ClaimVerificationUnavailable, match="locator hash mismatch"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), broken)


def test_verified_set_refuses_an_anchor_that_is_not_its_receipts_anchor() -> None:
    contributors = [{"server_stamp": "stamp-0"}]
    member = _t3_member(row_index=0, contributor_index=0, contributors=contributors)
    locator = json.loads(member.locator)
    locator["row_anchor"] = "3" * 64
    tampered = json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
    observation = _t3_set(
        contributors_per_row=1,
        rows=[list(_T3_ROWS[0])],
        members=(member.model_copy(update={"locator": tampered, "locator_hash": _hash(tampered)}),),
    )
    with pytest.raises(ClaimVerificationUnavailable, match="row anchor mismatch"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), observation)


def test_verified_set_refuses_a_substituted_contributor() -> None:
    contributors = [{"server_stamp": "stamp-0"}]
    member = _t3_member(row_index=0, contributor_index=0, contributors=contributors)
    locator = json.loads(member.locator)
    locator["contributor"] = {"server_stamp": "stamp-imposter"}
    tampered = json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
    observation = _t3_set(
        contributors_per_row=1,
        rows=[list(_T3_ROWS[0])],
        members=(member.model_copy(update={"locator": tampered, "locator_hash": _hash(tampered)}),),
    )
    with pytest.raises(ClaimVerificationUnavailable, match="contributor mismatch"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), observation)


def test_verified_set_refuses_a_rendering_that_is_not_the_served_bytes() -> None:
    with pytest.raises(ClaimVerificationUnavailable, match="rendering hash mismatch"):
        verify_claim(
            _claim("Meridian Bank", "Meridian Bank"), _t3_set(protected_result_hash="2" * 64)
        )


# --- NE-CLAIM-LINEFACT: rendering-contiguous spans over a verified table --------
# The server's span selector binds the longest verbatim run common to the answer
# and the RENDERING — and on a ` | ` table every >=5-word run crosses cell
# boundaries, which a per-cell phrase corpus refused as phrase-recombination BY
# CONSTRUCTION (measured live 2026-08-27: 10/10 runs of three demo scenarios,
# including a correct refusal, died at this gate). The corpus for a verified set
# now ALSO mints each served line as one single-cell fact — the same construction
# a search chunk gets — so a span that is contiguous in the served rendering can
# ground, while everything the rendering does not state stays refused.

_LINEFACT_COLUMNS = [
    "dispute",
    "dispute_id",
    "customer_id",
    "status",
    "notice_date",
    "amount_cents",
    "credit_amount_recorded",
    "credit_amount_cents",
    "credit_issue_date",
]
_LINEFACT_ROWS = [
    [
        "urn:x:dsp:mer-dsp-001",
        "mer-dsp-001",
        "mer-cus-001",
        "open",
        "2026-08-10",
        "65000",
        "false",
        "",
        "",
    ],
    [
        "urn:x:dsp:mer-dsp-003",
        "mer-dsp-003",
        "mer-cus-003",
        "open",
        "2026-08-03",
        "20000",
        "true",
        "20000",
        "",
    ],
    [
        "urn:x:dsp:mer-dsp-005",
        "mer-dsp-005",
        "mer-cus-005",
        "closed",
        "2026-07-20",
        "90000",
        "true",
        "90000",
        "",
    ],
]


def _linefact_set() -> verifier_module.VerifiedRowSetEvidenceObservation:
    return _t3_set(columns=_LINEFACT_COLUMNS, rows=_LINEFACT_ROWS)


def _linefact_rendering() -> str:
    return _t3_rendering(_LINEFACT_COLUMNS, _LINEFACT_ROWS).decode()


def test_a_correct_refusal_quoting_the_served_row_is_deliverable() -> None:
    """THE demo acceptance case (S4 shape): a refusal that quotes, verbatim, the
    row it is refusing to compute from. The server binds the quoted row line and
    the verifier must accept it — a correct refusal that cites its evidence is
    exactly the behaviour the product exists to deliver. Before NE-CLAIM-LINEFACT
    this refused {numeral-ungrounded x2, phrase-recombination x1}: the row's only
    identity is compound identifiers (invisible to F3's prose anchors) and the
    span straddles cells (contiguous in no per-cell phrase)."""
    row_line = " | ".join(_LINEFACT_ROWS[1])
    answer = (
        "I can't compute this. The directory records amounts and whether a credit "
        "was recorded, but the credit_issue_date field is blank for every dispute. "
        f'The source states: "{row_line}" '
        "Without issue dates I cannot determine which credits were issued late."
    )

    verified = verify_claim(_server_bound_slice(answer, _linefact_rendering()), _linefact_set())

    assert verified.verifier_version.startswith("v2/")
    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert "mer-dsp-003" in span and "20000" in span  # the row line really bound


def test_the_served_header_line_is_quotable() -> None:
    """The column header is served text; quoting it (e.g. to name a field that is
    blank) must verify. Its >=5-word run is contiguous only in the header LINE,
    never in any per-cell phrase."""
    header_line = " | ".join(_LINEFACT_COLUMNS)
    answer = f'The source states: "{header_line}" and credit_issue_date is blank throughout.'

    verify_claim(_server_bound_slice(answer, _linefact_rendering()), _linefact_set())


def test_a_cross_row_stitch_is_still_refused() -> None:
    """Row 1's identity spliced onto row 3's numeral: verbatim tokens, fabricated
    pairing. Contiguous in NO served line, and NE-GROUND-ROWID refuses the
    quoted-identifier/row mismatch — line facts widen nothing here."""
    stitched = "mer-dsp-001 | mer-cus-001 | open | 2026-08-10 | 65000 | false | 20000"

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(stitched, stitched), _linefact_set())


def test_a_wrong_row_attribution_is_still_refused() -> None:
    """The NE-GROUND-ROWID shape, over the line-fact corpus: 20000 is
    mer-dsp-003's cell; attributing it to mer-dsp-001 must refuse."""
    wrong = "mer-dsp-001 has credit_amount_cents 20000 recorded"

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(wrong, wrong), _linefact_set())


def test_a_numeral_the_rendering_does_not_carry_is_still_refused() -> None:
    near_row = "mer-dsp-003 | mer-cus-003 | open | 2026-08-03 | 20000 | true | 99999 | "

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(near_row, near_row), _linefact_set())


def test_a_near_miss_date_inside_a_row_shape_is_still_refused() -> None:
    """EXACT containment survives: the rendering says 2026-08-03; a claim shaped
    exactly like the row but dated 2026-08-11 refuses."""
    near_row = "mer-dsp-003 | mer-cus-003 | open | 2026-08-11 | 20000 | true | 20000 | "

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(near_row, near_row), _linefact_set())


# --- NE-CLAIM-ROWSENT: the binder's sentence path, verified end to end ----------
# On a ` | ` rendering an HONEST row sentence ("mer-dsp-003 has
# credit_amount_cents 20000") shares at most three consecutive words with the
# source, so the verbatim floor can never bind it (measured live 2026-08-27:
# maxrun=1..3 against floor=5, hard-failing the exact answer shape the prompt
# teaches). The binder now grades answer sentences with the REAL gate against the
# rendering and binds the first that fully grounds. These tests prove the bound
# sentence then passes the STRICT verifier (whose corpus is a superset of the
# binder's preview), and that nothing ungroundable rides the new path.


def test_a_row_sentence_bound_span_verifies_against_the_verified_set() -> None:
    answer = (
        "Two provisional credits are recorded. "
        "mer-dsp-003 has credit_amount_cents 20000. "
        "mer-dsp-005 has credit_amount_cents 90000. "
        "The records do not carry a total."
    )

    verified = verify_claim(_server_bound_slice(answer, _linefact_rendering()), _linefact_set())

    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert span == "mer-dsp-003 has credit_amount_cents 20000"


def test_the_s4_refusal_shape_binds_a_cents_sentence_and_verifies() -> None:
    """The demo's anti-fabrication showpiece, in the sentence-stated form.

    The refusal's dollar prose ($200.00 over a cents cell) grounds in nothing —
    F2 makes the currency symbol meaning-bearing and no recompute exists — so
    those sentences are skipped, and the one sentence stating the SERVED value
    binds and verifies. A refusal that names its evidence is deliverable."""
    answer = (
        "I can't compute this. The credit_issue_date field is blank for every "
        "dispute, so which credits were issued late is not recorded. "
        "mer-dsp-003 has credit_amount_cents 20000. "
        "Without issue dates no late-credit total can be computed."
    )

    verified = verify_claim(_server_bound_slice(answer, _linefact_rendering()), _linefact_set())

    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert span == "mer-dsp-003 has credit_amount_cents 20000"


def test_a_dollars_only_refusal_still_binds_nothing() -> None:
    """The currency trap does not ride the sentence path.

    Every value-bearing sentence renders the cents cells as dollars, every such
    numeral is ungrounded (F2), and no sentence passes the gate — so the run
    still hard-fails at the binder, exactly as a paraphrase does. The fix for
    this answer is the answer (state served values verbatim), not the binder."""
    from ne.runtime.errors import ActionValidationError

    answer = (
        "I can't compute this. The directory records amounts "
        "(mer-dsp-003: $200.00; mer-dsp-005: $900.00), but the credit_issue_date "
        "field is blank for every dispute, so no late total can be computed."
    )

    with pytest.raises(ActionValidationError) as raised:
        _server_bound_slice(answer, _linefact_rendering())
    assert "rowsent=" in getattr(raised.value, "span_shape", "")


# --- NE-KG-MULTIKEY partial coverage: the empty-member evidence member ----------


def _t3_empty_member(
    *,
    template_id: str = "disaster_declarations",
    template_version: int = 1,
    keys: dict[str, object] | None = None,
) -> verifier_module.VerifiedRowSetMemberObservation:
    receipt = {
        "receipt_schema_version": 1,
        "template_id": template_id,
        "template_version": template_version,
        "canonical_args_hash": "a" * 64,
        "dataset_graphs_hash": "b" * 64,
        "contributor_schema_version": 1,
        "contributors": [{"server_stamp": "stamp-0"}],
        "contributor_count": 1,
        "contributor_set_hash": "1" * 64,
        "receipt_hash": _hash(b"empty-member-receipt"),
    }
    payload: dict[str, object] = (
        {"kind": "verified_t3_empty", "receipt": receipt, "evidence_assurance": "direct"}
        if keys is None
        else keys
    )
    locator = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return verifier_module.VerifiedRowSetMemberObservation(
        evidence_id="evidence-empty-0",
        locator_id="locator-empty-0",
        locator_hash=_hash(locator),
        producing_event_id="event-empty-0",
        locator=locator,
    )


#: The exact note line ``render_answer_text`` appends to a windowed partial read.
_PARTIAL_NOTE = (
    "[note: 1 of 3 requested q_loan_id values returned no rows in the "
    "caller-chosen window 2026-07-25..2026-08-25: mer-loan-003 \u2014 a fact "
    "about this query's result only, not evidence that the data does not exist]"
)


def _partial_rendering() -> bytes:
    return _t3_rendering(_T3_COLUMNS, _T3_ROWS) + ("\n" + _PARTIAL_NOTE).encode("utf-8")


def _partial_t3_set(
    empty_member: verifier_module.VerifiedRowSetMemberObservation,
) -> verifier_module.VerifiedRowSetEvidenceObservation:
    """Two verified rows + one empty-member coverage entry + the note the model saw."""

    contributors = [{"server_stamp": f"stamp-{index}"} for index in range(2)]
    rendering = _partial_rendering()
    members = (
        *(
            _t3_member(
                row_index=row_index,
                contributor_index=contributor_index,
                contributors=contributors,
            )
            for row_index in range(len(_T3_ROWS))
            for contributor_index in range(2)
        ),
        empty_member,
    )
    return _t3_set(
        members=members,
        protected_rendering=rendering,
        protected_result_hash=_hash(rendering),
    )


def test_a_partial_set_with_an_empty_member_coverage_entry_still_verifies() -> None:
    """The coverage entry attests absence; it must never break row verification.

    Without the ``verified_t3_empty`` skip in ``_verified_set_rows`` this refuses
    with "verified evidence locator" / row-count errors — which would turn EVERY
    claim over a partial multi-key read into "unavailable" the moment the claim
    path stopped dropping the minted receipt.
    """

    output = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
    observation = _partial_t3_set(_t3_empty_member())

    result = verify_claim(_claim(output, output), observation)

    assert result.evidence_set_id == "evidence-set-a"
    # Every member — the coverage entry included — is named in the receipt.
    assert "evidence-empty-0" in result.member_evidence_ids
    assert len(result.member_evidence_ids) == 5


def test_a_coverage_entry_for_another_template_refuses_the_set() -> None:
    with pytest.raises(ClaimVerificationUnavailable, match="several templates"):
        verify_claim(
            _claim("Meridian Bank", "Meridian Bank"),
            _partial_t3_set(_t3_empty_member(template_id="another_template")),
        )


def test_a_malformed_coverage_entry_refuses_the_set() -> None:
    smuggled = _t3_empty_member(
        keys={
            "kind": "verified_t3_empty",
            "receipt": {"receipt_hash": _hash(b"x"), "template_id": "t", "template_version": 1},
            "evidence_assurance": "direct",
            "row_anchor": "9" * 64,
        }
    )
    with pytest.raises(ClaimVerificationUnavailable, match="verified empty-member locator"):
        verify_claim(_claim("Meridian Bank", "Meridian Bank"), _partial_t3_set(smuggled))


# --- coverage member x NE-CLAIM-LINEFACT / NE-CLAIM-ROWSENT interactions --------
# The coverage entry is an evidence-set MEMBER, never rendering text: line facts
# are minted from the SERVED LINES alone, and the binder sees only retained
# delivered text. So the note LINE (served) must ground and bind exactly like any
# other served line, while the coverage member's receipt internals must never
# become bindable or groundable text.


def test_the_partial_note_line_is_served_text_and_its_quote_verifies() -> None:
    """A verbatim quote of the windowed empty-member note verifies under v2.

    The note is envelope commentary the model was actually shown — under
    NE-CLAIM-LINEFACT it is a served line and therefore one grounding fact. This
    is the honest surface working end to end: a model that SAYS "no rows in the
    caller-chosen window 2026-07-25..2026-08-25 for mer-loan-003" can have that
    sentence verified, instead of laundering the absence into "no payment due"."""

    answer = f'The source states: "{_PARTIAL_NOTE}"'
    verified = verify_claim(
        _server_bound_slice(answer, _partial_rendering().decode()),
        _partial_t3_set(_t3_empty_member()),
    )

    assert verified.verifier_version.startswith("v2/")
    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert "mer-loan-003" in span and "2026-07-25..2026-08-25" in span


def test_the_note_grounds_identically_with_and_without_the_coverage_member() -> None:
    """The bindable text comes from the RENDERING, not the member: the same
    note-quoting claim verifies against the same rendering whether or not the
    coverage entry rides the set — the member adds accounting, never corpus."""

    answer = f'The source states: "{_PARTIAL_NOTE}"'
    claim = _server_bound_slice(answer, _partial_rendering().decode())
    with_member = verify_claim(claim, _partial_t3_set(_t3_empty_member()))

    contributors = [{"server_stamp": f"stamp-{index}"} for index in range(2)]
    rendering = _partial_rendering()
    without_member = verify_claim(
        claim,
        _t3_set(
            members=tuple(
                _t3_member(
                    row_index=row_index,
                    contributor_index=contributor_index,
                    contributors=contributors,
                )
                for row_index in range(len(_T3_ROWS))
                for contributor_index in range(2)
            ),
            protected_rendering=rendering,
            protected_result_hash=_hash(rendering),
        ),
    )

    assert with_member.span_start_byte == without_member.span_start_byte
    assert with_member.span_end_byte == without_member.span_end_byte
    assert len(with_member.member_evidence_ids) == 5
    assert len(without_member.member_evidence_ids) == 4


def test_the_coverage_receipt_internals_are_not_bindable_or_groundable() -> None:
    """Nothing inside the coverage member's receipt is servable text: the binder
    finds no span in it (the rendering nowhere carries those bytes), and a claim
    hand-built over receipt internals refuses as ungrounded."""

    from ne.runtime.errors import ActionValidationError

    receipt_prose = (
        f"The canonical args hash is {'a' * 64} and the dataset graphs hash "
        f"is {'b' * 64} for the empty member."
    )
    with pytest.raises(ActionValidationError):
        _server_bound_slice(receipt_prose, _partial_rendering().decode())

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(
            _claim(receipt_prose, receipt_prose),
            _partial_t3_set(_t3_empty_member()),
        )


def test_a_fabricated_absence_statement_is_still_refused_on_a_partial_set() -> None:
    """The failure mode itself, pinned: 'mer-loan-003 has no payment due' is a
    conclusion the rendering nowhere states (the note says only 'no rows in the
    caller-chosen window'), and it must refuse even with the coverage member
    present — the member proves the query's own emptiness, never the world's."""

    fabricated = "mer-loan-003 has no payment due in the 14-day hardship window 2026-08-18"

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(fabricated, fabricated), _partial_t3_set(_t3_empty_member()))


# --- NE-MERIDIAN-DOLLARS x NE-CLAIM-LINEFACT ------------------------------------
# The Meridian monetary templates serve a ``*_dollars`` column (the exact
# fixed-point rendering of the ``*_cents`` system of record) so a dollar phrase
# has a real served cell to ground against. These pin that a dollars cell
# round-trips the claim verifier's own machinery: the row re-hash
# (``sha256(json.dumps([columns, row]))``) accepts a ``$`` cell unchanged, and a
# served line carrying it is quotable under the per-line fact construction.

_DOLLARS_COLUMNS = [
    "dispute_id",
    "customer_id",
    "status",
    "amount_cents",
    "credit_amount_cents",
    "amount_dollars",
    "credit_amount_dollars",
]
_DOLLARS_ROWS = [
    ["mer-dsp-001", "mer-cus-001", "open", "65000", "", "$650.00", ""],
    ["mer-dsp-003", "mer-cus-003", "open", "20000", "20000", "$200.00", "$200.00"],
]


def test_a_served_dollars_cell_round_trips_the_row_hash_and_is_quotable() -> None:
    """A row line carrying ``$200.00`` re-hashes to exactly the receipt's
    ``canonical_row_hash`` (the ``$`` cell splits on ``" | "`` like any other)
    and the server-bound quote of that line verifies end to end."""
    row_line = " | ".join(_DOLLARS_ROWS[1])
    answer = (
        f'The source states: "{row_line}" So the provisional credit equals the disputed amount.'
    )
    rendering = _t3_rendering(_DOLLARS_COLUMNS, _DOLLARS_ROWS).decode()

    verified = verify_claim(
        _server_bound_slice(answer, rendering),
        _t3_set(columns=_DOLLARS_COLUMNS, rows=_DOLLARS_ROWS),
    )

    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert "mer-dsp-003" in span and "$200.00" in span  # the dollars cell really bound


def test_a_wrong_dollar_figure_in_a_row_shape_is_still_refused() -> None:
    """The rendering says $200.00; a claim shaped exactly like the row but
    carrying $210.00 refuses — a dollars cell is fabrication-guarded exactly
    like every other cell."""
    near_row = "mer-dsp-003 | mer-cus-003 | open | 20000 | 20000 | $210.00 | $200.00"

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(
            _claim(near_row, near_row), _t3_set(columns=_DOLLARS_COLUMNS, rows=_DOLLARS_ROWS)
        )


def test_a_dollars_cell_spliced_onto_the_wrong_row_is_still_refused() -> None:
    """mer-dsp-001 has no credit at all; attributing mer-dsp-003's $200.00 credit
    to it refuses (NE-GROUND-ROWID over a currency cell)."""
    wrong = "mer-dsp-001 has credit_amount_dollars $200.00 recorded"

    with pytest.raises(ClaimVerificationUnavailable, match="not grounded"):
        verify_claim(_claim(wrong, wrong), _t3_set(columns=_DOLLARS_COLUMNS, rows=_DOLLARS_ROWS))


def test_a_dollar_sentence_binds_via_the_sentence_path_when_the_cell_is_served() -> None:
    """The other half of ``test_a_dollars_only_refusal_still_binds_nothing``, and
    the "fix for this answer" it names: with NE-MERIDIAN-DOLLARS the rendering
    itself serves the dollar cell, so a dollar-speaking sentence FULLY GROUNDS in
    the rendering and binds via NE-CLAIM-ROWSENT — no >=floor verbatim run exists
    (the connectives are the model's own words), so this is the sentence path,
    verified end to end against the receipted set."""
    answer = (
        "The credit_amount_dollars for mer-dsp-003 is $200.00. That value comes from the directory."
    )
    rendering = _t3_rendering(_DOLLARS_COLUMNS, _DOLLARS_ROWS).decode()

    verified = verify_claim(
        _server_bound_slice(answer, rendering),
        _t3_set(columns=_DOLLARS_COLUMNS, rows=_DOLLARS_ROWS),
    )

    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert "mer-dsp-003" in span and "$200.00" in span  # the dollar sentence really bound


# --- NE-CLAIM-ROWCLAUSE: single-row clauses of an enumerating sentence ----------
# The natural answer to "what is the total...?" enumerates its rows in ONE
# sentence — "mer-dsp-003 with credit_amount_cents 20000, and mer-dsp-005 with
# credit_amount_cents 90000" — which the gate refuses whole, and CORRECTLY: a
# sentence naming two rows is an unverifiable pairing under NE-GROUND-ROWID
# (measured live 2026-08-27, S3 9b43b64d: rowsent=5, every candidate refused).
# Each single-row CLAUSE is fully verifiable, so the binder grades the failing
# sentences' clauses as candidates of their own and binds the first that passes;
# verify_claim then re-grades the exact slice with full force.


def test_an_enumerating_sentence_binds_its_first_grounded_clause() -> None:
    answer = (
        "The disputes directory shows two recorded provisional credits: "
        "mer-dsp-003 with credit_amount_cents 20000, and mer-dsp-005 with "
        "credit_amount_cents 90000. The records do not carry a total."
    )

    verified = verify_claim(_server_bound_slice(answer, _linefact_rendering()), _linefact_set())

    span = answer.encode()[verified.span_start_byte : verified.span_end_byte].decode()
    assert span == "mer-dsp-003 with credit_amount_cents 20000"


def test_a_swapped_enumeration_still_binds_nothing() -> None:
    """Clause grading widens nothing: each clause of a row/value SWAP is a
    wrong-row pairing and refuses under NE-GROUND-ROWID, so the whole answer
    still hard-fails."""
    from ne.runtime.errors import ActionValidationError

    answer = (
        "mer-dsp-003 with credit_amount_cents 90000, and "
        "mer-dsp-005 with credit_amount_cents 20000."
    )

    with pytest.raises(ActionValidationError) as raised:
        _server_bound_slice(answer, _linefact_rendering())
    assert "rowsent=" in getattr(raised.value, "span_shape", "")


def test_a_bare_identifier_clause_or_sentence_binds_nothing() -> None:
    """A clause must ASSERT A VALUE to bind. A bare identifier grounds trivially
    by containment and would bind a span that asserts nothing — the boilerplate
    hazard the prose word floor exists to refuse. Same rule for a short
    identifier-only sentence."""
    from ne.runtime.errors import ActionValidationError

    for answer in ("See mer-dsp-005.", "Review (mer-dsp-003, mer-dsp-005)."):
        with pytest.raises(ActionValidationError):
            _server_bound_slice(answer, _linefact_rendering())
