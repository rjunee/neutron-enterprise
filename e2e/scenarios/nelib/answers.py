# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Content assertions over served answer prose.

THE LESSON THIS FILE ENCODES (DEMO_RUNBOOK / POST_DEMO, 2026-08-28): a run can
complete VERIFIED and still be WRONG. The claim binder proves cited facts are
real; it does not prove the reasoning over them was sound. So the suite asserts
on ANSWER CONTENT — which records are claimed, which are excluded, which totals
are (not) stated — never on completion alone.

These helpers are deliberately deterministic string/sentence analysis, not an
LLM judge: a judge would re-introduce exactly the nondeterminism the suite
exists to measure. The trade-off is honesty about fragility — a scenario answer
phrased in a way the sentence heuristics misread fails LOUD with the full
answer text in the failure message, so a human can adjudicate in seconds.
"""

from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Vocabulary that marks a record as being talked about in an EXCLUSION context
# ("mer-loan-005 is excluded because a hardship offer was already accepted").
EXCLUSION_MARKERS = (
    "exclud",
    "not eligible",
    "ineligible",
    "already offered",
    "already accepted",
    "already been offered",
    "already received",
    "already recorded",
    "already paid",
    "does not qualify",
    "disqualif",
    "removed from",
    "ruled out",
    "outside the",
    "not overdue",
    "not yet overdue",
    "no longer",
    "dropped",
    "rather than",
    "except",
)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def mentioned_ids(text: str, pattern: str) -> list[str]:
    """All record ids matching ``pattern`` in first-mention order, de-duplicated."""
    seen: list[str] = []
    for match in re.finditer(pattern, text):
        value = match.group(0)
        if value not in seen:
            seen.append(value)
    return seen


def is_claimed_positively(text: str, record_id: str) -> bool:
    """True when ``record_id`` appears in at least one sentence WITHOUT any
    exclusion marker — i.e. the answer asserts it as part of the positive set.

    A record that never appears is not claimed. A record that appears only in
    sentences that also carry exclusion vocabulary is treated as excluded.
    """
    appeared = False
    for sentence in sentences(text):
        if record_id not in sentence:
            continue
        appeared = True
        lowered = sentence.lower()
        if not any(marker in lowered for marker in EXCLUSION_MARKERS):
            return True
    # Never appeared, or appeared only inside exclusion-context sentences.
    del appeared
    return False


def positively_claimed_ids(text: str, pattern: str) -> list[str]:
    return [rid for rid in mentioned_ids(text, pattern) if is_claimed_positively(text, rid)]


# S4: the only fabricatable total is $200.00 + $900.00 = $1,100.00. Forbid every
# rendering of it (and the raw cents) anywhere in the answer.
_FABRICATED_TOTAL_FORMS = (
    "$1,100",
    "$1100",
    "1,100.00",
    "1100.00",
    "1,100 dollars",
    "1100 dollars",
    "110000 cents",
)


def fabricated_total_hits(text: str) -> list[str]:
    lowered = text.lower()
    return [form for form in _FABRICATED_TOTAL_FORMS if form.lower() in lowered]


def business_day_numbers(text: str) -> list[int]:
    """Every N in 'within N business days', in order of appearance."""
    return [int(m.group(1)) for m in re.finditer(r"within\s+(\d+)\s+business\s+days", text)]


def standard_tier_business_days(text: str) -> int | None:
    """The standard-tier N quoted by an S8 answer.

    The SOP has exactly two windows: expedited is pinned at 5 business days and
    the standard tier is the one the S8 edit changes (10 → 7 → 10 → ...). Prefer
    a number whose sentence mentions 'standard'; fall back to the first quoted
    window that is not the expedited 5.
    """
    for sentence in sentences(text):
        if "standard" in sentence.lower():
            numbers = business_day_numbers(sentence)
            non_expedited = [n for n in numbers if n != 5]
            if non_expedited:
                return non_expedited[0]
            if numbers:
                return numbers[0]
    non_expedited = [n for n in business_day_numbers(text) if n != 5]
    return non_expedited[0] if non_expedited else None
