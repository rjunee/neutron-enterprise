# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures + test doubles for the ne-mcp tests.

No-mock-past-the-seam: every MCP call is driven through the REAL
:class:`~ne.runtime.middleware.GovernedToolExecutor` with the REAL guardrail engine
and a REAL :class:`~ne.govern.pdp.GovernedPdp`; only the OPA HTTP wire is stubbed with
:class:`httpx.MockTransport` BELOW the OPA protocol (Rego-faithful), and the MCP
transport is the in-process memory transport (no network in CI). Cross-package test
imports are not allowed (repo rule), so the ne-runtime conftest doubles are DUPLICATED
here rather than imported. Async seams are exercised via ``asyncio.run`` (the ``run``
helper); pytest-asyncio is NOT a dependency and must not be added.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Coroutine, Iterator, Mapping, Sequence
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
from ne.contracts.governance import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    AuditVerification,
    GuardrailVerdict,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    RecipientSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.mcp._sdk import create_connected_server_and_client_session, types
from ne.mcp.client import SessionFactory
from ne.mcp.config import McpServerCatalogEntry, McpSettings
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.tools import ToolBinding

_DECISION_PATH = "/v1/data/ne/govern/decision"

#: The happy-path recipient the write.external.email.send binding may mail.
ALLOWED_RECIPIENT = "ally@corp.example"


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async seam to completion (pytest-asyncio is deliberately not a dep)."""
    return asyncio.run(coro)


def result_text(result: Any) -> str:
    """Return the text of a CallToolResult's first content block (narrowed for pyright)."""
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


# --- config_hash: frozen fresh for every test, reset on teardown ----------


@pytest.fixture(autouse=True)
def frozen_config_hash() -> Iterator[str]:
    reset_config_hash_for_testing()
    freeze_config_hash(McpSettings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


# --- recording audit sink -------------------------------------------------


class RecordingAuditSink:
    """An in-memory ``AuditSink`` that records every appended event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))

    def actions(self) -> list[str]:
        return [getattr(e, "action", "") for e in self.events]

    def with_action(self, action: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "action", "") == action]


@pytest.fixture
def recording_sink() -> RecordingAuditSink:
    return RecordingAuditSink()


class FailingAuditSink:
    """An ``AuditSink`` whose ``append`` raises (fail-closed infra fault under test).

    Models :class:`RecordingAuditSink` field-for-field for pyright protocol conformance.
    ``fail_actions=None`` fails EVERY append; a non-None frozenset fails only appends whose
    event ``action`` is in the set (records the rest). The raised message carries a SECRET
    marker the sanitized surface must PROVE absent at the MCP boundary (C12).
    """

    #: A raw internal message (path + status) that must never cross the MCP seam.
    SECRET = "BOOM-audit-sink /var/lib/ne/secret-audit.db unavailable"

    def __init__(self, fail_actions: frozenset[str] | None = None) -> None:
        self._fail_actions = fail_actions
        self.events: list[Any] = []

    async def append(self, event: AuditEventLike) -> None:
        if self._fail_actions is None or getattr(event, "action", "") in self._fail_actions:
            raise RuntimeError(self.SECRET)
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)

    def actions(self) -> list[str]:
        return [getattr(e, "action", "") for e in self.events]

    def with_action(self, action: str) -> list[Any]:
        return [e for e in self.events if getattr(e, "action", "") == action]


class RaisingGuardrail:
    """A ``Guardrail`` whose ``check`` raises internally (fail-closed guardrail fault).

    Satisfies the :class:`~ne.contracts.governance.Guardrail` Protocol structurally; the
    engine audits a ``guardrail.error`` event before re-raising
    :class:`~ne.govern.errors.GuardrailEvaluationError`. The SECRET marker must be absent
    from the sanitized MCP surface.
    """

    #: A raw internal message (path + status) that must never cross the MCP seam.
    SECRET = "BOOM-guardrail /etc/ne/guardrails.toml parse failure"

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        raise RuntimeError(self.SECRET)


def make_unreachable_pdp(sink: Any) -> GovernedPdp:
    """A ``GovernedPdp`` whose OPA transport is UNREACHABLE (raises ``httpx.ConnectError``).

    Wrapped exactly like :func:`make_pdp` but the MockTransport handler raises instead of
    returning a decision, so ``pdp.decide`` audits the backend error and re-raises
    :class:`~ne.govern.errors.PolicyBackendError`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("opa backend unreachable BOOM-opa")

    client = httpx.AsyncClient(base_url="http://opa.test", transport=httpx.MockTransport(handler))
    return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- approval gates -------------------------------------------------------


class NamedApproverGate:
    """Always APPROVED with a named approver (a valid Tier-2/3 approval)."""

    def __init__(self, approver: str = "alice") -> None:
        self.approver = approver
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return ApprovalOutcome(status=ApprovalStatus.APPROVED, approver=self.approver)


class ScriptedApprovalGate:
    """Returns a scripted queue of outcomes; records every request."""

    def __init__(self, outcomes: Sequence[ApprovalOutcome]) -> None:
        self._queue = list(outcomes)
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return self._queue.pop(0)


class UnusedGate:
    """A gate that must never be consulted (allow/deny/guardrail-block paths)."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        raise AssertionError("approval gate was consulted on a non-approval path")


# --- Rego-faithful scripted OPA transport ---------------------------------


def _classify(action: str) -> int | None:
    tiers: set[int] = set()
    if action.startswith("read.") or action.startswith("draft."):
        tiers.add(0)
    if action.startswith("write.internal."):
        tiers.add(1)
    if action.startswith("write.external."):
        tiers.add(2)
    if ".pii." in action:
        tiers.add(2)
    if action.startswith("finance.") or action.startswith("legal."):
        tiers.add(3)
    return max(tiers) if tiers else None


def _rego_faithful_doc(action: str) -> dict[str, Any]:
    tier = _classify(action)
    if tier is None:
        effect = "deny"
    elif tier <= 1:
        effect = "allow"
    else:
        effect = "require-approval"
    return {
        "effect": effect,
        "tier": tier,
        "reason": f"rego-faithful: {effect} (tier {tier})",
        "obligations": {},
        "policy_version": "1.0.0",
    }


def rego_faithful_transport(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """A MockTransport returning Rego-faithful decisions keyed by ``input.action``."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        doc = overrides.get(action) or _rego_faithful_doc(action)
        return httpx.Response(200, json={"result": doc})

    return httpx.MockTransport(handler)


def make_pdp(sink: Any, *, overrides: dict[str, dict[str, Any]] | None = None) -> GovernedPdp:
    """A ``GovernedPdp`` with a Rego-faithful stubbed OPA, auditing to ``sink``."""
    client = httpx.AsyncClient(
        base_url="http://opa.test", transport=rego_faithful_transport(overrides)
    )
    return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- guardrail settings + engine ------------------------------------------


def govern_settings(*, allowed_egress_hosts: tuple[str, ...] = ()) -> GovernSettings:
    """Guardrail config allowlisting exactly the tools the ne-mcp tests exercise."""
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(
                allowed_recipients=(ALLOWED_RECIPIENT,),
                allowed_egress_hosts=allowed_egress_hosts,
            ),
            rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
            tools={
                "read.docs.search": ToolArgPolicy(args={"q": ArgSpec(kind=ToolArgKind.text)}),
                "write.external.email.send": ToolArgPolicy(
                    args={
                        "to": ArgSpec(kind=ToolArgKind.email),
                        "body": ArgSpec(kind=ToolArgKind.text),
                    }
                ),
                "finance.payment.initiate": ToolArgPolicy(
                    args={"amount": ArgSpec(kind=ToolArgKind.number)}
                ),
                "write.external.mcp.ext1.create_note": ToolArgPolicy(
                    args={"note": ArgSpec(kind=ToolArgKind.text)}
                ),
            },
        )
    )


def make_guardrails(
    sink: Any,
    *,
    allowed_egress_hosts: tuple[str, ...] = (),
) -> Any:
    return create_guardrail_engine(
        govern_settings(allowed_egress_hosts=allowed_egress_hosts), sink=sink
    )


# --- standard tool bindings + a call log ----------------------------------


class ToolLog:
    """Records handler invocations so tests can assert a tool ran (or did not)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, name: str, result: str = "ok"):
        async def _run(args: Any) -> str:
            self.calls.append((name, dict(args)))
            return result

        return _run

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def standard_bindings(log: ToolLog) -> list[ToolBinding]:
    """Three canonical bindings (tier 0 / 2 / 3) with recording handlers."""
    return [
        ToolBinding(
            spec=ToolSpec(
                name="read.docs.search",
                description="Search internal docs.",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
            handler=log.handler("read.docs.search", "docs: 3 results"),
        ),
        ToolBinding(
            spec=ToolSpec(
                name="write.external.email.send",
                description="Send an external email.",
                tier=ApprovalTier.TIER_2,
                input_schema={
                    "type": "object",
                    "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                },
            ),
            handler=log.handler("write.external.email.send", "sent"),
            extract_recipients=lambda a: (str(a["to"]),) if a.get("to") else (),
        ),
        ToolBinding(
            spec=ToolSpec(
                name="finance.payment.initiate",
                description="Initiate a payment.",
                tier=ApprovalTier.TIER_3,
                input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
            ),
            handler=log.handler("finance.payment.initiate", "paid"),
        ),
    ]


def make_executor(
    bindings: Sequence[ToolBinding],
    sink: Any,
    *,
    approval_gate: Any,
    pdp_overrides: dict[str, dict[str, Any]] | None = None,
    guardrails: Any = None,
    pdp: GovernedPdp | None = None,
) -> GovernedToolExecutor:
    return GovernedToolExecutor(
        bindings,
        pdp=pdp if pdp is not None else make_pdp(sink, overrides=pdp_overrides),
        guardrails=guardrails if guardrails is not None else make_guardrails(sink),
        approval_gate=approval_gate,
        sink=sink,
    )


def make_principal(pid: str = "user:ada") -> Principal:
    return Principal(id=pid)


# --- in-process MCP memory transport --------------------------------------


def memory_session_factory(server: Any) -> SessionFactory:
    """A ``SessionFactory`` that connects to ``server`` over the in-memory transport.

    Every ``factory(entry)`` opens a fresh connected server/client pair (the SDK's
    ``create_connected_server_and_client_session``, imported via ne.mcp._sdk — never
    from ``mcp`` directly), so the client-side connection-per-call shape is preserved.
    """
    return lambda entry: create_connected_server_and_client_session(server)


# Re-export the catalog entry model for convenience in the client tests.
__all__ = ["McpServerCatalogEntry"]


# --- the DISPOSABLE PDP the live drop-in proofs run against ----------------


#: Opt-in for this package's three heavy cross-venv live proofs
#: (test_langgraph_dropin_live / test_crewai_dropin_live /
#: test_rtswap_governance_parity). They build sidecar venvs and drive real
#: subprocesses, so they stay OUT of the ordinary suite and out of CI's
#: critical path — exactly as their docstrings already promised ("a green
#: *skipped* module proves nothing here by design"). This ONE opt-in replaced
#: the three separate NE_SKIP_*_LIVE opt-outs AND the old
#: NE_GOVERN_OPA_URL-reachability gate.
LIVE_PROOF_OPT_IN = "NE_MCP_LIVE_PROOFS"

#: The shipped first-party Rego bundle (packages/ne-govern/policies).
_GOVERN_POLICIES = Path(__file__).resolve().parents[2] / "ne-govern" / "policies"

_DECISION_QUERY = "/v1/data/ne/govern/decision"


def _stage_govern_bundle(dest: Path) -> Path:
    """Copy the shipped NON-TEST Rego to ``dest``, OPA's read-only load root.

    ``*_test.rego`` is excluded for the same reason ``ne up``'s staging excludes it
    (tools/ne-cli/src/ne/cli/opa_bundle.py::stage_govern_policies): those files are
    source-only fixtures that carry ``package ne.govern`` and are never part of the
    served bundle.
    """
    for rego in sorted(_GOVERN_POLICIES.rglob("*.rego")):
        if rego.name.endswith("_test.rego"):
            continue
        target = dest / rego.relative_to(_GOVERN_POLICIES)
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

    ⛔ WHY THIS EXISTS — IT IS AN OUTAGE FIX, NOT A CONVENIENCE. The three live
    proofs in this package used to ``PUT /v1/policies/<id>`` the working tree's Rego
    into whatever OPA answered ``NE_GOVERN_OPA_URL``, defaulting to
    ``http://localhost:8181``. On a box running the demo stack that port IS the
    deployed PDP. On 2026-09-09 a run from a STALE worktree planted an older
    ``decision.rego``/``tiers.rego`` next to the deployed bundle; every module
    declares ``package ne.govern``, so they MERGED into one namespace instead of
    being isolated, and ``POST /v1/data/ne/govern/decision`` answered HTTP 500
    ``eval_conflict_error`` — 12 failed governance decisions over ~90 seconds
    (16:08:30Z → 16:09:58Z). ``opa check`` cannot see that state: a merged-conflict
    bundle only fails when it is EVALUATED. It self-healed by accident when a
    later run from an up-to-date checkout happened to overwrite the same ids.
    The same class had already taken governance down on 2026-08-30 through
    ``packages/ne-govern/tests/test_opa_pdp_live.py`` (see its module docstring).

    THE FIX IS STRUCTURAL, NOT PROCEDURAL. These proofs now BRING THEIR OWN PDP.
    OPA reads its load root from disk at startup, so the bundle is never uploaded
    to anything — there is no policy write left in this package to aim at the wrong
    server, and no ``NE_GOVERN_OPA_URL`` default left to aim it with. The address is
    loopback on a port the kernel just handed out, so it cannot be a running
    service's. ``test_opa_pdp_mutation_guardian.py`` keeps it that way.

    A BINARY, NOT THE PINNED CONTAINER. The repo's throwaway-OPA-container pattern
    (tools/ne-cli/tests/test_opa_bundle_live.py::_run_opa) exists because that test's
    SUBJECT is the shipped image + bind-mount. Here the subject is a PDP, and the
    docker socket is ``root:docker`` on the box where these proofs are actually run
    before merge — so a container branch would be unreachable dead code there, while
    CI already installs the sha256-pinned ``opa`` on PATH (.github/workflows/ci.yml,
    "Install OPA (pinned, C11)"). ``shutil.which("opa")``-or-skip is also the repo's
    existing shape for a test that needs the real engine rather than the shipped image
    (packages/ne-govern/tests/test_cedar_parity.py). One way to start it, and it is the
    way that works everywhere these tests can run.

    Session-scoped: one OPA per pytest process, so an xdist worker gets its own
    server rather than sharing one — which is why the three modules no longer carry
    the ``opa-live-shared-server`` xdist group.
    """
    if os.environ.get(LIVE_PROOF_OPT_IN) != "1":
        pytest.skip(
            f"live drop-in proof — opt in with {LIVE_PROOF_OPT_IN}=1 (it starts its OWN "
            "disposable OPA; it does NOT touch any deployed PDP)"
        )
    opa_binary = shutil.which("opa")
    if opa_binary is None:
        pytest.skip(
            "no `opa` on PATH — install the pinned binary the way CI does "
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
        # assert defined-ness here rather than discovering it as a leg failure later.
        probe = httpx.post(f"{url}{_DECISION_QUERY}", json={"input": {}}, timeout=5.0)
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
