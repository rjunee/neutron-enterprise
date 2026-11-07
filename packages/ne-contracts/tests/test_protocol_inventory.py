# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-A receipt: the 32 SPEC section 4 Protocols exist and ARE Protocols.

Enumerated protocol-by-protocol against SPEC section 4:
Knowledge (9) + Models (2) + Governance (5) + Runtime (6) + Integration (3) +
Identity (2) + Secrets (1) + Storage (1) + Eval/obs (3) = 32.
"""

from __future__ import annotations

import ne.contracts as contracts

SPEC_S4_PROTOCOLS: list[str] = [
    # Knowledge (9)
    "KnowledgeRead",
    "KnowledgeWrite",
    "ChunkStore",
    "Embedder",
    "SparseEncoder",
    "Reranker",
    "GraphStore",
    "TripleStore",
    "ShapeValidator",
    # Models (2)
    "ModelGateway",
    "ModelRegistry",
    # Governance (5)
    "PolicyDecisionPoint",
    "Guardrail",
    "Redactor",
    "AuditSink",
    "ApprovalGate",
    # Runtime (6)
    "AgentSession",
    "ToolRegistry",
    "SubAgentDispatcher",
    "Scheduler",
    "MemoryProvider",
    "ChatAdapter",
    # Integration (3)
    "Connector",
    "ActionTool",
    "DataContract",
    # Identity (2)
    "PrincipalResolver",
    "TokenVerifier",
    # Secrets (1)
    "SecretStore",
    # Storage (1)
    "ObjectStore",
    # Eval/obs (3)
    "EvalSuite",
    "SignalEmitter",
    "KpiRecorder",
]


def _is_protocol(cls: object) -> bool:
    return bool(getattr(cls, "_is_protocol", False))


def test_exactly_32_protocols_enumerated() -> None:
    assert len(SPEC_S4_PROTOCOLS) == 32
    assert len(set(SPEC_S4_PROTOCOLS)) == 32


def test_every_protocol_is_exported_and_is_a_protocol() -> None:
    for name in SPEC_S4_PROTOCOLS:
        assert hasattr(contracts, name), f"{name} not exported from ne.contracts"
        cls = getattr(contracts, name)
        assert _is_protocol(cls), f"{name} is not a typing.Protocol"


def test_all_protocols_in_dunder_all() -> None:
    for name in SPEC_S4_PROTOCOLS:
        assert name in contracts.__all__, f"{name} missing from __all__"


SOURCE_AUTHORITY_PROTOCOLS: list[str] = [
    "AuthorizedContinuationTokenCodec",
    "CurrentAuthorizedArtifactSelector",
    "CurrentSourceAuthorityReader",
    "CurrentSourceAuthorityWriter",
    "ImmutableKnowledgeArtifactWrite",
    "ProtectedSourceExecutionWriter",
    "ProtectedSourceOutputWriter",
    "SourceBoundEvidenceProjectionResolver",
    "SourceBoundResultResolver",
]


def test_source_authority_protocols_are_additive_not_checkpoint_a_redefinitions() -> None:
    assert len(SPEC_S4_PROTOCOLS) == 32
    assert set(SOURCE_AUTHORITY_PROTOCOLS).isdisjoint(SPEC_S4_PROTOCOLS)
    for name in SOURCE_AUTHORITY_PROTOCOLS:
        assert name in contracts.__all__
        assert _is_protocol(getattr(contracts, name))
