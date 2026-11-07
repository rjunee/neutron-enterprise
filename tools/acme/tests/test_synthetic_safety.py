# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Mechanical guarantees that the corpus is obviously synthetic: reserved
``.example`` domains, never-issued ``000-`` SSN-shaped strings, the reserved
``555-01XX`` fictional phone block, no stray real-looking PII anywhere."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ne.schema import OperationalRecordSet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "acme-v1"
ARTIFACT_NAMES = (
    "records.json",
    "content.json",
    "org.json",
    "acl.json",
    "ground_truth.json",
    "manifest.json",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_SSN_SHAPED_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
_PHONE_RE = re.compile(r"^\+1-555-01\d{2}$")


def _load_record_set() -> OperationalRecordSet:
    obj = json.loads((FIXTURES / "records.json").read_bytes())
    return OperationalRecordSet.model_validate(obj)


def test_all_emails_on_acme_example() -> None:
    rs = _load_record_set()
    assert rs.people is not None
    for person in rs.people:
        assert person.email.endswith("@acme.example")

    content = json.loads((FIXTURES / "content.json").read_bytes())
    for body in content.values():
        for raw_match in _EMAIL_RE.findall(body):
            # Strip trailing sentence punctuation the regex's greedy domain
            # group can pick up (e.g. "...acme.example." at a sentence end).
            match = raw_match.rstrip(".,;:)")
            assert match.endswith("@acme.example"), f"non-synthetic-looking email: {match}"


def test_all_domains_reserved() -> None:
    rs = _load_record_set()
    assert rs.accounts is not None
    for account in rs.accounts:
        assert account.domain is not None and account.domain.endswith(".example")


def test_ssn_shaped_strings_invalid() -> None:
    for name in ARTIFACT_NAMES:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        for match in _SSN_SHAPED_RE.findall(text):
            assert match.startswith("000-"), f"non-reserved SSN-shaped string in {name}: {match}"


def test_phones_fictional_range() -> None:
    rs = _load_record_set()
    assert rs.people is not None
    for person in rs.people:
        assert person.phone is not None
        assert _PHONE_RE.match(person.phone), f"phone outside fictional range: {person.phone}"
