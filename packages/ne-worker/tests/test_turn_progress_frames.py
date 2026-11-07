# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TURN-PROGRESS — enqueue-mode turns stream their REAL per-hop tool frames.

The defect (SPEC §12 item 5): the pilot/demo stack runs ``NE_API_TURN_EXECUTION=
enqueue``, and the worker emitted exactly two frames at the very END of the turn —
25 s to 2 min of silence while the governed events were being written to the audit
chain and shown to nobody. These tests pin the fix at the worker seam:

* THE DECISIVE PROPERTY IS INCREMENTALITY. A final-state assertion ("the frames
  are all there afterwards") passes against an implementation that buffers
  everything to the end — which IS the bug. So the load-bearing tests assert the
  tool frame is on the frame stream WHILE the turn is still provably in flight
  (the gateway/gate is parked on an un-set asyncio.Event and the workflow task is
  not done).
* Nothing is fabricated: every frame is derived from a REAL audit event the
  governed executor/gate wrote first (the ``ToolTransitObservingSink``
  observation), and the pending frame's ``approval_id`` is the REAL gate row.
* The failure case stays visible: a turn that dies AFTER a hop still emits its
  terminal ``error`` frame (relay-side delivery of that colliding-seq terminal is
  pinned in ``packages/ne-api/tests/test_chat_enqueue.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from ne.api.tool_transit import ToolTransit, TurnTransitChannel
from ne.api.turn_wire import ProtectedTurnRef, TurnRequest
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.models import Completion, CompletionRequest
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.config import ApprovalSettings
from ne.runtime.factory import create_department_session
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.store import conversation_request_fingerprint
from ne.runtime.tools import ToolBinding
from ne.worker.relay import RecordingFrameWriter
from ne.worker.runtime_state import set_frame_writer_factory, set_worker_platform
from ne.worker.workflows import _TransitFrameDrain, run_turn_workflow

from .conftest import ScriptedGateway, build_sqlite_platform, run, text_completion

pytestmark = pytest.mark.usefixtures("protected_transport_env")

_DEPT = "engineering"
_AGENT = "console-chat"
_TIER1_TOOL = "write.internal.demo.note"
_TIER2_TOOL = "write.external.demo.send"


def _tool_call(name: str, arguments: dict[str, object]) -> Completion:
    import json

    from ne.contracts.models import Usage

    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


async def _noop_handler(_args: Mapping[str, object]) -> str:
    return "ok"


def _tier1_binding() -> ToolBinding:
    return ToolBinding(
        spec=ToolSpec(
            name=_TIER1_TOOL,
            description="a tier-1 internal write (rego-faithful: allow)",
            tier=ApprovalTier.TIER_1,
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        ),
        handler=_noop_handler,
    )


def _tier2_binding(*, fail: bool = False) -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        if fail:
            raise RuntimeError("controlled handler failure")
        return await _noop_handler(args)

    return ToolBinding(
        spec=ToolSpec(
            name=_TIER2_TOOL,
            description="a tier-2 external write (rego-faithful: require-approval)",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        ),
        handler=_handler,
    )


class GatedGateway(ScriptedGateway):
    """Scripted completions, with ONE call parked on an event.

    ``hold_call`` is 1-based: the Nth ``complete`` awaits ``release`` before
    answering. This is how a test holds a turn provably IN FLIGHT while it
    inspects what the frame stream already carries.
    """

    def __init__(self, completions: list[Completion], *, hold_call: int) -> None:
        super().__init__(completions)
        self._hold_call = hold_call
        self._calls = 0
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest) -> Completion:
        self._calls += 1
        if self._calls == self._hold_call:
            await self.release.wait()
        return await super().complete(request)


def _install(
    tmp_path: Path,
    gateway: object,
    *,
    extra_tool_bindings: tuple[object, ...] = (),
    extra_tool_policies: Mapping[str, object] | None = None,
    approvals: ApprovalSettings | None = None,
) -> tuple[object, RecordingFrameWriter]:
    platform = build_sqlite_platform(
        tmp_path,
        gateway=gateway,
        extra_tool_bindings=extra_tool_bindings,  # type: ignore[arg-type]
        extra_tool_policies=extra_tool_policies,  # type: ignore[arg-type]
        approvals=approvals,
    )
    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    return platform, writer


async def _session_id(platform: object) -> str:
    session = await create_department_session(
        _AGENT,
        _DEPT,
        owner_principal_id="user:ada",
        store=platform.session_store,  # type: ignore[attr-defined]
        gateway=platform.gateway,  # type: ignore[attr-defined]
        executor=platform.executor,  # type: ignore[attr-defined]
        settings=platform.runtime_settings,  # type: ignore[attr-defined]
    )
    return session.session_id


async def _request(platform: object, session_id: str) -> str:
    principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
    turn_id = "turn-progress"
    trace_id = hashlib.sha256(f"test-turn:{turn_id}".encode()).hexdigest()[:32]
    fingerprint = conversation_request_fingerprint(
        session_id,
        principal.id,
        _DEPT,
        _AGENT,
        "do the thing",
        None,
        None,
        trace_id,
        principal.groups,
        dict(principal.claims),
    )
    claim = await platform.session_store.claim_conversation_attachment(  # type: ignore[attr-defined]
        session_id, principal.id, _DEPT, _AGENT, turn_id, fingerprint, trace_id
    )
    assert claim.disposition == "owned" and claim.attempt_id is not None
    await platform.session_store.bind_conversation_execution(  # type: ignore[attr-defined]
        session_id, principal.id, turn_id, claim.attempt_id, fingerprint
    )
    ref = await platform.agent_run_store.claim_protected_payload(  # type: ignore[attr-defined]
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=principal.id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=conversation_turn_content_binding(
            conversation_id=session_id,
            attachment_id=turn_id,
            attempt_id=claim.attempt_id,
            slot="input",
        ),
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=b"do the thing",
    )
    return TurnRequest.from_principal(
        turn_id=turn_id,
        session_id=session_id,
        agent_name=_AGENT,
        department=_DEPT,
        protected_input=ProtectedTurnRef(
            protected_ref=ref.row_id,
            protected_hash=ref.plaintext_hash,
            content_status="source_free",
        ),
        authenticated_context=AuthenticatedRequestContext(
            principal_id=principal.id,
            authorization_groups=("eng",),
            department=_DEPT,
            issuer="https://issuer.test",
            expires_at=4_102_444_800,
        ),
        principal=principal,
        trace_id=trace_id,
        request_fingerprint=fingerprint,
        idempotency_fingerprint=fingerprint,
        claimed_attempt_id=claim.attempt_id,
    ).model_dump_json()


async def _until(predicate, *, timeout: float = 10.0, what: str = "condition") -> None:  # type: ignore[no-untyped-def]
    """Poll ``predicate`` on the running loop until true or fail loudly."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def test_tool_frame_is_on_the_stream_while_the_turn_is_still_running(tmp_path: Path) -> None:
    """THE DECISIVE TEST — incrementality itself is the assertion.

    Call 1 emits the tier-1 tool call; the executor runs it (pdp.decide[allow] +
    tool.execute[success] land on the REAL chain, the observer publishes the
    transit, the drain emits the ``tool`` frame). Call 2 — the model composing
    the final answer — is PARKED. While it is parked, the frame stream must
    already carry the hop; an implementation that buffers frames to the end of
    the turn (the shipped bug) fails HERE, not at the final-state check.
    """
    gw = GatedGateway(
        [_tool_call(_TIER1_TOOL, {"title": "x"}), text_completion("the answer")],
        hold_call=2,
    )
    platform, writer = _install(
        tmp_path,
        gw,
        extra_tool_bindings=(_tier1_binding(),),
        extra_tool_policies={
            _TIER1_TOOL: ToolArgPolicy(
                args={"title": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )

    async def _drive() -> None:
        sid = await _session_id(platform)
        task = asyncio.create_task(run_turn_workflow(await _request(platform, sid)))

        # The hop is visible WHILE the turn is provably in flight.
        await _until(
            lambda: any(f.type == "tool" for f in writer.frames),
            what="a tool frame during the turn",
        )
        assert not task.done(), "the turn must still be running when its hop renders"
        hop = next(f for f in writer.frames if f.type == "tool")
        assert hop.name == _TIER1_TOOL
        assert hop.decision == "allow"
        assert hop.tier == 1  # the REAL rego-faithful tier, not a guess
        assert hop.audit_seq is not None  # the REAL chain seq was resolvable
        # No terminal frame yet — progress precedes the answer, never simulates it.
        assert not any(f.type in ("done", "error") for f in writer.frames)

        gw.release.set()
        output = await task
        assert ProtectedTurnRef.model_validate_json(output).content_status == "source_free"

    run(_drive())

    # Final shape: hop, then the exactly-once terminal pair, seqs monotonic with
    # the token numbered AFTER the step's tool frames (frames_emitted).
    assert [f.type for f in writer.frames] == ["tool", "done"]
    assert [f.seq for f in writer.frames] == [0, 1]
    assert writer.frames[1].protected_terminal is not None
    assert writer.closed


def test_the_drain_carries_the_transit_reason_onto_the_frame_stream() -> None:
    """Enqueue parity for the PDP's own sentence — the demo runs in enqueue mode.

    The inline chat route reads ``ToolTransit.reason`` straight off ``sse_payload``;
    the worker has to carry it across the ``tool_frame`` seam or an enqueued turn's
    gate card silently reverts to "the server sent no reason" while the inline one
    explains itself. BOTH directions: the deny that carries a sentence keeps it, the
    allow that carries none stays ``None`` (it is not defaulted to the last one seen
    or to an empty string).
    """
    channel = TurnTransitChannel()
    writer = RecordingFrameWriter("turn-1")
    served = "tier 2: external/PII action \u2014 per-item human approval required"
    channel.publish(
        ToolTransit(name=_TIER2_TOOL, tier=2, decision="deny", audit_seq=7, reason=served)
    )
    channel.publish(ToolTransit(name=_TIER1_TOOL, tier=1, decision="allow", audit_seq=8))

    async def _drive() -> None:
        drain = _TransitFrameDrain(channel, writer)
        drain.start()
        await drain.flush()

    run(_drive())

    assert [f.reason for f in writer.frames] == [served, None]
    assert writer.frames[0].sse_payload()["reason"] == served
    assert "reason" not in writer.frames[1].sse_payload()


def test_pending_approval_frame_streams_while_the_turn_is_parked(tmp_path: Path) -> None:
    """A tier-2 park is visible AS IT HAPPENS, with the REAL gate request id.

    The turn stops inside the ``DurableApprovalGate``; while parked, the stream
    already carries ``{decision: pending, approval_id}`` — the reader can walk to
    Approvals and act. Approving releases the turn; the subsequent allow frame
    and the terminal pair follow on the same stream.
    """
    gw = ScriptedGateway([_tool_call(_TIER2_TOOL, {"to": "ops"}), text_completion("sent")])
    platform, writer = _install(
        tmp_path,
        gw,
        extra_tool_bindings=(_tier2_binding(),),
        extra_tool_policies={
            _TIER2_TOOL: ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.text, required=True)})
        },
        approvals=ApprovalSettings(poll_interval_s=0.05),
    )

    async def _drive() -> None:
        sid = await _session_id(platform)
        task = asyncio.create_task(run_turn_workflow(await _request(platform, sid)))

        await _until(
            lambda: any(f.type == "tool" and f.decision == "pending" for f in writer.frames),
            what="the pending frame while the gate holds the turn",
        )
        assert not task.done(), "the turn must still be parked when its pending frame renders"
        pending = next(f for f in writer.frames if f.decision == "pending")
        assert pending.name == _TIER2_TOOL
        assert pending.tier == 2
        assert pending.approval_id, "the pending frame must carry the REAL gate request id"

        # The approval_id is the real, actionable gate row — approve through it.
        gate = platform.gate  # type: ignore[attr-defined]
        rows = await gate.list_pending()
        assert [r.request_id for r in rows] == [pending.approval_id]
        await gate.approve(pending.approval_id, approver="user:boss")

        output = await task
        assert ProtectedTurnRef.model_validate_json(output).content_status == "source_free"

    run(_drive())

    types = [f.type for f in writer.frames]
    assert types == ["tool", "tool", "done"]
    decisions = [f.decision for f in writer.frames if f.type == "tool"]
    assert decisions == ["pending", "allow"]  # park first, then the post-approval execute
    transits = [f for f in writer.frames if f.type == "tool"]
    assert transits[1].approval_id == transits[0].approval_id
    assert [f.seq for f in writer.frames] == [0, 1, 2]
    assert writer.closed


def test_approved_execution_failure_replaces_pending_on_worker_stream(tmp_path: Path) -> None:
    """Enqueue mode carries the same approval id onto the failed execution event."""
    gw = ScriptedGateway([_tool_call(_TIER2_TOOL, {"to": "ops"})])
    platform, writer = _install(
        tmp_path,
        gw,
        extra_tool_bindings=(_tier2_binding(fail=True),),
        extra_tool_policies={
            _TIER2_TOOL: ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.text, required=True)})
        },
        approvals=ApprovalSettings(poll_interval_s=0.05),
    )

    async def _drive() -> None:
        sid = await _session_id(platform)
        task = asyncio.create_task(run_turn_workflow(await _request(platform, sid)))
        await _until(
            lambda: any(f.type == "tool" and f.decision == "pending" for f in writer.frames),
            what="the pending frame while the gate holds the turn",
        )
        pending = next(f for f in writer.frames if f.decision == "pending")
        assert pending.approval_id
        gate = platform.gate  # type: ignore[attr-defined]
        await gate.approve(pending.approval_id, approver="user:boss")

        await task

    run(_drive())

    transits = [f for f in writer.frames if f.type == "tool"]
    assert [f.decision for f in transits] == ["pending", "failed"]
    assert transits[1].approval_id == transits[0].approval_id
    assert [f.type for f in writer.frames] == ["tool", "tool", "done"]
    assert writer.closed


class StallingFrameWriter(RecordingFrameWriter):
    """A writer whose ``tool`` emits PARK until released — a loaded DBOS system
    DB whose step-context stream write is still in flight when ``session.run``
    returns. ``tool_emit_parked`` signals the write is in flight; the test
    releases it only AFTER the step has run past the turn, so the seq race is
    forced deterministically instead of hoped for with a sleep.
    """

    def __init__(self, turn_id: str) -> None:
        super().__init__(turn_id)
        self.tool_emit_parked = asyncio.Event()
        self.release_tool_emits = asyncio.Event()

    async def emit(self, frame) -> None:  # type: ignore[no-untyped-def]
        if frame.type == "tool" and not self.release_tool_emits.is_set():
            self.tool_emit_parked.set()
            await self.release_tool_emits.wait()
        await super().emit(frame)


def test_terminal_token_is_numbered_after_a_tool_frame_write_still_in_flight(
    tmp_path: Path,
) -> None:
    """THE SEQ-RACE TEST (P1 regression): the terminal ``token`` frame must be
    numbered strictly AFTER every tool frame EVEN WHEN a tool-frame write is
    still in flight at the moment the turn settles.

    The broken shape: ``execute_turn_step`` snapshots ``frames_emitted=
    drain.seqs_used`` before the drain is flushed, so a parked write consumes
    its seq AFTER the snapshot — frames land as ``[tool:0, token:0, done:1]``
    and BOTH relay dedup sites drop the ``token`` as a non-terminal duplicate.
    Realistic triggers: a slow DBOS system-DB write, or a routed verified turn
    that returns straight after its last governed read.

    WHAT THE COLLISION COSTS, measured rather than assumed. Replaying that exact
    broken shape through the real ``relay_event_stream`` delivers ``tool`` and
    ``done`` and drops ``token`` — and the ``done`` it delivers still carries
    ``output`` in full. In v1 turn-then-stream that ``done`` is written in the
    same instant as the ``token`` and the console renders it (``text:
    event.output || m.text``), so the reader is NOT left with an empty turn: the
    cost today is one lost progress frame.

    The seq law is enforced anyway, and this test keeps it, because the cost is
    bounded only by a property of v1 that this seam does not own — a mode whose
    terminal does not carry the whole output (real incremental token deltas)
    turns the same collision into lost answer text. So the test pins BOTH the
    ordering and the v1 property the current blast radius depends on.

    WHAT THIS TEST DOES NOT PIN, stated so no one reads it as covering more than
    it does: the relay's OTHER terminal rule — that a ``done``/``error`` is
    delivered even when its seq is BEHIND the last delivered one. The broken
    shape here cannot exercise it, because ``done`` lands at seq 1, ahead of the
    delivered ``tool`` at seq 0, so the ordinary monotonic dedup admits it
    without the bypass; deleting the bypass from ``relay_event_stream`` leaves
    every test in this file green. That rule is pinned where a terminal really
    does arrive behind its tool frames —
    ``test_chat_enqueue.py::test_terminal_error_is_delivered_despite_a_colliding_seq``,
    the raised-turn ``error`` at seq 0 — and deleting the bypass turns THAT red.
    """
    gw = ScriptedGateway([_tool_call(_TIER1_TOOL, {"title": "x"}), text_completion("the answer")])
    platform = build_sqlite_platform(
        tmp_path,
        gateway=gw,
        extra_tool_bindings=(_tier1_binding(),),
        extra_tool_policies={
            _TIER1_TOOL: ToolArgPolicy(
                args={"title": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )
    set_worker_platform(platform)
    writer = StallingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)

    async def _drive() -> None:
        sid = await _session_id(platform)
        task = asyncio.create_task(run_turn_workflow(await _request(platform, sid)))

        # The hop's frame write is provably IN FLIGHT...
        await _until(
            lambda: writer.tool_emit_parked.is_set(),
            what="the drain to park inside its tool-frame write",
        )
        # ...and the model has already composed the final answer, so the step is
        # past ``session.run`` (ScriptedGateway.complete has no awaits — once
        # the second request is recorded, the completion has returned).
        await _until(lambda: len(gw.requests) >= 2, what="the final model completion")
        # Give the step room to reach its seq snapshot / flush wait; the parked
        # write is the ONLY thing that can still be pending.
        await asyncio.sleep(0.2)
        assert not task.done(), "the turn must not complete while its hop write is in flight"

        writer.release_tool_emits.set()
        output = await asyncio.wait_for(task, timeout=10.0)
        assert ProtectedTurnRef.model_validate_json(output).content_status == "source_free"

    run(_drive())

    # The seq law itself: the terminal pair is numbered strictly AFTER every
    # tool frame the step wrote — an in-flight write may NOT be outrun.
    tool_seqs = [f.seq for f in writer.frames if f.type == "tool"]
    done = next(f for f in writer.frames if f.type == "done")
    assert tool_seqs, "the hop that happened must be on the stream"
    assert done.seq > max(tool_seqs)
    assert done.output is None and done.protected_terminal is not None
    assert [f.type for f in writer.frames] == ["tool", "done"]
    assert [f.seq for f in writer.frames] == [0, 1]


def test_turn_that_dies_after_a_hop_still_emits_its_terminal_error(tmp_path: Path) -> None:
    """Requirement 4: the failure case must be visible. A turn that raised AFTER
    a hop must not leave the stream hanging at "hop 1 of ?" — the terminal
    ``error`` frame (safe detail, never a traceback) follows the hop and the
    stream closes. (The relay delivers that seq-0 terminal regardless of the
    step's tool-frame seqs — pinned in ne-api's test_chat_enqueue.)
    """

    class _ExplodingGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__([_tool_call(_TIER1_TOOL, {"title": "x"})])
            self._calls = 0

        async def complete(self, request: CompletionRequest) -> Completion:
            self._calls += 1
            if self._calls >= 2:
                raise RuntimeError("model backend fell over mid-turn")
            return await super().complete(request)

    platform, writer = _install(
        tmp_path,
        _ExplodingGateway(),
        extra_tool_bindings=(_tier1_binding(),),
        extra_tool_policies={
            _TIER1_TOOL: ToolArgPolicy(
                args={"title": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )

    async def _drive() -> None:
        sid = await _session_id(platform)
        # The workflow re-raises the turn's failure so DBOS records it; the
        # gateway's own class (not a blind Exception) pins that the raise is OURS.
        with pytest.raises(RuntimeError, match="model backend fell over mid-turn"):
            await run_turn_workflow(await _request(platform, sid))

    run(_drive())

    types = [f.type for f in writer.frames]
    assert "tool" in types, "the hop that DID happen stays on the record"
    assert types[-1] == "error", "the death is a terminal frame, not silence"
    detail = writer.frames[-1].detail or ""
    assert "Traceback" not in detail  # safe-detail discipline holds on this path
    assert writer.closed
