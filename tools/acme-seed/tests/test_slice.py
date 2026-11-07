# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The deterministic slice: pinned ids, per-class balance, trap invariants."""

from __future__ import annotations

import json

from ne.acme.cli import default_fixtures_dir
from ne.acme_seed.slice import slice_corpus

# The pinned answer computed once against the committed corpus (catches drift).
PINNED_PUBLIC = ["doc-0011", "doc-0012", "doc-0017"]
PINNED_DEPARTMENT = ["doc-0000", "doc-0061", "doc-0062"]
PINNED_EXEC_ONLY = ["doc-0010", "doc-0021", "doc-0027"]
PINNED_NAMED = ["doc-0077", "doc-0310", "doc-0377"]
PINNED_ALL = sorted(PINNED_PUBLIC + PINNED_DEPARTMENT + PINNED_EXEC_ONLY + PINNED_NAMED)


def _exec_members() -> set[str]:
    org = json.loads((default_fixtures_dir() / "org.json").read_text(encoding="utf-8"))
    for group in org["groups"]:
        if group["name"] == "executive":
            return set(group["member_ids"])
    raise AssertionError("no executive group in the corpus")


def test_slice_is_deterministic_and_pinned() -> None:
    first = slice_corpus()
    second = slice_corpus()
    assert first == second
    assert [d.doc_id for d in first.docs] == PINNED_ALL
    # The named trio is the sharpest-trap anchor — pin it explicitly.
    named = [d.doc_id for d in first.docs if d.visibility == "named"]
    assert named == PINNED_NAMED


def test_slice_has_three_docs_per_visibility_class() -> None:
    selection = slice_corpus()
    assert [d.doc_id for d in selection.docs_by_visibility("public")] == PINNED_PUBLIC
    assert [d.doc_id for d in selection.docs_by_visibility("department")] == PINNED_DEPARTMENT
    assert [d.doc_id for d in selection.docs_by_visibility("exec_only")] == PINNED_EXEC_ONLY
    assert [d.doc_id for d in selection.docs_by_visibility("named")] == PINNED_NAMED


def test_named_docs_exclude_executives_and_grant_a_persona() -> None:
    selection = slice_corpus()
    execs = _exec_members()
    anchors = {"doc-0310": "per-0006", "doc-0377": "per-0016", "doc-0077": "per-0038"}
    for doc in selection.docs_by_visibility("named"):
        assert not (set(doc.allowed_people) & execs), doc.doc_id
        assert anchors[doc.doc_id] in doc.allowed_people
        # The persona grant computed for M365 must be exactly the anchor persona,
        # never a group, never an exec.
        assert selection.named_persona_grants(doc) == (anchors[doc.doc_id],)


def test_department_docs_grant_executive_group() -> None:
    selection = slice_corpus()
    for doc in selection.docs_by_visibility("department"):
        assert "executive" in doc.allowed_groups, doc.doc_id


def test_record_slice_counts_and_referential_integrity() -> None:
    selection = slice_corpus()
    assert len(selection.tickets) == 15
    assert len(selection.people) == 31
    person_ids = {p["id"] for p in selection.people}
    assert "per-0038" in person_ids

    account_ids = {a["id"] for a in selection.accounts}
    # Every works_at / belongs_to of the selected records resolves inside the slice.
    for person in selection.people:
        works_at = person.get("works_at")
        if works_at:
            assert works_at in account_ids
    for ticket in selection.tickets:
        belongs_to = ticket.get("belongs_to")
        if belongs_to:
            assert belongs_to in account_ids

    # accounts == the 20-lowest union works_at union belongs_to closure.
    expected = set(sorted(account_ids))
    assert len(selection.accounts) == len(expected)
