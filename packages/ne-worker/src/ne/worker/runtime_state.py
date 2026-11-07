# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Worker process-global state — the composed Platform + the frame-writer factory.

The durable turn workflow (``workflows.py``) is module-level with serializable
args; it obtains its governed resources (the ONE sink/gate/ledger/executor/session
store) and its frame writer from THESE process globals — never from args (the
``approvals_durable.py`` house pattern). ``main.py`` sets them ONCE at boot, BEFORE
``DBOS.launch`` recovers any PENDING workflow.

Fail loud: reading an unset global raises ``RuntimeError`` — a workflow must never
run against a half-built or missing composition root.
"""

from __future__ import annotations

from collections.abc import Callable

from ne.api.platform import Platform
from ne.worker.relay import TurnFrameWriter

_platform: Platform | None = None
_frame_writer_factory: Callable[[str], TurnFrameWriter] | None = None


def set_worker_platform(platform: Platform) -> None:
    """Install the composed platform (called ONCE at boot before ``DBOS.launch``)."""
    global _platform
    _platform = platform


def worker_platform() -> Platform:
    """Return the composed platform, or raise if the root is not built yet."""
    if _platform is None:
        raise RuntimeError(
            "worker_platform() called before set_worker_platform(); the composition "
            "root must be built before any workflow runs (fail loud)"
        )
    return _platform


def set_frame_writer_factory(factory: Callable[[str], TurnFrameWriter]) -> None:
    """Install the per-turn frame-writer factory (DBOS stream vs recording)."""
    global _frame_writer_factory
    _frame_writer_factory = factory


def frame_writer(turn_id: str) -> TurnFrameWriter:
    """Build the frame writer for ``turn_id``, or raise if unset (fail loud)."""
    if _frame_writer_factory is None:
        raise RuntimeError(
            "frame_writer() called before set_frame_writer_factory(); the writer "
            "factory must be installed at boot (fail loud)"
        )
    return _frame_writer_factory(turn_id)


def reset_worker_state_for_testing() -> None:
    """Clear the process globals (unit tests that reconfigure the root)."""
    global _platform, _frame_writer_factory
    _platform = None
    _frame_writer_factory = None
