# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``assurance`` provenance label on graph-aware citation types (ADR-0014, 1.11.0).

``GraphEntityRef`` / ``GraphHop`` gain a defaulted ``assurance: str = "source"``
field — additive, frozen, hashable. ``"source"`` = structured backbone (the
default, and the semantics of every pre-1.11 constructor/payload);
``"extracted"`` = produced by the optional NE-T2X LLM-extraction pathway,
not yet vouched for by a human; ``"confirmed"`` = extracted and subsequently
human-approved. This module test-locks the epic's F2 floor (truthful
provenance): the field is
what lets a consumer always distinguish a fabricated/LLM-extracted graph
element from a backbone one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.types import GraphEntityRef, GraphHop


def _hop() -> GraphHop:
    return GraphHop(
        subject=GraphEntityRef(entity_class="Ticket", entity_key="k1", display="Login issue"),
        edge_kind="BELONGS_TO",
        obj=GraphEntityRef(entity_class="Account", entity_key="k2", display="Acme"),
        source_system="salesforce",
        source_ref="Case:K1",
    )


def test_default_assurance_is_source() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k")
    assert ref.assurance == "source"

    hop = _hop()
    assert hop.assurance == "source"
    assert hop.subject.assurance == "source"
    assert hop.obj.assurance == "source"


def test_legacy_payload_validates_to_source() -> None:
    ref = GraphEntityRef.model_validate(
        {"entity_class": "Account", "entity_key": "k", "display": "Acme"}
    )
    assert ref.assurance == "source"

    hop = GraphHop.model_validate(
        {
            "subject": {"entity_class": "Ticket", "entity_key": "k1", "display": "Login issue"},
            "edge_kind": "BELONGS_TO",
            "obj": {"entity_class": "Account", "entity_key": "k2", "display": "Acme"},
            "source_system": "salesforce",
            "source_ref": "Case:K1",
        }
    )
    assert hop.assurance == "source"
    assert hop.subject.assurance == "source"
    assert hop.obj.assurance == "source"


def test_dump_includes_assurance() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k")
    dumped_ref = ref.model_dump()
    assert dumped_ref["assurance"] == "source"

    hop = _hop()
    dumped_hop = hop.model_dump()
    assert dumped_hop["assurance"] == "source"
    assert dumped_hop["subject"]["assurance"] == "source"
    assert dumped_hop["obj"]["assurance"] == "source"


def test_assurance_round_trips() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k", assurance="extracted")
    restored_ref = GraphEntityRef.model_validate(ref.model_dump())
    assert restored_ref == ref
    assert restored_ref.assurance == "extracted"

    hop = GraphHop(
        subject=GraphEntityRef(entity_class="Ticket", entity_key="k1"),
        edge_kind="BELONGS_TO",
        obj=GraphEntityRef(entity_class="Account", entity_key="k2"),
        source_system="ne:llm",
        source_ref="llm:run1:chunk1",
        assurance="confirmed",
    )
    restored_hop = GraphHop.model_validate(hop.model_dump())
    assert restored_hop == hop
    assert restored_hop.assurance == "confirmed"


def test_assurance_distinguishes_otherwise_identical_elements() -> None:
    backbone = GraphEntityRef(entity_class="Account", entity_key="k", display="Acme")
    extracted = GraphEntityRef(
        entity_class="Account", entity_key="k", display="Acme", assurance="extracted"
    )
    assert backbone != extracted
    collected: set[int] = {hash(backbone), hash(extracted)}
    assert len(collected) == 2


def test_hop_assurance_independent_of_refs() -> None:
    hop = GraphHop(
        subject=GraphEntityRef(entity_class="Ticket", entity_key="k1"),
        edge_kind="BELONGS_TO",
        obj=GraphEntityRef(entity_class="Account", entity_key="k2"),
        source_system="ne:llm",
        source_ref="llm:run1:chunk1",
        assurance="extracted",
    )
    assert hop.assurance == "extracted"
    assert hop.subject.assurance == "source"
    assert hop.obj.assurance == "source"

    dumped = hop.model_dump()
    assert dumped["assurance"] == "extracted"
    assert dumped["subject"]["assurance"] == "source"
    assert dumped["obj"]["assurance"] == "source"


def test_assurance_is_plain_str_not_validated_here() -> None:
    # Documenting pin (ADR-0014 point 3): the closed value set is a
    # writer/config-seam concern, not something this frozen carrier type
    # validates. Any string constructs successfully here.
    ref = GraphEntityRef(entity_class="Account", entity_key="k", assurance="anything-else")
    assert ref.assurance == "anything-else"


def test_graph_entity_ref_source_ref_defaults_empty() -> None:
    """ADR-0015: pre-1.12 constructor calls stay valid, landing on the
    'not threaded through this layer' default — never a fabricated claim of
    'no provenance'."""
    ref = GraphEntityRef(entity_class="Account", entity_key="k")
    assert ref.source_ref == ""

    legacy = GraphEntityRef.model_validate(
        {"entity_class": "Account", "entity_key": "k", "display": "Acme"}
    )
    assert legacy.source_ref == ""


def test_graph_entity_ref_threads_source_ref() -> None:
    """ADR-0015: a set source_ref round-trips through model_validate(model_dump())
    and participates in equality/hashing like every other field."""
    ref = GraphEntityRef(
        entity_class="Person",
        entity_key="p1",
        display="Jane",
        assurance="extracted",
        source_ref="llm:0123456789abcdef:doc#c1",
    )
    dumped = ref.model_dump()
    assert dumped["source_ref"] == "llm:0123456789abcdef:doc#c1"

    restored = GraphEntityRef.model_validate(dumped)
    assert restored == ref
    assert restored.source_ref == "llm:0123456789abcdef:doc#c1"

    other = GraphEntityRef(
        entity_class="Person", entity_key="p1", display="Jane", assurance="extracted"
    )
    assert other.source_ref == ""
    assert other != ref
    assert hash(other) != hash(ref)


def test_assurance_stays_frozen_and_hashable() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k", assurance="extracted")
    with pytest.raises(ValidationError):
        ref.assurance = "confirmed"  # type: ignore[misc]

    hop = _hop()
    with pytest.raises(ValidationError):
        hop.assurance = "extracted"  # type: ignore[misc]

    other = GraphEntityRef(entity_class="Account", entity_key="k", assurance="extracted")
    assert hash(ref) == hash(other)
    assert ref == other
