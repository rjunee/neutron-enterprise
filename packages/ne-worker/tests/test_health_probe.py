# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FIX-2 proof: ``python -m ne.worker --health`` is a LIGHT probe.

The Argus P2 was that the module top-level imported the full ne.api.platform /
ne.runtime.factory graph (litellm + qdrant + fastembed, ~3.4-4.3s) BEFORE
``--health`` was parsed, risking a healthcheck flap on the 5s-timeout ``cpus:2``
read-only container. After the restructure, ``--health`` touches ONLY
:class:`~ne.worker.settings.WorkerSettings` + the heartbeat marker; every
heavy composition import lives inside ``_run()``/``_launch_dbos()``.

``test_health_probe_avoids_heavy_imports`` proves this DETERMINISTICALLY (a
``sys.modules`` membership check in a fresh subprocess — NOT a timing assertion,
which was deliberately rejected as CI-flaky). ``test_health_probe_exit_codes``
proves the freshness liveness contract (fresh ⇒ 0, stale/missing ⇒ 1).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

#: Modules the --health path MUST NOT load. Empirically verified: importing
#: WorkerSettings and running main(['--health']) loads NONE of these (the light
#: path pulls only pydantic/pydantic-settings/ne.contracts.config).
FORBIDDEN = (
    "ne.api.platform",
    "ne.runtime.factory",
    "litellm",
    "qdrant_client",
    "fastembed",
    "dbos",
    "sqlalchemy",
)

# Run --health in a FRESH interpreter, then diff FORBIDDEN against sys.modules.
# Print HEAVY:<names> + exit 2 if any leaked; otherwise exit 0 (the SystemExit
# raised by main(['--health']) itself is expected and swallowed).
_SCRIPT = f"""
import sys
import ne.worker.main as m
try:
    m.main(["--health"])
except SystemExit:
    pass
FORBIDDEN = {list(FORBIDDEN)!r}
loaded = sorted(set(FORBIDDEN) & set(sys.modules))
if loaded:
    print("HEAVY:" + ",".join(loaded))
    sys.exit(2)
sys.exit(0)
"""


def test_health_probe_avoids_heavy_imports(tmp_path: Path) -> None:
    marker = tmp_path / "hb"
    import ne.worker.main as m

    m._write_heartbeat_marker(str(marker))
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        env={**os.environ, "NE_WORKER_HEARTBEAT_PATH": str(marker)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"--health leaked a heavy import (rc={proc.returncode}): "
        f"{proc.stdout.strip()!r} / {proc.stderr.strip()!r}"
    )
    assert not proc.stdout.strip().startswith("HEAVY"), proc.stdout


def test_health_probe_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ne.worker.main as m

    marker = tmp_path / "hb"

    # Fresh marker bound to this live process instance ⇒ exit 0.
    m._write_heartbeat_marker(str(marker))
    monkeypatch.setenv("NE_WORKER_HEARTBEAT_PATH", str(marker))
    with pytest.raises(SystemExit) as fresh:
        m.main(["--health"])
    assert fresh.value.code == 0

    # Stale marker (mtime 120s in the past, > the 60s max age) ⇒ exit 1.
    stale = time.time() - 120
    os.utime(marker, (stale, stale))
    with pytest.raises(SystemExit) as old:
        m.main(["--health"])
    assert old.value.code == 1

    # A fresh marker from an earlier container/process epoch is never accepted.
    marker.write_text('{"pid":1,"process_epoch":"stale-container"}', encoding="utf-8")
    with pytest.raises(SystemExit) as restarted:
        m.main(["--health"])
    assert restarted.value.code == 1

    # Missing path ⇒ exit 1.
    monkeypatch.setenv("NE_WORKER_HEARTBEAT_PATH", str(tmp_path / "does-not-exist"))
    with pytest.raises(SystemExit) as missing:
        m.main(["--health"])
    assert missing.value.code == 1


def test_normal_worker_start_invalidates_prior_marker_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ne.worker.main as m

    marker = tmp_path / "hb"
    marker.write_text('{"pid":1,"process_epoch":"stale-container"}', encoding="utf-8")
    monkeypatch.setenv("NE_WORKER_HEARTBEAT_PATH", str(marker))

    def close_without_running(coro: object) -> None:
        coro.close()  # type: ignore[attr-defined]

    monkeypatch.setattr(asyncio, "run", close_without_running)
    m.main([])

    assert not marker.exists()
