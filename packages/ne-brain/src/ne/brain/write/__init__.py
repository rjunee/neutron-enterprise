# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed KnowledgeWrite surface (NE-023).

``KnowledgeWriter`` implements the frozen ``ne.contracts.knowledge.KnowledgeWrite``
Protocol over the real NE-021 ``QdrantChunkStore``: shape-validated, provenance-
stamped, ACL-carrying, audit-emitting writes with an inspectable quarantine lane
and ``ShapeViolation`` signals. See :mod:`ne.brain.write.writer`.
"""

from __future__ import annotations

from ne.brain.write.contracts import (
    CuratedKnowledgeItem,
    InvalidPatchError,
    KnowledgeAuditError,
    KnowledgeItemNotFoundError,
    KnowledgeWriteError,
    ProvenanceClass,
    ProvenanceStamp,
    ShapeContract,
    ShapeValidationError,
    SinkRequiredError,
    UnknownShapeError,
    provenance_from_chunk,
    stamped_metadata,
)
from ne.brain.write.immutable_artifacts import (
    AUTHORITY_ARTIFACT_CANDIDATE_KEY,
    AUTHORITY_ARTIFACT_PAYLOAD_KEY,
    ImmutableArtifactContentMismatchError,
    ImmutableArtifactError,
    ImmutableArtifactVectorSeam,
    ImmutableKnowledgeArtifactStore,
    ensure_immutable_artifact_schema,
)
from ne.brain.write.quarantine import (
    QuarantinedWrite,
    SqliteWriteQuarantine,
    WriteQuarantine,
)
from ne.brain.write.sinks import OutboxShapeViolationSink, ShapeViolationSink
from ne.brain.write.writer import KnowledgeWriter

__all__ = [
    "AUTHORITY_ARTIFACT_CANDIDATE_KEY",
    "AUTHORITY_ARTIFACT_PAYLOAD_KEY",
    "CuratedKnowledgeItem",
    "ImmutableArtifactContentMismatchError",
    "ImmutableArtifactError",
    "ImmutableArtifactVectorSeam",
    "ImmutableKnowledgeArtifactStore",
    "InvalidPatchError",
    "KnowledgeAuditError",
    "KnowledgeItemNotFoundError",
    "KnowledgeWriteError",
    "KnowledgeWriter",
    "OutboxShapeViolationSink",
    "ProvenanceClass",
    "ProvenanceStamp",
    "QuarantinedWrite",
    "ShapeContract",
    "ShapeValidationError",
    "ShapeViolationSink",
    "SinkRequiredError",
    "SqliteWriteQuarantine",
    "UnknownShapeError",
    "WriteQuarantine",
    "ensure_immutable_artifact_schema",
    "provenance_from_chunk",
    "stamped_metadata",
]
