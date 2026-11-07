# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Graph-aware citation types — additive, frozen, hashable (ADR-0005, 1.3.0).

``GraphEntityRef`` / ``GraphHop`` / ``GraphPath`` are new frozen value objects and
``RetrievalResult`` gains a defaulted ``graph_paths`` field. Backward-compat: the
pre-1.3.0 ``RetrievalResult(chunks=(), citations=())`` constructor stays valid and
``graph_paths`` defaults to ``()``. Everything round-trips through
model_dump/model_validate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts import __version__
from ne.contracts.types import (
    GraphEntityRef,
    GraphHop,
    GraphPath,
    RetrievalResult,
)


def _hop() -> GraphHop:
    return GraphHop(
        subject=GraphEntityRef(entity_class="Ticket", entity_key="k1", display="Login issue"),
        edge_kind="BELONGS_TO",
        obj=GraphEntityRef(entity_class="Account", entity_key="k2", display="Acme"),
        source_system="salesforce",
        source_ref="Case:K1",
    )


def test_version_is_current() -> None:
    # Named for the property, not for one release: the name said 1.26.0 while
    # the assertion said 1.27.0, which is a second place for the version to
    # drift out of step with itself.
    assert __version__ == "1.29.0"


def test_new_types_are_frozen() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k")
    hop = _hop()
    path = GraphPath(hops=(hop,))
    with pytest.raises(ValidationError):
        ref.entity_key = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        hop.edge_kind = "X"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        path.hops = ()  # type: ignore[misc]


def test_new_types_are_hashable() -> None:
    a = GraphPath(hops=(_hop(),))
    b = GraphPath(hops=(_hop(),))
    assert hash(a) == hash(b)
    collected: set[int] = {hash(a), hash(b)}
    assert len(collected) == 1
    # frozen models are equal by value
    assert a == b


def test_graph_entity_ref_display_defaults_empty() -> None:
    ref = GraphEntityRef(entity_class="Account", entity_key="k")
    assert ref.display == ""


def test_retrieval_result_backward_compat_default_empty_paths() -> None:
    # The pre-1.3.0 constructor stays valid; graph_paths defaults to ().
    result = RetrievalResult(chunks=(), citations=())
    assert result.graph_paths == ()


def test_retrieval_result_graph_paths_round_trip() -> None:
    populated = RetrievalResult(chunks=(), citations=(), graph_paths=(GraphPath(hops=(_hop(),)),))
    dumped = populated.model_dump()
    assert dumped["graph_paths"][0]["hops"][0]["edge_kind"] == "BELONGS_TO"
    restored = RetrievalResult.model_validate(dumped)
    assert restored == populated
    assert restored.graph_paths[0].hops[0].source_ref == "Case:K1"


def test_retrieval_result_stays_frozen() -> None:
    result = RetrievalResult(chunks=(), citations=())
    with pytest.raises(ValidationError):
        result.graph_paths = (GraphPath(hops=()),)  # type: ignore[misc]
