# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""VerifiedRouter — the NE-VAF-U3 verified-or-clarify-or-refuse turn PRE-STEP.

Offline fakes stand in for the INJECTED U1 matcher / row table / renderer and
the governed executor (structural ``Protocol`` conformance only — no
``ne.brain_ontology`` import anywhere in this file either, mirroring the
production module's C3 discipline). The Layer-2 refusal channel uses the REAL
``ne.govern.guardrails.engine.GuardrailEngine`` + U2's real
``VerifiedDomainGuardrail`` over a ``RecordingAuditSink``, so every
exactly-one-``guardrail.block`` assertion is faithful to production wiring.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import ToolSpec, VerifiedProvenance
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    GovernedLocalT3SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundDirectResolution,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails._common import failed
from ne.govern.guardrails.engine import GuardrailEngine
from ne.govern.guardrails.verified import VerifiedDomainGuardrail
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError, ToolDeniedError, ToolExecutionError
from ne.runtime.grounding import current_answer_recorder
from ne.runtime.source_results import (
    SOURCE_WITHHELD_TEXT,
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    direct_publication_scope,
    encode_protected_reference,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.verified_router import ROUTER_PASS, RoutedTurn, VerifiedRouter

from .conftest import (
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
)

# --- fakes: the INJECTED U1 matcher / row table / executor surfaces --------


@dataclass(frozen=True, slots=True)
class FakeRow:
    """Mirrors ``ne.brain_ontology.verified.routing.RoutingRow`` structurally."""

    domain: str
    template_id: str
    tool_name: str
    description: str
    triggers: tuple[str, ...]
    guard_terms: tuple[str, ...]
    clarify_max: int


@dataclass(frozen=True, slots=True)
class FakeDecision:
    """Mirrors ``ne.brain_ontology.verified.routing.RouteDecision`` structurally."""

    outcome: str
    template_id: str | None = None
    domain: str | None = None
    domains: tuple[str, ...] = ()


class ScriptedMatcher:
    """A scripted ``match_template``-shaped callable; records every prompt seen."""

    def __init__(
        self,
        mapping: dict[str, FakeDecision] | None = None,
        *,
        default: FakeDecision | None = None,
    ) -> None:
        self._mapping = mapping or {}
        self._default = default or FakeDecision(outcome="pass")
        self.calls: list[str] = []

    def __call__(self, prompt: str, rows: Sequence[FakeRow]) -> FakeDecision:
        self.calls.append(prompt)
        return self._mapping.get(prompt, self._default)


class ScriptedSelector:
    """A scripted ``match_selection``-shaped callable."""

    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self._mapping = mapping or {}

    def __call__(self, reply: str, options: Sequence[FakeRow]) -> str | None:
        return self._mapping.get(reply)


@dataclass
class FakeExecutor:
    """A ``ToolExecutorLike`` fake: scripted result/exception, records every call.

    NE-CTXB-5: when ``result`` is a VerifiedAnswer-shaped JSON string, this fake
    ALSO records it to the active per-run recorder via ``current_answer_recorder``
    — standing in for the real verified-read handler's §4.2 direct-record behavior,
    so the router's ``recorder.answers``-based provenance path is exercised. A
    non-JSON ``result`` records nothing (drives the fail-closed test)."""

    tool_specs: tuple[ToolSpec, ...] = ()
    result: str | None = None
    raise_exc: Exception | None = None
    calls: list[tuple[str, dict[str, Any], Principal]] = field(default_factory=list)

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal))
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None
        record = current_answer_recorder()
        if record is not None:
            try:
                mapping = json.loads(self.result)
            except ValueError:
                mapping = None
            if isinstance(mapping, Mapping) and "template_id" in mapping:
                record(mapping)
        return self.result


def render_answer(raw: str) -> str:
    return "TABLE:" + json.loads(raw)["template_id"]


def answer_json(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "template_id": "accounts-directory",
        "template_version": 3,
        "config_hash": "deadbeef",
        "args": {},
        "columns": ["name", "iri"],
        "rows": [["acme", "urn:ne:entity:acme"]],
        "citations": ["urn:ne:entity:acme"],
        "truncated": False,
        "row_limit": 50,
        "ontology_version": "v1",
        "executed_at": "2026-07-16T00:00:00Z",
    }
    base.update(overrides)
    return json.dumps(base)


def make_engine(sink: RecordingAuditSink | None = None) -> GuardrailEngine:
    return GuardrailEngine(
        [VerifiedDomainGuardrail()], sink if sink is not None else RecordingAuditSink()
    )


def accounts_row(*, clarify_max: int = 2, domain: str = "accounts") -> FakeRow:
    return FakeRow(
        domain=domain,
        template_id="accounts-directory",
        tool_name="read.kg.accounts_directory",
        description="List every account you are cleared to see",
        triggers=("list every account",),
        guard_terms=("every account",),
        clarify_max=clarify_max,
    )


def accounts_spec(
    tier: ApprovalTier = ApprovalTier.TIER_0, name: str = "read.kg.accounts_directory"
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="List every account you are cleared to see.",
        tier=tier,
        input_schema={"type": "object", "properties": {}},
    )


def clarify_marker(domain: str = "accounts") -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content="menu")], model_name=f"ne-verified-router:clarify:{domain}"
    )


def user_turn(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


# --- (a) determinism: model bypassed on ROUTE -------------------------------


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    return eng


def _full_session(
    db_engine: sa.Engine,
    gw: ScriptedGateway,
    sink: Any,
    log: ToolLog,
    *,
    router: VerifiedRouter[Any] | None = None,
) -> DepartmentAgentSession:
    """Mirrors ``test_agent_loop.py``'s ``_session`` helper, extended with ``router=``.

    The SESSION's own executor (``standard_bindings``) is a real
    ``GovernedToolExecutor`` used only for constructor typing/the fallback
    model loop — a ROUTE/CLARIFY/REFUSE turn never touches it; the router
    carries its OWN (fake) executor for the governed verified-answer call.
    """
    store = SqlAlchemySessionStore(db_engine)
    executor = make_executor(standard_bindings(log), sink, approval_gate=_UnusedGate())
    session_id = asyncio.run(
        store.create_conversation("assistant", "eng", "user:ada")
    ).conversation_id
    return DepartmentAgentSession(
        session_id=session_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gw,
        settings=RuntimeSettings(),
        router=router,
    )


class _UnusedGate:
    async def request_approval(self, request: Any) -> Any:
        raise AssertionError("approval gate was consulted on a router-short-circuit path")


def test_route_bypasses_model_and_returns_structural_provenance(engine: sa.Engine) -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(tool_specs=(accounts_spec(),), result=answer_json())
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("this would never tool-call")])
    session = _full_session(engine, gw, sink, log, router=router)

    result = asyncio.run(
        session.run("list every account", principal=make_principal(department="eng"))
    )

    assert result.verified == VerifiedProvenance(
        template_id="accounts-directory",
        template_version=3,
        config_hash="deadbeef",
        row_count=1,
        truncated=False,
    )
    assert gw.requests == []  # model never instantiated
    assert fake_executor.calls[0][0] == "read.kg.accounts_directory"
    assert fake_executor.calls[0][1] == {}


# --- (b) clarify -> ordinal selection -> route ------------------------------


def test_clarify_then_ordinal_selection_routes() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher()  # never consulted: selection resolves first
    selector = ScriptedSelector({"1": "accounts-directory"})
    fake_executor = FakeExecutor(tool_specs=(accounts_spec(),), result=answer_json())
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=selector,
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )
    principal = make_principal(department="eng")
    history: list[ModelMessage] = [user_turn("something ambiguous"), clarify_marker("accounts")]

    outcome = asyncio.run(router.route("1", principal=principal, history=history))

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is not None
    assert outcome.marker == "ne-verified-router:route:accounts"
    assert fake_executor.calls == [("read.kg.accounts_directory", {}, principal)]
    assert matcher.calls == []


# --- (c) clarify-bound exhausted -> exactly one guardrail.block -------------


def test_clarify_bound_exhausted_refuses_with_exactly_one_block() -> None:
    row = accounts_row(clarify_max=2)
    matcher = ScriptedMatcher(
        {"still vague": FakeDecision(outcome="clarify", domain="accounts", domains=("accounts",))}
    )
    selector = ScriptedSelector()  # returns None: falls through to matcher
    fake_executor = FakeExecutor(tool_specs=(accounts_spec(),))
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=selector,
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )
    history: list[ModelMessage] = [
        user_turn("u0"),
        clarify_marker("accounts"),
        user_turn("u1"),
        clarify_marker("accounts"),
    ]

    outcome = asyncio.run(
        router.route("still vague", principal=make_principal(department="eng"), history=history)
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert outcome.verified is None
    assert "None of the verified questions" in outcome.output
    assert "1. " in outcome.output
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1
    assert fake_executor.calls == []


# --- (d) literal marker text in a user prompt is inert ----------------------


def test_literal_marker_in_user_prompt_does_not_count() -> None:
    row = accounts_row(clarify_max=2)
    matcher = ScriptedMatcher(
        {"still vague": FakeDecision(outcome="clarify", domain="accounts", domains=("accounts",))}
    )
    fake_executor = FakeExecutor(tool_specs=(accounts_spec(),))
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )
    history: list[ModelMessage] = [
        user_turn("what is ne-verified-router:clarify anyway"),
        ModelResponse(
            parts=[TextPart(content="just a real answer")], model_name="ne-gateway:agent"
        ),
        user_turn("u1"),
        clarify_marker("accounts"),
    ]

    outcome = asyncio.run(
        router.route("still vague", principal=make_principal(department="eng"), history=history)
    )

    # counter == 1 (only the real trailing marker), clarify_max == 2 => CLARIFY again.
    assert isinstance(outcome, RoutedTurn)
    assert outcome.marker == "ne-verified-router:clarify:accounts"
    assert outcome.verified is None


# --- (e) topic change after a clarify PASSes; empty table PASSes -----------


def test_topic_change_after_clarify_passes() -> None:
    row = accounts_row(clarify_max=2)
    matcher = ScriptedMatcher(default=FakeDecision(outcome="pass"))
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),  # returns None
        render_answer=render_answer,
        executor=FakeExecutor(tool_specs=(accounts_spec(),)),
        guardrail_engine=make_engine(),
    )
    history: list[ModelMessage] = [user_turn("u0"), clarify_marker("accounts")]

    outcome = asyncio.run(
        router.route(
            "totally unrelated topic", principal=make_principal(department="eng"), history=history
        )
    )

    assert outcome is ROUTER_PASS


def test_empty_routing_table_passes() -> None:
    matcher = ScriptedMatcher()
    router: VerifiedRouter[FakeRow] = VerifiedRouter(
        rows=(),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=FakeExecutor(),
        guardrail_engine=make_engine(),
    )

    outcome = asyncio.run(
        router.route("anything", principal=make_principal(department="eng"), history=[])
    )

    assert outcome is ROUTER_PASS
    assert matcher.calls == []


# --- (f) PDP deny -> audited refusal, no leak -------------------------------


def test_pdp_deny_becomes_audited_refusal() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),), raise_exc=ToolDeniedError("denied", "v1")
    )
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert "denied" not in outcome.output
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1


# --- (f2) provenance mismatch (miswired binding) -> audited refusal ----------


def test_provenance_mismatch_becomes_audited_refusal() -> None:
    # A miswired binding: the governed handler for this route's tool returns an
    # artifact stamped with a DIFFERENT template_id than the deterministically
    # selected row. The router must NEVER certify a foreign template_id under
    # this route's marker — fail closed to an audited refusal.
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),),
        result=answer_json(template_id="some-other-template"),
    )
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None  # never certified
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert "some-other-template" not in outcome.output
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1


# --- (f3) a handler that records NO structured answer -> audited refusal ------


def test_route_with_no_recorded_answer_fails_closed() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    # A governed handler that returns the COMPACT rendering but records NOTHING
    # structurally (§4.2): the router must never certify provenance from an
    # unstructured return — fail closed to the audited Layer-2 refusal.
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),), result="[showing 1 of 1 rows]\naccount | name"
    )
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1


# --- (g) wrapped scope error refuses; empty rows is a SUCCESS ---------------


def test_scope_error_becomes_refusal_distinct_from_empty_rows() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )

    scope_executor = FakeExecutor(
        tool_specs=(accounts_spec(),),
        raise_exc=ToolExecutionError("tool 'x' handler raised VerifiedAnswerScopeError"),
    )
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=scope_executor,
        guardrail_engine=make_engine(),
    )
    refused = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )
    assert isinstance(refused, RoutedTurn)
    assert refused.verified is None
    assert refused.marker.startswith("ne-verified-router:refuse:")

    empty_executor = FakeExecutor(tool_specs=(accounts_spec(),), result=answer_json(rows=[]))
    router2 = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=empty_executor,
        guardrail_engine=make_engine(),
    )
    success = asyncio.run(
        router2.route("list every account", principal=make_principal(department="eng"), history=[])
    )
    assert isinstance(success, RoutedTurn)
    assert success.verified is not None
    assert success.verified.row_count == 0
    assert success.marker == "ne-verified-router:route:accounts"


def test_unbound_principal_failure_becomes_refusal_not_leak() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),),
        raise_exc=ToolExecutionError("tool 'accounts-directory' handler raised RuntimeError"),
    )
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert "accounts-directory" not in outcome.output
    assert "RuntimeError" not in outcome.output


# --- (h) GuardrailViolation from execute: no double-audit -------------------


def test_guardrail_violation_from_execute_does_not_double_audit() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),),
        raise_exc=GuardrailViolation((failed("verified.unrouted", "x"),)),
    )
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert blocks == []  # the router's OWN engine never enforced a second time


# --- (h2) GuardrailEvaluationError from the router's OWN enforce() ----------
# is already audited (guardrail.error) and must still render the Layer-2
# refusal, not escape as a raised exception (Codex P1).


@dataclass(frozen=True, slots=True)
class _RaisingGuardrail:
    """A ``Guardrail`` whose ``check`` raises internally (never a verdict)."""

    def check(self, payload: Mapping[str, Any]) -> Any:
        raise RuntimeError("boom: guardrail predicate cannot complete deterministically")


def test_guardrail_evaluation_error_from_own_enforce_still_refuses() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),), raise_exc=ToolDeniedError("denied", "v1")
    )
    sink = RecordingAuditSink()
    engine = GuardrailEngine([_RaisingGuardrail()], sink)
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=engine,
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert "denied" not in outcome.output
    errors = [e for e in sink.events if getattr(e, "action", "") == "guardrail.error"]
    assert len(errors) == 1  # already audited before GuardrailEvaluationError was raised


# --- (i) routed turn appends the synthetic exchange to history -------------


def test_routed_turn_appends_synthetic_exchange_to_history(engine: sa.Engine) -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(tool_specs=(accounts_spec(),), result=answer_json())
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("never reached")])
    session = _full_session(engine, gw, sink, log, router=router)

    result = asyncio.run(
        session.run("list every account", principal=make_principal(department="eng"))
    )

    history = asyncio.run(
        session._store.load_conversation_history(session.session_id, "user:ada", "eng", "assistant")
    )
    assert len(history) >= 2
    request, response = history[-2], history[-1]
    assert isinstance(request, ModelRequest)
    assert isinstance(request.parts[0], UserPromptPart)
    assert request.parts[0].content == "list every account"
    assert isinstance(response, ModelResponse)
    assert isinstance(response.parts[0], TextPart)
    assert response.parts[0].content == result.output
    assert response.model_name == "ne-verified-router:route:accounts"


# --- (j) router=None leaves the pre-existing model path byte-identical -----


def test_router_none_leaves_model_path_unchanged(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final answer")])
    session = _full_session(engine, gw, sink, log)  # router omitted entirely

    result = asyncio.run(session.run("hello", principal=make_principal(department="eng")))

    assert result.output == "final answer"
    assert result.verified is None
    assert gw.requests  # the model ran


# --- (k) provenance template_version is a genuine int -----------------------


def test_provenance_template_version_is_int() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),), result=answer_json(template_version=3)
    )
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is not None
    assert isinstance(outcome.verified.template_version, int)
    assert outcome.verified.template_version == 3


# --- tier-gated HITL defense: never awaits the executor ---------------------


def test_tier_gated_route_refuses_without_executor_call() -> None:
    row = accounts_row()
    row = FakeRow(
        domain=row.domain,
        template_id=row.template_id,
        tool_name="write.external.kg.accounts_directory",
        description=row.description,
        triggers=row.triggers,
        guard_terms=row.guard_terms,
        clarify_max=row.clarify_max,
    )
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(
            accounts_spec(tier=ApprovalTier.TIER_2, name="write.external.kg.accounts_directory"),
        )
    )
    sink = RecordingAuditSink()
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(sink),
    )

    outcome = asyncio.run(
        router.route("list every account", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(outcome, RoutedTurn)
    assert outcome.verified is None
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert fake_executor.calls == []  # the executor is NEVER awaited
    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert len(blocks) == 1


# --- audit-append failure propagates, never becomes a refusal --------------


def test_audit_failure_from_execute_propagates() -> None:
    row = accounts_row()
    matcher = ScriptedMatcher(
        {
            "list every account": FakeDecision(
                outcome="route", template_id="accounts-directory", domain="accounts"
            )
        }
    )
    fake_executor = FakeExecutor(
        tool_specs=(accounts_spec(),), raise_exc=RuntimeAuditError("audit sink down")
    )
    router = VerifiedRouter(
        rows=(row,),
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=fake_executor,
        guardrail_engine=make_engine(),
    )

    with pytest.raises(RuntimeAuditError):
        asyncio.run(
            router.route(
                "list every account", principal=make_principal(department="eng"), history=[]
            )
        )


# --- deterministic, sorted, byte-stable clarify menu ------------------------


def test_clarify_menu_is_deterministic_and_sorted() -> None:
    row_b = FakeRow(
        domain="d",
        template_id="zzz-two",
        tool_name="read.kg.two",
        description="Second Q",
        triggers=("second trigger",),
        guard_terms=("second term",),
        clarify_max=2,
    )
    row_a = FakeRow(
        domain="d",
        template_id="aaa-one",
        tool_name="read.kg.one",
        description="First Q",
        triggers=("first trigger",),
        guard_terms=("first term",),
        clarify_max=2,
    )
    matcher = ScriptedMatcher(
        {"vague": FakeDecision(outcome="clarify", domain="d", domains=("d",))}
    )
    router = VerifiedRouter(
        rows=(row_b, row_a),  # deliberately out-of-order
        match=matcher,
        match_selection=ScriptedSelector(),
        render_answer=render_answer,
        executor=FakeExecutor(),
        guardrail_engine=make_engine(),
    )

    first = asyncio.run(
        router.route("vague", principal=make_principal(department="eng"), history=[])
    )
    second = asyncio.run(
        router.route("vague", principal=make_principal(department="eng"), history=[])
    )

    assert isinstance(first, RoutedTurn) and isinstance(second, RoutedTurn)
    assert first.output == second.output
    assert first.output.index("1. First Q") < first.output.index("2. Second Q")


# --- C3 guardian: no ne.brain_ontology import anywhere in the module -------


def test_verified_router_module_imports_no_brain_ontology() -> None:
    import ne.runtime.verified_router as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("ne.brain_ontology")
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("ne.brain_ontology")


# --- PRE-015A0-ENFORCEMENT: a source-bearing routed row is a DIRECT publication ---
#
# A contributor-stamped verified row reaches the user without a model send, so the
# router re-authorizes its opaque reference under the request's own snapshot before
# rendering. Anything but AUTHORIZED refuses the turn rather than rendering the safe
# placeholder as if it were a verified answer.

_EXPIRES_AT = 1_800_000_000
_BEFORE_EXPIRY = datetime.fromtimestamp(_EXPIRES_AT - 1, tz=UTC)


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="governed_local_t3",
        connector_instance_id="connector-a",
        point=GovernedLocalT3SourcePoint(dataset_name="meridian", contributor_key="accounts"),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="governed_local_t3", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="rev-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _protected_reference() -> str:
    return encode_protected_reference(
        SourceBoundToolResultRef(
            reference_id="verified-row-1", stamps=(_stamp(),), result_hash="e" * 64
        )
    )


def _tolerant_render(raw: str) -> str:
    """Render a verified-answer blob, and any other text verbatim.

    Deliberately NOT the strict module-level ``render_answer``: that one raises on
    anything that is not verified-answer JSON, so it would refuse the safe
    placeholder BY ACCIDENT and the drift control below would pass even if the
    router had stopped deciding anything. A tolerant renderer makes the assertion
    about the ROUTER's decision.
    """

    try:
        return "TABLE:" + json.loads(raw)["template_id"]
    except (ValueError, KeyError, TypeError):
        return raw


@dataclass
class ProtectedRowExecutor:
    """A verified-read handler whose model-visible return is an opaque reference.

    It still records the structured answer directly (§4.2), exactly like the real
    handler — so provenance comes from ``recorder.answers`` while the rendered text
    comes only from the re-authorized protected material.
    """

    tool_specs: tuple[ToolSpec, ...] = ()
    reference: str = ""
    calls: list[tuple[str, dict[str, Any], Principal]] = field(default_factory=list)

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal))
        record = current_answer_recorder()
        if record is not None:
            record(json.loads(answer_json()))
        return self.reference


class DirectResolver:
    """A ``SourceBoundResultResolver`` scripting one direct-publication decision."""

    def __init__(self, decision: SourceAuthorityDecision, text: str | None) -> None:
        self._decision = decision
        self._text = text
        self.calls: list[tuple[str, str, str]] = []

    async def for_direct_result(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        publication_attempt_id: str,
    ) -> SourceBoundDirectResolution:
        self.calls.append((principal_id, workflow_execution_id, publication_attempt_id))
        authorized = self._decision is SourceAuthorityDecision.AUTHORIZED
        return SourceBoundDirectResolution(
            decision=self._decision,
            materialized_text=self._text if authorized else None,
            stamps=(_stamp(),) if authorized else (),
            safe_reason=None if authorized else "safe",
            workflow_execution_id=workflow_execution_id,
            publication_attempt_id=publication_attempt_id,
        )

    async def for_model_send(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_approval(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _protected_router(resolver: DirectResolver) -> VerifiedRouter[FakeRow]:
    return VerifiedRouter(
        rows=(accounts_row(),),
        match=ScriptedMatcher(
            {
                "list every account": FakeDecision(
                    outcome="route", template_id="accounts-directory", domain="accounts"
                )
            }
        ),
        match_selection=ScriptedSelector(),
        render_answer=_tolerant_render,
        executor=ProtectedRowExecutor(
            tool_specs=(accounts_spec(),), reference=_protected_reference()
        ),
        guardrail_engine=make_engine(),
        source_guard=ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY),
    )


def _route_protected(router: VerifiedRouter[FakeRow], *, with_context: bool) -> Any:
    async def scenario() -> Any:
        with direct_publication_scope(workflow_execution_id="wf-1", publication_attempt_id="pub-1"):
            if not with_context:
                return await router.route(
                    "list every account", principal=make_principal(), history=[]
                )
            with authenticated_request_scope(
                AuthenticatedRequestContext(
                    principal_id="user:ada",
                    authorization_groups=("group:staff",),
                    issuer="https://oidc.test/realms/ne",
                    expires_at=_EXPIRES_AT,
                )
            ):
                return await router.route(
                    "list every account", principal=make_principal(), history=[]
                )

    return asyncio.run(scenario())


def test_authorized_protected_row_renders_only_the_reauthorized_material() -> None:
    resolver = DirectResolver(SourceAuthorityDecision.AUTHORIZED, answer_json())
    outcome = _route_protected(_protected_router(resolver), with_context=True)
    assert isinstance(outcome, RoutedTurn)
    assert outcome.marker == "ne-verified-router:route:accounts"
    assert outcome.output == "TABLE:accounts-directory"
    assert [(principal, wf, attempt) for principal, wf, attempt in resolver.calls] == [
        ("user:ada", "wf-1", "pub-1")
    ]


def test_drifted_protected_row_refuses_instead_of_rendering_the_withheld_placeholder() -> None:
    resolver = DirectResolver(SourceAuthorityDecision.PERMANENT_WITHHELD, None)
    outcome = _route_protected(_protected_router(resolver), with_context=True)
    assert isinstance(outcome, RoutedTurn)
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert outcome.verified is None
    # The placeholder is never rendered AS the answer — under a renderer that would
    # happily pass it through, the router still refuses.
    assert SOURCE_WITHHELD_TEXT not in outcome.output
    assert _tolerant_render(SOURCE_WITHHELD_TEXT) == SOURCE_WITHHELD_TEXT


def test_protected_row_without_a_bound_context_refuses_rather_than_routing() -> None:
    resolver = DirectResolver(SourceAuthorityDecision.AUTHORIZED, answer_json())
    outcome = _route_protected(_protected_router(resolver), with_context=False)
    assert isinstance(outcome, RoutedTurn)
    assert outcome.marker.startswith("ne-verified-router:refuse:")
    assert outcome.verified is None
    assert resolver.calls == []
