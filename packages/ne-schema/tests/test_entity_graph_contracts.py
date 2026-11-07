# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-SCHEMA: the Tier-2 entity-graph contracts (NE-081, 4th codegen artifact).

Cross-checks the generated ``operational_graph.py`` against the LinkML source by
REGEX-scanning the YAML text for ``inlined: false`` slot blocks (the
``test_versioning.py`` regex-not-yaml precedent — the codegen venv is the only
place a YAML parser lives). Also pins the frozen/extra-forbid discipline and the
identifier allowlists the Tier-2 cypher builder validates against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ne.schema import (
    EDGE_DEFS,
    EDGE_KINDS,
    ENTITY_CLASSES,
    ENTITY_CONTRACTS,
    AccountEntity,
    EdgeDef,
)

_SCHEMA_YAML = (
    Path(__file__).resolve().parents[1] / "src" / "ne" / "schema" / "schemas" / "operational.yaml"
)

_EXPECTED_ENTITY_CLASSES = {
    "Account",
    "Person",
    "Document",
    "Ticket",
    "Order",
    "Customer",
    "AutoLoan",
    "PaymentSchedule",
    "ServicingNote",
}
_RELATIONSHIP_SLOT_NAMES = {
    "works_at",
    "author",
    "owned_by",
    "about",
    "belongs_to",
    "requester",
    "assignee",
    "account",
    "placed_by",
    "has_auto_loan",
    "has_payment_schedule",
    "has_servicing_note",
}


def _yaml_inlined_false_slots() -> set[str]:
    """Return the set of slot names carrying ``inlined: false`` in the YAML.

    A slot block opens at ``  <name>:`` (4-space indent under ``attributes:``)
    and the ``inlined: false`` marker sits deeper in the same block; we walk the
    text tracking the most recent attribute name and record it when the marker
    appears before the next attribute begins.
    """
    text = _SCHEMA_YAML.read_text(encoding="utf-8")
    found: set[str] = set()
    current: str | None = None
    attr_re = re.compile(r"^      (\w+):\s*$")
    for line in text.splitlines():
        m = attr_re.match(line)
        if m:
            current = m.group(1)
        elif current is not None and re.match(r"^\s+inlined:\s*false\s*$", line):
            found.add(current)
    return found


def test_entity_classes_exact() -> None:
    assert set(ENTITY_CLASSES) == _EXPECTED_ENTITY_CLASSES
    assert set(ENTITY_CONTRACTS) == _EXPECTED_ENTITY_CLASSES


def test_edge_defs_cover_exactly_the_inlined_false_slots() -> None:
    yaml_slots = _yaml_inlined_false_slots()
    yaml_edge_count = len(
        re.findall(r"(?m)^\s+inlined:\s*false\s*$", _SCHEMA_YAML.read_text(encoding="utf-8"))
    )
    assert yaml_slots == _RELATIONSHIP_SLOT_NAMES, (
        "the YAML-scanned inlined:false slots drifted from the expected set"
    )
    edge_slot_names = {e.slot_name for e in EDGE_DEFS}
    assert edge_slot_names == yaml_slots, (
        "EDGE_DEFS must cover exactly the inlined:false relationship slots"
    )
    # A semantic slot name may be reused by multiple endpoint triples (Meridian's
    # Customer and AutoLoan both have HAS_SERVICING_NOTE), so cardinality must
    # follow the YAML declarations rather than the de-duplicated slot-name set.
    assert len(EDGE_DEFS) == yaml_edge_count


def test_edge_kinds_are_upper_snake_of_slot_names() -> None:
    assert frozenset(e.edge_kind for e in EDGE_DEFS) == EDGE_KINDS
    for edge in EDGE_DEFS:
        assert edge.edge_kind == edge.slot_name.upper()


def test_known_edge_definitions() -> None:
    by_kind = {e.edge_kind: e for e in EDGE_DEFS}
    assert by_kind["BELONGS_TO"] == EdgeDef(
        "BELONGS_TO", "Ticket", "belongs_to", "Account", False, True
    )
    assert by_kind["ABOUT"].multivalued is True
    assert by_kind["ACCOUNT"].required is True
    assert by_kind["WORKS_AT"].required is False


def test_meridian_edge_definitions_are_exact_and_directed() -> None:
    meridian_classes = {"Customer", "AutoLoan", "PaymentSchedule", "ServicingNote"}
    actual = {
        edge
        for edge in EDGE_DEFS
        if edge.subject_class in meridian_classes or edge.object_class in meridian_classes
    }
    assert actual == {
        EdgeDef("HAS_AUTO_LOAN", "Customer", "has_auto_loan", "AutoLoan", True, False),
        EdgeDef(
            "HAS_SERVICING_NOTE",
            "Customer",
            "has_servicing_note",
            "ServicingNote",
            True,
            False,
        ),
        EdgeDef(
            "HAS_PAYMENT_SCHEDULE",
            "AutoLoan",
            "has_payment_schedule",
            "PaymentSchedule",
            True,
            True,
        ),
        EdgeDef(
            "HAS_SERVICING_NOTE",
            "AutoLoan",
            "has_servicing_note",
            "ServicingNote",
            True,
            True,
        ),
    }
    assert not any(
        edge.subject_class in {"PaymentSchedule", "ServicingNote"}
        and edge.object_class in {"Customer", "AutoLoan"}
        for edge in EDGE_DEFS
    )


def test_contracts_exclude_relationship_slots() -> None:
    for entity_class, model in ENTITY_CONTRACTS.items():
        fields = set(model.model_fields)
        overlap = fields & _RELATIONSHIP_SLOT_NAMES
        assert not overlap, f"{entity_class} contract leaks relationship slot(s): {overlap}"


def test_contracts_are_frozen_and_forbid_extras() -> None:
    account = AccountEntity(id="a1", name="Acme")
    with pytest.raises(ValidationError):
        AccountEntity(id="a1", name="Acme", not_a_field="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        account.name = "Other"  # type: ignore[misc]


def test_contract_required_and_provenance_fields() -> None:
    # Provenance slots are optional; identity + declared-required attrs are required.
    with pytest.raises(ValidationError):
        AccountEntity(name="Acme")  # type: ignore[call-arg]  # missing id
    ok = AccountEntity(id="a1", name="Acme", source_system="salesforce")
    assert ok.source_record_id is None
    assert ok.source_system == "salesforce"
