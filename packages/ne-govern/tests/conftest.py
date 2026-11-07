# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-govern tests.

The OPA HTTP seam is stood in for with :class:`httpx.MockTransport` (BELOW the
OPA wire protocol — the real-OPA path is live-parked; the Rego itself is
validated by ``decision_test.rego`` / ``opa eval``). Everything ABOVE the seam
is exercised for real: the audit-coupling acceptance test runs against the real
``PostgresAuditSink`` over in-memory SQLite (see ``test_pdp_to_audit_sink_real``).
Async seams are exercised via ``asyncio.run``; pytest-asyncio is NOT a
dependency and must not be added.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ne.contracts import (
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification, GuardrailVerdict
from ne.contracts.types import Principal
from ne.govern.bundle import read_policy_version
from ne.govern.config import GovernSettings
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp

_VECTORS_PATH = Path(__file__).parent / "vectors" / "tier_vectors.json"
_DECISION_PATH = "/v1/data/ne/govern/decision"
_POLICIES_DIR = Path(__file__).parents[1] / "policies"


# --- config_hash: frozen fresh for every test, reset on teardown ----------


@pytest.fixture(autouse=True)
def frozen_config_hash() -> Iterator[str]:
    reset_config_hash_for_testing()
    freeze_config_hash(GovernSettings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


# --- recording / failing audit sinks --------------------------------------


class RecordingAuditSink:
    """An in-memory ``AuditSink`` that records every appended event."""

    def __init__(self) -> None:
        self.events: list[AuditEventLike] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))


class FailingAuditSink:
    """An ``AuditSink`` whose ``append`` always fails (fail-closed test double)."""

    async def append(self, event: AuditEventLike) -> None:
        raise RuntimeError("audit sink is down")

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)


@pytest.fixture
def recording_sink() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def failing_sink() -> FailingAuditSink:
    return FailingAuditSink()


# --- OPA stub transport ---------------------------------------------------


def stub_opa_transport(
    result: dict[str, Any] | None = None,
    *,
    status: int = 200,
    omit_result: bool = False,
) -> httpx.MockTransport:
    """A MockTransport asserting the OPA data-API contract, returning a fixed doc.

    Asserts the request path is the decision path and the body is ``{"input": …}``
    carrying a principal/action/resource. When ``omit_result`` the response body
    omits ``result`` (OPA's ``undefined`` — the fail-closed path).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        assert set(payload) == {"input"}
        input_doc = payload["input"]
        assert "principal" in input_doc
        assert "action" in input_doc
        assert "resource" in input_doc
        body: dict[str, Any] = {} if omit_result else {"result": result}
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def make_opa_client(transport: httpx.MockTransport) -> OpaClient:
    """Build an ``OpaClient`` bound to a stub transport."""
    client = httpx.AsyncClient(base_url="http://opa.test", transport=transport)
    return OpaClient("http://opa.test", client=client)


def make_pdp(result: dict[str, Any], sink: Any) -> GovernedPdp:
    """Build a ``GovernedPdp`` whose OPA returns ``result`` and audits to ``sink``."""
    return GovernedPdp(make_opa_client(stub_opa_transport(result)), sink)


@pytest.fixture
def opa_transport() -> Callable[..., httpx.MockTransport]:
    return stub_opa_transport


@pytest.fixture
def opa_client_factory() -> Callable[[httpx.MockTransport], OpaClient]:
    return make_opa_client


@pytest.fixture
def pdp_factory() -> Callable[[dict[str, Any], Any], GovernedPdp]:
    return make_pdp


# --- decision vectors -----------------------------------------------------


def load_vectors() -> list[dict[str, Any]]:
    return json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))


def opa_doc_for(expected: dict[str, Any], *, policy_version: str = "1.0.0") -> dict[str, Any]:
    """Build the full OPA decision document a vector's ``expected`` implies."""
    return {
        "effect": expected["effect"],
        "tier": expected["tier"],
        "reason": f"vector: {expected['effect']}",
        "obligations": expected["obligations"],
        "approval_window_seconds": expected.get("approval_window_seconds"),
        "approval_window_declared": expected.get("approval_window_declared", False),
        "standing_approval_seconds": expected.get("standing_approval_seconds"),
        "policy_version": policy_version,
    }


@pytest.fixture
def vectors() -> list[dict[str, Any]]:
    return load_vectors()


def oracle_doc_for(vector: dict[str, Any]) -> dict[str, Any]:
    """Build the FULL shared-oracle decision document for a vector (NE-092).

    Unlike :func:`opa_doc_for` (which synthesizes a placeholder ``reason`` for
    the OPA-stub-transport tests), this reads the vector's OWN ``expected``
    block — which now carries the byte-exact ``reason`` — and stamps the REAL
    bundle ``policy_version`` (via :func:`~ne.govern.bundle.read_policy_version`),
    so it is directly comparable to a cedar/live-opa full decision document on
    all eight fields (the window, its provenance bit, and the standing clock
    included).
    """
    expected = vector["expected"]
    return {
        "effect": expected["effect"],
        "tier": expected["tier"],
        "reason": expected["reason"],
        "obligations": expected["obligations"],
        "approval_window_seconds": expected["approval_window_seconds"],
        "approval_window_declared": expected["approval_window_declared"],
        "standing_approval_seconds": expected["standing_approval_seconds"],
        "policy_version": read_policy_version(_POLICIES_DIR),
    }


# --- C12 guardrail helpers (NE-032) ---------------------------------------


def make_principal(pid: str = "user:1", *, department: str | None = None) -> Principal:
    """Build a test principal, optionally with a declared department claim."""
    claims = {"department": department} if department is not None else {}
    return Principal(id=pid, claims=claims)


class RaisingGuardrail:
    """A guardrail whose ``check`` always raises (fail-closed engine test double)."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("guardrail boom")

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        raise self._exc


class StubGuardrail:
    """A guardrail returning a fixed verdict (engine composition test double)."""

    def __init__(self, verdict: GuardrailVerdict) -> None:
        self._verdict = verdict

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        return self._verdict


@pytest.fixture
def principal_factory() -> Callable[..., Principal]:
    return make_principal


# --- the DISPOSABLE PDP the live end-to-end proof runs against -------------


def _stage_govern_bundle(dest: Path) -> Path:
    """Copy the shipped NON-TEST Rego to ``dest``, OPA's read-only load root.

    ``*_test.rego`` is excluded for the same reason ``ne up``'s staging excludes it
    (tools/ne-cli/src/ne/cli/opa_bundle.py::stage_govern_policies): those files are
    source-only fixtures that carry ``package ne.govern`` and are never part of the
    served bundle.
    """
    for rego in sorted(_POLICIES_DIR.rglob("*.rego")):
        if rego.name.endswith("_test.rego"):
            continue
        target = dest / rego.relative_to(_POLICIES_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rego, target)
    return dest


def _free_loopback_port() -> int:
    """A port the kernel just handed out, on loopback. Never a service's port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def disposable_opa_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway OPA serving the REAL ne-govern bundle from disk, on an ephemeral port.

    ⛔ WHY THIS EXISTS — IT IS AN OUTAGE FIX, NOT A CONVENIENCE. ``test_opa_pdp_live.py``
    used to ``PUT /v1/policies/<id>`` this working tree's Rego into whatever OPA answered
    ``NE_GOVERN_OPA_URL``. On a box running the stack that port IS the deployed PDP, and
    on 2026-08-30 a plain ``pytest packages/ne-govern`` uploaded a working tree into live
    governance: every module held twice, two ``tiers.rego`` versions answering one query,
    HTTP 500 ``eval_conflict_error`` instead of decisions. The consent gate added then
    (explicit ``NE_GOVERN_OPA_URL``, no default) did not close the class — the file's own
    documented recipe named ``http://localhost:8181``, so following the instructions
    satisfied the gate. On 2026-09-09 the same shape in ``packages/ne-mcp`` took
    governance down again: 12 failed decisions over ~90 seconds (16:08:30Z → 16:09:58Z).
    ``opa check`` cannot see that state — a merged-conflict bundle only fails when it is
    EVALUATED.

    THE FIX IS STRUCTURAL, NOT PROCEDURAL. The proof now BRINGS ITS OWN PDP. OPA reads
    its load root from disk at startup, so the bundle is never uploaded to anything —
    there is no policy write left in this package to aim at the wrong server, and no
    ``NE_GOVERN_OPA_URL`` read left to aim it with. The address is loopback on a port the
    kernel just handed out, so it cannot be a running service's.
    ``packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py`` keeps it that way, and
    its allowlist of sanctioned uploaders is now EMPTY.

    DELIBERATELY DUPLICATED from ``packages/ne-mcp/tests/conftest.py::disposable_opa_url``
    — cross-package test imports are banned by repo rule, the same reason that file
    duplicates the ne-runtime doubles. Keep the two in step. ONE difference, and it is
    the gate: ne-mcp's fixture also demands ``NE_MCP_LIVE_PROOFS=1`` because the proofs it
    serves build sidecar venvs and drive real subprocesses, so they stay out of CI's
    critical path. This one serves a single in-process vector sweep against a local
    server, so it is gated ONLY on the binary — the shape this package already uses for
    a test that needs the real engine (``test_rego_policies.py``,
    ``test_cedar_parity.py``). CI installs the sha256-pinned ``opa`` on PATH
    (.github/workflows/ci.yml, "Install OPA (pinned, C11)"), so the live-PDP proof now
    RUNS on every PR instead of skipping on an unset variable.

    A BINARY, NOT THE PINNED CONTAINER. The repo's throwaway-OPA-container pattern
    (tools/ne-cli/tests/test_opa_bundle_live.py::_run_opa) exists because that test's
    SUBJECT is the shipped image + bind-mount. Here the subject is a PDP.

    THE STAGED BUNDLE IS REGO ONLY — the deployment's OPA data document
    (``deploy/opa/data/ne/config/data.json``) is NOT staged, matching both ne-mcp's
    fixture and this package's own ``opa eval`` proofs (``test_rego_policies.py``,
    ``test_cedar_parity.py``), which load ``-d policies`` and nothing else. The decision
    vectors are data-document-independent by construction; that the staged data tree
    reaches the SERVED bundle is proved separately, and under its own must-run CI gate,
    by ``tools/ne-cli/tests/test_opa_bundle_live.py``.

    Session-scoped: one OPA per pytest process, so an xdist worker gets its own server
    rather than sharing one — which is why the proof no longer carries the
    ``opa-live-shared-server`` xdist group.
    """
    opa_binary = shutil.which("opa")
    if opa_binary is None:
        pytest.skip(
            "LIVE-PARKABLE: no `opa` on PATH — install the pinned binary the way CI does "
            "(.github/workflows/ci.yml, 'Install OPA (pinned, C11)') and re-run"
        )

    policies = _stage_govern_bundle(tmp_path_factory.mktemp("ne-govern-bundle"))
    port = _free_loopback_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            opa_binary,
            "run",
            "--server",
            f"--addr=127.0.0.1:{port}",
            "--disable-telemetry",
            str(policies),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"opa exited {proc.returncode} before serving:\n{proc.communicate()[0]}"
                )
            try:
                if httpx.get(f"{url}/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"disposable OPA at {url} never became healthy")
            time.sleep(0.25)

        # Non-vacuity: the bundle really LOADED. The retired PUT loop surfaced a bad
        # bundle as a 400; loading from disk surfaces it as an UNDEFINED decision, so
        # assert defined-ness here rather than discovering it as a vector failure later.
        probe = httpx.post(f"{url}{_DECISION_PATH}", json={"input": {}}, timeout=5.0)
        assert probe.status_code == 200, f"disposable OPA decision probe: {probe.text}"
        assert "result" in probe.json(), (
            f"disposable OPA served an UNDEFINED decision — bundle did not load from "
            f"{policies}: {probe.text}"
        )
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)
