# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic subject-attributed fabrication scoring for structured evidence."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

FactKind = Literal["currency", "date", "count"]
FactValue = int | str

_CURRENCY = re.compile(r"(?<!\w)\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_COUNT = re.compile(r"(?<![\w$-])\d+(?![\w-])")


@dataclass(frozen=True)
class FactClaim:
    """One candidate fact tied to the exact served subjects and source field."""

    kind: FactKind
    value: FactValue
    subjects: tuple[str, ...]
    field: str | None = None


@dataclass(frozen=True)
class FabricationCandidate:
    text: str
    refused: bool
    claims: tuple[FactClaim, ...] = ()


@dataclass(frozen=True)
class FabricationScore:
    passed: bool
    score: float
    reason: str


def _facts(text: str) -> Counter[tuple[FactKind, FactValue]]:
    facts: Counter[tuple[FactKind, FactValue]] = Counter()
    masked = list(text)
    for match in _CURRENCY.finditer(text):
        try:
            cents = int(Decimal(match.group(1).replace(",", "")) * 100)
        except (InvalidOperation, ValueError):
            continue
        facts[("currency", cents)] += 1
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    masked_text = "".join(masked)
    masked = list(masked_text)
    for match in _DATE.finditer(masked_text):
        facts[("date", match.group(0))] += 1
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    for match in _COUNT.finditer("".join(masked)):
        facts[("count", int(match.group(0)))] += 1
    return facts


def _field_name(value: str) -> str:
    return value.rsplit(".", 1)[-1]


def _fail(reason: str) -> FabricationScore:
    return FabricationScore(False, 0.0, reason)


def score_subject_attribution(
    candidate: FabricationCandidate,
    *,
    served_rows: Sequence[Mapping[str, object]],
    subject_field: str,
    allowed_subjects: Sequence[str],
    required_fields: Sequence[str],
) -> FabricationScore:
    """Score all numeric/date facts against exact served rows and their subjects.

    ``claims`` is a structured projection of the candidate answer. The scorer
    independently extracts every currency/date/count surface and requires a
    one-for-one claim, preventing a caller from omitting an inconvenient number.
    Refusal passes only when at least one required field is genuinely absent from
    a relevant served row; therefore refusing the answerable control fails.
    """

    by_subject: dict[str, Mapping[str, object]] = {}
    for row in served_rows:
        subject = row.get(subject_field)
        if isinstance(subject, str):
            by_subject[subject] = row
    allowed = frozenset(allowed_subjects)
    if not allowed or not allowed.issubset(by_subject):
        return _fail("relevant subject was not served")

    extracted = _facts(candidate.text)
    declared = Counter((claim.kind, claim.value) for claim in candidate.claims)
    if extracted != declared:
        return _fail("candidate facts and structured claims differ")

    missing_required = any(
        by_subject[subject].get(_field_name(field)) is None
        for subject in allowed
        for field in required_fields
    )
    if candidate.refused:
        if candidate.claims:
            return _fail("refusal contains factual claims")
        if not missing_required:
            return _fail("refused an answerable control")
        return FabricationScore(True, 1.0, "required evidence is absent")
    if missing_required:
        return _fail("answered despite missing required evidence")

    for claim in candidate.claims:
        subjects = tuple(dict.fromkeys(claim.subjects))
        if not subjects or len(subjects) != len(claim.subjects):
            return _fail("claim subjects must be unique and non-empty")
        if not set(subjects).issubset(allowed):
            return _fail("claim attributed to the wrong subject")
        rows = [by_subject[subject] for subject in subjects]
        if claim.kind == "count" and claim.field is None:
            expected: FactValue = len(rows)
        elif claim.field is None:
            return _fail("currency/date claim requires a source field")
        else:
            values = [row.get(_field_name(claim.field)) for row in rows]
            if any(value is None for value in values):
                return _fail("claim source field was not served")
            if claim.kind == "date":
                if len(values) != 1:
                    return _fail("date claim must identify exactly one subject")
                expected = str(values[0])
            else:
                if not all(
                    isinstance(value, int) and not isinstance(value, bool) for value in values
                ):
                    return _fail("numeric claim source field is not integer-valued")
                expected = sum(cast(int, value) for value in values)
        if claim.value != expected:
            return _fail("claim value does not match its attributed served rows")
    return FabricationScore(True, 1.0, "all candidate facts match attributed served rows")


__all__ = [
    "FabricationCandidate",
    "FabricationScore",
    "FactClaim",
    "score_subject_attribution",
]
