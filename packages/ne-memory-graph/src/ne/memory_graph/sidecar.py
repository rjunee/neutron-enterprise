# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``GraphitiSidecar`` — the ONLY seam to Graphiti (out-of-process, C4).

graphiti-core drags the openai SDK (C3) + neo4j driver + posthog telemetry, so it
runs in a hash-pinned sidecar venv (``ne.envbuild.ensure_sidecar_venv``; the
splink/ragas precedent) and is launched as a SUBPROCESS uvicorn server. NOTHING in
this module (or anywhere platform-side) imports ``graphiti_core`` / ``neo4j`` /
``posthog`` — the whole closure lives only in the sidecar venv and never enters
``uv.lock`` (proven by ``test_no_graph_closure_in_lock.py`` + the
``memory-graph-sidecar-out-of-process`` import-linter contract).
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ne.envbuild import (
    UvRunner,
    ensure_sidecar_venv,
    marker_path,
    read_marker,
    requirements_sha256,
    sidecar_venv_dir,
    venv_python,
)
from ne.net.airgap import AirGapViolationError, air_gap_engaged

__all__ = ["GraphitiSidecar", "default_graphiti_env_dir"]


def default_graphiti_env_dir() -> Path:
    """The in-repo ``packages/ne-memory-graph/graphiti_env`` directory."""
    # sidecar.py -> memory_graph -> ne -> src -> ne-memory-graph
    return Path(__file__).resolve().parents[3] / "graphiti_env"


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP hook for :func:`ensure_sidecar_venv`.

    The returned closure is passed as ``pre_lock_check`` so it fires AFTER the
    unlocked up-to-date fast-path but BEFORE any mkdir/lock/rmtree/subprocess.
    Reaching it means the venv is absent or stale, which would trigger a
    ``uv pip install`` egress to pypi.org/files.pythonhosted.org from inside the
    customer VPC — forbidden under AIR_GAP. ``air_gap_engaged()`` is read at CALL
    TIME (inside the closure) so tests can toggle it via ``monkeypatch.setenv``.
    """

    def _check() -> None:
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and {name} at {venv_dir} is missing or stale; "
                "runtime uv pip install egress (pypi.org/files.pythonhosted.org) is "
                "forbidden — pre-build the venv at image build/pre-staging time."
            )

    return _check


def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    """A urllib opener that hard-disables env proxies and refuses cross-host redirects.

    ``ProxyHandler({})`` (an INSTANCE with an empty map) prevents urllib from
    honoring ``HTTP(S)_PROXY`` / ``ALL_PROXY`` — otherwise the loopback health
    poll could be routed through an unsanctioned proxy (C2). The redirect handler
    refuses any cross-host redirect (returns ``None`` → urllib raises, the poll
    treats it as unhealthy and fail-closed) so a bearer/service token is never
    forwarded to a foreign host.
    """

    class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> urllib.request.Request | None:
            target = urllib.parse.urlparse(urllib.parse.urljoin(req.full_url, newurl))
            origin = urllib.parse.urlparse(req.full_url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameHostRedirect())


_HEALTH_OPENER = _build_no_proxy_opener()


class GraphitiSidecar:
    """Build/refresh the Graphiti sidecar venv and run ``service.py`` as a subprocess."""

    def __init__(
        self,
        *,
        env_dir: Path | None = None,
        venv_dir: Path | None = None,
        run: UvRunner | None = None,
    ) -> None:
        self._env_dir = env_dir or default_graphiti_env_dir()
        self._venv_dir = venv_dir or sidecar_venv_dir(self._env_dir, ".graphiti-venv")
        self._run = run

    @property
    def env_dir(self) -> Path:
        return self._env_dir

    @property
    def venv_dir(self) -> Path:
        return self._venv_dir

    def available(self) -> bool:
        """True iff the venv python exists AND the marker matches the committed reqs."""
        python = venv_python(self._venv_dir)
        if not python.is_file():
            return False
        try:
            want = requirements_sha256(self._env_dir)
        except OSError:
            return False
        return read_marker(marker_path(self._venv_dir)) == want

    def ensure(self) -> None:
        """(Re)build the hash-pinned sidecar venv if missing/stale — fail-closed under AIR_GAP.

        A CURRENT venv is a silent fast-path no-op. An ABSENT or STALE venv would
        trigger a ``uv pip install`` egress; under AIR_GAP that is refused with a
        typed :class:`~ne.net.airgap.AirGapViolationError` BEFORE any
        lock/mkdir/rmtree/subprocess (the ``pre_lock_check`` hook). The sanctioned
        path is a pre-built venv (build it at image build/pre-staging time).
        """
        ensure_sidecar_venv(
            env_dir=self._env_dir,
            venv_dir=self._venv_dir,
            pre_lock_check=_air_gap_check(
                self._venv_dir, "the Graphiti temporal-memory sidecar venv"
            ),
            name="the Graphiti temporal-memory sidecar venv",
            run=self._run,
        )

    def start(self, env: Mapping[str, str], *, port: int = 8093) -> subprocess.Popen[bytes]:
        """Launch the sidecar uvicorn server (loopback-bound) as a subprocess."""
        self.ensure()
        python = venv_python(self._venv_dir)
        return subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "--app-dir",
                str(self._env_dir),
                "service:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=dict(env),
        )

    @staticmethod
    def wait_healthy(url: str, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> bool:
        """Poll ``<url>/healthz`` until a 2xx or the timeout elapses."""
        deadline = time.monotonic() + timeout_s
        health = url.rstrip("/") + "/healthz"
        while time.monotonic() < deadline:
            try:
                # Dedicated no-proxy opener: env HTTP(S)_PROXY/ALL_PROXY are
                # hard-disabled and cross-host redirects are refused, so the
                # loopback health poll cannot be diverted to a foreign host (C2).
                with _HEALTH_OPENER.open(health, timeout=interval_s) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(interval_s)
        return False

    @staticmethod
    def stop(proc: subprocess.Popen[bytes]) -> None:
        """Terminate the sidecar subprocess (SIGTERM, then wait)."""
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
