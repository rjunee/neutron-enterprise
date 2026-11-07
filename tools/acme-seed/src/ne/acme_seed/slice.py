# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, lightest-sufficient SLICE of the committed Acme corpus (NE-102).

This module is PURE (its only I/O is reading the committed fixture dir): given
the ``tools/acme/fixtures/acme-v1`` corpus it selects a balanced ~12-doc /
~30-record slice that still exercises every ACL-trap class, and returns a frozen
:class:`SliceSelection`. No network, no secrets.

Doc slice (12 docs, everything selected by ascending ``doc_id``):

* PUBLIC     — the 3 lowest-id ``public`` docs.
* DEPARTMENT — for each of ``dept-engineering`` / ``dept-sales`` / ``dept-legal``,
  the lowest-id ``department`` doc granting that group (each ALSO grants
  ``executive`` — verified/asserted here, so P1 sees dept docs).
* EXEC_ONLY  — the 3 lowest-id ``exec_only`` docs.
* NAMED (the sharpest trap) — for each of P2/P3/P4 (``per-0006`` / ``per-0016`` /
  ``per-0038``) the lowest-id ``named`` doc whose ``allowed_people`` contains that
  persona AND contains ZERO executive-group members. Verified fixture answer:
  ``doc-0310`` (P2) / ``doc-0377`` (P3) / ``doc-0077`` (P4). P1 (an exec) must NOT
  be able to see these.

Record slice: the 15 lowest tickets; the 30 lowest people plus P4 (``per-0038``);
and the 20 lowest accounts unioned with the ``works_at`` of every selected person
and the ``belongs_to`` of every selected ticket (referential closure).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ne.acme.cli import default_fixtures_dir
from ne.acme.models import AclEntry, GroundTruth, OrgChart
from ne.acme_seed.errors import SeedError
from ne.acme_seed.personas import PERSONAS

__all__ = ["SliceDoc", "SliceFact", "SliceSelection", "slice_corpus"]

_DEPT_GROUPS: tuple[str, ...] = ("dept-engineering", "dept-sales", "dept-legal")
#: The named-trap personas, in ascending persona-id order (P2, P3, P4). P1
#: (per-0000, an exec) is deliberately excluded — named docs must trap execs.
_NAMED_PERSONAS: tuple[str, ...] = ("per-0006", "per-0016", "per-0038")

_TICKET_COUNT = 15
_PEOPLE_COUNT = 30
_ACCOUNT_COUNT = 20


class SliceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    statement: str
    question: str
    answer: str


class SliceDoc(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    visibility: str
    allowed_groups: tuple[str, ...]
    allowed_people: tuple[str, ...]
    content: str
    facts: tuple[SliceFact, ...]


class SliceSelection(BaseModel):
    """The frozen, deterministic selection pushed into every tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_version: str
    docs: tuple[SliceDoc, ...]
    accounts: tuple[dict[str, Any], ...]
    people: tuple[dict[str, Any], ...]
    tickets: tuple[dict[str, Any], ...]

    def docs_by_visibility(self, visibility: str) -> tuple[SliceDoc, ...]:
        return tuple(d for d in self.docs if d.visibility == visibility)

    def doc(self, doc_id: str) -> SliceDoc:
        for d in self.docs:
            if d.doc_id == doc_id:
                return d
        raise KeyError(doc_id)

    def named_persona_grants(self, doc: SliceDoc) -> tuple[str, ...]:
        """Personas in ``doc.allowed_people`` (never a group, never P1)."""
        return tuple(p for p in doc.allowed_people if p in PERSONAS)


def _load(fixtures: Path, name: str) -> Any:
    return json.loads((fixtures / name).read_text(encoding="utf-8"))


def slice_corpus(fixtures: Path | None = None) -> SliceSelection:
    """Compute the deterministic slice over the committed corpus."""
    fixtures = fixtures if fixtures is not None else default_fixtures_dir()

    records = _load(fixtures, "records.json")
    content: dict[str, str] = _load(fixtures, "content.json")
    manifest = _load(fixtures, "manifest.json")

    # Validate the sidecars (catches corpus drift loudly).
    acl_entries = [AclEntry.model_validate(a) for a in _load(fixtures, "acl.json")]
    org = OrgChart.model_validate(_load(fixtures, "org.json"))
    ground_truth = GroundTruth.model_validate(_load(fixtures, "ground_truth.json"))

    groups = {g.name: set(g.member_ids) for g in org.groups}
    execs = groups.get("executive", set())
    facts_by_doc = {d.doc_id: d.facts for d in ground_truth.docs}

    acl_sorted = sorted(acl_entries, key=lambda a: a.doc_id)

    def _first(pred: Any) -> AclEntry:
        for entry in acl_sorted:
            if pred(entry):
                return entry
        raise SeedError("corpus drift: no ACL entry satisfied a slice predicate")

    selected: list[AclEntry] = []
    selected += [a for a in acl_sorted if a.visibility == "public"][:3]
    selected += [a for a in acl_sorted if a.visibility == "exec_only"][:3]

    for group in _DEPT_GROUPS:
        entry = _first(lambda a, g=group: a.visibility == "department" and g in a.allowed_groups)
        if "executive" not in entry.allowed_groups:
            raise SeedError(
                f"corpus drift: department doc {entry.doc_id} does not also grant 'executive' "
                "(P1-sees-dept-docs invariant broken)"
            )
        selected.append(entry)

    for persona in _NAMED_PERSONAS:
        entry = _first(
            lambda a, p=persona: (
                a.visibility == "named"
                and p in a.allowed_people
                and not (set(a.allowed_people) & execs)
            )
        )
        selected.append(entry)

    # Sort the 12 selected docs by doc_id for a fully deterministic ordering.
    selected_sorted = sorted(selected, key=lambda a: a.doc_id)

    docs: list[SliceDoc] = []
    for entry in selected_sorted:
        body = content.get(entry.doc_id)
        if body is None:
            raise SeedError(f"corpus drift: doc {entry.doc_id} has no content.json body")
        facts = tuple(
            SliceFact(
                fact_id=f.fact_id, statement=f.statement, question=f.question, answer=f.answer
            )
            for f in facts_by_doc.get(entry.doc_id, [])
        )
        docs.append(
            SliceDoc(
                doc_id=entry.doc_id,
                visibility=entry.visibility,
                allowed_groups=tuple(entry.allowed_groups),
                allowed_people=tuple(entry.allowed_people),
                content=body,
                facts=facts,
            )
        )

    # -- record slice ---------------------------------------------------------
    people_by_id = {p["id"]: p for p in records["people"]}
    tickets_by_id = {t["id"]: t for t in records["tickets"]}
    accounts_by_id = {a["id"]: a for a in records["accounts"]}

    ticket_ids = sorted(tickets_by_id)[:_TICKET_COUNT]
    person_ids = set(sorted(people_by_id)[:_PEOPLE_COUNT]) | {"per-0038"}

    account_ids: set[str] = set(sorted(accounts_by_id)[:_ACCOUNT_COUNT])
    for pid in person_ids:
        works_at = people_by_id[pid].get("works_at")
        if works_at:
            account_ids.add(works_at)
    for tid in ticket_ids:
        belongs_to = tickets_by_id[tid].get("belongs_to")
        if belongs_to:
            account_ids.add(belongs_to)

    missing = {a for a in account_ids if a not in accounts_by_id}
    if missing:
        raise SeedError(
            f"corpus drift: referenced accounts missing from records: {sorted(missing)}"
        )

    accounts = tuple(accounts_by_id[a] for a in sorted(account_ids))
    people = tuple(people_by_id[p] for p in sorted(person_ids))
    tickets = tuple(tickets_by_id[t] for t in ticket_ids)

    return SliceSelection(
        corpus_version=str(manifest["corpus_version"]),
        docs=tuple(docs),
        accounts=accounts,
        people=people,
        tickets=tickets,
    )
