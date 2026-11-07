# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-events."""

from __future__ import annotations


def test_import_and_version() -> None:
    import ne.events

    assert ne.events.__version__ == "0.1.0"


def test_public_api_surface() -> None:
    import ne.events as ev

    for name in (
        "append",
        "read_batch",
        "ack",
        "drain_once",
        "ensure_outbox_schema",
        "OutboxRecord",
        "DrainResult",
        "OutboxError",
        "ConsumerFilterMismatchError",
        "CursorRegressionError",
        "new_audit_event",
        "new_correction_signal",
        "new_approval_event",
        "new_cost_event",
        "new_shape_violation",
        "current_run_trace_id",
        "run_trace_scope",
    ):
        assert hasattr(ev, name), f"ne.events missing public symbol {name!r}"
