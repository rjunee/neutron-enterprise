# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-audit — hash-chained, append-only audit sink with signed WORM checkpoints.

The durable, tamper-evident AUTHORITATIVE audit store (the ne-events outbox is
the export transport; this sink is the source of truth). See the package README
for the chain formula, the sink-vs-outbox relationship, the checkpoint/signing
design, the replay convention, and the pgAudit defense-in-depth note.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.audit import errors
from ne.audit.anchor import (
    CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S,
    CHAIN_ANCHOR_MAX_AGE_S,
    AnchoredChainVerifier,
    AnchoredVerification,
    VerificationBasis,
    VerifiedAnchor,
)
from ne.audit.checkpoint import (
    Checkpoint,
    CheckpointVerification,
    HmacSigner,
    maybe_checkpoint,
    verify_checkpoints,
    write_checkpoint,
)
from ne.audit.config import AuditSettings, CheckpointConfig
from ne.audit.engine import (
    EngineTimeouts,
    create_engine_with_timeouts,
    ensure_transactional_sqlite,
)
from ne.audit.factory import create_audit_sink
from ne.audit.replay import (
    MAX_PAGE_ROWS,
    AnchorResolution,
    ReplayResult,
    count_records,
    find_approval_decision,
    list_records,
    list_records_anchored,
    records_by_event_ids,
    replay,
)
from ne.audit.sink import PostgresAuditSink
from ne.audit.store import (
    GENESIS_HASH,
    AuditRecord,
    append_record,
    canonical_bytes,
    compute_record_hash,
    ensure_audit_schema,
)
from ne.audit.verify import (
    ChainVerification,
    ChainWalkObserver,
    register_chain_walk_observer,
    to_audit_verification,
    unregister_chain_walk_observer,
    verified_extension,
    verify_chain,
)

__all__ = [
    "CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S",
    "CHAIN_ANCHOR_MAX_AGE_S",
    "GENESIS_HASH",
    "MAX_PAGE_ROWS",
    "AnchorResolution",
    "AnchoredChainVerifier",
    "AnchoredVerification",
    "AuditRecord",
    "AuditSettings",
    "ChainVerification",
    "ChainWalkObserver",
    "Checkpoint",
    "CheckpointConfig",
    "CheckpointVerification",
    "EngineTimeouts",
    "HmacSigner",
    "PostgresAuditSink",
    "ReplayResult",
    "VerificationBasis",
    "VerifiedAnchor",
    "__version__",
    "append_record",
    "canonical_bytes",
    "compute_record_hash",
    "count_records",
    "create_audit_sink",
    "create_engine_with_timeouts",
    "ensure_audit_schema",
    "ensure_transactional_sqlite",
    "errors",
    "find_approval_decision",
    "list_records",
    "list_records_anchored",
    "maybe_checkpoint",
    "records_by_event_ids",
    "register_chain_walk_observer",
    "replay",
    "to_audit_verification",
    "unregister_chain_walk_observer",
    "verified_extension",
    "verify_chain",
    "verify_checkpoints",
    "write_checkpoint",
]
