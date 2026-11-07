# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""⭐ NE-088 governance-parity suite — Flowise tool call == first-party tool call.

The deliverable's proof (the NE-092 parity precedent): a Flowise-initiated tool
call and an equivalent first-party chat-turn tool call reach the SAME governed
executor, so they produce the SAME ``pdp.decide`` decision doc, the SAME governed
audit projection (action/resource/outcome/actor/detail/provenance — seq /
timestamps / event-ids excluded), and the SAME stricter-of tier HITL gating. A
PDP-denied tool is denied for Flowise (no bypass); a tier-2 tool triggers HITL for
Flowise; every Flowise call is audited (C6 no-ungoverned-execution invariant).

Two governed channels are compared:

* first-party = a REAL chat turn (session create → message POST with a scripted
  ``tool_call`` completion), exactly like ``test_hitl_e2e``;
* Flowise      = ``POST /api/v1/flowise/tools/execute`` for the SAME principal.

Each channel runs in its OWN ``make_app`` harness (its own audit DB), so the two
projections can be compared cleanly. The composed ``config_hash`` is frozen once
per harness and the DB URLs are hash inputs, so we ``reset_config_hash_for_testing``
between the two builds — each channel is run to completion (emitting under its own
frozen hash) BEFORE the next harness is built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import sqlalchemy as sa

from ne.audit import list_records
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.runtime.config import ApprovalSettings
from ne.runtime.errors import ActionValidationError
from ne.runtime.tools import ToolBinding

from .conftest import AppBuilder, Harness, run, text_completion, tool_call_completion
from .conftest import _rego_faithful_doc as rego_faithful_doc

_EXECUTE = "/api/v1/flowise/tools/execute"
_CATALOG = "/api/v1/flowise/tools"
_GOVERNED_ACTIONS = {"pdp.decide", "tool.approval", "tool.execute", "tool.unknown"}


# --- a deny-the-.blocked. PDP factory (rego-faithful otherwise) -------------


def _deny_blocked_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        if ".blocked." in action:
            doc: dict[str, Any] = {
                "effect": "deny",
                "tier": None,
                "reason": "customer deny rule",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        else:
            doc = rego_faithful_doc(action)
        return httpx.Response(200, json={"result": doc})

    return httpx.MockTransport(handler)


class _DenyBlockedPdpFactory:
    """A ``policy_decision_point`` plugin: the REAL GovernedPdp whose transport DENYs
    any action containing ``.blocked.`` (rego-faithful for everything else)."""

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        client = httpx.AsyncClient(base_url="http://opa.test", transport=_deny_blocked_transport())
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- tool bindings / policies ----------------------------------------------


def _binding(action: str, tier: ApprovalTier, *, arg: str) -> ToolBinding:
    async def _handler(_args: Mapping[str, object]) -> str:
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name=action,
            description="demo",
            tier=tier,
            input_schema={
                "type": "object",
                "properties": {arg: {"type": "string"}},
                "required": [arg],
            },
        ),
        handler=_handler,
    )


def _policy(arg: str) -> ToolArgPolicy:
    return ToolArgPolicy(args={arg: ArgSpec(kind=ToolArgKind.text, required=True)})


def _validating_binding(action: str, tier: ApprovalTier, *, calls: list[Any]) -> ToolBinding:
    """A binding whose ``validate_args`` unconditionally refuses (NE-VALIDATE-SURFACES
    P2 repro): the handler records into ``calls`` so the test can prove it never ran
    (the pre-gate refusal must consume no execution and no human decision)."""

    async def _handler(args: Mapping[str, object]) -> str:
        calls.append(dict(args))
        return "ok"

    def _validate_args(_args: Mapping[str, object]) -> None:
        raise ActionValidationError(f"invalid input for {action!r}: fields ['record_id']")

    return ToolBinding(
        spec=ToolSpec(
            name=action,
            description="demo write action missing a required identity field",
            tier=tier,
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        ),
        handler=_handler,
        validate_args=_validate_args,
    )


# --- governed-audit projection (seq/timestamps/ids excluded) ----------------


def _project(record: Any) -> tuple[Any, ...]:
    payload = record.payload
    detail_raw = payload.get("detail")
    detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
    provenance = payload.get("provenance")
    if isinstance(provenance, dict) and "approval_id" in provenance:
        # The B14 join id is a per-run durable request uuid — an "id" in this
        # projection's excluded class. Parity still asserts the KEY is present
        # on both channels; the per-channel join COHERENCE (tool.approval and
        # tool.execute carry the same id) is asserted separately by
        # _assert_join_coherent in the HITL test.
        provenance = {**provenance, "approval_id": "<per-run-approval-id>"}
    return (
        record.action,
        record.resource,
        record.outcome,
        record.actor,
        detail,
        provenance,
    )


def _assert_join_coherent(audit_url: str, tool: str) -> None:
    """Within ONE channel's run: tool.approval and tool.execute must carry the
    SAME approval_id (the B14 join), present and non-empty on both."""
    engine = sa.create_engine(audit_url)
    try:
        rows = list_records(engine, limit=1000)
    finally:
        engine.dispose()
    ids: dict[str, str] = {}
    for r in rows:
        if r.action in ("tool.approval", "tool.execute") and r.resource == f"tool:{tool}":
            provenance = r.payload.get("provenance") or {}
            approval_id = provenance.get("approval_id")
            if isinstance(approval_id, str):
                ids[r.action] = approval_id
    assert ids.get("tool.approval"), "tool.approval carries no approval_id"
    assert ids.get("tool.execute"), "tool.execute carries no approval_id"
    assert ids["tool.approval"] == ids["tool.execute"]


def _governed_events(audit_url: str, tool: str) -> list[tuple[Any, ...]]:
    engine = sa.create_engine(audit_url)
    try:
        rows = list_records(engine, limit=1000)
    finally:
        engine.dispose()
    projected = [
        _project(r) for r in rows if r.action in _GOVERNED_ACTIONS and r.resource == f"tool:{tool}"
    ]
    return sorted(projected, key=lambda t: json.dumps(t, sort_keys=True, default=str))


def _all_governed(audit_url: str) -> list[Any]:
    engine = sa.create_engine(audit_url)
    try:
        rows = list_records(engine, limit=1000)
    finally:
        engine.dispose()
    return [r for r in rows if r.action in _GOVERNED_ACTIONS]


def _bearer_ada(h: Harness) -> dict[str, str]:
    return h.bearer("user:ada", groups=["eng"], department="engineering")


# --- channel drivers --------------------------------------------------------


async def _flowise_execute(h: Harness, tool: str, args: Mapping[str, Any]) -> httpx.Response:
    async with h.client() as client:
        return await client.post(
            _EXECUTE, json={"tool": tool, "args": dict(args)}, headers=_bearer_ada(h)
        )


async def _first_party_turn(h: Harness, prompt: str = "go") -> None:
    """Drive a non-blocking (tier-0/1 or denied) chat turn to completion."""
    ada = _bearer_ada(h)
    async with h.client() as client:
        created = await client.post(
            "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
        )
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        resp = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": prompt}, headers=ada
        )
        assert resp.status_code == 200, resp.text
        # Drain the SSE body so the turn fully resolves and audits.
        _ = resp.text


# --- tests ------------------------------------------------------------------


def test_allowed_tool_decision_and_audit_parity(make_app: AppBuilder) -> None:
    tool = "read.demo.lookup"
    binding = _binding(tool, ApprovalTier.TIER_0, arg="q")
    policies = {tool: _policy("q")}

    # Flowise channel.
    h_fl = make_app(extra_tool_bindings=(binding,), extra_tool_policies=policies)
    resp = run(_flowise_execute(h_fl, tool, {"q": "hi"}))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tool": tool, "result": "ok"}

    # First-party channel (fresh frozen hash + its own harness).
    reset_config_hash_for_testing()
    h_fp = make_app(
        extra_tool_bindings=(binding,),
        extra_tool_policies=policies,
        completions=[tool_call_completion(tool, {"q": "hi"}), text_completion("done")],
    )
    run(_first_party_turn(h_fp))

    fl_events = _governed_events(h_fl.audit_url, tool)
    fp_events = _governed_events(h_fp.audit_url, tool)
    assert fl_events == fp_events, f"parity broke:\nflowise={fl_events}\nfirst-party={fp_events}"

    # The pdp.decide doc carries effect allow / tier 0 / policy_version 1.0.0.
    decides = [e for e in fl_events if e[0] == "pdp.decide"]
    assert len(decides) == 1
    detail = decides[0][4]
    assert detail["effect"] == "allow"
    assert detail["tier"] == 0
    assert detail["policy_version"] == "1.0.0"
    # And the tool actually executed on both channels.
    assert any(e[0] == "tool.execute" and e[2] == "success" for e in fl_events)


def test_pdp_denied_tool_is_denied_for_flowise_too(make_app: AppBuilder) -> None:
    tool = "read.blocked.export"  # classifies tier 0 (read.) so registration passes; PDP denies.
    binding = _binding(tool, ApprovalTier.TIER_0, arg="q")
    policies = {tool: _policy("q")}

    # Flowise channel → 403 (deny means deny).
    h_fl = make_app(
        pdp_factory=_DenyBlockedPdpFactory,
        extra_tool_bindings=(binding,),
        extra_tool_policies=policies,
    )
    resp = run(_flowise_execute(h_fl, tool, {"q": "x"}))
    assert resp.status_code == 403, resp.text

    # First-party channel: the runner feeds ToolDeniedError back as a BLOCKED result,
    # so the turn completes — but the tool is equally denied (no execution).
    reset_config_hash_for_testing()
    h_fp = make_app(
        pdp_factory=_DenyBlockedPdpFactory,
        extra_tool_bindings=(binding,),
        extra_tool_policies=policies,
        completions=[tool_call_completion(tool, {"q": "x"}), text_completion("blocked")],
    )
    run(_first_party_turn(h_fp))

    fl_events = _governed_events(h_fl.audit_url, tool)
    fp_events = _governed_events(h_fp.audit_url, tool)
    assert fl_events == fp_events, (
        f"deny parity broke:\nflowise={fl_events}\nfirst-party={fp_events}"
    )

    # IDENTICAL pdp.decide deny docs, and NO tool.execute in EITHER channel.
    for events in (fl_events, fp_events):
        decides = [e for e in events if e[0] == "pdp.decide"]
        assert decides and all(e[2] == "denied" for e in decides)
        assert all(e[4]["effect"] == "deny" for e in decides)
        assert not any(e[0] == "tool.execute" for e in events)


def test_tier2_tool_triggers_hitl_for_flowise(make_app: AppBuilder) -> None:
    action = "write.external.demo.send"  # TIER_2 → require-approval → HITL gate.
    binding = _binding(action, ApprovalTier.TIER_2, arg="to")
    policies = {action: _policy("to")}
    kwargs: dict[str, Any] = {
        "approver_roles": ("approvers",),
        "approvals": ApprovalSettings(poll_interval_s=0.05),
        "extra_tool_bindings": (binding,),
        "extra_tool_policies": policies,
    }

    # --- Flowise channel: the POST BLOCKS in the real gate until a named human approves.
    h_fl = make_app(**kwargs)

    async def _flowise_hitl() -> None:
        loop = asyncio.get_running_loop()
        ada = _bearer_ada(h_fl)
        appr = h_fl.bearer("user:approver", groups=["approvers"])
        async with h_fl.client() as caller, h_fl.client() as approver:
            task = asyncio.create_task(
                caller.post(_EXECUTE, json={"tool": action, "args": {"to": "ops"}}, headers=ada)
            )
            deadline = loop.time() + 10.0
            pending = None
            while loop.time() < deadline:
                r = await approver.get("/api/v1/approvals/pending", headers=appr)
                assert r.status_code == 200
                match = [x for x in r.json()["approvals"] if x["action"] == action]
                if match:
                    pending = match[0]
                    break
                await asyncio.sleep(0.05)
            assert pending is not None, "the tier-2 approval never appeared for Flowise"
            assert pending["principal_id"] == "user:ada"  # the verified Flowise caller
            assert pending["tier"] == 2
            ok = await approver.post(
                f"/api/v1/approvals/{pending['request_id']}/approve", json={}, headers=appr
            )
            assert ok.status_code == 200
            resp = await task
            assert resp.status_code == 200, resp.text
            assert resp.json()["result"] == "ok"

    run(_flowise_hitl())

    # --- First-party channel: the SAME tier-2 tool via a chat turn (mirror test_hitl_e2e).
    reset_config_hash_for_testing()
    h_fp = make_app(
        completions=[tool_call_completion(action, {"to": "ops"}), text_completion("sent")],
        **kwargs,
    )

    async def _first_party_hitl() -> None:
        loop = asyncio.get_running_loop()
        ada = _bearer_ada(h_fp)
        appr = h_fp.bearer("user:approver", groups=["approvers"])
        async with h_fp.client() as writer, h_fp.client() as approver:
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]
            task = asyncio.create_task(
                writer.post(
                    f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "send"}, headers=ada
                )
            )
            deadline = loop.time() + 10.0
            pending = None
            while loop.time() < deadline:
                r = await approver.get("/api/v1/approvals/pending", headers=appr)
                match = [x for x in r.json()["approvals"] if x["action"] == action]
                if match:
                    pending = match[0]
                    break
                await asyncio.sleep(0.05)
            assert pending is not None
            ok = await approver.post(
                f"/api/v1/approvals/{pending['request_id']}/approve", json={}, headers=appr
            )
            assert ok.status_code == 200
            sse = await task
            assert sse.status_code == 200

    run(_first_party_hitl())

    fl_events = _governed_events(h_fl.audit_url, action)
    fp_events = _governed_events(h_fp.audit_url, action)
    assert fl_events == fp_events, (
        f"HITL parity broke:\nflowise={fl_events}\nfirst-party={fp_events}"
    )

    # A tool.approval SUCCESS event carrying the named approver in provenance.
    approvals = [e for e in fl_events if e[0] == "tool.approval" and e[2] == "success"]
    assert approvals, "no successful tool.approval event for the Flowise tier-2 call"
    assert approvals[0][5]["approver"] == "user:approver"
    # And the tool executed only AFTER approval.
    assert any(e[0] == "tool.execute" and e[2] == "success" for e in fl_events)
    # B14 join coherence, per channel: the executed write names the SAME
    # approval the tool.approval record does (the projection normalizes the
    # per-run uuid, so this is asserted on the raw records).
    _assert_join_coherent(h_fl.audit_url, action)
    _assert_join_coherent(h_fp.audit_url, action)


def test_flowise_approval_rejected_is_denied(make_app: AppBuilder) -> None:
    action = "write.external.demo.send"
    binding = _binding(action, ApprovalTier.TIER_2, arg="to")
    h = make_app(
        approver_roles=("approvers",),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        extra_tool_bindings=(binding,),
        extra_tool_policies={action: _policy("to")},
    )

    async def _t() -> None:
        loop = asyncio.get_running_loop()
        ada = _bearer_ada(h)
        appr = h.bearer("user:approver", groups=["approvers"])
        async with h.client() as caller, h.client() as approver:
            task = asyncio.create_task(
                caller.post(_EXECUTE, json={"tool": action, "args": {"to": "ops"}}, headers=ada)
            )
            deadline = loop.time() + 10.0
            pending = None
            while loop.time() < deadline:
                r = await approver.get("/api/v1/approvals/pending", headers=appr)
                match = [x for x in r.json()["approvals"] if x["action"] == action]
                if match:
                    pending = match[0]
                    break
                await asyncio.sleep(0.05)
            assert pending is not None
            rej = await approver.post(
                f"/api/v1/approvals/{pending['request_id']}/reject",
                json={"reason": "no"},
                headers=appr,
            )
            assert rej.status_code == 200
            resp = await task
            assert resp.status_code == 403, resp.text

    run(_t())

    events = _governed_events(h.audit_url, action)
    assert any(e[0] == "tool.approval" and e[2] == "denied" for e in events)
    assert not any(e[0] == "tool.execute" for e in events)


def test_unknown_tool_is_404_and_audited(make_app: AppBuilder) -> None:
    tool = "read.nope.missing"
    h = make_app()
    resp = run(_flowise_execute(h, tool, {}))
    assert resp.status_code == 404, resp.text

    events = _governed_events(h.audit_url, tool)
    unknowns = [e for e in events if e[0] == "tool.unknown"]
    assert unknowns, "no tool.unknown audit event for the unregistered tool"
    assert all(e[2] == "denied" for e in unknowns)
    # The unknown tool NEVER executed.
    assert not any(e[0] == "tool.execute" for e in events)


def test_every_flowise_call_is_audited(make_app: AppBuilder) -> None:
    allowed = "read.demo.lookup"
    denied = "read.blocked.export"
    unknown = "read.nope.missing"
    h = make_app(
        pdp_factory=_DenyBlockedPdpFactory,
        extra_tool_bindings=(
            _binding(allowed, ApprovalTier.TIER_0, arg="q"),
            _binding(denied, ApprovalTier.TIER_0, arg="q"),
        ),
        extra_tool_policies={allowed: _policy("q"), denied: _policy("q")},
    )

    async def _t() -> None:
        r_ok = await _flowise_execute(h, allowed, {"q": "x"})
        assert r_ok.status_code == 200
        r_deny = await _flowise_execute(h, denied, {"q": "x"})
        assert r_deny.status_code == 403
        r_unknown = await _flowise_execute(h, unknown, {})
        assert r_unknown.status_code == 404

    run(_t())

    # EVERY call left at least one governed audit event (C6 no-ungoverned-execution).
    for tool in (allowed, denied, unknown):
        events = _governed_events(h.audit_url, tool)
        assert any(e[0] in {"pdp.decide", "tool.unknown"} for e in events), (
            f"no governed audit event for {tool}"
        )


def test_flowise_requires_bearer(make_app: AppBuilder) -> None:
    h = make_app(
        extra_tool_bindings=(_binding("read.demo.lookup", ApprovalTier.TIER_0, arg="q"),),
        extra_tool_policies={"read.demo.lookup": _policy("q")},
    )

    async def _t() -> httpx.Response:
        async with h.client() as client:
            return await client.post(
                _EXECUTE, json={"tool": "read.demo.lookup", "args": {"q": "x"}}
            )

    resp = run(_t())
    assert resp.status_code == 401, resp.text
    # No bearer ⇒ the OIDC wrap rejects before the router: zero governed events.
    assert _all_governed(h.audit_url) == []


def test_flowise_catalog_lists_specs(make_app: AppBuilder) -> None:
    tool = "read.demo.lookup"
    h = make_app(
        extra_tool_bindings=(_binding(tool, ApprovalTier.TIER_0, arg="q"),),
        extra_tool_policies={tool: _policy("q")},
    )

    async def _t() -> httpx.Response:
        async with h.client() as client:
            return await client.get(_CATALOG, headers=_bearer_ada(h))

    resp = run(_t())
    assert resp.status_code == 200, resp.text
    tools = resp.json()["tools"]
    by_name = {t["name"]: t for t in tools}
    assert tool in by_name, f"registered demo spec missing from catalog: {list(by_name)}"
    spec = by_name[tool]
    assert spec["tier"] == 0
    assert spec["input_schema"]["required"] == ["q"]
    # Specs only — no handler/callable field anywhere in the response.
    for t in tools:
        assert set(t) == {"name", "description", "tier", "input_schema"}


def test_pre_gate_validation_refusal_is_422_and_surface_audited(make_app: AppBuilder) -> None:
    """NE-VALIDATE-SURFACES P2: a pre-gate ``ActionValidationError`` (NE-ACTION-VALIDATE-
    BEFORE-GATE) must surface as 422 + an audited ``api.flowise.execute[denied]`` record,
    never an unaudited 500. write.internal.* rego-faithfully classifies tier 1 / effect
    allow (conftest._classify), so step 2b is reached with NO HITL detour."""
    tool = "write.internal.crm.note"
    calls: list[Any] = []
    binding = _validating_binding(tool, ApprovalTier.TIER_2, calls=calls)
    policies = {
        tool: ToolArgPolicy(
            args={
                "title": ArgSpec(kind=ToolArgKind.text, required=True),
                "body": ArgSpec(kind=ToolArgKind.text, required=True),
            }
        )
    }
    h = make_app(extra_tool_bindings=(binding,), extra_tool_policies=policies)

    sentinel_title = "SENTINEL-TITLE-9f3a"
    sentinel_body = "SENTINEL-BODY-9f3a"
    resp = run(_flowise_execute(h, tool, {"title": sentinel_title, "body": sentinel_body}))

    # BY IDENTITY — never `!= 500`.
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "record_id" in detail
    # C12: the offending FIELD name, never the rejected VALUES.
    assert sentinel_title not in resp.text
    assert sentinel_body not in resp.text
    # The handler never ran (pre-gate: no execution).
    assert calls == []

    engine = sa.create_engine(h.audit_url)
    try:
        records = list_records(engine, limit=1000)
    finally:
        engine.dispose()

    surface_records = [r for r in records if r.action == "api.flowise.execute"]
    assert len(surface_records) == 1, f"expected exactly one surface record: {surface_records}"
    surface = surface_records[0]
    assert surface.outcome == "denied"
    assert surface.resource == f"flowise:{tool}"
    surface_detail = surface.payload.get("detail")
    surface_detail = (
        json.loads(surface_detail) if isinstance(surface_detail, str) else surface_detail
    )
    assert surface_detail == {"tool": tool, "error_class": "ActionValidationError"}
    surface_provenance = surface.payload.get("provenance")
    assert surface_provenance is not None and surface_provenance["surface"] == "flowise"

    validate_records = [
        r
        for r in records
        if r.action == "tool.validate" and r.resource == f"tool:{tool}" and r.outcome == "denied"
    ]
    assert validate_records, "no tool.validate[denied] record on the chain"

    # Pre-gate: no human decision consumed, handler never invoked.
    assert not any(
        r.action == "approval.requested" and r.resource == f"tool:{tool}" for r in records
    )
    assert not any(r.action == "tool.execute" and r.resource == f"tool:{tool}" for r in records)

    # C12 through the WHOLE chain: sentinel VALUES never appear in any serialized record.
    for r in records:
        blob = json.dumps(r.payload, default=str)
        assert sentinel_title not in blob
        assert sentinel_body not in blob
