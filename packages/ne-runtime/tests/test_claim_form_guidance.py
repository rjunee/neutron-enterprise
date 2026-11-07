# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ROWS-ARE-NOT-QUOTABLE: the answer shape the prompt teaches is the shape that binds.

WHAT CHANGED UNDER IT. NE-SPAN-BIND removed ``claim_text`` from the model's
contract: the model authors ONLY ``final_text``, and the SERVER picks the span --
the longest verbatim word run common to the answer and a retained served chunk --
then ``verify_claim`` runs ``check_grounding`` on THAT span. So guidance about
claim sentences has nothing left to aim at. The substance survives, re-aimed at
the answer's own prose, because the answer is now the only thing the model writes.

WHY ROWS ARE A TRAP HERE, in the two ways this suite measures:

  THE SPAN IS STOLEN. The binder takes the LONGEST run. An answer that quotes a
  source sentence correctly AND also pastes the raw row binds to the PASTED ROW,
  and the verifier refuses it. The quotation was right and lost anyway -- which is
  a failure mode the old contract did not have, and the sharpest reason the "never
  paste a fragment" rule is now load-bearing rather than stylistic.

  OR NOTHING BINDS. ``_MIN_BOUND_SPAN_WORDS`` counts WHITESPACE-separated tokens,
  so on a ``|``-delimited table every 5-token run crosses cell boundaries, while
  the GATE's table corpus makes each CELL its own phrase. Against that corpus a
  quote long enough to bind is phrase-recombination by construction. (The claim
  VERIFIER has since stopped grading spans against that corpus alone:
  NE-CLAIM-LINEFACT additionally mints each served line as one fact, so a
  verbatim row or header quote now VERIFIES there — see
  ``test_run_claim_verifier``. The guidance below still teaches prose-first
  answers, which read better and bind identically; these tests pin the gate and
  selector mechanics that make both paths true.)

THE WAY THROUGH, measured end-to-end below on the real S3 shape: quote PROSE, state
row values as ordinary sentences. The prose source binds through the verbatim run;
the rows source binds through NE-CLAIM-ROWSENT (the binder's sentence path: the
first answer sentence that fully GROUNDS in the served rendering — measured live,
row sentences share maxrun<=3 words with a ` | ` rendering, so the verbatim floor
alone hard-failed exactly the answer shape this guidance teaches). A paraphrase, a
$-rendering of a cents cell, or a fabricated pairing still grounds in nothing and
still hard-fails.

Everything here runs the REAL selector (``_server_bound_claims``) and the REAL
verifier gate (``check_grounding`` over a corpus built the way ``verify_claim``
builds it), with the example lifted OUT OF THE PROMPT, so guidance that drifted
from either mechanism fails rather than merely reading well.
"""

from __future__ import annotations

import re

from ne.govern.grounding import PROFILE_NG2, GroundingCorpus, check_grounding
from ne.runtime.errors import ActionValidationError
from ne.runtime.runner import _STRUCTURED_AGENTIC_COMPLETION_TAIL

from .test_model_planned_runner import _PROTECTED_REF, _PROTECTED_REF_B, _bind

#: A prose policy chunk and an ALL-ATOMIC rows result -- the exact pairing the
#: live S3/S4 questions read (a dispute SOP plus the disputes directory), and the
#: hardest case: nothing in the table is quotable.
_PROSE = (
    "Provisional credit must be issued within ten business days of the notice date "
    "unless the investigation is complete."
)
_COLS = ["dispute", "dispute_id", "status", "credit_amount_cents", "credit_issue_date"]
_ROWS = [
    ["urn:ne:entity:mer-dsp-003", "mer-dsp-003", "closed", "20000", ""],
    ["urn:ne:entity:mer-dsp-005", "mer-dsp-005", "closed", "90000", ""],
]
#: Byte-for-byte what ``render_answer_text`` delivers, because the retained text
#: the binder searches IS that rendering -- a prettier fixture would prove nothing.
_ROWS_CHUNK = "\n".join(
    [
        "[showing 2 of 2 rows]",
        " | ".join(_COLS),
        "-+-".join("-" * len(col) for col in _COLS),
        *(" | ".join(row) for row in _ROWS),
    ]
)


def _rows_corpus() -> GroundingCorpus:
    return GroundingCorpus.from_answers(
        [
            {
                "template_id": "disputes-directory",
                "template_version": 1,
                "config_hash": "cfg",
                "columns": _COLS,
                "rows": [list(row) for row in _ROWS],
                "citations": [row[0] for row in _ROWS],
                "truncated": False,
                "args": {},
            }
        ]
    )


def _prose_corpus() -> GroundingCorpus:
    return GroundingCorpus.from_answers(
        [
            {
                "template_id": "dispute-sop",
                "template_version": 1,
                "config_hash": "cfg",
                "columns": ["text"],
                "rows": [[_PROSE]],
                "citations": ["urn:ne:doc:dispute-sop"],
                "truncated": False,
                "args": {},
            }
        ]
    )


def _bind_both(answer: str) -> tuple[object, ...]:
    return _bind(
        answer,
        {_PROTECTED_REF, _PROTECTED_REF_B},
        protected_output_a=_PROSE,
        protected_output_b=_ROWS_CHUNK,
    )


def _span_of(answer: str, claim: object) -> str:
    start = claim.span_start_byte  # type: ignore[attr-defined]
    end = claim.span_end_byte  # type: ignore[attr-defined]
    return answer.encode("utf-8")[start:end].decode("utf-8")


def _row_sentence_example() -> str:
    """The one row sentence the prompt shows, read out of the prompt itself."""
    (example,) = re.findall(r'"(id-7 has [^"]+)"', _STRUCTURED_AGENTIC_COMPLETION_TAIL)
    return example


# --- the guidance says what it must, and about the ANSWER -------------------


def test_the_guidance_is_about_the_answer_and_not_about_claims() -> None:
    """Re-aimed, not merely reworded: no claim vocabulary is reintroduced.

    Under NE-SPAN-BIND the model authors no claim, no key, no id and no offsets,
    so guidance that still spoke of claim_text would be teaching a field that no
    longer exists -- and the transport section immediately above says so.
    """
    tail = _STRUCTURED_AGENTIC_COMPLETION_TAIL
    assert "claim_text" not in tail
    assert '"claims"' not in tail
    assert "you declare no claims, no ids, no offsets and no hashes" in tail

    # The row form, and the connective that is the point of it.
    assert "ONE ROW PER SENTENCE" in tail
    assert re.search(r"\b(has|for|is)\b", _row_sentence_example())
    # The mechanism, stated -- this is WHY a pasted fragment loses the run.
    assert "The server binds the LONGEST verbatim run it finds" in tail
    assert "never paste a row, a header line, or cells side by side" in tail


# --- the measured table, against the real selector and the real verifier -----


def test_quoting_prose_and_stating_rows_in_sentences_binds_and_verifies() -> None:
    """THE SHAPE THE PROMPT TEACHES, on the hardest real pairing.

    The prose source binds a clean span and verifies. The all-atomic table has
    no qualifying verbatim run — and since NE-CLAIM-ROWSENT it binds anyway,
    through the sentence path: the first answer sentence that fully GROUNDS in
    the served rendering. Both sources end up cited, each by material it
    actually supports.
    """
    answer = (
        f'The rule is explicit. The source states: "{_PROSE}" '
        "mer-dsp-003 has credit_amount_cents 20000. "
        "mer-dsp-005 has credit_amount_cents 90000. "
        "The records do not carry a total."
    )
    bound = _bind_both(answer)
    by_key = {c.claim_key: c for c in bound}  # type: ignore[attr-defined]
    assert set(by_key) == {"bound:protected-output-a", "bound:protected-output-b"}
    assert _span_of(answer, by_key["bound:protected-output-a"]) == _PROSE

    verdict = check_grounding(
        _span_of(answer, by_key["bound:protected-output-a"]),
        _prose_corpus(),
        "",
        profile=PROFILE_NG2,
    )
    assert verdict.passed, verdict.findings

    # NE-CLAIM-ROWSENT: the table source now binds too — not a pasted fragment,
    # but the FIRST answer sentence that fully grounds in the served rendering.
    # (The mer-dsp-003 sentence is fused to the quotation by the `."` boundary —
    # the same boundary rule the gate's splitter applies — and that fused
    # sentence carries prose the table does not ground, so the 005 sentence is
    # the first clean one.)
    row_span = _span_of(answer, by_key["bound:protected-output-b"])
    assert row_span == "mer-dsp-005 has credit_amount_cents 90000"
    verdict = check_grounding(row_span, _rows_corpus(), "", profile=PROFILE_NG2)
    assert verdict.passed, verdict.findings


def test_a_pasted_row_steals_the_span_from_a_correct_quotation() -> None:
    """The selector mechanics the "never paste a row" rule responds to.

    Identical answer, identical correct quotation, one raw row appended -- and the
    binder picks the ROW for source B, because it is longer. Against the GATE's
    per-cell table corpus that span refuses, which is what this test pins. (The
    claim VERIFIER itself no longer dies on it: NE-CLAIM-LINEFACT mints each
    served line as one fact, so the pasted-row span verifies there and a stray
    paste no longer kills the run. The prose quotation still binds its own
    source's claim either way.)
    """
    answer = (
        f'The rule is explicit. The source states: "{_PROSE}" '
        "Raw row: urn:ne:entity:mer-dsp-003 | mer-dsp-003 | closed | 20000 | "
    )
    bound = _bind_both(answer)
    by_key = {c.claim_key: c for c in bound}  # type: ignore[attr-defined]
    stolen = by_key["bound:protected-output-b"]
    span = _span_of(answer, stolen)
    assert span.count("|") >= 3, "the bound span is the pasted table row"

    verdict = check_grounding(span, _rows_corpus(), "", profile=PROFILE_NG2)
    assert not verdict.passed
    # Both, and both are the same defect seen twice: the cells are not contiguous
    # in any chunk phrase, so the run recombines AND the numeral it carries is
    # left without a row identity the gate will accept.
    assert {finding.reason for finding in verdict.findings} == {
        "phrase-recombination",
        "numeral-ungrounded",
    }


def test_row_sentences_alone_now_bind_the_table_source() -> None:
    """NE-CLAIM-ROWSENT: the shape the guidance teaches is bindable ON ITS OWN.

    Row sentences share at most two or three consecutive words with a ` | `
    rendering, so under the verbatim floor alone this answer hard-failed
    (measured live: aw=90, maxrun=3, floor=5). The sentence path binds the first
    sentence that fully grounds in the rendering — the model's own connectives
    around served values, exactly what the prompt asks it to write.
    """
    answer = "mer-dsp-003 has credit_amount_cents 20000. mer-dsp-005 has credit_amount_cents 90000."
    bound = _bind_both(answer)
    by_key = {c.claim_key: c for c in bound}  # type: ignore[attr-defined]
    assert set(by_key) == {"bound:protected-output-b"}
    assert _span_of(answer, by_key["bound:protected-output-b"]) == (
        "mer-dsp-003 has credit_amount_cents 20000"
    )


def test_a_paraphrased_answer_still_hard_fails_before_publication() -> None:
    """The binder's whole point, still in force under the sentence path.

    An answer entirely in the model's own words — no served value, no quotable
    sentence — grounds in nothing and binds nothing. The sentence path grades
    with the REAL gate, so a paraphrase ("about two hundred dollars"), a unit
    conversion of a cents cell, or a fabricated pairing cannot ride it past the
    verbatim rule: sufficiency became STRICTER for a table, not looser.
    """
    for answer in (
        "About two hundred dollars was provisionally credited for the third dispute.",
        # the currency trap: a $-rendering of a cents cell never grounds (F2).
        "mer-dsp-003 was credited $200.00 and mer-dsp-005 was credited $900.00.",
        # a fabricated near-miss value: real row, real column, wrong number.
        "mer-dsp-003 has credit_amount_cents 20001.",
    ):
        try:
            _bind_both(answer)
        except ActionValidationError as exc:
            assert "no span of the final answer is verbatim" in str(exc)
        else:  # pragma: no cover - a bound claim here would mean the bar moved
            raise AssertionError(f"must not bind: {answer!r}")


def test_a_model_computed_total_is_ungrounded_and_the_rows_are_not() -> None:
    """Why the prompt says give the rows and decline the total.

    The gate mints a sum only when EVERY cell of a column parses as a numeral, so
    a column two rows leave empty has no aggregate fact at all: no phrasing of the
    total can ground. The contributing rows can, so the answer keeps everything the
    records support and drops only what they do not.
    """
    sparse_rows = [
        ["urn:ne:entity:mer-dsp-001", "mer-dsp-001", "open", "", ""],
        ["urn:ne:entity:mer-dsp-003", "mer-dsp-003", "closed", "20000", ""],
        ["urn:ne:entity:mer-dsp-005", "mer-dsp-005", "closed", "90000", ""],
    ]
    corpus = GroundingCorpus.from_answers(
        [
            {
                "template_id": "disputes-directory",
                "template_version": 1,
                "config_hash": "cfg",
                "columns": _COLS,
                "rows": sparse_rows,
                "citations": [row[0] for row in sparse_rows],
                "truncated": False,
                "args": {},
            }
        ]
    )
    prompt = "what is the total recorded amount of provisional credits"

    for row_sentence in (
        "mer-dsp-003 has credit_amount_cents 20000",
        "mer-dsp-005 has credit_amount_cents 90000",
    ):
        assert check_grounding(row_sentence, corpus, prompt, profile=PROFILE_NG2).passed

    total = check_grounding(
        "the total of credit_amount_cents is 110000", corpus, prompt, profile=PROFILE_NG2
    )
    assert not total.passed
    assert [finding.reason for finding in total.findings] == ["numeral-ungrounded"]


def test_the_guidance_did_not_weaken_the_rules_around_it() -> None:
    """It shapes an answer the model already decided to give; it licenses nothing.

    The verbatim-quotation requirement, the five-word floor and the honest escape
    hatch all still stand, and the aggregate rule names the refusal it wants rather
    than suppressing one.
    """
    tail = _STRUCTURED_AGENTIC_COMPLETION_TAIL
    assert "QUOTE YOUR SOURCES VERBATIM." in tail
    assert "under five consecutive words" in tail
    assert "Never invent a quotation" in tail
    assert "leave that point out rather than quote something that does not say it" in tail
    assert "say plainly that the records do not carry it" in tail
    # Form, not content: no subject, domain or expected answer leaks into it.
    for leak in ("dispute", "credit", "Reg E", "loan", "Meridian"):
        assert leak not in tail
