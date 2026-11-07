# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sovereignty-posture route (NE-131) — GET /v1/posture.

Surfaces two distinct evidence classes: the C1-C12 BUILD-TIME attestation, and
runtime deployment readings (the EFFECTIVE AIR_GAP + sanctioned-egress state,
composed model-registry lanes, and frozen config_hash + its audit-derived
history). The static constraint rows must never be described as live operational
health merely because they share this endpoint with runtime data.

ACCESS DECISION (documented): ANY authenticated principal. The rail renders these
lamps on every authed view for every role (design §1.6), and the payload carries
deployment-level facts ONLY — no user data, no ACLs, no chunk content, no secrets.
This mirrors the kpi.py precedent (aggregates carry no user data). Read-only: no
tool execution, no write path, request has no body (C12).

The build-time attestation is REAL (§0 law 5): ``constraints`` is copied verbatim
from the drift-guarded ``ne.api.posture.CONSTRAINT_ATTESTATIONS`` (each
``attested`` flag mirrors the readiness harness at build time; a guardian-less
constraint would render honestly manual / unattested). It is not a runtime probe.
``config_hash`` is the process's frozen hash, and the history comes from the real
audit store. NOTE: the model-registry license/origin tags are NOT
composed into ne-api, so model lanes carry the gateway's own five fields
(``model_id``/``provider``/``lane``/``configured_model``/``context_window``) and
nothing else — tags are omitted honestly rather than fabricated. All five are
CONFIGURATION reads; none of them is an observation that the lane is serving
(that is GET /v1/models/serving, and only for probeable private lanes).

BOOT-FROZEN EGRESS (NE-UI-P2-CLEANUP): /v1/posture reports the SAME
``EgressSettings`` instance ``build_platform`` used to construct the live gateway
guard (``build_egress_guard(egress_settings)``), threaded through ``ApiState``.
After a post-boot env / ``ne.toml`` change the endpoint keeps reporting what the
running gateway actually enforces (matching the frozen ``config_hash`` semantics —
a change lands only after a restart), so REPORTED == ENFORCED (§0 law 5). The
AIR_GAP bare-global remains a live per-call read because the enforced guard
(``EgressPolicy._check_frontier``) consults ``air_gap_engaged()`` per call too. A
malformed ``NE_EGRESS_*`` now fails LOUD at boot in ALL compositions (the gateway
seam constructs ``EgressSettings`` unconditionally), never as a request-time error.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.network_egress import EGRESS_EXPAND_ACTION, EGRESS_RESTRICT_ACTION
from ne.api.posture import (
    ATTESTATION_SOURCE,
    CONSTRAINT_ATTESTATIONS,
    config_hash_history,
    effective_egress,
)
from ne.api.serving import expected_served_model
from ne.api.wire import (
    AirGapPendingView,
    ConfigHashSpanView,
    ModelLaneView,
    PostureConstraintView,
    PostureView,
)
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


@router.get("/v1/posture")
async def posture(
    principal: _Principal,
    state: _State,
) -> PostureView:
    # (a) EFFECTIVE AIR_GAP + sanctioned frontier allowlist — reported from the SAME
    # per-call provider the live C2 guard enforces (NE-AIRGAP-TOGGLE): the boot-frozen
    # EgressSettings overlaid with the durable governed posture register, fail-closed.
    # A governed flip is therefore visible here on the very next read — never a cached
    # copy (§0 law 5 reported==enforced). The boot-frozen instance remains the
    # fallback for compositions that predate the provider.
    provider = state.egress_settings_provider
    if provider is not None:
        settings = provider()
    else:
        settings = state.egress_settings
        if settings is None:
            raise RuntimeError(
                "ApiState.egress_settings was not threaded from build_platform — posture "
                "must report the ENFORCED boot-frozen egress policy, never a re-read "
                "(NE-UI-P2-CLEANUP)"
            )
    air_gap, allowlist = effective_egress(settings)
    # This is the time the effective enforcement-state read above actually
    # completed. It is not a health-probe result and must never be minted by a
    # browser from fetch completion.
    network_observed_at = datetime.now(UTC)

    # (a2) The §2.5 pending-change read-back (NE-AIRGAP-TOGGLE): a parked governed
    # air-gap change renders on the object it targets. Ids only; the displayed
    # posture stays the in-force one.
    air_gap_pending: AirGapPendingView | None = None
    pending_rows = [
        row
        for row in await state.gate.list_pending()
        if row.action in (EGRESS_RESTRICT_ACTION, EGRESS_EXPAND_ACTION)
    ]
    if pending_rows:
        first = min(pending_rows, key=lambda r: r.requested_at)
        air_gap_pending = AirGapPendingView(
            request_id=first.request_id, requested_by=first.principal_id
        )

    # (b) Composed model lanes — read from the frozen GatewaySettings as opaque
    # data (no provider SDK import, C3). None ⇒ honest empty (license/origin tags
    # are not composed into ne-api and are omitted rather than fabricated).
    #
    # WHAT A LANE RESOLVES TO IS PART OF THE LANE. `model_id` is the routing
    # ALIAS, and an alias alone cannot answer "what is `frontier-default`?" —
    # the question an operator asked of a screen that could not answer it. The
    # served model and its declared window are carried for EVERY lane, on the
    # same three-field read, so the frontier lanes are not a special case bolted
    # beside the private ones.
    #
    # STILL CONFIGURATION (the LaneServingView boundary): these are reads of the
    # frozen settings, never a probe. `expected_served_model` is reused rather
    # than re-derived so the two surfaces cannot spell one lane's model two
    # ways. Nothing here is contacted, so nothing here ages.
    model_lanes: list[ModelLaneView] = []
    if state.gateway_settings is not None:
        model_lanes = [
            ModelLaneView(
                model_id=m.name,
                provider=m.provider,
                lane=m.lane.value,
                configured_model=expected_served_model(m.litellm_model),
                context_window=m.context_window,
            )
            for m in state.gateway_settings.models
        ]

    # (c) The real frozen config_hash (§0 law 5) + (d) its audit-derived history,
    # read in ONE threadpool hop so the event loop never blocks (audit.py pattern).
    history = await anyio.to_thread.run_sync(
        functools.partial(config_hash_history, state.audit_engine, state.config_hash)
    )
    # Date the audit-backed read after the store has answered. It is separate
    # from the network reading because the two observations are formed at
    # different seams and must age independently if a response is cached.
    configuration_observed_at = datetime.now(UTC)

    # (e) The C1-C12 BUILD-TIME attestation, verbatim from the drift-guarded
    # readiness table. Its mode remains explicit on the wire so consumers do not
    # mistake a passing guardian for current runtime health.
    constraints = [
        PostureConstraintView(
            id=c.id,
            short=c.short,
            enforcement=c.enforcement,
            attested=c.attested,
            mode=c.mode,
            evidence=list(c.evidence),
        )
        for c in CONSTRAINT_ATTESTATIONS
    ]

    return PostureView(
        constraints=constraints,
        air_gap=air_gap,
        egress_allowlist=allowlist,
        network_observed_at=network_observed_at,
        model_lanes=model_lanes,
        config_hash=state.config_hash,
        config_hash_history=[
            ConfigHashSpanView(hash=h, from_=frm, to=to) for h, frm, to in history
        ],
        configuration_observed_at=configuration_observed_at,
        attestation_source=ATTESTATION_SOURCE,
        air_gap_pending=air_gap_pending,
    )
