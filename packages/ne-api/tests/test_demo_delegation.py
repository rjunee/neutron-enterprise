# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-168 — the governed multi-lane agent-to-model delegation demo (composed level).

Mirrors ``packages/ne-runtime/tests/test_dispatch_routing.py`` assertions at the
COMPOSED ``build_platform``/HTTP level: (1) default-off byte-parity (the flag
defaulted vs explicit-off produce the SAME config_hash, ``demo_dispatcher`` is
``None``, the route 404s; flag-on differs — the honest stamp); (2) the 3-leg
delegation dispatches EXACTLY 2 real private-vllm gateway requests (triage +
reason) and ZERO frontier requests — the adjudicate leg is a governed,
PDP-CHECKED ``SubAgentRoutePolicyDeniedError`` DENY (NE-169: the ``ne.govern``
``subagent.route`` rule default-denies frontier with no grant), never a live
frontier call (C2); (3) the NE-167a trace-assembly view renders the 3
``subagent.route`` legs sharing the run's trace_id; (4) the route is ops-gated
(403 for a non-ops bearer) — the SAME role-gate discipline as
``GET /v1/trace/{trace_id}`` it feeds.

Reuses this module's own ``_api_settings``/``_seams`` builders (test_demo_seams.py)
+ the conftest ``ScriptedGateway``/``FakeTokenVerifier``/``RegoFaithfulPdpFactory``/
``get_state``/``run`` seams — the governed spine is REAL; only the network edges
are faked. No pytest-asyncio (repo policy).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from ne.api import create_app
from ne.api.platform import build_platform
from ne.audit.replay import replay
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.types import Lane

from .conftest import ScriptedGateway, get_state, run, text_completion
from .test_demo_seams import _api_settings, _seams


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


def _mint_ops(verifier: Any, subject: str = "user:ops-demo") -> dict[str, str]:
    token = verifier.mint(subject, groups=("ops",), department="operations")
    return {"Authorization": f"Bearer {token}"}


# --- (1) default-off byte-parity ---------------------------------------------
def test_default_off_parity(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    seams_a, _ = _seams(tmp_path, key="ident")
    default = build_platform(_api_settings(), seams_a)
    engines.extend(default.engines)
    assert default.demo_dispatcher is None

    reset_config_hash_for_testing()
    seams_b, _ = _seams(tmp_path, key="ident")
    explicit_off = build_platform(_api_settings(demo_delegation=False), seams_b)
    engines.extend(explicit_off.engines)
    assert explicit_off.demo_dispatcher is None

    assert default.config_hash == explicit_off.config_hash

    reset_config_hash_for_testing()
    seams_on, verifier = _seams(tmp_path, key=uuid.uuid4().hex)
    app = create_app(_api_settings(demo_delegation=True, ops_roles=("ops",)), seams=seams_on)
    state = get_state(app)
    assert state.demo_dispatcher is not None
    # The flag-on hash DIFFERS (honest stamp) — the demo_writeback_tool precedent.
    assert state.config_hash != default.config_hash

    async def _t() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            return await client.post("/api/v1/demo/delegate", json={}, headers=_mint_ops(verifier))

    r_on = run(_t())
    assert r_on.status_code == 200
    for engine in state.engines:
        engine.dispose()

    # The default (flag-off) app never composed a dispatcher — the route is a
    # 404, not a silently-degraded surface (routers/graph.py precedent). Its
    # ops_roles is empty by default, so the gate would 403 first — build one
    # WITH ops_roles set but the flag left off to isolate the 404 behaviour.
    reset_config_hash_for_testing()
    seams_off_ops, verifier_off = _seams(tmp_path, key=uuid.uuid4().hex)
    off_app = create_app(_api_settings(ops_roles=("ops",)), seams=seams_off_ops)
    off_state = get_state(off_app)
    assert off_state.demo_dispatcher is None

    async def _t_off() -> httpx.Response:
        transport = httpx.ASGITransport(app=off_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            return await client.post(
                "/api/v1/demo/delegate", json={}, headers=_mint_ops(verifier_off)
            )

    r_off = run(_t_off())
    assert r_off.status_code == 404
    for engine in off_state.engines:
        engine.dispose()


# --- (2) the 3-leg delegation under one trace_id -----------------------------
def _build_delegation_app(tmp_path: Path) -> tuple[Any, Any, ScriptedGateway]:
    reset_config_hash_for_testing()
    gateway = ScriptedGateway([text_completion("triaged"), text_completion("reasoned")])
    seams, verifier = _seams(tmp_path, key=uuid.uuid4().hex, gateway=gateway)
    app = create_app(_api_settings(demo_delegation=True, ops_roles=("ops",)), seams=seams)
    return app, verifier, gateway


def test_delegate_three_legs_one_trace(tmp_path: Path) -> None:
    app, verifier, gateway = _build_delegation_app(tmp_path)
    state = get_state(app)
    ops = _mint_ops(verifier)

    async def _t() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            return await client.post("/api/v1/demo/delegate", json={}, headers=ops)

    resp = run(_t())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    trace_id = body["trace_id"]
    legs = body["legs"]
    by_agent = {leg["agent"]: leg for leg in legs}

    assert by_agent["triage"]["outcome"] == "completed"
    assert by_agent["triage"]["succeeded"] is True
    assert by_agent["triage"]["tier"] == "private"
    assert by_agent["reason"]["outcome"] == "completed"
    assert by_agent["reason"]["succeeded"] is True
    assert by_agent["reason"]["tier"] == "private"
    assert by_agent["adjudicate"]["outcome"] == "denied-route-policy"
    assert by_agent["adjudicate"]["succeeded"] is False
    assert by_agent["adjudicate"]["tier"] == "frontier"

    # C2: EXACTLY 2 gateway requests (triage + reason), both private-vllm; the
    # frontier leg NEVER reaches the gateway (the PDP ROUTE AUTHORITY consult
    # denies it before the RUNTIME GATE is even reached).
    assert len(gateway.requests) == 2
    tasks = [r.task for r in gateway.requests]
    assert tasks == ["triage", "reason"]
    for r in gateway.requests:
        assert r.lane == Lane.PRIVATE_VLLM

    # Audit db: exactly 3 subagent.route events, all sharing the response trace_id.
    result = replay(state.audit_engine, trace_id=trace_id)
    route_events = [e for e in result.events if e.action == "subagent.route"]
    assert len(route_events) == 3
    assert all(e.trace_id == trace_id for e in route_events)
    outcomes = [e.outcome for e in route_events]
    assert outcomes.count("success") == 2
    assert outcomes.count("denied") == 1
    denied = next(e for e in route_events if e.outcome == "denied")
    detail = json.loads(denied.payload["detail"])
    assert detail["tier"] == "frontier"
    assert detail["reason"] == "route_policy_denied"
    assert detail["pdp_effect"] == "deny"

    for engine in state.engines:
        engine.dispose()


# --- (3) GET /v1/trace/{trace_id} shows the 3 legs ---------------------------
def test_trace_view_shows_legs(tmp_path: Path) -> None:
    app, verifier, _gateway = _build_delegation_app(tmp_path)
    state = get_state(app)
    ops = _mint_ops(verifier)

    async def _t() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            delegate_resp = await client.post("/api/v1/demo/delegate", json={}, headers=ops)
            trace_id = delegate_resp.json()["trace_id"]
            trace_resp = await client.get(f"/api/v1/trace/{trace_id}", headers=ops)
            return delegate_resp, trace_resp

    delegate_resp, trace_resp = run(_t())
    assert trace_resp.status_code == 200, trace_resp.text
    view = trace_resp.json()
    assert view["rollup"]["trace_id"] == delegate_resp.json()["trace_id"]

    def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in nodes:
            out.append(node)
            out.extend(_walk(node.get("children", [])))
        return out

    all_nodes = _walk(view["nodes"])
    route_nodes = [n for n in all_nodes if n["action"] == "subagent.route"]
    assert len(route_nodes) == 3
    tiers = sorted(n["route"]["tier"] for n in route_nodes)
    assert tiers == ["frontier", "private", "private"]
    frontier_node = next(n for n in route_nodes if n["route"]["tier"] == "frontier")
    assert frontier_node["outcome"] == "denied"
    assert frontier_node["route"]["reason"] == "route_policy_denied"

    for engine in state.engines:
        engine.dispose()


# --- (4) ops-gated (403 for non-ops) -----------------------------------------
def test_delegate_requires_ops(tmp_path: Path) -> None:
    app, verifier, _gateway = _build_delegation_app(tmp_path)
    state = get_state(app)
    non_ops_token = verifier.mint("user:someone", groups=(), department="engineering")

    async def _t() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            return await client.post(
                "/api/v1/demo/delegate",
                json={},
                headers={"Authorization": f"Bearer {non_ops_token}"},
            )

    resp = run(_t())
    assert resp.status_code == 403

    for engine in state.engines:
        engine.dispose()
