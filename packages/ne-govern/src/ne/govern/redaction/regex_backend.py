# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RegexRedactor — stdlib-only PII recognizers (air-gap SUPPLEMENT, NOT primary).

This backend is deliberately a lightweight, dependency-free fallback for
air-gapped deployments where the isolated Presidio env cannot be built. It is
NOT the primary redactor: reimplementing PII detection with regexes invites
compliance-grade false negatives, so Presidio (the industry-standard,
NER-backed detector) is the adopt-first default (SPEC §11 build-vs-adopt). Use
this only as the documented supplement/fallback behind the same
:class:`ne.contracts.governance.Redactor` contract.

Recognizers (all deterministic, anchored on word boundaries): EMAIL_ADDRESS,
PHONE_NUMBER (E.164 + common US forms), US_SSN, CREDIT_CARD (13-19 digits with
Luhn VALIDATION — non-Luhn numbers are left untouched), IP_ADDRESS (v4,
octet-validated), IBAN_CODE. Redactions replace the match with
``<REDACTED:{CATEGORY}>``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from ne.contracts.governance import RedactionResult

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Za-z0-9]{11,30}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
    r"|(?<!\w)\+\d{7,15}(?!\w)"
)
# Loose candidate for a 13-19 digit card (single space/dash separators allowed);
# Luhn is validated in code before redacting.
_CARD_CANDIDATE_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

_TOKEN = "<REDACTED:{category}>"

# Ordered so distinctive shapes (email, IBAN, card) are consumed before the
# looser numeric recognizers can nibble at their digits.
_CATEGORY_ORDER = (
    "EMAIL_ADDRESS",
    "IBAN_CODE",
    "CREDIT_CARD",
    "US_SSN",
    "IP_ADDRESS",
    "PHONE_NUMBER",
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _valid_ipv4(value: str) -> bool:
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in value.split("."))


def _sub_simple(pattern: re.Pattern[str], category: str) -> Callable[[str], tuple[str, bool]]:
    token = _TOKEN.format(category=category)

    def apply(text: str) -> tuple[str, bool]:
        new_text, n = pattern.subn(token, text)
        return new_text, n > 0

    return apply


def _sub_ipv4(text: str) -> tuple[str, bool]:
    token = _TOKEN.format(category="IP_ADDRESS")
    hit = False

    def repl(match: re.Match[str]) -> str:
        nonlocal hit
        if _valid_ipv4(match.group(0)):
            hit = True
            return token
        return match.group(0)

    return _IPV4_RE.sub(repl, text), hit


def _sub_card(text: str) -> tuple[str, bool]:
    token = _TOKEN.format(category="CREDIT_CARD")
    hit = False

    def repl(match: re.Match[str]) -> str:
        nonlocal hit
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            hit = True
            return token
        return match.group(0)

    return _CARD_CANDIDATE_RE.sub(repl, text), hit


_APPLIERS: dict[str, Callable[[str], tuple[str, bool]]] = {
    "EMAIL_ADDRESS": _sub_simple(_EMAIL_RE, "EMAIL_ADDRESS"),
    "IBAN_CODE": _sub_simple(_IBAN_RE, "IBAN_CODE"),
    "CREDIT_CARD": _sub_card,
    "US_SSN": _sub_simple(_SSN_RE, "US_SSN"),
    "IP_ADDRESS": _sub_ipv4,
    "PHONE_NUMBER": _sub_simple(_PHONE_RE, "PHONE_NUMBER"),
}


class RegexRedactor:
    """Stdlib regex PII redactor — the air-gap SUPPLEMENT behind the Redactor contract."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        wanted = set(categories) if categories is not None else None
        redacted: list[str] = []
        for category in _CATEGORY_ORDER:
            if wanted is not None and category not in wanted:
                continue
            text, hit = _APPLIERS[category](text)
            if hit:
                redacted.append(category)
        return RedactionResult(text=text, redactions=tuple(redacted))
