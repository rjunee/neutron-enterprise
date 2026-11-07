# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures + test doubles for the ne-chat tests.

No pytest-asyncio: every async scenario runs via ``asyncio.run``. No cross-package
test imports: the ne-runtime / ne-govern test doubles that ne-chat needs
(RecordingAuditSink, ScriptedGateway, the Rego-faithful OPA transport, the
guardrail settings) are duplicated here, patterned on
``packages/ne-runtime/tests/conftest.py``. The Slack Web API wire is stubbed with
``httpx.MockTransport`` BELOW the SlackChatClient; live Slack never runs in CI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from ne.chat.identity import ChatIdentityLinker
from ne.chat.slack.client import SlackChatClient
from ne.chat.slack.config import SlackChatSettings
from ne.contracts import freeze_config_hash, get_config_hash, reset_config_hash_for_testing
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.contracts.models import Completion, CompletionRequest, Usage
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
from ne.identity.provisioning import InMemoryPrincipalStore, JitPrincipalResolver
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.tools import ToolBinding

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "slack"

TEST_SIGNING_SECRET = "test-secret"
TEST_BOT_TOKEN = "xoxb-test"
ALLOWED_RECIPIENT = "ally@corp.example"
_DECISION_PATH = "/v1/data/ne/govern/decision"


# --- settings + config_hash -------------------------------------------------


def chat_settings(**overrides: Any) -> SlackChatSettings:
    kwargs: dict[str, Any] = {
        "signing_secret": SecretStr(TEST_SIGNING_SECRET),
        "bot_token": SecretStr(TEST_BOT_TOKEN),
    }
    kwargs.update(overrides)
    return SlackChatSettings(**kwargs)


@pytest.fixture(autouse=True)
def frozen_config_hash() -> Iterator[str]:
    reset_config_hash_for_testing()
    freeze_config_hash(chat_settings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


# --- signing + recorded fixtures --------------------------------------------


def sign(body: bytes, ts: int, secret: str = TEST_SIGNING_SECRET) -> dict[str, str]:
    """Compute a REAL v0 Slack signature over ``body`` (fixtures stay valid)."""
    basestring = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": f"v0={digest}",
    }


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --- Slack Web API recording transport (no sockets) -------------------------


def slack_recording_transport(recorder: list[httpx.Request]) -> httpx.MockTransport:
    """A MockTransport that records every request and returns ok:true with a ts."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1720000000.000100"})

    return httpx.MockTransport(handler)


def recording_client(recorder: list[httpx.Request], **overrides: Any) -> SlackChatClient:
    # High rpm so the client's real throttle does not add wall-clock sleeps in CI
    # (the throttle itself is exercised in test_client's retry path).
    overrides.setdefault("requests_per_minute", 6000.0)
    return SlackChatClient(
        chat_settings(**overrides), transport=slack_recording_transport(recorder)
    )


# --- audit sinks ------------------------------------------------------------


class RecordingAuditSink:
    """An in-memory AuditSink that records every appended event."""

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
    """An AuditSink whose append always fails (fail-closed test double)."""

    async def append(self, event: AuditEventLike) -> None:
        raise RuntimeError("audit sink is down")

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=False, checked=0)


# --- scripted model gateway -------------------------------------------------


class ScriptedGateway:
    """A ModelGateway returning scripted completions; records every request."""

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


def tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


# --- Rego-faithful scripted OPA transport -----------------------------------


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
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        doc = overrides.get(action) or _rego_faithful_doc(action)
        return httpx.Response(200, json={"result": doc})

    return httpx.MockTransport(handler)


def make_pdp(sink: Any, *, overrides: dict[str, dict[str, Any]] | None = None) -> GovernedPdp:
    client = httpx.AsyncClient(
        base_url="http://opa.test", transport=rego_faithful_transport(overrides)
    )
    return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- guardrails + bindings + executor ---------------------------------------


def govern_settings() -> GovernSettings:
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(allowed_recipients=(ALLOWED_RECIPIENT,)),
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
            },
        )
    )


def make_guardrails(sink: Any) -> Any:
    return create_guardrail_engine(govern_settings(), sink=sink)


class ToolLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, name: str, result: str = "ok"):
        async def _run(args: Any) -> str:
            self.calls.append((name, dict(args)))
            return result

        return _run

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def conversation_bindings(log: ToolLog) -> list[ToolBinding]:
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
) -> GovernedToolExecutor:
    return GovernedToolExecutor(
        bindings,
        pdp=make_pdp(sink, overrides=pdp_overrides),
        guardrails=make_guardrails(sink),
        approval_gate=approval_gate,
        sink=sink,
    )


# --- approval gate + principals ---------------------------------------------


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def make_gate(
    sink: Any,
    *,
    tier3_named_approvers: tuple[str, ...] = ("carol", "dave"),
    expiry_s: float = 1000.0,
    clock: Any = None,
) -> tuple[SqlAlchemyApprovalStore, DurableApprovalGate, sa.Engine]:
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    settings = RuntimeSettings(
        approvals=ApprovalSettings(
            tier3_named_approvers=tier3_named_approvers,
            tier2_sla_s=100.0,
            tier3_sla_s=100.0,
            expiry_s=expiry_s,
            poll_interval_s=0.0,
        )
    )
    kwargs = {"clock": clock} if clock is not None else {}
    gate = DurableApprovalGate(store=store, sink=sink, settings=settings, **kwargs)
    return store, gate, engine


async def make_linker(
    sink: Any,
    *,
    links: dict[str, str],
    principals: Sequence[Principal] = (),
) -> ChatIdentityLinker:
    store = InMemoryPrincipalStore()
    for p in principals:
        await store.put(p)
    return ChatIdentityLinker(links, JitPrincipalResolver(store), sink)


def principal(pid: str, *, groups: tuple[str, ...] = (), department: str = "ops") -> Principal:
    return Principal(id=pid, groups=groups, claims={"department": department})


# Slack ids -> platform principals used across the approval-mirroring suite. The
# gate's tier3_named_approvers list is ("carol", "dave") — the PLATFORM principal
# ids, never the Slack ids — so approving from Slack is only ever recorded against
# the mapped principal.id.
APPROVER_LINKS = {
    "U0CAROL000": "carol",  # named Tier-3 approver, role:approver
    "U0DAVE000": "dave",  # named Tier-3 approver, role:approver
    "U0EVE000": "eve",  # role:approver but NOT a named Tier-3 approver
    "U0FRANK000": "frank",  # mapped but has NO approver role
}


async def approver_linker(sink: Any) -> ChatIdentityLinker:
    return await make_linker(
        sink,
        links=APPROVER_LINKS,
        principals=[
            principal("carol", groups=("role:approver",)),
            principal("dave", groups=("role:approver",)),
            principal("eve", groups=("role:approver",)),
            principal("frank", groups=("staff",)),
        ],
    )
