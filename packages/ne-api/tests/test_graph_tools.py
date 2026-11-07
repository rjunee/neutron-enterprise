# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused current-authority and opaque-result controls for the graph Tool."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from ne.api.graph_tools import GRAPH_PATH_MAX_HOPS, StampedGraphPath, build_graph_path_binding
from ne.api.knowledge_source_evidence import ClaimedKnowledgeResult, ExistingKnowledgeRead
from ne.contracts.source_authority import SourceAuthorityDecision
from ne.contracts.types import GraphEntityRef, GraphHop, GraphPath, Principal
from ne.runtime.factory import ExistingRunEvidenceContext
from ne.runtime.run_store import EvidenceRecord

from .test_knowledge_source_evidence import _stamp

_PRINCIPAL = Principal(id="principal-1", groups=("analyst",))


async def _invoke(
    handler: Callable[[Mapping[str, object]], Awaitable[str]], args: Mapping[str, object]
) -> str:
    return await handler(args)


def _path() -> GraphPath:
    return GraphPath(
        hops=(
            GraphHop(
                subject=GraphEntityRef(entity_class="Account", entity_key="a"),
                edge_kind="OWNS",
                obj=GraphEntityRef(entity_class="Account", entity_key="b"),
                source_system="governed_local_t3",
                source_ref="row-1",
            ),
        )
    )


class _Graph:
    def __init__(self, candidate: StampedGraphPath) -> None:
        self.candidate = candidate
        self.calls: list[dict[str, Any]] = []

    async def paths(self, **kwargs: Any) -> tuple[StampedGraphPath, ...]:
        self.calls.append(kwargs)
        return (self.candidate,)


class _Reader:
    def __init__(self, decision: SourceAuthorityDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, stamp: Any, **kwargs: Any) -> Any:
        self.calls.append({"stamp": stamp, **kwargs})
        return SimpleNamespace(decision=self.decision)


class _Author:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def claim(self, **kwargs: Any) -> ClaimedKnowledgeResult:
        self.calls.append(kwargs)
        return ClaimedKnowledgeResult(
            protected_ref="ne-protected:opaque",
            evidence=EvidenceRecord(
                evidence_id="evidence-1",
                invocation_key="invocation-1",
                locator_status="located",
                locator_id="locator-1",
                pending_result_id="pending-1",
                producing_event_id="event-1",
            ),
        )


def _existing() -> ExistingKnowledgeRead:
    return ExistingKnowledgeRead(
        evidence=ExistingRunEvidenceContext(
            run_id="run-1", step_id="step-1", tool_call_id="call-1", read_ordinal=0
        ),
        owner_principal_id=_PRINCIPAL.id,
        attempt_id="attempt-1",
        turn_id="turn-1",
    )


def test_graph_path_rechecks_every_hop_and_returns_only_opaque_ref() -> None:
    candidate = StampedGraphPath(path=_path(), hop_stamps=((_stamp(),),))
    graph = _Graph(candidate)
    reader = _Reader(SourceAuthorityDecision.AUTHORIZED)
    author = _Author()
    binding = build_graph_path_binding(
        graph,
        authority_reader=reader,  # type: ignore[arg-type]
        access_context_hash_provider=lambda principal: "access-hash",
        principal_provider=lambda: _PRINCIPAL,
        evidence_author=author,  # type: ignore[arg-type]
        evidence_context_provider=_existing,
    )

    result = asyncio.run(_invoke(binding.handler, {"subject": "a", "object": "b"}))

    assert result == "ne-protected:opaque"
    assert graph.calls == [
        {
            "principal": _PRINCIPAL,
            "subject": "a",
            "obj": "b",
            "max_hops": GRAPH_PATH_MAX_HOPS,
        }
    ]
    assert reader.calls[0]["principal_id"] == _PRINCIPAL.id
    assert reader.calls[0]["access_context_hash"] == "access-hash"
    assert author.calls[0]["protected_result"] != result.encode()
    assert author.calls[0]["read_kind"] == "graph_path"
    assert author.calls[0]["assurance"].value == "inferred"


def test_one_unknown_hop_drops_whole_path_before_evidence_effect() -> None:
    candidate = StampedGraphPath(path=_path(), hop_stamps=((_stamp(),),))
    reader = _Reader(SourceAuthorityDecision.RETRYABLE_UNKNOWN)
    author = _Author()
    binding = build_graph_path_binding(
        _Graph(candidate),
        authority_reader=reader,  # type: ignore[arg-type]
        access_context_hash_provider=lambda principal: "access-hash",
        principal_provider=lambda: _PRINCIPAL,
        evidence_author=author,  # type: ignore[arg-type]
        evidence_context_provider=_existing,
    )

    with pytest.raises(LookupError, match="graph path unavailable"):
        asyncio.run(_invoke(binding.handler, {"subject": "a", "object": "b"}))
    assert author.calls == []


def test_absent_existing_run_context_has_zero_read_or_evidence_effects() -> None:
    candidate = StampedGraphPath(path=_path(), hop_stamps=((_stamp(),),))
    graph = _Graph(candidate)
    author = _Author()
    binding = build_graph_path_binding(
        graph,
        authority_reader=_Reader(SourceAuthorityDecision.AUTHORIZED),  # type: ignore[arg-type]
        access_context_hash_provider=lambda principal: "access-hash",
        principal_provider=lambda: _PRINCIPAL,
        evidence_author=author,  # type: ignore[arg-type]
        evidence_context_provider=lambda: None,
    )

    with pytest.raises(LookupError, match="existing canonical Run"):
        asyncio.run(_invoke(binding.handler, {"subject": "a", "object": "b"}))
    assert graph.calls == []
    assert author.calls == []
