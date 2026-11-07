# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed runtime air-gap toggle (NE-AIRGAP-TOGGLE) — C2's on/off switch.

SPEC §11 ruling 4 orders the Models surface writable: *"including setting
air-gap mode so no external models are allowed"*. This module is the durable
state + the two governed actions behind ``POST /v1/network/air-gap``
(redesign-spec §18 B3's network write).

Design decisions, each load-bearing:

* **One durable row, on the AUDIT engine.** ``ne_egress_posture`` is a
  single-row register of the runtime egress posture (air-gap engaged or not).
  It lives on the SAME engine as ``ne_audit_log`` so a posture change and its
  chain record commit in ONE transaction (``ne.audit.store.append_record`` is
  documented for exactly this co-located atomic write). A flip that cannot be
  recorded on the chain does not happen; a flip that happens is on the chain
  (C10). Both process roots (ne-api + ne-worker) share the audit database, so
  the toggle reaches EVERY gateway's guard, not just the process that served
  the HTTP request.

* **Tier asymmetry, derived from the normative policy, not invented here.**
  The two directions are two DIFFERENT actions, named so ``tiers.rego``
  classifies them (deny-by-default; the Python mirror
  ``ne.runtime.actions.tier_for_action`` floors registration):

  - ``write.internal.network.egress.restrict`` (engage air gap) — Tier 1: a
    low-risk internal write. It can only REDUCE what leaves the deployment, is
    reversible only through the governed loosen path, and SPEC §4.2 Tier 1
    ("autonomous + daily digest") is exactly that class. It applies
    immediately — the demo beat "flip it on, watch the platform refuse the
    frontier call" is a live, sub-second action.
  - ``write.external.network.egress.expand`` (disengage air gap) — Tier 2: it
    authorizes outbound flows that can carry context off the deployment
    (SPEC C2: "any egress path can carry context"), which is SPEC §4.2's
    "external-facing" class. It parks at the approval gate for a per-item
    human decision and never auto-approves. It is NOT Tier 3: it is neither
    financial nor legal, and the flows it re-enables remain individually
    governed (per-call PDP + allowlist + audit) — the flip itself is the
    reversible act of re-opening a governed lane, not an irreversible action.

* **The runtime posture is OUTSIDE the frozen ``config_hash`` — deliberately,
  and that is the pre-existing design, not a shortcut.** ``EgressSettings`` has
  never been part of the composed platform hash (``build_platform`` states
  "It is NOT stamped into extra_modules"), so the posture screen's
  Configuration hash never attested egress state and does not start lying when
  the toggle moves it. The honest timeline of the egress posture is the chain
  itself: every flip appends a ``network.egress.changed`` AuditEvent (actor,
  old→new, via which governed action) in the same transaction as the state
  write, and ``GET /v1/posture`` reports the LIVE posture from the same
  provider the enforcing guard reads (reported == enforced, posture §0 law 5).
  Pinned by ``test_network_airgap.py::test_flip_does_not_move_config_hash``.

* **Fail-closed everywhere.** The settings provider returns the
  most-restrictive posture (air gap engaged, empty frontier allowlist) when the
  store cannot be read; an apply whose chain append fails rolls back the state
  write (one transaction); the bare-global ``AIR_GAP`` env flag still ORs in at
  the guard and at posture, so the runtime toggle can never loosen a sealed
  deployment.

* **Durability limit, stated plainly (the NE-WORKFLOWS precedent verbatim).**
  A parked Tier-2 loosen is applied by the in-process waiter that submitted it
  (``GovernedToolExecutor.execute`` blocks on the gate and falls through to the
  handler on grant). The approval ROW is durable; the waiter is not. If the
  process restarts while the loosen is parked, approving the row records the
  grant but nothing applies it — the deployment stays in the MORE restrictive
  state (the fail-closed direction) and the requester re-submits, reaching the
  same approver with a fresh row. A durable re-driver is the same filed
  follow-up NE-WORKFLOWS carries.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.audit.store import append_record
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.events import new_audit_event
from ne.gateway.egress import EgressSettings
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.tools import ToolBinding
from ne.schema import AuditOutcome

__all__ = [
    "EGRESS_EXPAND_ACTION",
    "EGRESS_RESTRICT_ACTION",
    "NETWORK_EGRESS_ARG_POLICY",
    "NETWORK_EGRESS_CHANGED_ACTION",
    "EgressPostureState",
    "EgressPostureStore",
    "FrontierRoutedTasksError",
    "build_egress_settings_provider",
    "build_network_egress_bindings",
    "ensure_egress_posture_schema",
]

_LOG = logging.getLogger(__name__)

#: Engage air gap (tighten). ``write.internal.`` ⇒ tiers.rego Tier 1: applies
#: immediately, autonomous + logged (surfaced in the daily digest obligation).
EGRESS_RESTRICT_ACTION = "write.internal.network.egress.restrict"

#: Disengage air gap (loosen). ``write.external.`` ⇒ tiers.rego Tier 2: parks
#: for a per-item human approval; never auto-approves.
EGRESS_EXPAND_ACTION = "write.external.network.egress.expand"

#: The chain record every applied flip appends (same transaction as the state
#: write — C10 "the flip must be on the chain").
NETWORK_EGRESS_CHANGED_ACTION = "network.egress.changed"


class FrontierRoutedTasksError(ValueError):
    """Engaging the air gap is refused while task PRIMARIES route to frontier
    models — carried as a TYPED error naming the offending routes so the HTTP
    surface can answer a legible 409 instead of the generic wrapped-500 every
    other handler failure gets (``routers/network.py``). ``routes`` holds
    ``"task -> model"`` strings, sorted, exactly as the guard computed them."""

    def __init__(self, routes: tuple[str, ...]) -> None:
        self.routes = routes
        super().__init__(
            "air gap cannot engage while task(s) route to frontier models: "
            + ", ".join(routes)
            + "; repoint them in-VPC first"
        )


_metadata = sa.MetaData()

#: Single-row runtime egress-posture register (id is always 1). On the AUDIT
#: engine so apply() is atomic with its chain record.
ne_egress_posture = sa.Table(
    "ne_egress_posture",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("air_gap", sa.Boolean, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("changed_by", sa.String, nullable=False),
    sa.Column("changed_via", sa.String, nullable=False),
    sa.CheckConstraint("id = 1", name="ck_egress_posture_singleton"),
)


def ensure_egress_posture_schema(engine: sa.Engine) -> None:
    """Create the egress-posture table if absent (idempotent, boot-time)."""
    _metadata.create_all(engine, tables=[ne_egress_posture])


@dataclass(frozen=True)
class EgressPostureState:
    """The runtime posture row, or its boot-derived default when no row exists."""

    air_gap: bool
    version: int
    changed_at: datetime | None
    changed_by: str | None


class _NetworkEgressArgs(BaseModel):
    """Typed input for BOTH egress actions (reject-by-default, C12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    air_gap: bool


class EgressPostureStore:
    """Read/apply surface for the ONE runtime egress-posture row.

    ``boot`` is the boot-frozen :class:`EgressSettings` instance the platform
    composed (the same one the guard historically enforced): it supplies the
    default posture before any governed flip exists, and every effective
    settings object is DERIVED from it via ``model_copy`` — never a re-read of
    env / ``ne.toml`` (the posture module's law).
    """

    def __init__(
        self,
        engine: sa.Engine,
        boot: EgressSettings,
        frontier_route_provider: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._engine = engine
        self._boot = boot
        self._frontier_route_provider = frontier_route_provider

    @property
    def boot_settings(self) -> EgressSettings:
        return self._boot

    def read_state(self) -> EgressPostureState:
        """The current posture (SELECT of the singleton row; boot default when
        absent). Raises on a store failure — fail-closed handling is the
        PROVIDER's job (:func:`build_egress_settings_provider`)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_egress_posture.c.air_gap,
                    ne_egress_posture.c.version,
                    ne_egress_posture.c.changed_at,
                    ne_egress_posture.c.changed_by,
                ).where(ne_egress_posture.c.id == 1)
            ).first()
        if row is None:
            return EgressPostureState(
                air_gap=self._boot.air_gap, version=0, changed_at=None, changed_by=None
            )
        return EgressPostureState(
            air_gap=bool(row.air_gap),
            version=int(row.version),
            changed_at=row.changed_at,
            changed_by=str(row.changed_by),
        )

    def effective_settings(self) -> EgressSettings:
        """The settings in force: boot settings with the runtime posture applied."""
        state = self.read_state()
        if state.air_gap == self._boot.air_gap:
            return self._boot
        return self._boot.model_copy(update={"air_gap": state.air_gap})

    def apply(self, *, air_gap: bool, actor: str, via_action: str) -> str:
        """Apply a flip: state write + ``network.egress.changed`` chain record,
        ONE transaction (C10 — a flip that cannot be recorded does not happen).

        Idempotent: applying the posture already in force writes nothing and
        appends nothing (an approved loosen that another grant already applied
        reports "already in effect" rather than double-recording). Returns a
        JSON summary string (the governed tool-handler contract).
        """
        if air_gap and self._frontier_route_provider is not None:
            try:
                frontier_routed = tuple(self._frontier_route_provider())
            except Exception as exc:
                raise RuntimeError(
                    "model routes could not be checked; refusing to engage air gap"
                ) from exc
            if frontier_routed:
                # Typed + named (NE-AIRGAP-NO-APPROVAL): the refusal used to be a
                # bare ValueError the executor wrapped into a generic 500, so the
                # one failure an operator can actually FIX on the spot surfaced
                # with no clue which route was to blame. FrontierRoutedTasksError
                # is a ValueError subclass, so any caller matching the old
                # contract still matches.
                raise FrontierRoutedTasksError(frontier_routed)
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_egress_posture.c.air_gap, ne_egress_posture.c.version)
                .where(ne_egress_posture.c.id == 1)
                .with_for_update()
            ).first()
            old = bool(row.air_gap) if row is not None else self._boot.air_gap
            if old == air_gap:
                return json.dumps(
                    {"applied": False, "air_gap": air_gap, "detail": "already in effect"}
                )
            if row is None:
                conn.execute(
                    ne_egress_posture.insert().values(
                        id=1,
                        air_gap=air_gap,
                        version=1,
                        changed_at=now,
                        changed_by=actor,
                        changed_via=via_action,
                    )
                )
            else:
                conn.execute(
                    ne_egress_posture.update()
                    .where(
                        ne_egress_posture.c.id == 1,
                        ne_egress_posture.c.version == int(row.version),
                    )
                    .values(
                        air_gap=air_gap,
                        version=int(row.version) + 1,
                        changed_at=now,
                        changed_by=actor,
                        changed_via=via_action,
                    )
                )
            record = append_record(
                conn,
                new_audit_event(
                    action=NETWORK_EGRESS_CHANGED_ACTION,
                    resource="network:egress",
                    outcome=AuditOutcome.success,
                    actor=actor,
                    detail=json.dumps({"air_gap": {"old": old, "new": air_gap}}),
                    provenance={"changed_via": via_action},
                    occurred_at=now,
                ),
            )
        return json.dumps({"applied": True, "air_gap": air_gap, "audit_seq": record.seq})


def build_egress_settings_provider(store: EgressPostureStore) -> Callable[[], EgressSettings]:
    """The per-call settings provider for :func:`ne.gateway.egress.build_egress_guard`.

    FAIL-CLOSED: when the posture row cannot be read (store/database failure),
    the provider returns the MOST RESTRICTIVE posture — air gap engaged, no
    frontier hosts — derived from the boot instance. The frontier lane then
    refuses (``AirGapViolationError``) until the store is readable again; the
    in-VPC private lane stays alive (air-gap semantics, SPEC C2). It never
    raises and never fails open.
    """

    def _current() -> EgressSettings:
        try:
            return store.effective_settings()
        except Exception:
            _LOG.warning(
                "egress posture store unreadable; failing CLOSED (air gap engaged)",
                exc_info=True,
            )
            return store.boot_settings.model_copy(
                update={"air_gap": True, "allowed_frontier_hosts": ()}
            )

    return _current


def build_network_egress_bindings(
    store: EgressPostureStore,
    principal_provider: Callable[[], Principal | None],
) -> tuple[ToolBinding, ...]:
    """The two governed egress actions, as plain :class:`ToolBinding`s.

    ``principal_provider`` is the executor's sanctioned ambient carrier
    (``ne.runtime.middleware.current_tool_principal`` — set by ``_run_handler``
    from its VERIFIED principal argument), injected per the skill-bindings
    discipline rather than imported here. The handler refuses (fail closed)
    when no principal is bound: the chain record's ``actor`` must be the real
    requester, never a placeholder.

    Each handler enforces its own DIRECTION: the restrict action only ever
    engages the air gap, the expand action only ever disengages it. A caller
    that smuggles the opposite target through the "cheaper" action is refused
    before any state is touched — the tier a change gets derives from what the
    change DOES, not from which endpoint the caller picked.
    """
    import functools

    import anyio.to_thread

    def _make_handler(action_name: str, *, expected_air_gap: bool):
        async def _handler(args: Mapping[str, object]) -> str:
            parsed = _NetworkEgressArgs.model_validate(dict(args))
            if parsed.air_gap is not expected_air_gap:
                raise ValueError(
                    f"{action_name} only ever sets air_gap={expected_air_gap}; "
                    f"got air_gap={parsed.air_gap} (direction mismatch, refused)"
                )
            principal = principal_provider()
            if principal is None:
                raise RuntimeError(
                    "no verified principal bound to this governed call; refusing to "
                    "change the egress posture (fail closed)"
                )
            return await anyio.to_thread.run_sync(
                functools.partial(
                    store.apply,
                    air_gap=parsed.air_gap,
                    actor=principal.id,
                    via_action=action_name,
                )
            )

        return _handler

    def _validate(args: Mapping[str, object]) -> None:
        _NetworkEgressArgs.model_validate(dict(args))

    def _audit_detail(args: Mapping[str, object], result: str) -> Mapping[str, object]:
        # Names/shapes only (C12): the target posture and whether it applied.
        out: dict[str, object] = {"air_gap_target": bool(args.get("air_gap"))}
        try:
            out["applied"] = bool(json.loads(result).get("applied"))
        except Exception:  # pragma: no cover - defensive; result is our own JSON
            out["applied_parse_error"] = True
        return out

    input_schema = {
        "type": "object",
        "properties": {"air_gap": {"type": "boolean"}},
        "required": ["air_gap"],
    }
    restrict = ToolBinding(
        spec=ToolSpec(
            name=EGRESS_RESTRICT_ACTION,
            description=(
                "Engage air-gap mode: frontier egress stops on the next model call. "
                "Applies immediately (Tier 1, logged on the audit chain)."
            ),
            tier=ApprovalTier.TIER_1,
            input_schema=input_schema,
        ),
        handler=_make_handler(EGRESS_RESTRICT_ACTION, expected_air_gap=True),
        validate_args=_validate,
        extract_audit_detail=_audit_detail,
    )
    expand = ToolBinding(
        spec=ToolSpec(
            name=EGRESS_EXPAND_ACTION,
            description=(
                "Disengage air-gap mode: re-opens the governed frontier egress lane. "
                "Requires per-item human approval (Tier 2)."
            ),
            tier=ApprovalTier.TIER_2,
            input_schema=input_schema,
        ),
        handler=_make_handler(EGRESS_EXPAND_ACTION, expected_air_gap=False),
        validate_args=_validate,
        extract_audit_detail=_audit_detail,
    )
    return (restrict, expand)


#: The reject-by-default C12 arg policy BOTH egress actions share; merged into
#: ``guardrails.tools`` unconditionally pre-freeze (the ``read.trace`` /
#: ``credential_rotation`` always-on pattern in ``platform._merge_tool_policy``).
NETWORK_EGRESS_ARG_POLICY = ToolArgPolicy(
    args={"air_gap": ArgSpec(kind=ToolArgKind.boolean, required=True)}
)
