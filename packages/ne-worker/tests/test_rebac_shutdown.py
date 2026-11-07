# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-087 (O-4, FIX-2) — the ne-worker graceful shutdown closes the ReBAC client.

The worker root must release the retained confined OpenFGA client's owned async
HTTP session on shutdown, alongside ``DBOS.destroy()``/``engine.dispose()`` — else
an enabled worker leaks a session on every restart. Default-off (no client) stays a
no-op. Proven over the REAL ``build_platform`` (only the OpenFGA wire is faked).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ne.api.platform import build_platform
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.testing.fakes import InMemoryOpenFga
from ne.worker.main import _shutdown_platform

from .conftest import build_sqlite_platform, run, sqlite_seams


def _enabled() -> RebacSettings:
    return RebacSettings(enabled=True, store_id="s", authorization_model_id="m")


def test_worker_shutdown_closes_rebac_client(tmp_path: Path) -> None:
    fake = InMemoryOpenFga()
    settings, seams = sqlite_seams(tmp_path)
    seams = dataclasses.replace(seams, rebac_settings=_enabled(), rebac_client=fake)
    platform = build_platform(settings, seams)

    assert platform.rebac_client is fake
    assert fake.closed is False
    run(_shutdown_platform(platform))
    assert fake.closed is True
    # Engines are disposed (second dispose is idempotent — no raise).
    for engine in platform.engines:
        engine.dispose()


def test_worker_shutdown_default_off_is_noop(tmp_path: Path) -> None:
    platform = build_sqlite_platform(tmp_path)
    assert platform.rebac_client is None
    run(_shutdown_platform(platform))  # closes nothing, disposes engines, no raise
