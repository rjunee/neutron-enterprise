# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal-bound, capped ``read.knowledge.graph_path`` Tool adapter."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ne.api.knowledge_source_evidence import ExistingKnowledgeRead, KnowledgeSourceEvidenceAuthor
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    EvidenceAssurance,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
)
from ne.contracts.types import (
    ApprovalTier,
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Principal,
)
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.tools import ToolBinding

GRAPH_PATH_TOOL_NAME = "read.knowledge.graph_path"
GRAPH_PATH_MAX_HOPS = 6
GRAPH_PATH_ARG_POLICY = ToolArgPolicy(
    args={
        "subject": ArgSpec(kind=ToolArgKind.identifier, required=True),
        "object": ArgSpec(kind=ToolArgKind.identifier, required=True),
    }
)


@dataclass(frozen=True, slots=True)
class StampedGraphPath:
    """One already-trimmed path with the exact retained stamps for every hop."""

    path: GraphPath
    hop_stamps: tuple[tuple[SourceAuthorityStamp, ...], ...]


class PrincipalGraphPathRead(Protocol):
    async def paths(
        self, *, principal: Principal, subject: str, obj: str, max_hops: int
    ) -> Sequence[StampedGraphPath]: ...


class EntityGraphPathRead:
    """Adapt the production EntityGraph stamped path seam without reconstruction."""

    def __init__(self, graph: object) -> None:
        self._graph = graph

    async def paths(
        self, *, principal: Principal, subject: str, obj: str, max_hops: int
    ) -> Sequence[StampedGraphPath]:
        from ne.brain_graph.model import EntityKey

        read = getattr(self._graph, "paths_with_source_stamps", None)
        if not callable(read):
            return ()
        stamped_read = cast(Callable[..., Awaitable[Sequence[Any]]], read)
        records = await stamped_read(
            EntityKey(entity_class="", key=subject),
            EntityKey(entity_class="", key=obj),
            principal=principal,
            max_hops=max_hops,
            limit=1,
        )
        out: list[StampedGraphPath] = []
        for stamped in records:
            record = stamped.path
            if len(record.nodes) != len(record.edges) + 1:
                continue
            hops = tuple(
                GraphHop(
                    subject=GraphEntityRef(
                        entity_class=record.nodes[index].entity_class,
                        entity_key=record.nodes[index].key,
                        display=record.nodes[index].display_name,
                        assurance=record.nodes[index].assurance,
                        source_ref=record.nodes[index].source_ref,
                    ),
                    edge_kind=edge.edge_kind,
                    obj=GraphEntityRef(
                        entity_class=record.nodes[index + 1].entity_class,
                        entity_key=record.nodes[index + 1].key,
                        display=record.nodes[index + 1].display_name,
                        assurance=record.nodes[index + 1].assurance,
                        source_ref=record.nodes[index + 1].source_ref,
                    ),
                    source_system=edge.source_system,
                    source_ref=edge.source_ref,
                    assurance=edge.assurance,
                )
                for index, edge in enumerate(record.edges)
            )
            out.append(StampedGraphPath(path=GraphPath(hops=hops), hop_stamps=stamped.hop_stamps))
        return tuple(out)


def _path_bytes(path: GraphPath) -> bytes:
    return json.dumps(
        path.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


async def _currently_authorized(
    candidate: StampedGraphPath,
    *,
    principal: Principal,
    reader: CurrentSourceAuthorityReader,
    access_context_hash: str,
) -> tuple[SourceAuthorityStamp, ...] | None:
    if not candidate.path.hops or len(candidate.path.hops) > GRAPH_PATH_MAX_HOPS:
        return None
    if len(candidate.hop_stamps) != len(candidate.path.hops):
        return None
    contributors: list[SourceAuthorityStamp] = []
    for stamps in candidate.hop_stamps:
        if not stamps:
            return None
        for stamp in stamps:
            try:
                resolution = await reader.resolve(
                    stamp,
                    principal_id=principal.id,
                    access_context_hash=access_context_hash,
                )
            except Exception:
                return None
            if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                return None
            contributors.append(stamp)
    return tuple(dict.fromkeys(contributors))


def build_graph_path_binding(
    graph: PrincipalGraphPathRead,
    *,
    authority_reader: CurrentSourceAuthorityReader,
    access_context_hash_provider: Callable[[Principal], str],
    principal_provider: Callable[[], Principal | None],
    evidence_author: KnowledgeSourceEvidenceAuthor,
    evidence_context_provider: Callable[[], ExistingKnowledgeRead | None],
) -> ToolBinding:
    """Build the governed graph read; absent existing-Run context has zero effects."""

    async def _read(args: Mapping[str, object]) -> str:
        principal = principal_provider()
        existing = evidence_context_provider()
        if principal is None or existing is None or existing.owner_principal_id != principal.id:
            raise LookupError("existing canonical Run evidence context is unavailable")
        subject = str(args.get("subject", "")).strip()
        obj = str(args.get("object", "")).strip()
        if not subject or not obj:
            raise ValueError("subject and object are required")
        candidates = await graph.paths(
            principal=principal,
            subject=subject,
            obj=obj,
            max_hops=GRAPH_PATH_MAX_HOPS,
        )
        if not candidates:
            raise LookupError("graph path unavailable")
        candidate = candidates[0]
        stamps = await _currently_authorized(
            candidate,
            principal=principal,
            reader=authority_reader,
            access_context_hash=access_context_hash_provider(principal),
        )
        if stamps is None:
            raise LookupError("graph path unavailable")
        protected = _path_bytes(candidate.path)
        locator = {
            "kind": "knowledge_graph_path",
            "path": candidate.path.model_dump(mode="json"),
            "hop_source_stamps": [
                [stamp.model_dump(mode="json") for stamp in hop] for hop in candidate.hop_stamps
            ],
        }
        claimed = await evidence_author.claim(
            existing=existing,
            protected_result=protected,
            locator=locator,
            source_stamps=stamps,
            read_kind="graph_path",
            assurance=EvidenceAssurance.INFERRED,
        )
        return claimed.protected_ref

    return ToolBinding(
        spec=ToolSpec(
            name=GRAPH_PATH_TOOL_NAME,
            description=(
                "Read one governed compact knowledge graph path between two "
                "entities. Each endpoint is a record id (e.g. mer-loan-001) or "
                "an entity class (e.g. Customer) meaning the nearest connected "
                "entity of that class."
            ),
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Record id or entity class the path starts from",
                    },
                    "object": {
                        "type": "string",
                        "description": "Record id or entity class the path must reach",
                    },
                },
                "required": ["subject", "object"],
            },
        ),
        handler=_read,
    )
