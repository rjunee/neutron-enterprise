# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The additive ``tool`` SSE event (NE-137 Part A) — REAL C6 decisions + real audit seq.

Each governed tool call transiting the ONE ``GovernedToolExecutor`` emits a
``{type:'tool', name, tier, decision, audit_seq}`` frame (+ the exact
``approval_id`` on pending and its correlated terminal transition), interleaved
BEFORE the terminal frame. The decision/tier/audit_seq are
DERIVED from the real audit events the real PDP / gate / executor emitted — never
fabricated: ``audit_seq`` is cross-checked against the REAL hash-chain record, the
pending ``approval_id`` against the REAL ``/v1/approvals/pending`` row. The existing
token/done payloads stay byte-identical (proven here + by unchanged ``test_sse``).
"""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa

from ne.api.sse import relay_event_stream, sse_frame
from ne.api.tool_transit import (
    TURN_TOOL_TRANSITS,
    ToolTransit,
    ToolTransitObservingSink,
    TurnTransitChannel,
)
from ne.api.turn_wire import TurnFrame, done_frame, tool_frame
from ne.audit import list_records
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.config import ApprovalSettings
from ne.runtime.errors import ActionValidationError
from ne.runtime.tools import ToolBinding

from .conftest import (
    AppBuilder,
    Harness,
    chunk,
    get_state,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)

_DEPT = "engineering"


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department=_DEPT)


async def _open_session(client, h) -> str:  # type: ignore[no-untyped-def]
    created = await client.post(
        "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
    )
    return created.json()["session_id"]


async def _noop_handler(_args: Mapping[str, object]) -> str:
    return "ok"


def test_allow_tool_event_with_real_audit_seq(make_app: AppBuilder) -> None:
    h = make_app(
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "x"}),
            text_completion("ans"),
        ],
        chunks=[chunk("c1", acl=["eng"])],
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)
            events = [f.event for f in parsed.frames]

            tools = parsed.of_type("tool")
            assert len(tools) == 1
            data = tools[0].data
            assert data is not None
            assert data["name"] == "read.knowledge.search"
            assert data["decision"] == "allow"
            assert data["tier"] == 0  # rego-faithful read. ⇒ tier 0
            assert "approval_id" not in data  # allow carries no approval_id
            # An allow is derived from tool.execute[success], whose audit detail
            # records no policy sentence — so none is served and the card keeps
            # the honest absent note. THE OTHER DIRECTION of the reason slot; the
            # served one is test_deny_tool_event_is_the_real_pdp_deny below.
            assert "reason" not in data

            # audit_seq is the REAL tool.execute[success] chain seq (never invented).
            records = list_records(get_state(h.app).audit_engine, action="tool.execute")
            success = [
                rec
                for rec in records
                if rec.outcome == "success" and rec.resource == "tool:read.knowledge.search"
            ]
            assert success, "no tool.execute[success] record on the chain"
            assert data["audit_seq"] == success[0].seq

            # The successful governed retrieval's exact ACL-trimmed citation is
            # relayed once, between answer token and terminal done.
            assert events == ["tool", "token", "citations", "done"]
            citations = parsed.of_type("citations")[0].data
            assert citations == {
                "type": "citations",
                "citations": [
                    {
                        "chunk_id": "c1",
                        "document_id": "doc-c1",
                        "source_system": "wiki",
                        "source_ref": "ref-c1",
                    }
                ],
            }

            # token/done payloads remain byte-identical.
            token = parsed.of_type("token")[0].data
            assert token == {"type": "token", "delta": "ans"}
            done = parsed.of_type("done")[0].data
            assert done is not None
            assert done["type"] == "done" and done["output"] == "ans"

    run(_t())


def test_multiple_searches_stably_dedupe_only_acl_served_citations(
    make_app: AppBuilder,
) -> None:
    h = make_app(
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "first"}),
            tool_call_completion("read.knowledge.search", {"query": "second"}),
            text_completion("ans"),
        ],
        chunks=[
            chunk("served-1", acl=["eng"]),
            chunk("forbidden", acl=["marketing"]),
            chunk("served-2", acl=["eng"]),
        ],
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            response = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "search twice"},
                headers=_auth(h),
            )
            assert response.status_code == 200
            parsed = parse_sse(response.text)
            assert parsed.events() == ["tool", "tool", "token", "citations", "done"]
            citation_frames = parsed.of_type("citations")
            assert len(citation_frames) == 1
            assert citation_frames[0].data == {
                "type": "citations",
                "citations": [
                    {
                        "chunk_id": "served-1",
                        "document_id": "doc-served-1",
                        "source_system": "wiki",
                        "source_ref": "ref-served-1",
                    },
                    {
                        "chunk_id": "served-2",
                        "document_id": "doc-served-2",
                        "source_system": "wiki",
                        "source_ref": "ref-served-2",
                    },
                ],
            }
            assert "forbidden" not in response.text

    run(_t())


def test_deny_tool_event_is_the_real_pdp_deny(make_app: AppBuilder) -> None:
    # A model-emitted call to an UNREGISTERED, unclassified tool: the executor's
    # unknown-tool branch audits ``tool.unknown`` then STILL consults the PDP, which
    # rego-faithfully classifies ``None`` ⇒ a real DENY (``pdp.decide[denied]``). The
    # observer maps the pdp.decide deny to the SCRAM card (NOT the tool.unknown event —
    # that would double-card). A registered binding cannot carry an unclassified name
    # (the tiers.rego python mirror rejects it at registration), so the honest real
    # deny rides the unknown-tool path.
    action = "misc.unclassified.op"
    h = make_app(
        completions=[tool_call_completion(action, {}), text_completion("blocked answer")],
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)

            tools = parsed.of_type("tool")
            assert len(tools) == 1
            data = tools[0].data
            assert data is not None
            assert data["decision"] == "deny"
            assert data["tier"] is None  # unclassified action has no tier — honest None
            # audit_seq == the REAL pdp.decide[denied] record seq.
            records = list_records(get_state(h.app).audit_engine, action="pdp.decide")
            denied = [
                rec
                for rec in records
                if rec.outcome == "denied" and rec.resource == f"tool:{action}"
            ]
            assert denied, "no pdp.decide[denied] record on the chain"
            assert data["audit_seq"] == denied[0].seq

            # END TO END: the wire's sentence IS the sentence the REAL pdp.decide
            # audit record holds, byte for byte. Derived from the chain rather than
            # hardcoded, so this pins the PIPE (audit detail -> transit -> SSE) and
            # cannot be satisfied by the console and the harness agreeing on a
            # string neither of them read off a decision. The literal the shipped
            # bundle writes is pinned separately, at the unit layer, by
            # test_pdp_deny_serves_the_policy_reason_and_null_where_there_is_none.
            audited = json.loads(str(denied[0].payload["detail"]))["reason"]
            assert isinstance(audited, str) and audited, "the PDP recorded no reason to serve"
            assert data["reason"] == audited

            # SCRAM is a real deny, NOT a turn crash — the turn still completes.
            assert parsed.of_type("done"), "the denied turn did not complete"
            assert not parsed.of_type("citations")

    run(_t())


def _validate_denied_binding() -> ToolBinding:
    """NE-VALIDATE-SURFACES P3 repro: a binding whose ``validate_args`` unconditionally
    refuses pre-gate. ``write.internal.*`` rego-faithfully classifies tier 1 / effect
    allow (conftest._classify), so ``pdp.decide`` fires (remembering tier 1) BEFORE the
    refusal — the real tier the deny transit must report."""

    def _validate_args(_args: object) -> None:
        raise ActionValidationError(
            "invalid input for 'write.internal.crm.note': fields ['record_id']"
        )

    return ToolBinding(
        spec=ToolSpec(
            name="write.internal.crm.note",
            description="demo write action missing a required identity field",
            tier=ApprovalTier.TIER_1,
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        ),
        handler=_noop_handler,
        validate_args=_validate_args,
    )


def _tier2_binding(
    executions: list[dict[str, object]] | None = None, *, fail: bool = False
) -> ToolBinding:
    async def _recording_handler(args: Mapping[str, object]) -> str:
        if executions is not None:
            executions.append(dict(args))
        if fail:
            raise RuntimeError("controlled handler failure")
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name="write.external.demo.send",
            description="demo",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        ),
        handler=_recording_handler,
    )


def _hitl_app(
    make_app: AppBuilder,
    executions: list[dict[str, object]] | None = None,
    *,
    fail: bool = False,
) -> Harness:
    action = "write.external.demo.send"
    return make_app(
        approver_roles=("approvers",),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        extra_tool_bindings=(_tier2_binding(executions, fail=fail),),
        extra_tool_policies={
            action: ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.text, required=True)})
        },
        completions=[
            tool_call_completion(action, {"to": "ops"}),
            text_completion("sent"),
        ],
    )


async def _drive_hitl(h, *, decision: str):  # type: ignore[no-untyped-def]
    """Start a tier-2 chat turn, wait for the pending row, then approve/reject it."""
    action = "write.external.demo.send"
    loop = asyncio.get_running_loop()
    ada = h.bearer("user:ada", groups=["eng"], department="engineering")
    appr = h.bearer("user:approver", groups=["approvers"])
    async with h.client() as writer, h.client() as approver:
        created = await writer.post(
            "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
        )
        sid = created.json()["session_id"]
        task = asyncio.create_task(
            writer.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "send it"},
                headers=ada,
            )
        )
        deadline = loop.time() + 10.0
        pending = None
        while loop.time() < deadline:
            rr = await approver.get("/api/v1/approvals/pending", headers=appr)
            match = [x for x in rr.json()["approvals"] if x["action"] == action]
            if match:
                pending = match[0]
                break
            await asyncio.sleep(0.05)
        assert pending is not None, "the tier-2 approval never appeared in the queue"
        resp = await approver.post(
            f"/api/v1/approvals/{pending['request_id']}/{decision}",
            json={} if decision == "approve" else {"reason": ""},
            headers=appr,
        )
        assert resp.status_code == 200
        sse = await task
        return pending, parse_sse(sse.text)


def test_pending_then_allow_through_real_hitl(make_app: AppBuilder) -> None:
    executions: list[dict[str, object]] = []
    h = _hitl_app(make_app, executions)

    async def _t() -> None:
        pending, parsed = await _drive_hitl(h, decision="approve")
        tools = parsed.of_type("tool")
        decisions = [t.data["decision"] for t in tools if t.data is not None]
        assert decisions == ["pending", "allow"]

        pend = tools[0].data
        assert pend is not None
        assert pend["decision"] == "pending"
        assert pend["tier"] == 2
        assert pend["approval_id"] == pending["request_id"]  # the REAL gate request_id

        allow = tools[1].data
        assert allow is not None
        assert allow["decision"] == "allow"
        assert allow["name"] == "write.external.demo.send"
        assert allow["approval_id"] == pending["request_id"]

        # The connected request resumed exactly once. The transit id is not a
        # browser correlation guess: it came from this sole tool.execute event's
        # own provenance, which the executor stamped at the execution site.
        assert executions == [{"to": "ops"}]
        executed = [
            rec
            for rec in list_records(get_state(h.app).audit_engine, action="tool.execute")
            if rec.resource == "tool:write.external.demo.send" and rec.outcome == "success"
        ]
        assert len(executed) == 1
        assert allow["audit_seq"] == executed[0].seq

        done = parsed.of_type("done")
        assert done and done[0].data is not None
        assert done[0].data["output"] == "sent"

    run(_t())


def test_rejected_approval_emits_deny(make_app: AppBuilder) -> None:
    executions: list[dict[str, object]] = []
    h = _hitl_app(make_app, executions)

    async def _t() -> None:
        pending, parsed = await _drive_hitl(h, decision="reject")
        tools = parsed.of_type("tool")
        decisions = [t.data["decision"] for t in tools if t.data is not None]
        assert decisions == ["pending", "rejected"]
        denied = tools[1].data
        assert denied is not None
        assert denied["approval_id"] == pending["request_id"]
        assert executions == []
        executed = [
            rec
            for rec in list_records(get_state(h.app).audit_engine, action="tool.execute")
            if rec.resource == "tool:write.external.demo.send"
        ]
        assert executed == []
        # The turn still TERMINATES (a rejected approval is a real deny, not a crash).
        assert parsed.of_type("done")

    run(_t())


def test_approved_handler_failure_replaces_pending_with_failed(make_app: AppBuilder) -> None:
    executions: list[dict[str, object]] = []
    h = _hitl_app(make_app, executions, fail=True)

    async def _t() -> None:
        pending, parsed = await _drive_hitl(h, decision="approve")
        tools = parsed.of_type("tool")
        decisions = [t.data["decision"] for t in tools if t.data is not None]
        assert decisions == ["pending", "failed"]
        failed = tools[1].data
        assert failed is not None
        assert failed["approval_id"] == pending["request_id"]
        assert executions == [{"to": "ops"}]
        errors = [
            rec
            for rec in list_records(get_state(h.app).audit_engine, action="tool.execute")
            if rec.resource == "tool:write.external.demo.send" and rec.outcome == "error"
        ]
        assert len(errors) == 1
        assert failed["audit_seq"] == errors[0].seq
        done = parsed.of_type("done")
        assert done and done[0].data is not None
        assert done[0].data["output"] == "sent"  # the agent recovered after the failed action

    run(_t())


def test_validate_denied_tool_event_is_the_real_pre_gate_refusal(make_app: AppBuilder) -> None:
    """NE-VALIDATE-SURFACES P3: a pre-gate ``tool.validate[denied]`` refusal must render
    as a single deny card — not silently vanish from the console's tool-card lane."""
    tool = "write.internal.crm.note"
    h = make_app(
        completions=[
            tool_call_completion(tool, {"title": "t", "body": "b"}),
            text_completion("recovered"),
        ],
        extra_tool_bindings=(_validate_denied_binding(),),
        extra_tool_policies={
            tool: ToolArgPolicy(
                args={
                    "title": ArgSpec(kind=ToolArgKind.text, required=True),
                    "body": ArgSpec(kind=ToolArgKind.text, required=True),
                }
            )
        },
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "note it"},
                headers=_auth(h),
            )
            assert r.status_code == 200, r.text
            parsed = parse_sse(r.text)

            tools = parsed.of_type("tool")
            assert len(tools) == 1, f"expected exactly one tool card, got {len(tools)}"
            data = tools[0].data
            assert data is not None
            assert data["decision"] == "deny"
            assert data["name"] == tool
            assert "approval_id" not in data
            # The REAL PDP-decided tier remembered from pdp.decide (write.internal.* ⇒ 1).
            assert data["tier"] == 1

            validate_records = list_records(get_state(h.app).audit_engine, action="tool.validate")
            denied = [
                rec
                for rec in validate_records
                if rec.outcome == "denied" and rec.resource == f"tool:{tool}"
            ]
            assert denied, "no tool.validate[denied] record on the chain"
            assert data["audit_seq"] == denied[0].seq

            approval_requests = list_records(
                get_state(h.app).audit_engine, action="approval.requested"
            )
            assert not any(rec.resource == f"tool:{tool}" for rec in approval_requests)

            # The runner yields a BLOCKED result, not a crash — the turn completes.
            assert parsed.of_type("done"), "the validation-refused turn did not complete"

    run(_t())


def test_unrelated_tool_event_maps_to_no_card() -> None:
    """The anti-'map-everything-to-deny' control (unit-level, via the PUBLIC append
    path): only ``tool.validate[denied]`` yields a card. A ``tool.validate[success]``
    (denied-qualifier control) and an unrelated ``tool.frobnicate[denied]`` action must
    NOT — and the inner sink must still observe every append (governance is never
    suppressed by observation)."""
    appended: list[AuditEventLike] = []

    class _RecordingInner:
        async def append(self, event: AuditEventLike) -> None:
            appended.append(event)

        async def verify(
            self, *, start: datetime | None = None, end: datetime | None = None
        ) -> AuditVerification:
            raise AssertionError("verify is unused by this test")

    def _event(*, action: str, outcome: str, event_id: str) -> AuditEventLike:
        # A minimal stand-in for a real audit event: only the attributes _observe
        # actually reads via getattr (action/outcome/resource/event_id/detail/
        # provenance — action and outcome as PLAIN STRINGS, the str-backed
        # AuditOutcome comparison is part of the contract under test). Cast to the
        # structural protocol — ToolTransitObservingSink.append only ever reads
        # attributes dynamically, so a SimpleNamespace is a faithful stand-in.
        return cast(
            AuditEventLike,
            types.SimpleNamespace(
                event_type="audit",
                config_hash="test-hash",
                kpi_tag=None,
                actor="user:test",
                provenance=None,
                occurred_at=datetime.now(UTC),
                action=action,
                resource="tool:x",
                outcome=outcome,
                event_id=event_id,
                detail=None,
            ),
        )

    sink = ToolTransitObservingSink(_RecordingInner(), audit_engine=sa.create_engine("sqlite://"))
    channel = TurnTransitChannel()
    token = TURN_TOOL_TRANSITS.set(channel)
    try:

        async def _feed() -> None:
            await sink.append(_event(action="tool.validate", outcome="denied", event_id="e1"))
            await sink.append(_event(action="tool.validate", outcome="success", event_id="e2"))
            await sink.append(_event(action="tool.frobnicate", outcome="denied", event_id="e3"))

        run(_feed())

        assert channel.queue.qsize() == 1
        transit = channel.queue.get_nowait()
        assert transit.decision == "deny"
        assert transit.name == "x"
    finally:
        TURN_TOOL_TRANSITS.reset(token)

    # Observation never suppresses the governance write: all three appends landed.
    assert len(appended) == 3


def test_approval_terminal_statuses_and_execution_error_are_not_human_rejection() -> None:
    """The observer preserves the current audit event's exact approval truth.

    Expiry/escalation and the APPROVED-without-named-approver integrity refusal all
    share ``tool.approval[denied]`` at the audit outcome layer; they must remain
    distinguishable. A failed approved execution is its own lifecycle transition,
    while an ungated execution error is not projected into the approval lane.
    """

    class _Inner:
        async def append(self, event: AuditEventLike) -> None:
            del event
            return None

        async def verify(
            self, *, start: datetime | None = None, end: datetime | None = None
        ) -> AuditVerification:
            raise AssertionError("verify is unused by this test")

    def _event(
        *,
        action: str,
        outcome: str,
        event_id: str,
        detail: str | None,
        approval_id: str | None,
    ) -> AuditEventLike:
        return cast(
            AuditEventLike,
            types.SimpleNamespace(
                event_type="audit",
                config_hash="test-hash",
                kpi_tag=None,
                actor="user:test",
                provenance={"approval_id": approval_id} if approval_id else None,
                occurred_at=datetime.now(UTC),
                action=action,
                resource="tool:write.external.demo.send",
                outcome=outcome,
                event_id=event_id,
                detail=detail,
            ),
        )

    sink = ToolTransitObservingSink(_Inner(), audit_engine=sa.create_engine("sqlite://"))
    channel = TurnTransitChannel()
    token = TURN_TOOL_TRANSITS.set(channel)
    try:

        async def _feed() -> None:
            for idx, status in enumerate(("rejected", "expired", "escalated", "approved")):
                await sink.append(
                    _event(
                        action="tool.approval",
                        outcome="denied",
                        event_id=f"approval-{idx}",
                        detail=f'{{"status":"{status}","tier":2}}',
                        approval_id=f"req-{idx}",
                    )
                )
            await sink.append(
                _event(
                    action="tool.approval",
                    outcome="denied",
                    event_id="approval-legacy",
                    detail=None,
                    approval_id="req-legacy",
                )
            )
            await sink.append(
                _event(
                    action="tool.execute",
                    outcome="error",
                    event_id="execute-approved-error",
                    detail='{"error_class":"RuntimeError"}',
                    approval_id="req-execute",
                )
            )
            await sink.append(
                _event(
                    action="tool.execute",
                    outcome="error",
                    event_id="execute-ungated-error",
                    detail='{"error_class":"RuntimeError"}',
                    approval_id=None,
                )
            )

        run(_feed())
        transits = [channel.queue.get_nowait() for _ in range(channel.queue.qsize())]
    finally:
        TURN_TOOL_TRANSITS.reset(token)

    assert [transit.decision for transit in transits] == [
        "rejected",
        "expired",
        "escalated",
        "integrity-failed",
        "approval-failed",
        "failed",
    ]
    assert [transit.approval_id for transit in transits] == [
        "req-0",
        "req-1",
        "req-2",
        "req-3",
        "req-legacy",
        "req-execute",
    ]


def _reason_probe_sink() -> tuple[ToolTransitObservingSink, TurnTransitChannel]:
    """A sink over a no-op inner, wired to a channel — the reason-parse probe rig."""

    class _Inner:
        async def append(self, event: AuditEventLike) -> None:
            del event
            return None

        async def verify(
            self, *, start: datetime | None = None, end: datetime | None = None
        ) -> AuditVerification:
            raise AssertionError("verify is unused by this test")

    return (
        ToolTransitObservingSink(_Inner(), audit_engine=sa.create_engine("sqlite://")),
        TurnTransitChannel(),
    )


def _pdp_event(*, outcome: str, event_id: str, detail: str | None) -> AuditEventLike:
    return cast(
        AuditEventLike,
        types.SimpleNamespace(
            event_type="audit",
            config_hash="test-hash",
            kpi_tag=None,
            actor="user:test",
            provenance=None,
            occurred_at=datetime.now(UTC),
            action="pdp.decide",
            resource="tool:write.external.demo.send",
            outcome=outcome,
            event_id=event_id,
            detail=detail,
        ),
    )


def _drain(details: list[str | None], *, outcome: str = "denied") -> list[ToolTransit]:
    sink, channel = _reason_probe_sink()
    token = TURN_TOOL_TRANSITS.set(channel)
    try:

        async def _feed() -> None:
            for idx, detail in enumerate(details):
                await sink.append(_pdp_event(outcome=outcome, event_id=f"pdp-{idx}", detail=detail))

        run(_feed())
        return [channel.queue.get_nowait() for _ in range(channel.queue.qsize())]
    finally:
        TURN_TOOL_TRANSITS.reset(token)


# The exact sentence packages/ne-govern/policies/ne/govern/decision.rego rule (e)
# writes, em dash and all — the string the demo's refusal beat actually serves.
_POLICY_SENTENCE = "tier 2: external/PII action \u2014 per-item human approval required"


def test_pdp_deny_serves_the_policy_reason_and_null_where_there_is_none() -> None:
    """BOTH directions of the reason slot, off the REAL audit detail shape.

    ``GovernedPdp._append_audit`` writes ``{effect, reason, tier, policy_version}``
    as a JSON string. A deny whose detail carries a policy sentence serves that
    sentence VERBATIM; a deny whose detail carries no reason serves ``None``, which
    the console renders as its existing "the server sent no reason". Nothing in
    between is invented.
    """
    served, absent, empty = _drain(
        [
            json.dumps(
                {
                    "effect": "deny",
                    "reason": _POLICY_SENTENCE,
                    "tier": 2,
                    "policy_version": "1.2.0",
                }
            ),
            # A detail with a tier and NO reason key at all (the qualifier control:
            # the tier still parses, so the event was read — only the reason is out).
            json.dumps({"effect": "deny", "tier": 2, "policy_version": "1.2.0"}),
            # OpaClient defaults a missing OPA reason to "" (opa.py `_parse_result`).
            # An empty string is not a sentence: it must not render an empty body.
            json.dumps({"effect": "deny", "reason": "", "tier": 2, "policy_version": "1.2.0"}),
        ]
    )
    assert served.reason == _POLICY_SENTENCE
    assert served.tier == 2
    assert absent.reason is None
    assert absent.tier == 2
    assert empty.reason is None
    # …and it reaches the browser on the wire, under its own key, unaltered.
    assert served.sse_payload()["reason"] == _POLICY_SENTENCE
    assert "reason" not in absent.sse_payload()
    assert "reason" not in empty.sse_payload()


def test_reason_parse_degrades_to_none_on_every_malformed_detail() -> None:
    """The defensive cases the tier helper already handles, for the reason helper.

    Absent detail, non-JSON detail, JSON that is not an object, and a reason that is
    not a string all degrade to ``None`` — never a raise (a raise inside ``append``
    would fail-close a legitimate governed call) and never a stringified value.
    """
    transits = _drain(
        [
            None,
            "",
            "not json at all",
            '["reason", "in", "a", "list"]',
            '"a bare json string"',
            json.dumps({"reason": 42}),
            json.dumps({"reason": None}),
            json.dumps({"reason": ["a", "list"]}),
            json.dumps({"reason": {"nested": "object"}}),
        ]
    )
    assert len(transits) == 9, "every malformed detail must still publish its deny card"
    assert [t.reason for t in transits] == [None] * 9
    # The deny itself is unaffected: a malformed detail loses the sentence, not the card.
    assert {t.decision for t in transits} == {"deny"}


def test_backend_error_reason_never_reaches_a_card() -> None:
    """C12 — the operator-only reason the SAME detail key carries on a PDP failure.

    ``GovernedPdp._append_audit`` writes ``reason=str(exc)`` into ``detail["reason"]``
    when the policy BACKEND fails, stamped ``effect: "error"``. That string
    interpolates the OPA decision path, the sidecar URL, up to 200 bytes of raw OPA
    response body and (via the Cedar evaluator) internal rule ids and the live
    principal id — none of which an end user may see through a governance surface.

    TWO independent guards, asserted separately so neither can silently carry the
    other: the observer publishes NO transit for a ``pdp.decide`` that is not a
    denial, AND the reason parser refuses an ``effect: "error"`` detail outright.
    """
    leaky = json.dumps(
        {
            "effect": "error",
            "reason": (
                "OPA returned HTTP 500 for /v1/data/ne/govern/decision: "
                "'principal_id=user:ada rule=e2_tier2_provisional'"
            ),
            "tier": None,
            "policy_version": "unavailable",
        }
    )

    # Guard 1: outcome=error publishes nothing at all.
    assert _drain([leaky], outcome="error") == []

    # Guard 2: even reached with a DENIED outcome — the shape a future edit to the
    # branch above would produce — the error-marked detail yields no sentence.
    (forced,) = _drain([leaky])
    assert forced.decision == "deny"
    assert forced.reason is None
    assert "reason" not in forced.sse_payload()

    # CONTROL: the guard keys on the effect marker, not on the sentence's content.
    # The same operator string under a real deny effect WOULD be served — which is
    # exactly why guard 1 exists and why pdp.py stamps the marker.
    (control,) = _drain([leaky.replace('"effect": "error"', '"effect": "deny"')])
    assert control.reason is not None


def test_only_the_pdp_decision_serves_a_reason() -> None:
    """The other three carded decisions serve none, because they record none.

    ``approval.requested`` writes ``{tier, action}``, ``tool.approval`` writes
    ``{status, tier}`` and ``tool.validate[denied]`` writes ``{error_class}`` — an
    internal Python exception class name, which is operator triage detail and not
    the policy's sentence. Each is fed here with a ``reason`` key it does NOT carry
    in production, so this fails if the observer ever starts reading one off them.
    """
    sink, channel = _reason_probe_sink()

    def _event(*, action: str, outcome: str, event_id: str, detail: str) -> AuditEventLike:
        return cast(
            AuditEventLike,
            types.SimpleNamespace(
                event_type="audit",
                config_hash="test-hash",
                kpi_tag=None,
                actor="user:test",
                provenance={"approval_id": "req-1"},
                occurred_at=datetime.now(UTC),
                action=action,
                resource="tool:write.external.demo.send",
                outcome=outcome,
                event_id=event_id,
                detail=detail,
            ),
        )

    token = TURN_TOOL_TRANSITS.set(channel)
    try:

        async def _feed() -> None:
            await sink.append(
                _event(
                    action="approval.requested",
                    outcome="success",
                    event_id="e1",
                    detail=json.dumps(
                        {"tier": 2, "action": "write.external.demo.send", "reason": "planted"}
                    ),
                )
            )
            await sink.append(
                _event(
                    action="tool.approval",
                    outcome="denied",
                    event_id="e2",
                    detail=json.dumps({"status": "rejected", "tier": 2, "reason": "planted"}),
                )
            )
            await sink.append(
                _event(
                    action="tool.validate",
                    outcome="denied",
                    event_id="e3",
                    detail=json.dumps(
                        {"error_class": "ActionValidationError", "reason": "planted"}
                    ),
                )
            )

        run(_feed())
        transits = [channel.queue.get_nowait() for _ in range(channel.queue.qsize())]
    finally:
        TURN_TOOL_TRANSITS.reset(token)

    assert [t.decision for t in transits] == ["pending", "rejected", "deny"]
    assert [t.reason for t in transits] == [None, None, None]
    assert all("reason" not in t.sse_payload() for t in transits)


def test_turn_frame_tool_type_roundtrip() -> None:
    # sse_payload shape: approval_id present whenever this exact transit supplied
    # one, and omitted when absent (including an ungated allow).
    pend = tool_frame(
        3,
        name="write.external.demo.send",
        tier=2,
        decision="pending",
        approval_id="req-1",
        audit_seq=7,
    )
    assert pend.sse_payload() == {
        "type": "tool",
        "name": "write.external.demo.send",
        "tier": 2,
        "decision": "pending",
        "audit_seq": 7,
        "approval_id": "req-1",
    }
    approved = tool_frame(
        4,
        name="write.external.demo.send",
        tier=2,
        decision="allow",
        approval_id="req-1",
        audit_seq=8,
    )
    assert approved.sse_payload()["approval_id"] == "req-1"
    expired = tool_frame(
        5,
        name="write.external.demo.send",
        tier=2,
        decision="expired",
        approval_id="req-2",
        audit_seq=9,
    )
    assert expired.sse_payload() == {
        "type": "tool",
        "name": "write.external.demo.send",
        "tier": 2,
        "decision": "expired",
        "audit_seq": 9,
        "approval_id": "req-2",
    }
    failed = tool_frame(
        6,
        name="write.external.demo.send",
        tier=2,
        decision="failed",
        approval_id="req-3",
        audit_seq=10,
    )
    assert failed.sse_payload()["decision"] == "failed"
    assert failed.sse_payload()["approval_id"] == "req-3"
    allow = tool_frame(4, name="read.knowledge.search", tier=0, decision="allow", audit_seq=8)
    assert "approval_id" not in allow.sse_payload()
    assert allow.sse_payload() == {
        "type": "tool",
        "name": "read.knowledge.search",
        "tier": 0,
        "decision": "allow",
        "audit_seq": 8,
    }

    # The reason rides the enqueue wire too, so an enqueued turn's gate card reads
    # exactly like an inline one's. Omitted when absent (byte-identity above).
    denied = tool_frame(
        7,
        name="misc.unclassified.op",
        tier=None,
        decision="deny",
        audit_seq=11,
        reason="unclassified action",
    )
    assert denied.sse_payload() == {
        "type": "tool",
        "name": "misc.unclassified.op",
        "tier": None,
        "decision": "deny",
        "audit_seq": 11,
        "reason": "unclassified action",
    }
    # …and inline/enqueue serve the SAME bytes for the SAME transit: the parity the
    # module docstring claims, checked against the inline model rather than restated.
    assert (
        denied.sse_payload()
        == ToolTransit(
            name="misc.unclassified.op",
            tier=None,
            decision="deny",
            audit_seq=11,
            reason="unclassified action",
        ).sse_payload()
    )

    # A fake-reader tool frame relays to BYTE-IDENTICAL SSE bytes (enqueue readiness).
    async def _reader() -> AsyncIterator[TurnFrame]:
        yield tool_frame(0, name="read.knowledge.search", tier=0, decision="allow", audit_seq=8)
        yield done_frame(1, "s", "ans")

    async def _collect() -> list[bytes]:
        out: list[bytes] = []
        async for chunk_bytes in relay_event_stream(_reader(), keepalive_s=15.0, timeout_s=5.0):
            out.append(chunk_bytes)
        return out

    out = asyncio.run(_collect())
    assert out[0] == sse_frame(
        "tool",
        {
            "type": "tool",
            "name": "read.knowledge.search",
            "tier": 0,
            "decision": "allow",
            "audit_seq": 8,
        },
    )
