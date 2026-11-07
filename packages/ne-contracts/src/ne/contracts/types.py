# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cross-domain value objects and enums shared by every contract domain.

All value objects are **frozen** pydantic models: attributes are non-reassignable
and every mapping field is coerced at validation to an immutable, hashable
:class:`~ne.contracts.frozen.FrozenDict` (recursively frozen values). Every value
object is therefore hashable, and in-place mutation of a mapping field raises
``TypeError``. A value that cannot be made hashable — anything that is not a
JSON-shaped scalar/list/dict/set after recursive freezing (e.g. a ``bytearray``
or an exotic mutable object) — is rejected at model construction with a clear
``TypeError`` rather than deferring to a later ``hash()`` failure. Domain-specific
value types live in their own domain module, not here.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict, StrMap


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True)


class Principal(_Frozen):
    """An authenticated subject and its group/claim context."""

    id: str
    groups: tuple[str, ...] = ()
    claims: StrMap = FrozenDict()


class SparseVector(_Frozen):
    """A sparse embedding: parallel index/value arrays."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


class Chunk(_Frozen):
    """A retrievable unit of text with provenance and access-control labels."""

    chunk_id: str
    document_id: str
    text: str
    metadata: AnyMap = FrozenDict()
    acl: tuple[str, ...] = ()


class ScoredChunk(_Frozen):
    """A chunk with a relevance score."""

    chunk: Chunk
    score: float


class Citation(_Frozen):
    """A pointer back to the source of record for an answer span."""

    chunk_id: str
    document_id: str
    source_system: str
    source_ref: str


class GraphEntityRef(_Frozen):
    """A pointer to a Tier-2 graph entity (ADR-0005, additive 1.3.0).

    ``assurance`` is the element-level provenance-assurance label (added
    1.11.0, ADR-0014): ``"source"`` = derived from a structured connector
    (the backbone; the default, and the semantics of every pre-1.11
    constructor/payload), ``"extracted"`` = produced by the optional
    LLM-extraction pathway and not yet vouched for by a human, ``"confirmed"``
    = extracted and subsequently human-approved. Plain ``str`` (not
    ``Literal``/``Enum``) deliberately — the closed value set is owned and
    validated at the writer/config seam (``ne-brain-graph``, later NE-T2X
    units), never here; the field is stamped by trusted write paths, never
    sourced from model output or caller requests (design A-P1-3/B-P0-1 —
    enforcement lands in NE-T2X-2/3).

    ``source_ref`` is the provenance ref of the record evidencing THIS
    entity (added 1.12.0, ADR-0015) — same semantics as
    :attr:`GraphHop.source_ref` for the edge, and mirrors
    ``ne.brain_graph.model.EntityNode.source_ref`` one layer down. For a
    ``ne:llm``-extracted entity it is the writer-stamped
    ``llm:<sha16>:<ref>`` grammar (``ne-brain-graph``'s ``ExtractionWriter``)
    that lets a consumer click through to the exact evidencing chunk (design
    §6.1). The default ``""`` exists ONLY for pre-1.12 payload/constructor
    compatibility and means "not threaded through this layer yet" — it is
    never a truthful claim of "no provenance". Opaque ref, never fact text
    or a display string (C12).
    """

    entity_class: str
    entity_key: str
    display: str = ""
    assurance: str = "source"
    source_ref: str = ""


class GraphHop(_Frozen):
    """One traversed relation in a graph-aware citation (ADR-0005).

    ``source_system`` / ``source_ref`` carry the provenance of the ASSERTING
    record — the same fields/semantics as :class:`Citation` — so every hop is
    independently click-through-able to its source of record.

    ``assurance`` (added 1.11.0, ADR-0014) labels the ASSERTING relation (the
    edge) itself and is independent of ``subject.assurance``/``obj.assurance``
    — an extracted edge may connect two backbone entities. Same three-value
    semantics as :attr:`GraphEntityRef.assurance`: ``"source"`` (structured
    backbone, the default), ``"extracted"`` (LLM-produced, unvouched),
    ``"confirmed"`` (extracted then human-approved).
    """

    subject: GraphEntityRef
    edge_kind: str
    obj: GraphEntityRef
    source_system: str
    source_ref: str
    assurance: str = "source"


class GraphPath(_Frozen):
    """An ordered, trimmed-by-construction traversal path (ADR-0005)."""

    hops: tuple[GraphHop, ...]


class RetrievalResult(_Frozen):
    """The result of a knowledge search: scored chunks plus citations.

    ``graph_paths`` (additive, ADR-0005, 1.3.0; default ``()``) carries the
    graph-aware citation traversals for a multi-hop answer. Every hop is trimmed
    by construction (only ever built from ``EntityGraph`` output), so a path
    never cites an untraversable node/edge.
    """

    chunks: tuple[ScoredChunk, ...]
    citations: tuple[Citation, ...]
    graph_paths: tuple[GraphPath, ...] = ()


class ChatMessage(_Frozen):
    """A single message in a model conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class Lane(StrEnum):
    """Model routing lane (C3 model independence)."""

    FRONTIER_API = "frontier-api"
    PRIVATE_VLLM = "private-vllm"


class ApprovalTier(IntEnum):
    """Approval tiers per SPEC section 4.2 (higher = stricter control)."""

    TIER_0 = 0
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3
