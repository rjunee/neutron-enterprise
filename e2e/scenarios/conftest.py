# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Configuration for the demo-scenario E2E suite. See e2e/README.md.

THIS SUITE IS NOT PART OF ANY DEFAULT TEST RUN. The root ``testpaths`` only
collects ``packages/*/tests`` and ``tools/*/tests``; this directory is reached
ONLY by naming it (``uv run pytest e2e/scenarios``, or ``e2e/run.sh``, or the
``e2e-scenarios`` workflow's dispatch/cron lanes). It drives a LIVE deployment,
takes tens of minutes, and the frontier scenarios spend real API money.

Configuration is env-only (no config file to go stale):

  NE_E2E_API_BASE        e.g. https://demo.example.host:9443     (required)
  NE_E2E_KEYCLOAK_BASE   e.g. https://demo.example.host:8443     (required)
  NE_E2E_REALM           default ne-pilot
  NE_E2E_OPS_USER / NE_E2E_OPS_PASSWORD            (required)
  NE_E2E_APPROVER_USER / NE_E2E_APPROVER_PASSWORD  (required for S5 only)
  NE_E2E_INSECURE_TLS=1  accept the deployment's self-signed cert
  NE_E2E_RESOLVE_TO      pin the deployment hostnames to one IP while keeping
                         TLS SNI (box-side runs: 127.0.0.1 — the API port
                         binds loopback-only there)
  NE_E2E_CONSOLE_BASE    console origin when it is not https://<api-host>/
  NE_E2E_MUTATING=1      opt-in for the tenant-mutating lane (S5 send, S8 edit)
  NE_E2E_RESULTS_DIR     transcript/ledger directory (default e2e/results/<utc>)
  NE_E2E_ALLOW_COLD=1    override the m365 warmth gate (answers may be STALE —
                         only for debugging the suite itself, never for a
                         pre-demo/pre-release verdict)
  NE_E2E_GRAPH_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET / _DRIVE_ID /
  NE_E2E_GRAPH_SOP_ITEM_PATH   (S8's live SharePoint edit; see nelib/graphedit)

A missing required variable FAILS the run loudly (refused is never rendered as
an empty green); the deliberately-optional lanes SKIP with a visible reason.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nelib.client import NeutronClient
from nelib.flakes import FlakeLedger

_MARKERS = (
    "frontier: spends real frontier-API money (see e2e/README.md for the cost basis)",
    "private_lane: runs on the private GPU lane (no per-token API cost)",
    "mutating: mutates the live tenant (sends real email / edits the live SOP); "
    "requires NE_E2E_MUTATING=1",
)


def pytest_configure(config: pytest.Config) -> None:
    for marker in _MARKERS:
        config.addinivalue_line("markers", marker)
    _apply_resolve_override()


def _apply_resolve_override() -> None:
    """``NE_E2E_RESOLVE_TO``: pin the deployment hostnames to one IP.

    The demo box's API port (9443) binds to 127.0.0.1 only while the public
    DNS name resolves to the public interface, and Caddy's TLS requires the
    real hostname as SNI — so box-side runs keep the hostname and pin the
    address (``NE_E2E_RESOLVE_TO=127.0.0.1``), exactly like the S9-trials
    driver did. Off the box, leave it unset.
    """
    import socket
    from urllib.parse import urlsplit

    target = os.environ.get("NE_E2E_RESOLVE_TO", "").strip()
    if not target:
        return
    hosts = {
        urlsplit(os.environ.get(name, "")).hostname
        for name in ("NE_E2E_API_BASE", "NE_E2E_KEYCLOAK_BASE", "NE_E2E_CONSOLE_BASE")
    }
    hosts.discard(None)
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in hosts:
            host = target
        return original(host, *args, **kwargs)

    socket.getaddrinfo = patched  # type: ignore[assignment]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(
            f"e2e config missing: {name} is not set. This suite drives a live deployment "
            "and refuses to guess its target — see e2e/scenarios/conftest.py for the full "
            "env contract, or invoke via e2e/run.sh.",
            pytrace=False,
        )
    return value


@pytest.fixture(scope="session")
def results_dir() -> Path:
    configured = os.environ.get("NE_E2E_RESULTS_DIR", "").strip()
    if configured:
        path = Path(configured)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = Path(__file__).resolve().parent.parent / "results" / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


_ACTIVE_LEDGER: list[FlakeLedger] = []


@pytest.fixture(scope="session")
def flake_ledger(results_dir: Path) -> FlakeLedger:
    ledger = FlakeLedger(path=results_dir / "flake-ledger.json")
    _ACTIVE_LEDGER.append(ledger)
    return ledger


def _client(username_env: str, password_env: str, results_dir: Path) -> NeutronClient:
    return NeutronClient(
        api_base=_require_env("NE_E2E_API_BASE").rstrip("/"),
        keycloak_base=_require_env("NE_E2E_KEYCLOAK_BASE").rstrip("/"),
        realm=os.environ.get("NE_E2E_REALM", "ne-pilot"),
        username=_require_env(username_env),
        password=_require_env(password_env),
        verify_tls=os.environ.get("NE_E2E_INSECURE_TLS") != "1",
        results_dir=results_dir,
    )


@pytest.fixture(scope="session")
def ops(results_dir: Path) -> Iterator[NeutronClient]:
    """The driving principal — mirrors the demo's window A."""
    client = _client("NE_E2E_OPS_USER", "NE_E2E_OPS_PASSWORD", results_dir)
    yield client
    client.close()


@pytest.fixture(scope="session")
def approver(results_dir: Path) -> Iterator[NeutronClient]:
    """The DISTINCT approving principal (S5 separation of duties)."""
    client = _client("NE_E2E_APPROVER_USER", "NE_E2E_APPROVER_PASSWORD", results_dir)
    yield client
    client.close()


@pytest.fixture(scope="session")
def warm_box(ops: NeutronClient) -> None:
    """The warmth gate (DEMO_RUNBOOK Gate 2 / boot-ordering trap).

    For ~15 minutes after an api/worker restart the m365 corpus is WITHHELD
    from retrieval; the Analyst then answers from structured records only —
    confidently STALE, with no error anywhere. Scenario results taken cold are
    evidence of nothing, so knowledge scenarios refuse a cold box outright.
    """
    if os.environ.get("NE_E2E_ALLOW_COLD") == "1":
        return
    connector = ops.m365_connector()
    if connector is None:
        pytest.fail(
            "warmth gate: /v1/connect/status lists no m365 connector — this deployment "
            "cannot serve the knowledge scenarios at all.",
            pytrace=False,
        )
    if connector.get("posture") != "live" or not connector.get("sync_allowed"):
        pytest.fail(
            f"warmth gate: m365 connector posture={connector.get('posture')!r} "
            f"sync_allowed={connector.get('sync_allowed')!r} — the box is NOT warm. A cold "
            "box silently serves stale answers (DEMO_RUNBOOK Gate 2). Warm it (one "
            "off-stage ask, or Sources -> Sync now and wait for Synced) and re-run; "
            "NE_E2E_ALLOW_COLD=1 overrides for suite debugging only.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def mutating_enabled() -> None:
    if os.environ.get("NE_E2E_MUTATING") != "1":
        pytest.skip(
            "mutating lane disabled (NE_E2E_MUTATING != 1): this scenario sends real "
            "email / edits the live SOP. Enable deliberately."
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # The suite is timing- and load-sensitive and mutates shared tenant state;
    # parallelising it manufactures failures (xdist collapse was
    # measured). Refuse -n outright rather than flaking mysteriously.
    if config.pluginmanager.hasplugin("xdist") and getattr(config.option, "numprocesses", None):
        pytest.exit("e2e/scenarios must run serially: drop -n / --numprocesses.", returncode=4)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Print the attributed-flake summary so a green run still discloses what
    it absorbed (flake policy rule 2 — see nelib/flakes.py)."""
    del session
    for ledger in _ACTIVE_LEDGER:
        print("\n" + ledger.summary())
        print(f"flake ledger written to {ledger.path}")
