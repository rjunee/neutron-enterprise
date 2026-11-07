# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures + test doubles for the ne-runtime tests.

The gateway seam is stood in for with :class:`ScriptedGateway` (a
``ne.contracts.models.ModelGateway`` returning scripted ``Completion`` objects);
the OPA HTTP wire is stubbed with :class:`httpx.MockTransport` BELOW the OPA
protocol, scripted to match the real ``tiers.rego`` semantics. Everything above
those seams runs for real. Async seams are exercised via ``asyncio.run``;
pytest-asyncio is NOT a dependency and must not be added. Cross-package test
imports are not allowed, so the ne-govern conftest doubles are duplicated here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

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
)
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.runtime import ToolSpec
from ne.contracts.tokens import ByteTokenEstimator, ContextBudget
from ne.contracts.types import ApprovalTier, Principal
from ne.govern import build_tool_call_payload as build_tool_call_payload
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
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import SpendLedger
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.runtime.config import RuntimeSettings
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.tools import ToolBinding

_DECISION_PATH = "/v1/data/ne/govern/decision"

# The happy-path recipient the write.external.email.send binding is allowed to mail.
ALLOWED_RECIPIENT = "ally@corp.example"


# --- config_hash: frozen fresh for every test, reset on teardown ----------


@pytest.fixture(autouse=True)
def frozen_config_hash() -> Iterator[str]:
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


# --- recording / failing audit sinks --------------------------------------


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


class FailingAuditSink:
    """An ``AuditSink`` whose ``append`` always fails (fail-closed test double)."""

    async def append(self, event: AuditEventLike) -> None:
        raise RuntimeError("audit sink is down")

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)


class AppendCountFailingSink:
    """Records events but fails the ``append`` at a chosen 1-based call index.

    Lets a test drive a middleware call through the guardrails+PDP (which append
    successfully) and then fail specifically on the middleware's OWN append.
    """

    def __init__(self, fail_on_action: str) -> None:
        self.events: list[Any] = []
        self._fail_on_action = fail_on_action

    async def append(self, event: AuditEventLike) -> None:
        if getattr(event, "action", "") == self._fail_on_action:
            raise RuntimeError(f"audit sink is down for {self._fail_on_action}")
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))


class FailOnceAuditSink:
    """Records events like ``RecordingAuditSink`` but fails the FIRST append whose
    ``event.action == fail_on_action``; every later append (including a retry of
    that same action) succeeds. Drives the audit-outage-then-recovery retry path.
    """

    def __init__(self, fail_on_action: str) -> None:
        self.events: list[Any] = []
        self._fail_on_action = fail_on_action
        self._failed = False

    async def append(self, event: AuditEventLike) -> None:
        if not self._failed and getattr(event, "action", "") == self._fail_on_action:
            self._failed = True
            raise RuntimeError("audit sink is down (once)")
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


# --- approval gates -------------------------------------------------------


class NamedApproverGate:
    """Always APPROVED with a named approver (a valid Tier-2/3 approval)."""

    def __init__(self, approver: str = "alice") -> None:
        self.approver = approver
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        return ApprovalOutcome(status=ApprovalStatus.APPROVED, approver=self.approver)


class AutoApprovingGate:
    """APPROVED but with NO named approver — the never-auto-approve trap."""

    def __init__(self, approver: str | None = None) -> None:
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
    """A gate that must never be consulted (allow/deny paths at tier 0/1)."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        raise AssertionError("approval gate was consulted on a non-approval path")


# --- scripted model gateway (ne.contracts.models.ModelGateway) ------------


class ScriptedGateway:
    """A ``ModelGateway`` returning scripted completions; records every request."""

    def __init__(self, completions: Sequence[Completion] | None = None) -> None:
        self._queue = list(completions or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return text_completion("done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("ScriptedGateway does not stream")


class RecordingGateway:
    """A ``ModelGateway`` that records every ``CompletionRequest`` and returns a
    scripted (or default text) completion — like :class:`ScriptedGateway` but the
    NAME advertises that budget tests inspect the CAPTURED requests, not just drive
    completions. ``requests`` is the wire-visible assembled chat per call."""

    def __init__(self, completions: Sequence[Completion] | None = None) -> None:
        self._queue = list(completions or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return text_completion("done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("RecordingGateway does not stream")


# --- context-budget helpers (NE-CTXB-3) -----------------------------------


def make_budget(
    *, context_window: int, max_output_tokens: int, overhead_margin: int
) -> ContextBudget:
    """A :class:`ContextBudget` for trim tests (``input_budget`` = the three-way diff)."""
    return ContextBudget(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        overhead_margin=overhead_margin,
    )


def default_estimator() -> ByteTokenEstimator:
    """The estimator a ``GatewayModel`` built over ``RuntimeSettings()`` uses — the
    same DEFAULT params (bytes_per_token=1.5, per_call_overhead=8) so a test can
    RE-estimate a captured request and assert the trim invariant byte-for-byte."""
    return ByteTokenEstimator()


def estimate_request(request: CompletionRequest) -> int:
    """Re-estimate a captured request's assembled chat with the SAME serialization
    the trim + gateway pre-flight use (json.dumps of the message model_dumps)."""
    return default_estimator().estimate(
        json.dumps([m.model_dump() for m in request.messages], ensure_ascii=False, default=str)
    )


def req_system(content: str) -> ModelRequest:
    return ModelRequest(parts=[SystemPromptPart(content=content)])


def req_user(content: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=content)])


def req_first(system: str, user: str) -> ModelRequest:
    """A first request bearing BOTH a system prompt and the turn-0 user prompt — the
    partition splits its system parts into the protected head and its user parts
    into droppable group 0."""
    return ModelRequest(parts=[SystemPromptPart(content=system), UserPromptPart(content=user)])


def resp_text(content: str) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content=content)],
        usage=RequestUsage(input_tokens=1, output_tokens=1),
        model_name="prev",
    )


def resp_tool_call(name: str, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=name, args=args)],
        usage=RequestUsage(input_tokens=1, output_tokens=1),
        model_name="prev",
    )


def req_tool_return(name: str, content: str) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name=name, content=content)])


def req_retry(content: str) -> ModelRequest:
    return ModelRequest(parts=[RetryPromptPart(content=content)])


def turn(user: str, *, answer: str | None = None) -> list[ModelMessage]:
    """One WHOLE prior turn: a user request + an assistant text answer."""
    msgs: list[ModelMessage] = [req_user(user)]
    if answer is not None:
        msgs.append(resp_text(answer))
    return msgs


def tool_turn(user: str, *, tool: str, args: dict[str, Any], result: str) -> list[ModelMessage]:
    """One WHOLE prior tool-using turn: user → assistant tool_call → tool return →
    assistant answer. The tool_call/tool_return pair must stay together on a drop."""
    return [
        req_user(user),
        resp_tool_call(tool, args),
        req_tool_return(tool, result),
        resp_text(f"answered-{user}"),
    ]


def tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


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


def _rego_faithful_doc(action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if action == "subagent.route":
        # Mirrors packages/ne-govern/policies/ne/govern/subagent.rego (NE-169,
        # bundle 1.1.0): private/unpinned resolved lanes are tier-0 allow;
        # frontier is a total deny; anything else is unclassified.
        route_tier = (context or {}).get("route_tier")
        if route_tier in ("private", "unpinned"):
            return {
                "effect": "allow",
                "tier": 0,
                "reason": "tier 0: read/internal draft — autonomous, logged",
                "obligations": {},
                "approval_window_seconds": None,
                "approval_window_declared": False,
                "standing_approval_seconds": None,
                "policy_version": "1.1.0",
            }
        if route_tier == "frontier":
            return {
                "effect": "deny",
                "tier": None,
                "reason": (
                    "subagent.route: frontier-lane routing requires an explicit "
                    "grant — default deny"
                ),
                "obligations": {},
                "approval_window_seconds": None,
                "approval_window_declared": False,
                "standing_approval_seconds": None,
                "policy_version": "1.1.0",
            }
        return {
            "effect": "deny",
            "tier": None,
            "reason": "unclassified action",
            "obligations": {},
            "approval_window_seconds": None,
            "approval_window_declared": False,
            "standing_approval_seconds": None,
            "policy_version": "1.1.0",
        }

    tier = _classify(action)
    # Bundle 1.3.0 (ADR-0030): a require-approval decision carries the policy-
    # resolved approval window FIRST-CLASS (built-in floors here: 4 h tier 2,
    # 24 h tier 3 — no data overrides in the faithful stub); allow/deny carry
    # null, exactly like decision.rego. Bundle 1.4.0 adds the provenance bit,
    # and 1.5.0 (F1) the standing lifetime, beside it on the same rules (7 d
    # tier 2, 2 d tier 3). Because the stub has no data document, EVERY window
    # it resolves is a FLOOR, so `approval_window_declared` is false throughout
    # — which is precisely the deployment shape the park-time coherence check
    # must NOT refuse. A test that wants the declared side supplies `overrides`.
    window: int | None = None
    standing: int | None = None
    if tier is None:
        effect = "deny"
    elif tier <= 1:
        effect = "allow"
    else:
        effect = "require-approval"
        window = {2: 14400, 3: 86400}.get(tier)
        standing = {2: 604800, 3: 172800}.get(tier)
    return {
        "effect": effect,
        "tier": tier,
        "reason": f"rego-faithful: {effect} (tier {tier})",
        "obligations": {},
        "approval_window_seconds": window,
        "approval_window_declared": False,
        "standing_approval_seconds": standing,
        "policy_version": "1.1.0",
    }


def rego_faithful_transport(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """A MockTransport that returns Rego-faithful decisions keyed by ``input.action``.

    ``overrides`` maps an action -> a full decision doc (to force e.g. DENY on an
    otherwise-classified action, since real classified actions never deny).
    """
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        context = payload["input"].get("context") or {}
        doc = overrides.get(action) or _rego_faithful_doc(action, context)
        return httpx.Response(200, json={"result": doc})

    return httpx.MockTransport(handler)


def make_pdp(sink: Any, *, overrides: dict[str, dict[str, Any]] | None = None) -> GovernedPdp:
    """A ``GovernedPdp`` with a Rego-faithful stubbed OPA, auditing to ``sink``."""
    client = httpx.AsyncClient(
        base_url="http://opa.test", transport=rego_faithful_transport(overrides)
    )
    return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- guardrail settings + engine ------------------------------------------


def govern_settings() -> GovernSettings:
    """Guardrail config allowlisting exactly the four standard tools + one recipient."""
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(allowed_recipients=(ALLOWED_RECIPIENT,)),
            rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
            tools={
                "read.docs.search": ToolArgPolicy(args={"q": ArgSpec(kind=ToolArgKind.text)}),
                "write.internal.notes.append": ToolArgPolicy(
                    args={"note": ArgSpec(kind=ToolArgKind.text)}
                ),
                "write.external.email.send": ToolArgPolicy(
                    args={
                        "to": ArgSpec(kind=ToolArgKind.email),
                        "body": ArgSpec(kind=ToolArgKind.text),
                    }
                ),
                "finance.payment.initiate": ToolArgPolicy(
                    args={"amount": ArgSpec(kind=ToolArgKind.number)}
                ),
            },
        )
    )


def make_guardrails(
    sink: Any,
    *,
    counter: SlidingWindowCounter | None = None,
    ledger: SpendLedger | None = None,
) -> Any:
    return create_guardrail_engine(govern_settings(), sink=sink, counter=counter, ledger=ledger)


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

    def raising_handler(self, name: str, exc: Exception):
        async def _run(args: Any) -> str:
            self.calls.append((name, dict(args)))
            raise exc

        return _run

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def standard_bindings(log: ToolLog) -> list[ToolBinding]:
    """The four canonical bindings, one per tier, with recording handlers."""
    return [
        ToolBinding(
            spec=ToolSpec(
                name="read.docs.search",
                description="Search internal docs.",
                tier=ApprovalTier.TIER_0,
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            ),
            handler=log.handler("read.docs.search", "docs: 3 results"),
        ),
        ToolBinding(
            spec=ToolSpec(
                name="write.internal.notes.append",
                description="Append an internal note.",
                tier=ApprovalTier.TIER_1,
                input_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                },
            ),
            handler=log.handler("write.internal.notes.append", "noted"),
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
                input_schema={
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                },
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
) -> GovernedToolExecutor:
    return GovernedToolExecutor(
        bindings,
        pdp=make_pdp(sink, overrides=pdp_overrides),
        guardrails=guardrails if guardrails is not None else make_guardrails(sink),
        approval_gate=approval_gate,
        sink=sink,
    )


def make_principal(pid: str = "user:ada", *, department: str | None = None) -> Principal:
    claims = {"department": department} if department is not None else {}
    return Principal(id=pid, claims=claims)


# --- HITL approval-gate fixtures (NE-035) ---------------------------------


class FakeClock:
    """A deterministic, advanceable clock for approval timeout/escalation tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 7, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
def approval_store() -> Iterator[Any]:
    """An in-memory SqlAlchemyApprovalStore with the approval + outbox schema."""
    from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema

    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    try:
        yield SqlAlchemyApprovalStore(engine)
    finally:
        engine.dispose()


def approval_settings(
    *,
    tier3_named_approvers: tuple[str, ...] = (),
    tier2_sla_s: float = 100.0,
    tier3_sla_s: float = 100.0,
    expiry_s: float = 1000.0,
    poll_interval_s: float = 0.0,
    promotion_window: int = 50,
    promotion_min_instances: int = 20,
    promotion_min_approval_rate: float = 0.95,
) -> RuntimeSettings:
    """A RuntimeSettings with fast, test-friendly ApprovalSettings."""
    from ne.runtime.config import ApprovalSettings

    return RuntimeSettings(
        approvals=ApprovalSettings(
            tier3_named_approvers=tier3_named_approvers,
            tier2_sla_s=tier2_sla_s,
            tier3_sla_s=tier3_sla_s,
            expiry_s=expiry_s,
            poll_interval_s=poll_interval_s,
            promotion_window=promotion_window,
            promotion_min_instances=promotion_min_instances,
            promotion_min_approval_rate=promotion_min_approval_rate,
        )
    )


@pytest.fixture
def tier3_settings() -> RuntimeSettings:
    """Settings with a two-name Tier-3 list and fast SLA/expiry/poll."""
    return approval_settings(tier3_named_approvers=("alice", "bob"))


def approval_request(
    action: str, tier: int, *, principal_id: str = "user:ada", payload: dict[str, Any] | None = None
) -> ApprovalRequest:
    """Build an ApprovalRequest (payload defaults to the redacted {'tool': action})."""
    return ApprovalRequest(
        tier=ApprovalTier(tier),
        action=action,
        principal=Principal(id=principal_id),
        payload=payload if payload is not None else {"tool": action},
    )
