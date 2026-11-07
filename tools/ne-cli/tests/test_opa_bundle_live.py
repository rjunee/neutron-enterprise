# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: real OPA sidecar proof for NE-PILOT-OPA-BUNDLE.

⚠ THIS MODULE IS UNDER A MUST-RUN-NOT-SKIP GATE, and that is deliberate. The
resolution case below is the ONLY proof that the staged data tree actually
reaches the SERVED bundle — the link whose failure makes ``tiers.rego``'s 14400 s
built-in floor the effective tier-2 window again, so every tier-2 Run park goes
back to promising four hours this deployment strands as ``authorization_expired``
fifteen minutes later. ⚠️ ``_require_honourable_approval_window`` does NOT catch
that: it refuses only DECLARED windows and warns-and-parks on a floor, which is
precisely why this link failing is SILENT and needs its own gate. A
module-level docker skip would let that cessation pass as a green run, so
``.github/workflows/ci.yml``'s "Served OPA bundle executing gate" fails the
REQUIRED ``check`` job on ``skipped``/``no tests ran``, exactly as the
Checkpoint-C, C6-Rego, tier-parity, SOPS and run-store-PG gates do for theirs.
Locally, with no docker daemon, it still skips.

Skipped when no docker daemon is reachable (the same skip-when-the-engine-is-absent
shape packages/ne-govern/tests/test_opa_pdp_live.py uses for the `opa` binary — note
that module starts its OWN OPA as of 2026-09-09 and no longer borrows a sidecar).
To run for real:

    uv run pytest tools/ne-cli/tests/test_opa_bundle_live.py -q

Starts the EXACT pinned OPA image the pilot compose / Helm tier ship
(deploy/compose/pilot/compose.yaml services.opa.image — single-sourced here,
never a second hard-coded pin, per C11) with a mounted policies directory,
and proves:

  * (positive) the ``ne up``-staged real ne-govern bundle answers a DEFINED
    ``/v1/data/ne/govern/decision`` query — the exact fix this unit ships.
  * (negative control) the SHIPPED placeholder answers the SAME query
    UNDEFINED — reproducing the fail-closed gap this unit fixes, proving the
    positive test is non-vacuous.
  * (resolution) the deployment's own tier-2 ACTION resolves
    ``approval_window_seconds`` to the value the DEPLOYMENT's data document
    declares for it, not to ``tiers.rego``'s built-in floor — the only place the
    staged data tree is proved to actually reach the served bundle.

Docker bind-mount note: the staged directory MUST live under a host path the
docker daemon actually shares into its VM (this environment's docker context
only bind-mounts $HOME, not the system tempdir) — so the staged fixture is
created under the repo root rather than pytest's ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from ne.cli.opa_bundle import stage_govern_policies

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
DATA_DOCUMENT = REPO_ROOT / "deploy" / "opa" / "data" / "ne" / "config" / "data.json"
PLACEHOLDER_POLICIES = REPO_ROOT / "deploy" / "compose" / "pilot" / "opa" / "policies"
VERSION_REGO = REPO_ROOT / "packages" / "ne-govern" / "policies" / "ne" / "govern" / "version.rego"

_VERSION_RE = re.compile(r'policy_version\s*:=\s*"([^"]+)"')


def _docker_daemon_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    return result.returncode == 0


if not _docker_daemon_reachable():
    pytest.skip(
        "LIVE-PARKED: no reachable docker daemon — start Docker/Colima and re-run "
        "`uv run pytest tools/ne-cli/tests/test_opa_bundle_live.py -q`",
        allow_module_level=True,
    )


def _opa_image() -> str:
    services = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]
    return services["opa"]["image"]


def _declared_action_window_s(action: str) -> float:
    """The window the DEPLOYMENT's data document declares for ``action``.

    Read from the document rather than retyped, so this test asserts that the
    served bundle agrees with what is shipped — not that it agrees with a second
    copy of the number living in a test. Keyed by ACTION because that is how the
    document declares it: the derivation covers one agent-parked action's
    pre-park stretch, not every approval of its tier (see
    ``packages/ne-runtime/tests/test_approval_window_is_honourable.py``).
    """

    document = json.loads(DATA_DOCUMENT.read_text(encoding="utf-8"))
    return float(document["approval_windows"]["actions"][action])


def _expected_policy_version() -> str:
    match = _VERSION_RE.search(VERSION_REGO.read_text(encoding="utf-8"))
    assert match is not None, f"no policy_version constant found in {VERSION_REGO}"
    return match.group(1)


@contextlib.contextmanager
def _run_opa(policies_host_dir: Path) -> Iterator[str]:
    """Start the pinned OPA image serving `policies_host_dir` at /policies:ro.

    Binds an ephemeral loopback port, waits for /health, yields the base URL,
    and always `docker rm -f`s the container in `finally` — never a broad
    kill (the container is addressed by its own id only)."""
    image = _opa_image()
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            "127.0.0.1:0:8181",
            "-v",
            f"{policies_host_dir.resolve()}:/policies:ro",
            image,
            "run",
            "--server",
            "--addr=0.0.0.0:8181",
            "--disable-telemetry",
            "/policies",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = run.stdout.strip()
    try:
        port_result = subprocess.run(
            ["docker", "port", container_id, "8181/tcp"],
            capture_output=True,
            text=True,
            check=True,
        )
        # "0.0.0.0:PORT" (possibly multiple lines) — take the first mapping's port.
        host_port = port_result.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        base_url = f"http://127.0.0.1:{host_port}"

        deadline = time.monotonic() + 30.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(0.5)
        else:
            raise RuntimeError(f"OPA container {container_id} never became healthy: {last_error}")

        yield base_url
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=False)


@pytest.fixture
def staged_govern_bundle() -> Iterator[Path]:
    """The real ne-govern bundle staged under the REPO ROOT (not tmp_path —
    see module docstring: this docker context only bind-mounts $HOME)."""
    workdir = Path(tempfile.mkdtemp(dir=REPO_ROOT, prefix=".opa-live-test-"))
    try:
        dest = stage_govern_policies(workdir / "opa-policies", root=REPO_ROOT)
        yield dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_staged_bundle_yields_defined_decision(staged_govern_bundle: Path) -> None:
    """Positive: the `ne up`-staged real bundle answers a DEFINED decision."""
    with _run_opa(staged_govern_bundle) as base_url:
        resp = httpx.post(
            f"{base_url}/v1/data/ne/govern/decision",
            json={"input": {}},
            timeout=5.0,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body, f"expected a DEFINED decision, got {body!r}"
        result = body["result"]
        assert result["effect"] == "deny"
        assert result["reason"] == "missing or invalid principal"
        assert result["policy_version"] == _expected_policy_version()


def test_the_served_bundle_resolves_the_deployments_tier2_window(
    staged_govern_bundle: Path,
) -> None:
    """A TIER-2 decision off the served bundle carries the DEPLOYED window.

    ⛔ NOTHING ELSE PROVES THIS, and the failure it guards is total.
    ``decision_test.rego``'s built-in-floor cases assert 14400/86400 by relying
    on ``approval_windows`` being ABSENT, so they cannot see the deployment
    document at all; and the positive test above POSTs ``{"input": {}}``, which
    denies on a missing principal and never reaches
    ``approval_window_seconds``. If the staged data tree failed to land at
    ``data.ne.config`` — a rename, a changed staging relpath, an OPA loader
    keying it somewhere else — every decision would silently fall back to the
    14400 s floor: **100% of tier-2 Run parks promising four hours on a 900 s
    session, with a green bundle**. Nothing downstream would say so —
    ``ne.runtime.middleware._require_honourable_approval_window`` warns and parks
    on a floor and refuses only DECLARED windows — so this test is the proof.

    The input is the same tier-2 shape ``decision_test.rego`` uses, so the tier
    resolution itself is not this test's subject — the WINDOW is.
    """

    expected = _declared_action_window_s("write.external.email.send")
    with _run_opa(staged_govern_bundle) as base_url:
        resp = httpx.post(
            f"{base_url}/v1/data/ne/govern/decision",
            json={
                "input": {
                    "action": "write.external.email.send",
                    "principal": {"id": "user:ops"},
                    "context": {},
                }
            },
            timeout=5.0,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body, f"expected a DEFINED decision, got {body!r}"
        result = body["result"]
        assert result["tier"] == 2, f"the fixture input no longer classifies tier 2: {result!r}"
        assert result["effect"] == "require-approval"
        assert result["approval_window_seconds"] == expected, (
            f"the served bundle resolved approval_window_seconds to "
            f"{result['approval_window_seconds']!r}, not the "
            f"{expected!r} declared in {DATA_DOCUMENT} — the deployment data "
            f"document is not reaching data.ne.config in the staged bundle, so "
            f"every tier-2 park would silently promise the built-in floor's four "
            f"hours instead of the three minutes this deployment can keep"
        )


def test_placeholder_mount_is_undefined_negative_control() -> None:
    """Negative control: the SHIPPED placeholder answers the SAME query
    UNDEFINED — reproducing the exact fail-closed gap this unit fixes, and
    proving the positive test above is not vacuously true."""
    with _run_opa(PLACEHOLDER_POLICIES) as base_url:
        resp = httpx.post(
            f"{base_url}/v1/data/ne/govern/decision",
            json={"input": {}},
            timeout=5.0,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" not in body, (
            f"expected UNDEFINED (no 'result' key) against the placeholder-only "
            f"mount, got {body!r} — the placeholder unexpectedly carries a "
            f"ne.govern.decision rule"
        )
