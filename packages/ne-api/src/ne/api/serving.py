# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Live serving-status probe for the private model lanes (NE-SERVING-STATUS).

The console's Models screen claims this deployment routes inference to a
privately-served model. Until this module, that claim rendered as a *label* —
``posture.model_lanes[].lane == "private-vllm"`` is the same string the config
already asserted. This module turns it into a MEASUREMENT: an HTTP observation
of the OpenAI-compatible serving endpoint the gateway is configured to call,
answering (a) is it reachable NOW, (b) what model does IT say it is serving,
(c) what serving window / engine version does it report — each fact observed,
none echoed from config.

WHAT IS PROBED, AND WHY THAT IS NOT NEW EGRESS (C2). Only non-frontier lanes
with a configured ``api_base`` are probed — the EXACT destination the gateway
already sends whole prompts to on every private-lane call. Frontier lanes are
NEVER probed from ne-api: a probe to a provider host would be a second,
ungoverned egress path, and their liveness is not this surface's claim. Before
a byte is sent, the endpoint passes the SAME Phase-A SSRF shape gate the
gateway lane applies (:func:`ne.net.ssrf.validate_endpoint_url`,
``allow_private=True`` — private/in-VPC hosts are the POINT of this lane;
``require_https=False`` — in-VPC vLLM is plain http by deployment default).
A rejected shape (metadata host, embedded credentials, query string, packed
IPv4, link-local…) is state ``not_probed``, fail-closed, and the refused URL
is never contacted and never echoed.

REDACTION RULING (C12), stated for review: the response identifies the probed
endpoint as ``scheme://host[:port]`` ONLY, rebuilt from the PARSED URL — the
raw config string is never serialized, so embedded userinfo or a query-string
token structurally cannot reach the wire. A private IP or internal DNS name IS
served: the reader of this ops-gated surface is the operator whose deployment
config already names it, and "where does inference run" is the fact the
surface exists to prove. ``ModelDef.extra_params`` (``api_key`` lives there)
is never read.

ABSENT, STALE AND UNREACHABLE ARE THREE DIFFERENT THINGS:

* a lane the deployment does not carry (no ``api_base``, or frontier) yields
  NO observation row at all — never a fabricated one;
* ``checked_at`` is the moment the served verdict was actually formed — a
  cache hit (TTL ``cache_ttl_s``) serves the OLD ``checked_at``, so a cached
  success renders with its age, never as current truth;
* ``last_ok_at`` survives failures: an endpoint that just refused a
  connection but answered five minutes ago is distinguishable from one that
  has never answered (``last_ok_at is None``).

The probe is BOUNDED (``timeout_s``): a wedged GPU endpoint yields state
``timeout`` within the budget instead of hanging the read; the route stays
answerable because expiry is itself a displayable observation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from ne.api.wire import LaneServingView, ServingState
from ne.contracts import Lane
from ne.gateway.config import ModelDef
from ne.net.ssrf import validate_endpoint_url

#: One probe's total HTTP budget, seconds. Small enough that a wedged endpoint
#: cannot make the ops read feel hung; large enough for a cold remote GPU box
#: on a WAN link (the demo rig is NOT a loopback container).
DEFAULT_TIMEOUT_S = 2.5

#: How long a formed verdict is served again without re-contacting the
#: endpoint. Coalesces concurrent console loads; the served ``checked_at``
#: stays the OBSERVATION time, so the reuse is visible as age, never hidden.
DEFAULT_CACHE_TTL_S = 5.0


class _ShapeRefused(ValueError):
    """The endpoint failed the Phase-A SSRF shape gate — never contacted."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def endpoint_identity(api_base: str) -> str:
    """``scheme://host[:port]`` from the PARSED URL — the only endpoint string
    this module ever serves. Path, query, fragment and userinfo are dropped by
    construction (C12: a credential in the config string cannot reach the
    wire because the raw string is never used). Empty when no host parses."""
    parsed = urlsplit(api_base)
    host = parsed.hostname
    if not parsed.scheme or not host:
        return ""
    if ":" in host:  # IPv6 literal re-bracketed for display
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


def expected_served_model(litellm_model: str) -> str:
    """The model id the endpoint is EXPECTED to report: the litellm target
    with its provider prefix stripped (``hosted_vllm/org/name`` → ``org/name``
    — vLLM serves the HF id, which itself may contain ``/``). A target with no
    prefix is expected verbatim."""
    _prefix, sep, rest = litellm_model.partition("/")
    return rest if sep else litellm_model


def _models_url(api_base: str) -> str:
    """The OpenAI-compatible model listing for a gateway ``api_base``.

    Gateway config carries the litellm convention (``…/v1``); ne-serve carries
    the bare origin. Derive rather than assume: a base already ending ``/v1``
    gets ``/models``, anything else gets ``/v1/models``."""
    base = api_base.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


@dataclass(frozen=True)
class _CacheEntry:
    view: LaneServingView
    formed_at: datetime


class ServingStatusService:
    """Forms and caches per-lane serving observations (module docstring rules).

    ``transport`` (tests: ``httpx.MockTransport``) fakes only the network edge;
    ``clock`` is injectable so staleness is provable without sleeping.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._last_ok_at: dict[str, datetime] = {}
        # Request-order authority for each lane. A newer forced probe may
        # finish before an older slow probe; only the latest-started generation
        # may publish cache/history state when it returns.
        self._probe_generation: dict[str, int] = {}

    @staticmethod
    def probeable(models: Sequence[ModelDef]) -> list[ModelDef]:
        """The lanes this surface observes: non-frontier AND an ``api_base``.

        A frontier lane is never probed (C2 — see module docstring); a lane
        with no endpoint has nothing to observe and yields NO row."""
        return [m for m in models if m.lane is not Lane.FRONTIER_API and m.api_base]

    async def observe(
        self,
        models: Sequence[ModelDef],
        *,
        force_refresh: bool = False,
    ) -> list[LaneServingView]:
        """Observe each probeable lane in config order.

        Ordinary display reads reuse the bounded cache.  An authorization
        authority passes ``force_refresh=True`` so a prior UI/preflight read
        can never lend it evidence that expires while the decision is forming.
        Every real probe has one total ``timeout_s`` budget across both the
        authoritative model-list read and best-effort version enrichment.
        """
        lanes = self.probeable(models)
        results: list[LaneServingView | None] = [None] * len(lanes)

        async def _one(index: int, model: ModelDef) -> None:
            results[index] = await self._observe_lane(model, force_refresh=force_refresh)

        async with anyio.create_task_group() as tg:
            for i, m in enumerate(lanes):
                tg.start_soon(_one, i, m)
        return [view for view in results if view is not None]

    async def _observe_lane(
        self,
        model: ModelDef,
        *,
        force_refresh: bool,
    ) -> LaneServingView:
        api_base = model.api_base or ""
        key = f"{model.name} {api_base}"
        now = self._clock()

        cached = self._cache.get(key)
        if (
            not force_refresh
            and cached is not None
            and (now - cached.formed_at).total_seconds() < self._cache_ttl_s
        ):
            return cached.view

        generation = self._probe_generation.get(key, 0) + 1
        self._probe_generation[key] = generation
        try:
            validate_endpoint_url(
                api_base,
                require_https=False,
                allow_private=True,
                error_cls=_ShapeRefused,
            )
        except _ShapeRefused:
            # Fail-closed: never contacted, and served WITHOUT a checked_at —
            # there was no observation to date.
            return self._finish(
                key,
                generation,
                self._clock(),
                self._base_view(model, state="not_probed", checked_at=None),
            )

        view = await self._probe(model)
        # Cache age starts only after the complete verdict is ready to return.
        # A slow best-effort /version read therefore cannot consume the cache's
        # lifetime before another caller has even received the observation.
        return self._finish(key, generation, self._clock(), view)

    def _finish(
        self,
        key: str,
        generation: int,
        formed_at: datetime,
        view: LaneServingView,
    ) -> LaneServingView:
        if self._probe_generation.get(key) != generation:
            # This caller still receives its own measured result, but a probe
            # superseded in request order cannot roll shared truth backward.
            return view.model_copy(update={"last_ok_at": self._last_ok_at.get(key)})
        if view.state == "ok":
            self._last_ok_at[key] = view.checked_at or formed_at
        # last_ok_at is attached AFTER the ok-bookkeeping so a fresh success
        # reports itself as its own last confirmation.
        view = view.model_copy(update={"last_ok_at": self._last_ok_at.get(key)})
        self._cache[key] = _CacheEntry(view=view, formed_at=formed_at)
        return view

    def _base_view(
        self,
        model: ModelDef,
        *,
        state: ServingState,
        checked_at: datetime | None,
        latency_ms: int | None = None,
        http_status: int | None = None,
        served_models: Sequence[str] = (),
        serves_configured_model: bool | None = None,
        observed_context_window: int | None = None,
        engine_version: str | None = None,
    ) -> LaneServingView:
        return LaneServingView(
            model_id=model.name,
            lane=model.lane.value,
            endpoint=endpoint_identity(model.api_base or ""),
            configured_model=expected_served_model(model.litellm_model),
            state=state,
            checked_at=checked_at,
            latency_ms=latency_ms,
            http_status=http_status,
            served_models=list(served_models),
            serves_configured_model=serves_configured_model,
            observed_context_window=observed_context_window,
            engine_version=engine_version,
            last_ok_at=None,
        )

    async def _probe(self, model: ModelDef) -> LaneServingView:
        authoritative: LaneServingView | None = None
        try:
            with anyio.fail_after(self._timeout_s):
                api_base = model.api_base or ""
                timeout = httpx.Timeout(self._timeout_s)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    transport=self._transport,
                ) as client:
                    started = time.monotonic()
                    try:
                        response = await client.get(_models_url(api_base))
                    except httpx.TimeoutException:
                        return self._base_view(
                            model,
                            state="timeout",
                            checked_at=self._clock(),
                        )
                    except httpx.HTTPError:
                        return self._base_view(
                            model,
                            state="unreachable",
                            checked_at=self._clock(),
                        )
                    # This is the authoritative serving observation time:
                    # immediately after /models completes, before optional
                    # /version enrichment.
                    checked_at = self._clock()
                    latency_ms = int((time.monotonic() - started) * 1000)

                    if response.status_code != 200:
                        return self._base_view(
                            model,
                            state="http_error",
                            checked_at=checked_at,
                            latency_ms=latency_ms,
                            http_status=response.status_code,
                        )

                    served = _parse_served_models(response)
                    if served is None:
                        # Something answered 200, but not an OpenAI-compatible
                        # model listing — a proxy or a wrong service, not a
                        # serving model.
                        return self._base_view(
                            model,
                            state="malformed",
                            checked_at=checked_at,
                            latency_ms=latency_ms,
                            http_status=200,
                        )
                    served_ids, window_by_id = served

                    expected = expected_served_model(model.litellm_model)
                    matches = expected in served_ids or model.litellm_model in served_ids
                    matched_id = (
                        expected
                        if expected in window_by_id
                        else (model.litellm_model if model.litellm_model in window_by_id else None)
                    )
                    window = (
                        window_by_id.get(matched_id)
                        if matched_id is not None
                        else next(iter(window_by_id.values()), None)
                    )

                    # /models alone owns the serving verdict. Record it before
                    # best-effort enrichment so the SAME total deadline can
                    # expire during /version without relabelling a healthy lane
                    # as timeout.
                    authoritative = self._base_view(
                        model,
                        state="ok",
                        checked_at=checked_at,
                        latency_ms=latency_ms,
                        http_status=200,
                        served_models=served_ids,
                        serves_configured_model=matches,
                        observed_context_window=window,
                    )
                    version = await _engine_version(client, api_base)
                    return authoritative.model_copy(update={"engine_version": version})
        except TimeoutError:
            if authoritative is not None:
                return authoritative
            return self._base_view(model, state="timeout", checked_at=self._clock())


def _parse_served_models(
    response: httpx.Response,
) -> tuple[list[str], dict[str, int | None]] | None:
    """``data[].id`` (+ vLLM's ``max_model_len`` when present) from an
    OpenAI-compatible ``/v1/models`` body; ``None`` when the body is not that
    shape. Only the enumerated keys are read — nothing else the endpoint sends
    can reach the wire."""
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    ids: list[str] = []
    windows: dict[str, int | None] = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            return None
        model_id = entry["id"]
        ids.append(model_id)
        raw_window = entry.get("max_model_len")
        windows[model_id] = raw_window if isinstance(raw_window, int) else None
    return ids, windows


async def _engine_version(client: httpx.AsyncClient, api_base: str) -> str | None:
    """Best-effort ``GET {origin}/version`` (a vLLM extension). Absent, slow,
    non-200 or non-``{"version": str}`` all yield ``None`` — an unobserved
    fact renders as nothing, never as a guess."""
    origin = endpoint_identity(api_base)
    if not origin:
        return None
    try:
        response = await client.get(f"{origin}/version")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("version"), str):
        return payload["version"]
    return None
