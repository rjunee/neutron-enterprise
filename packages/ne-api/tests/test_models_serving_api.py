# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/models/serving (NE-SERVING-STATUS) — the live private-lane surface.

Proves the properties that make the surface a MEASUREMENT rather than a label
(ne.api.serving module docstring), each bidirectionally per
docs/testing/vacuous-guards.md:

* a reachable lane renders ``ok`` WITH the model the ENDPOINT reported — and an
  unreachable one renders ``unreachable`` with NO model data, never a cached
  success re-labelled current;
* absent / stale / unreachable are three different renderings: a lane the
  deployment does not carry yields no row (with an in-response presence control
  proving the sweep can see rows at all); a cache hit keeps its ORIGINAL
  ``checked_at`` (age carried); a failure after a success keeps ``last_ok_at``
  (down-since-when) while a never-answered lane serves ``last_ok_at=None``;
* C2/C12: an SSRF-shaped endpoint (metadata host, embedded credential, query
  token) is never contacted (transport call-count 0 for that host) and no
  sentinel secret from the config reaches the serialized response — with
  presence controls proving each sentinel really sits in the config the sweep
  is claimed to cover;
* C6: the read is ops-gated exactly like /v1/audit/* (fail-closed on empty
  ``ops_roles``).

Mutation evidence (each run manually with a file snapshot, revert after):
  M1 ``state="ok"`` hardcoded in the unreachable arm → RED
     test_unreachable_lane_renders_unreachable_not_stale_success
  M2 drop the ``httpx.TimeoutException`` arm (fold into unreachable) → RED
     test_timeout_is_a_distinct_displayable_state
  M3 cache serves ``checked_at=now`` instead of the formed-at value → RED
     test_cache_hit_keeps_the_original_checked_at
  M4 failure path overwrites ``last_ok_at`` (or ok path stops recording it) →
     RED test_failure_after_success_is_stale_not_never_answered (both arms)
  M5 serve the raw config ``api_base`` string as ``endpoint`` → RED
     test_no_config_secret_reaches_the_wire (sentinel HUSHTOKEN / QUERYSECRET)
  M6 probe frontier lanes too (drop the lane filter) → RED
     test_frontier_and_endpointless_lanes_yield_no_row (frontier host contacted)
  M7 ``serves_configured_model=True`` hardcoded → RED
     test_endpoint_reporting_a_different_model_is_a_visible_mismatch
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

from ne.api.serving import ServingStatusService
from ne.contracts import Lane, reset_config_hash_for_testing
from ne.gateway.config import ModelDef

from .conftest import AppBuilder, run

# ---------------------------------------------------------------------------
# Fixture lanes + a scripted OpenAI-compatible endpoint
# ---------------------------------------------------------------------------

#: NOT a loopback-container assumption: a routable private IP + port, the shape
#: the demo's remote GPU rig actually has.
GPU_BASE = "http://10.9.8.7:8000/v1"

PRIVATE_LANE = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen3-32b",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base=GPU_BASE,
    context_window=32768,
)

FRONTIER_LANE = ModelDef(
    name="frontier-default",
    litellm_model="anthropic/claude-fable-5",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    api_base="https://frontier.example/v1",
    context_window=200000,
)


class ScriptedEndpoint:
    """A per-host scripted transport: records every request, answers like vLLM."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        #: host → behaviour: "ok" | "connect-error" | "timeout" | int (status)
        #: | "not-json"
        self.behaviour: dict[str, object] = {}
        self.served_model = "qwen3-32b"
        self.max_model_len = 16384
        self.version = "0.12.0"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        mode = self.behaviour.get(request.url.host, "ok")
        if mode == "connect-error":
            raise httpx.ConnectError("refused", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("no answer within budget", request=request)
        if isinstance(mode, int):
            return httpx.Response(mode, text="upstream error")
        if mode == "not-json":
            return httpx.Response(200, text="<html>a proxy answered</html>")
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": self.served_model,
                            "object": "model",
                            "owned_by": "vllm",
                            "max_model_len": self.max_model_len,
                        }
                    ],
                },
            )
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": self.version})
        return httpx.Response(404, text="unknown path")


class Clock:
    """An injectable, advanceable UTC clock (staleness without sleeping)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_probe(endpoint: ScriptedEndpoint, clock: Clock | None = None) -> ServingStatusService:
    return ServingStatusService(
        transport=endpoint.transport(), clock=clock if clock is not None else Clock()
    )


def models_url_count(endpoint: ScriptedEndpoint) -> int:
    return sum(1 for url in endpoint.requests if url.endswith("/models"))


# ---------------------------------------------------------------------------
# The ops gate (C6 — the audit/trace parity)
# ---------------------------------------------------------------------------


def test_requires_auth(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving")
            assert r.status_code == 401

    run(_t())


def test_ops_gate_refuses_non_ops_and_fails_closed_on_empty_roles(
    make_app: AppBuilder,
) -> None:
    endpoint = ScriptedEndpoint()
    gated = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )
    # Second composition in one process: differing settings hash differently,
    # and the process-frozen config_hash correctly refuses a re-freeze.
    reset_config_hash_for_testing()
    unconfigured = make_app(gateway_models=[PRIVATE_LANE], serving_probe=make_probe(endpoint))

    async def _t() -> None:
        async with gated.client() as client:
            r = await client.get(
                "/api/v1/models/serving", headers=gated.bearer("u", groups=["not-ops"])
            )
            assert r.status_code == 403
        async with unconfigured.client() as client:
            # Empty ops_roles ⇒ 403 for EVERYONE, even a group literally named
            # "ops" — fail closed, the /v1/audit/* rule.
            r = await client.get(
                "/api/v1/models/serving",
                headers=unconfigured.bearer("u", groups=["ops", "admins"]),
            )
            assert r.status_code == 403
        # BIDIRECTIONAL CONTROL: the refusals above are the GATE, not a broken
        # route — the properly-entitled key reads 200 from the same app.
        async with gated.client() as client:
            r = await client.get(
                "/api/v1/models/serving", headers=gated.bearer("u", groups=["ops"])
            )
            assert r.status_code == 200
        # …and no probe traffic happened for the refused reads (two refusals,
        # one grant ⇒ exactly one round of probing).
        assert models_url_count(endpoint) == 1

    run(_t())


# ---------------------------------------------------------------------------
# Measured, not configured
# ---------------------------------------------------------------------------


def test_reachable_lane_reports_the_endpoints_own_answer(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        assert r.status_code == 200
        lanes = r.json()["lanes"]
        assert len(lanes) == 1
        lane = lanes[0]
        assert lane["model_id"] == "private-default"
        assert lane["lane"] == "private-vllm"
        assert lane["state"] == "ok"
        # The MODEL fact is the endpoint's own /v1/models answer, not an echo
        # of config: the served id, the runtime window the endpoint reports
        # (16384 — deliberately NOT the configured 32768), and the engine's
        # self-reported version.
        assert lane["served_models"] == ["qwen3-32b"]
        assert lane["serves_configured_model"] is True
        assert lane["configured_model"] == "qwen3-32b"
        assert lane["observed_context_window"] == 16384
        assert lane["observed_context_window"] != PRIVATE_LANE.context_window
        assert lane["engine_version"] == "0.12.0"
        assert lane["http_status"] == 200
        assert isinstance(lane["latency_ms"], int) and lane["latency_ms"] >= 0
        # The observation is timestamped, and a fresh success is its own last
        # confirmation.
        assert lane["checked_at"] is not None
        assert lane["last_ok_at"] == lane["checked_at"]
        # C12 endpoint identity: scheme://host:port ONLY — the raw config
        # string (which carries the /v1 path) is never serialized.
        assert lane["endpoint"] == "http://10.9.8.7:8000"
        assert "10.9.8.7:8000/v1" not in r.text
        # …and the probe went to the OpenAI-compatible surface of that host.
        assert any(u.startswith("http://10.9.8.7:8000/v1/models") for u in endpoint.requests)

    run(_t())


def test_endpoint_reporting_a_different_model_is_a_visible_mismatch(
    make_app: AppBuilder,
) -> None:
    endpoint = ScriptedEndpoint()
    endpoint.served_model = "llama-guard-8b"  # NOT what the policy names
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        lane = r.json()["lanes"][0]
        # Reachability and identity are separate facts: the lane is up…
        assert lane["state"] == "ok"
        # …but the surface renders the endpoint's word AGAINST the config's,
        # never a hardcoded agreement (M7).
        assert lane["serves_configured_model"] is False
        assert lane["served_models"] == ["llama-guard-8b"]
        assert lane["configured_model"] == "qwen3-32b"

    run(_t())


# ---------------------------------------------------------------------------
# Absent · stale · unreachable — three different things
# ---------------------------------------------------------------------------


def test_unreachable_lane_renders_unreachable_not_stale_success(
    make_app: AppBuilder,
) -> None:
    endpoint = ScriptedEndpoint()
    endpoint.behaviour["10.9.8.7"] = "connect-error"
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        lane = r.json()["lanes"][0]
        assert lane["state"] == "unreachable"
        assert lane["state"] != "ok"  # M1 both directions, explicitly
        # A failure carries NO observed model data — an unreachable endpoint
        # structurally cannot render a model name…
        assert lane["served_models"] == []
        assert lane["serves_configured_model"] is None
        assert lane["engine_version"] is None
        # …and a lane that has NEVER answered says so: no last confirmation.
        assert lane["last_ok_at"] is None
        assert lane["checked_at"] is not None

    run(_t())


def test_timeout_is_a_distinct_displayable_state(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    endpoint.behaviour["10.9.8.7"] = "timeout"
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        lane = r.json()["lanes"][0]
        # A wedged endpoint expires the budget and that expiry is itself the
        # displayed state — NOT folded into "unreachable" (M2): no-route and
        # accepted-then-hung are different operator diagnoses.
        assert lane["state"] == "timeout"
        assert lane["state"] not in ("unreachable", "ok")
        assert lane["served_models"] == []

    run(_t())


def test_answered_wrong_is_neither_ok_nor_unreachable(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    endpoint.behaviour["10.9.8.7"] = 503
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        lane = r.json()["lanes"][0]
        assert lane["state"] == "http_error"
        assert lane["http_status"] == 503
        assert lane["served_models"] == []
        # And a 200 that is not an OpenAI-compatible model listing (a captive
        # proxy, a wrong service) is "malformed", never a fabricated ok.
        endpoint.behaviour["10.9.8.7"] = "not-json"
        probe2 = make_probe(endpoint)
        views = await probe2.observe([PRIVATE_LANE])
        assert views[0].state == "malformed"
        assert views[0].http_status == 200
        assert views[0].served_models == []

    run(_t())


def test_cache_hit_keeps_the_original_checked_at(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    clock = Clock()
    probe = make_probe(endpoint, clock)

    async def _t() -> None:
        first = (await probe.observe([PRIVATE_LANE]))[0]
        t0 = first.checked_at
        assert t0 == clock.now
        probes_after_first = models_url_count(endpoint)

        # Within the TTL: served from cache — the endpoint is NOT re-contacted
        # and the verdict keeps the ORIGINAL observation time, so a cached
        # success renders with its age instead of masquerading as current (M3).
        clock.advance(2)
        second = (await probe.observe([PRIVATE_LANE]))[0]
        assert models_url_count(endpoint) == probes_after_first
        assert second.checked_at == t0
        assert second.checked_at != clock.now

        # Past the TTL: a REAL re-observation, new timestamp (the control that
        # the cache expires at all — a never-expiring cache would freeze t0).
        clock.advance(10)
        third = (await probe.observe([PRIVATE_LANE]))[0]
        assert models_url_count(endpoint) == probes_after_first + 1
        assert third.checked_at == clock.now
        assert third.checked_at != t0

    run(_t())


def test_models_completion_owns_checked_at_and_return_owns_cache_age() -> None:
    clock = Clock()
    started = clock.now
    model_reads = 0

    def endpoint(request: httpx.Request) -> httpx.Response:
        nonlocal model_reads
        if request.url.path == "/v1/models":
            model_reads += 1
            clock.advance(1)
            return httpx.Response(200, json={"data": [{"id": "qwen3-32b"}]})
        if request.url.path == "/version":
            clock.advance(3)
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(404)

    probe = ServingStatusService(transport=httpx.MockTransport(endpoint), clock=clock)

    async def _t() -> None:
        first = (await probe.observe([PRIVATE_LANE]))[0]
        assert first.state == "ok"
        assert first.checked_at == started + timedelta(seconds=1)
        assert clock.now == started + timedelta(seconds=4)

        # Cache lifetime begins when the enriched verdict is ready to return,
        # not at /models completion. This remains a display cache hit even
        # though optional /version work consumed three simulated seconds.
        clock.advance(2)
        second = (await probe.observe([PRIVATE_LANE]))[0]
        assert second.checked_at == first.checked_at
        assert model_reads == 1

    run(_t())


def test_version_timeout_degrades_enrichment_without_overriding_models_success() -> None:
    async def endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3-32b"}]})
        if request.url.path == "/version":
            await anyio.sleep(0.05)
            return httpx.Response(200, json={"version": "late"})
        return httpx.Response(404)

    probe = ServingStatusService(
        transport=httpx.MockTransport(endpoint),
        timeout_s=0.01,
    )

    async def _t() -> None:
        [view] = await probe.observe([PRIVATE_LANE], force_refresh=True)
        assert view.state == "ok"
        assert view.served_models == ["qwen3-32b"]
        assert view.serves_configured_model is True
        assert view.engine_version is None

    run(_t())


def test_models_timeout_still_consumes_the_one_total_probe_budget() -> None:
    async def endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            await anyio.sleep(0.05)
            return httpx.Response(200, json={"data": [{"id": "qwen3-32b"}]})
        raise AssertionError("version enrichment cannot precede /models")

    probe = ServingStatusService(
        transport=httpx.MockTransport(endpoint),
        timeout_s=0.01,
    )

    async def _t() -> None:
        [view] = await probe.observe([PRIVATE_LANE], force_refresh=True)
        assert view.state == "timeout"
        assert view.served_models == []
        assert view.serves_configured_model is None
        assert view.engine_version is None

    run(_t())


def test_older_forced_probe_cannot_overwrite_newer_cache_or_success_history() -> None:
    older_started = anyio.Event()
    release_older = anyio.Event()
    newer_finished = anyio.Event()
    model_reads = 0

    async def endpoint(request: httpx.Request) -> httpx.Response:
        nonlocal model_reads
        if request.url.path == "/v1/models":
            model_reads += 1
            if model_reads == 1:
                older_started.set()
                await release_older.wait()
                return httpx.Response(200, json={"data": [{"id": "qwen3-32b"}]})
            if model_reads == 2:
                return httpx.Response(503, json={"detail": "scripted newer failure"})
            raise AssertionError("ordinary read must use the newer cached verdict")
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(404)

    probe = ServingStatusService(transport=httpx.MockTransport(endpoint))
    results: dict[str, Any] = {}

    async def older() -> None:
        results["older"] = (await probe.observe([PRIVATE_LANE], force_refresh=True))[0]

    async def newer() -> None:
        results["newer"] = (await probe.observe([PRIVATE_LANE], force_refresh=True))[0]
        newer_finished.set()

    async def _t() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(older)
            await older_started.wait()
            tg.start_soon(newer)
            await newer_finished.wait()
            release_older.set()

        older_view = results["older"]
        newer_view = results["newer"]
        assert older_view.state == "ok"
        assert newer_view.state == "http_error"

        [cached] = await probe.observe([PRIVATE_LANE])
        assert cached.state == "http_error"
        assert cached.http_status == 503
        assert cached.last_ok_at is None
        assert model_reads == 2

    run(_t())


def test_failure_after_success_is_stale_not_never_answered(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    clock = Clock()
    probe = make_probe(endpoint, clock)

    async def _t() -> None:
        first = (await probe.observe([PRIVATE_LANE]))[0]
        assert first.state == "ok"
        t_ok = first.checked_at

        # The endpoint goes down; the TTL passes; the next verdict is a FAILURE
        # that still remembers when the lane last actually answered — the
        # difference between "down for five minutes" and "never up" (M4).
        endpoint.behaviour["10.9.8.7"] = "connect-error"
        clock.advance(300)
        second = (await probe.observe([PRIVATE_LANE]))[0]
        assert second.state == "unreachable"
        assert second.last_ok_at == t_ok
        assert second.checked_at == clock.now
        # …and the stale success is NOT re-served as the current state: the
        # current state is the failure; only last_ok_at carries the history.
        assert second.state != "ok"
        assert second.served_models == []

        # Recovery updates the confirmation forward (M4's other arm: the ok
        # path must RECORD, not just the failure path preserve).
        endpoint.behaviour.pop("10.9.8.7")
        clock.advance(10)
        third = (await probe.observe([PRIVATE_LANE]))[0]
        assert third.state == "ok"
        assert third.last_ok_at == clock.now
        assert third.last_ok_at != t_ok

    run(_t())


def test_frontier_and_endpointless_lanes_yield_no_row(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    no_endpoint_lane = ModelDef(
        name="private-no-endpoint",
        litellm_model="hosted_vllm/other",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        context_window=8192,
    )
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[FRONTIER_LANE, no_endpoint_lane, PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        lanes = r.json()["lanes"]
        # PRESENCE CONTROL first (the absence assertions below cannot be
        # satisfied by an endpoint that renders nothing at all):
        assert [lane["model_id"] for lane in lanes] == ["private-default"]
        # A frontier lane is not "down" — it is not this surface's claim; a
        # lane with no endpoint has nothing to observe. Neither gets a row…
        assert all(lane["lane"] != "frontier-api" for lane in lanes)
        # …and the frontier host was never CONTACTED either (C2: probing a
        # provider host from ne-api would be a second egress path) (M6).
        assert not any("frontier.example" in u for u in endpoint.requests)
        assert any("10.9.8.7" in u for u in endpoint.requests)

    run(_t())


# ---------------------------------------------------------------------------
# C12 — no secret from the endpoint config reaches the response
# ---------------------------------------------------------------------------


def test_no_config_secret_reaches_the_wire(make_app: AppBuilder) -> None:
    userinfo_lane = ModelDef(
        name="private-userinfo",
        litellm_model="hosted_vllm/qwen3-32b",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://svc:HUSHTOKEN@10.0.0.5:8000/v1",
        context_window=32768,
    )
    query_lane = ModelDef(
        name="private-query",
        litellm_model="hosted_vllm/qwen3-32b",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://10.0.0.6:8000/v1?api_key=QUERYSECRET",
        context_window=32768,
    )
    key_lane = PRIVATE_LANE.model_copy(
        update={"extra_params": {"api_key": SecretStr("EXTRASECRET")}}
    )
    metadata_lane = ModelDef(
        name="private-metadata",
        litellm_model="hosted_vllm/qwen3-32b",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://169.254.169.254/v1",
        context_window=32768,
    )
    # PRESENCE CONTROLS (§5 residual discipline): each sentinel really sits in
    # the config this test claims to sweep — a misspelled sentinel or a moved
    # secret fails HERE, loudly, not by silently passing the absence checks.
    assert "HUSHTOKEN" in (userinfo_lane.api_base or "")
    assert "QUERYSECRET" in (query_lane.api_base or "")
    assert key_lane.extra_params["api_key"].get_secret_value() == "EXTRASECRET"

    endpoint = ScriptedEndpoint()
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[userinfo_lane, query_lane, key_lane, metadata_lane],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        assert r.status_code == 200
        by_id = {lane["model_id"]: lane for lane in r.json()["lanes"]}

        # The SSRF-shaped endpoints were refused BEFORE any byte was sent:
        # state not_probed, no observation timestamp, and — the fail-closed
        # crux — the transport never saw those hosts or the metadata host.
        for name in ("private-userinfo", "private-query", "private-metadata"):
            assert by_id[name]["state"] == "not_probed"
            assert by_id[name]["checked_at"] is None
        for never in ("10.0.0.5", "10.0.0.6", "169.254.169.254"):
            assert not any(never in u for u in endpoint.requests)

        # POSITIVE CONTROL for the sweep below: the healthy lane's endpoint
        # identity IS served — the response is not simply empty.
        assert by_id["private-default"]["state"] == "ok"
        assert by_id["private-default"]["endpoint"] == "http://10.9.8.7:8000"

        # THE SWEEP: no sentinel appears anywhere in the serialized response.
        body = r.text
        for sentinel in ("HUSHTOKEN", "QUERYSECRET", "EXTRASECRET"):
            assert sentinel not in body
        # And the refused rows still carry a SAFE endpoint identity (host:port
        # only, credential and query dropped by construction).
        assert by_id["private-userinfo"]["endpoint"] == "http://10.0.0.5:8000"
        assert by_id["private-query"]["endpoint"] == "http://10.0.0.6:8000"

    run(_t())


# ---------------------------------------------------------------------------
# Wire-shape pin (the console types mirror this exactly)
# ---------------------------------------------------------------------------

_LANE_KEYS = {
    "model_id",
    "lane",
    "endpoint",
    "configured_model",
    "state",
    "checked_at",
    "latency_ms",
    "http_status",
    "served_models",
    "serves_configured_model",
    "observed_context_window",
    "engine_version",
    "last_ok_at",
}


def test_wire_shape(make_app: AppBuilder) -> None:
    endpoint = ScriptedEndpoint()
    h = make_app(
        ops_roles=("ops",),
        gateway_models=[PRIVATE_LANE],
        serving_probe=make_probe(endpoint),
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/serving", headers=h.bearer("u", groups=["ops"]))
        body = r.json()
        assert set(body) == {"lanes"}
        assert set(body["lanes"][0]) == _LANE_KEYS
        # The whole payload is JSON-serializable data the console renders
        # verbatim (C7) — re-serializing round-trips.
        assert json.loads(json.dumps(body)) == body

    run(_t())


# ---------------------------------------------------------------------------
# GET /v1/models/frontier-readiness — the deploy's cold-start acceptance gate
# ---------------------------------------------------------------------------


def test_frontier_readiness_is_ops_gated_like_serving(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",), gateway_models=[PRIVATE_LANE, FRONTIER_LANE])

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/models/frontier-readiness")
            assert r.status_code == 401
            r = await client.get(
                "/api/v1/models/frontier-readiness", headers=h.bearer("u", groups=["not-ops"])
            )
            assert r.status_code == 403
            # Bidirectional control: the refusals are the gate, not a broken
            # route.
            r = await client.get(
                "/api/v1/models/frontier-readiness", headers=h.bearer("u", groups=["ops"])
            )
            assert r.status_code == 200

    run(_t())


def test_frontier_readiness_serves_the_selection_services_own_verdict(
    make_app: AppBuilder, monkeypatch: Any
) -> None:
    """The route serializes the live service's answer and computes nothing of
    its own.  With no warmer composed (the test default —
    ``frontier_warm_enabled`` off) the honest answer is ``not-applicable``;
    the warming/ready lifecycle itself is proven at the service seam in
    ``test_frontier_warm.py``."""
    from .conftest import get_state

    h = make_app(ops_roles=("ops",), gateway_models=[PRIVATE_LANE, FRONTIER_LANE])
    headers = h.bearer("u", groups=["ops"])

    async def _read() -> Any:
        async with h.client() as client:
            return await client.get("/api/v1/models/frontier-readiness", headers=headers)

    r = run(_read())
    assert r.status_code == 200
    assert r.json() == {"status": "not-applicable"}

    service = get_state(h.app).model_selection
    assert service is not None
    monkeypatch.setattr(service, "frontier_evidence_readiness", lambda: "warming")
    assert run(_read()).json() == {"status": "warming"}
    monkeypatch.setattr(service, "frontier_evidence_readiness", lambda: "ready")
    assert run(_read()).json() == {"status": "ready"}
    monkeypatch.setattr(service, "frontier_evidence_readiness", lambda: "unknown")
    assert run(_read()).json() == {"status": "unknown"}
