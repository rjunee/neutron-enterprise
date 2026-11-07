# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/posture (NE-131) — the sovereignty-posture wire surface.

Proves the SPEC-row shape, the honest attestation (a guardian-less constraint would
render manual/unattested — none currently; C10 single-tenant retired 2026-07-17, the
slot re-purposed 2026-07-18 for audit-log integrity, NE-AUDIT-CRON), the EFFECTIVE
AIR_GAP/allowlist read from the real C2 sources, the composed model lanes, and the
frozen config_hash + audit-derived history (with 'from'/'to' serialization).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ne.audit.store import ne_audit_log
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run

_TOP_KEYS = {
    "constraints",
    "air_gap",
    "egress_allowlist",
    "network_observed_at",
    "model_lanes",
    "config_hash",
    "config_hash_history",
    "configuration_observed_at",
    "attestation_source",
    # NE-AIRGAP-TOGGLE: the §2.5 pending-change read-back for a parked governed
    # air-gap change (null when nothing is parked — test_network_airgap.py owns
    # its non-null behaviour).
    "air_gap_pending",
}
_CONSTRAINT_KEYS = {"id", "short", "enforcement", "attested", "mode", "evidence"}


def test_requires_auth(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture")
            assert r.status_code == 401

    run(_t())


def test_posture_shape(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            request_started = datetime.now(UTC)
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            response_received = datetime.now(UTC)
            assert r.status_code == 200
            body = r.json()
            assert set(body) == _TOP_KEYS

            # Both runtime facts are dated by the server at their owning read,
            # never by the console at fetch completion. Network is read first;
            # configuration/audit history second.
            network_observed_at = datetime.fromisoformat(body["network_observed_at"])
            configuration_observed_at = datetime.fromisoformat(body["configuration_observed_at"])
            assert request_started <= network_observed_at <= response_received
            assert request_started <= configuration_observed_at <= response_received
            assert network_observed_at <= configuration_observed_at

            constraints = body["constraints"]
            # C10 (single-tenant) was retired 2026-07-17, then re-purposed
            # 2026-07-18 for audit-log integrity (NE-AUDIT-CRON) — C1-C12, no gap.
            assert [c["id"] for c in constraints] == [f"C{i}" for i in range(1, 13)]
            for c in constraints:
                assert set(c) == _CONSTRAINT_KEYS
                assert isinstance(c["evidence"], list)

            # All current C1-C12 rows are BUILD-TIME mechanical attestations.
            # This mode is the wire-level distinction from the live egress and
            # runtime configuration readings beside them.
            assert {c["mode"] for c in constraints} == {"static"}
            assert all(c["attested"] is True for c in constraints)
            assert all(c["evidence"] for c in constraints)

            by_id = {c["id"]: c for c in constraints}
            # A mechanically-attested constraint is attested + static + has evidence.
            assert by_id["C1"]["attested"] is True
            assert by_id["C1"]["mode"] == "static"
            assert by_id["C1"]["evidence"]
            assert by_id["C10"]["attested"] is True
            assert by_id["C10"]["mode"] == "static"

            assert isinstance(body["attestation_source"], str) and body["attestation_source"]
            assert "build-time guardian evidence" in body["attestation_source"]

    run(_t())


def test_air_gap_and_allowlist(make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    # The allowlist is now FROZEN at boot (the same EgressSettings instance the live
    # guard enforces), so set the env BEFORE make_app().
    monkeypatch.setenv("NE_EGRESS_ALLOWED_FRONTIER_HOSTS", '["api.anthropic.com"]')
    monkeypatch.delenv("AIR_GAP", raising=False)
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            # Allowlisted frontier host, AIR_GAP unset ⇒ the host is sanctioned egress.
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert body["air_gap"] is False
            assert body["egress_allowlist"] == ["api.anthropic.com"]

            # Engage the global AIR_GAP flag ⇒ frontier lane killed, allowlist empty.
            # This stays a POST-boot flip: the enforced EgressPolicy._check_frontier
            # reads air_gap_engaged() per call, so posture mirroring it live IS
            # reported==enforced (§0 law 5).
            monkeypatch.setenv("AIR_GAP", "1")
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            body = r.json()
            assert body["air_gap"] is True
            assert body["egress_allowlist"] == []

    run(_t())


def test_posture_reports_boot_frozen_egress_after_env_change(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Boot with allowlist A frozen into the platform's egress guard.
    monkeypatch.setenv("NE_EGRESS_ALLOWED_FRONTIER_HOSTS", '["api.anthropic.com"]')
    monkeypatch.delenv("AIR_GAP", raising=False)
    h = make_app()

    # SIMULATE a post-boot config change to allowlist B — WITHOUT a restart. The
    # running gateway still enforces A; posture must keep reporting A (§0 law 5).
    monkeypatch.setenv(
        "NE_EGRESS_ALLOWED_FRONTIER_HOSTS", '["api.openai.com", "changed.example.com"]'
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            # Reports the BOOT-FROZEN allowlist A, never the post-boot re-read B.
            assert body["egress_allowlist"] == ["api.anthropic.com"]
            assert body["air_gap"] is False

    run(_t())

    # And the reported allowlist equals the frozen guard instance ApiState carries —
    # proving posture reads THAT instance, not a fresh per-request EgressSettings().
    state = get_state(h.app)
    assert state.egress_settings is not None
    body_allowlist = ["api.anthropic.com"]
    assert body_allowlist == sorted(state.egress_settings.allowed_frontier_hosts)


def test_model_lanes(make_app: AppBuilder) -> None:
    # The conftest injects a GatewaySettings with one ModelDef (name=stub,
    # provider=stub, lane=frontier-api, litellm_model=openai/stub) — assert it
    # round-trips as an opaque lane that NAMES THE MODEL IT RESOLVES TO. The
    # alias ("stub") and the served model ("stub", provider prefix stripped from
    # "openai/stub") are different fields answering different questions; a lane
    # that serves only the alias cannot answer "what is this lane?" at all.
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            lanes = r.json()["model_lanes"]
            assert lanes == [
                {
                    "model_id": "stub",
                    "provider": "stub",
                    "lane": "frontier-api",
                    "configured_model": "stub",
                    "context_window": 32768,
                }
            ]
            # Exact keys: the CONFIGURATION read must not grow a measured-looking
            # field (checked_at / state / latency / served_models) by accident.
            # Liveness is GET /v1/models/serving and lives nowhere else.
            assert set(lanes[0]) == {
                "model_id",
                "provider",
                "lane",
                "configured_model",
                "context_window",
            }

    run(_t())


def test_model_lanes_name_the_model_for_a_frontier_lane_too(make_app: AppBuilder) -> None:
    """The defect this closed: a frontier lane rendered its alias and nothing
    else, so an operator could not tell two frontier lanes apart — and the
    private-only serving probe never covers them (C2: ne-api must not probe a
    provider host). Configuration is the ONLY honest source for those rows, and
    it is served here for every lane, frontier included.

    Mutation: dropping ``configured_model``/``context_window`` from the frontier
    branch, or special-casing them onto private lanes only -> RED.
    """
    from ne.contracts.types import Lane
    from ne.gateway import GatewaySettings, ModelDef, TaskRoute

    settings = GatewaySettings(
        models=[
            ModelDef(
                name="frontier-default",
                litellm_model="anthropic/claude-sonnet-5",
                provider="anthropic",
                lane=Lane.FRONTIER_API,
                context_window=32768,
            ),
            ModelDef(
                name="frontier-best",
                litellm_model="anthropic/claude-fable-5",
                provider="anthropic",
                lane=Lane.FRONTIER_API,
                context_window=1_000_000,
            ),
            ModelDef(
                name="private-default",
                litellm_model="hosted_vllm/qwen3-0.6b",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm:8000/v1",
                context_window=32768,
            ),
        ],
        tasks={"agent": TaskRoute(model="private-default")},
    )
    h = make_app()
    get_state(h.app).gateway_settings = settings

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            by_alias = {lane["model_id"]: lane for lane in r.json()["model_lanes"]}
            assert by_alias["frontier-default"]["configured_model"] == "claude-sonnet-5"
            assert by_alias["frontier-best"]["configured_model"] == "claude-fable-5"
            # Symmetric: the private lane answers the same two questions.
            assert by_alias["private-default"]["configured_model"] == "qwen3-0.6b"
            assert by_alias["frontier-best"]["context_window"] == 1_000_000
            assert by_alias["private-default"]["context_window"] == 32768
            # The two frontier lanes are DISTINGUISHABLE, which is the point.
            assert (
                by_alias["frontier-default"]["configured_model"]
                != by_alias["frontier-best"]["configured_model"]
            )

    run(_t())


def test_config_hash_and_history(make_app: AppBuilder) -> None:
    h = make_app()
    state = get_state(h.app)
    now = datetime.now(UTC)
    prior_hash = "a" * 64

    # Seed one REAL current-config_hash event through the governed sink (seq 1).
    async def _seed() -> None:
        await state.sink.append(
            new_audit_event(
                action="ne.gateway.complete",
                resource="gateway",
                outcome=AuditOutcome.success,
                actor="user:ada",
                config_hash=state.config_hash,
                occurred_at=now,
            )
        )

    run(_seed())

    # INSERT one synthetic row with a fake PRIOR config_hash directly into the log —
    # earlier occurred_at + seq 0 so it sorts oldest-first. (INSERT is allowed; the
    # append-only triggers only forbid UPDATE/DELETE.)
    with state.audit_engine.begin() as conn:
        conn.execute(
            ne_audit_log.insert().values(
                seq=0,
                event_id=uuid.uuid4().hex,
                event_type="audit",
                config_hash=prior_hash,
                actor="user:ada",
                occurred_at=now - timedelta(days=1),
                payload={"config_hash": prior_hash},
                prev_hash="0" * 64,
                record_hash="f" * 64,
            )
        )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert body["config_hash"] == state.config_hash

            history = body["config_hash_history"]
            assert [set(span) for span in history] == [{"hash", "from", "to"}] * len(history)
            # Ordered oldest-first: the synthetic prior span, then the current span.
            assert [span["hash"] for span in history] == [prior_hash, state.config_hash]
            # The prior span is CLOSED (non-null 'to'); the in-force span is OPEN.
            assert history[0]["to"] is not None
            assert history[1]["to"] is None

    run(_t())


def test_config_hash_history_empty(make_app: AppBuilder) -> None:
    # A fresh deployment with no audit events yields an empty history (honest []).
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/posture", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            # verify_chain against an empty log is fine; the history is just [].
            assert r.json()["config_hash_history"] == []

    run(_t())


def test_config_span_serializes_from_key() -> None:
    # Guard the reserved-word alias: the wire key is exactly 'from', never 'from_'.
    from ne.api.wire import ConfigHashSpanView

    span = ConfigHashSpanView(hash="h", from_=datetime(2026, 7, 1, tzinfo=UTC), to=None)
    dumped = span.model_dump(mode="json", by_alias=True)
    assert "from" in dumped and "from_" not in dumped
