# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test + public API surface for ne-audit."""

from __future__ import annotations

import ne.audit


def test_import_and_version() -> None:
    assert ne.audit.__version__ == "0.1.0"


def test_public_api_surface() -> None:
    expected = {
        "GENESIS_HASH",
        "AuditRecord",
        "AuditSettings",
        "ChainVerification",
        "Checkpoint",
        "CheckpointConfig",
        "CheckpointVerification",
        "EngineTimeouts",
        "HmacSigner",
        "PostgresAuditSink",
        "ReplayResult",
        "append_record",
        "canonical_bytes",
        "compute_record_hash",
        "create_audit_sink",
        "create_engine_with_timeouts",
        "ensure_audit_schema",
        "errors",
        "maybe_checkpoint",
        "replay",
        "to_audit_verification",
        "verify_chain",
        "verify_checkpoints",
        "write_checkpoint",
    }
    assert expected <= set(ne.audit.__all__)
    for name in expected:
        assert getattr(ne.audit, name) is not None


def test_errors_hierarchy() -> None:
    from ne.audit.errors import (
        AppendOnlyViolationError,
        AuditError,
        CheckpointSignatureError,
    )

    assert issubclass(AppendOnlyViolationError, AuditError)
    assert issubclass(CheckpointSignatureError, AuditError)
