# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed runtime model route (NE-MODEL-ROUTING) — which model serves a task.

Before this module, "which model answers a turn" resolved entirely through boot
config: ``RuntimeSettings.task`` → ``GatewaySettings.tasks[task].model`` →
``ModelDef`` → litellm. Changing it meant editing compose and restarting, which
drops every in-flight session. This module is the durable state + the two
governed actions that make it a live, audited runtime change.

Design decisions, each load-bearing:

* **An override names a MODEL, never an endpoint.** A row is ``(task, model)``.
  ``model`` MUST resolve in the BOOT-composed ``GatewaySettings.models`` — the
  same set the litellm ``Router`` was constructed from, every entry SSRF-validated
  and lane-classified at boot. So a routing change CANNOT introduce a network
  destination that did not already exist at boot; it re-selects among endpoints
  the deployment already sanctioned. Enforced here at apply
  (:func:`refusal_for_target`) AND independently at call time
  (``LiteLLMGateway._candidates`` raises ``UnknownRoutedModelError``) AND,
  regardless of both, by the per-attempt ``egress_guard`` that still runs on the
  ``ModelDef`` actually selected.

* **One durable row per task, on the AUDIT engine.** ``ne_model_route_overrides``
  is a versioned per-task register beside ``ne_audit_log``, so a route change and
  its chain record commit in ONE transaction (``ne.audit.store.append_record`` is
  documented for exactly this co-located atomic write). A change that cannot be
  recorded does not happen (C10). Both process roots share the audit database, so
  the change reaches EVERY gateway, not just the process that served the request.
  The route's HISTORY is the chain, not this table — the table is state.

* **LIVE, not restart-to-activate — and the tier precedent does not transfer.**
  ``ne.api.action_tiers`` freezes an applied tier until restart because a tier is
  the grading rule applied to an APPROVAL ROW: re-tiering live would retroactively
  change the meaning of a row a human is already looking at. A route has no such
  in-flight artifact — it is consulted at exactly one instant, ``_candidates`` at
  the top of ``complete()``, and ``GatewayModel`` is already constructed fresh
  inside the turn loop. There is nothing for a freeze to protect, and a swap that
  needed a restart would be the very "config change plus a restart" this unit
  exists to remove. The MECHANISM is not new: it is NE-AIRGAP-TOGGLE's verbatim
  (durable register + per-call provider + atomic chain record), so no second
  governance engine is created.

* **The runtime route is OUTSIDE the frozen ``config_hash``** — deliberately, the
  same decision and the same reason as the egress posture. A hash that moved under
  a running process would be worse than one that never claimed to cover runtime
  state. The honest per-turn answer to "which model served this" is the gateway's
  own per-call ``CostEvent`` + chained ``AuditEvent``, which already name the
  selected model, lane and provider, joined to the turn by ``trace_id``.

* **The tier derives from what the change DOES.** Two actions, named so
  ``tiers.rego`` classifies them; each handler enforces its own direction, so a
  caller cannot smuggle a frontier target through the cheaper name.

  - ``write.internal.model.routing.repoint`` (target on the in-VPC private-vllm
    lane) — rego class Tier 1, DECLARED **Tier 2**. The escalation is deliberate:
    class-1 would mean "autonomous + daily digest", but repointing changes which
    model answers EVERY subsequent turn on that task and no other human is in that
    loop. A declared tier may only escalate, so this needs no policy-bundle edit.
  - ``write.external.model.routing.repoint`` (target on the frontier-api lane) —
    Tier 2. It directs customer context to an external provider (C2: any egress
    path can carry context), SPEC §4.2's external-facing class. Not Tier 3: every
    call it enables stays individually governed (per-call PDP, egress allowlist,
    redaction, audit) and the change is reversed by repointing back — the same
    reasoning ``write.external.network.egress.expand`` records.

* **Separation of duties, defence in depth here.** Tier 2 means one named
  approval. The gate used to grant on ANY single named approver (only the
  Tier-3 SECOND approver was checked for distinctness), which made these
  handlers the ONLY thing refusing a self-approved route change. The rule now
  lives on the decide path — ``ne.runtime.approvals.refuse_self_approval``,
  applied by ``DurableApprovalGate.approve`` and on both branches of
  ``POST /v1/approvals/{id}/approve`` — so a requester never obtains a grant to
  spend. Both handlers KEEP their own re-check through the sanctioned
  ``approval_provider`` seam (``current_tool_approval`` — the
  NE-CRM-NOTE-PROVENANCE precedent) against ``current_tool_principal``: an
  approval granted by the requesting principal is REFUSED before any state is
  written, however it reached the handler.

* **Fail-closed everywhere.** The route provider returns the BOOT routes — the
  deployment-sanctioned assignment the frozen ``config_hash`` attests — when the
  store cannot be read; it never raises and never returns a remembered override.
  An apply whose chain append fails rolls back the state write. A handler with no
  bound principal, or no consumed approval, refuses.
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
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.events import new_audit_event
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.errors import ActionValidationError
from ne.runtime.tools import ToolBinding
from ne.schema import AuditOutcome

__all__ = [
    "MODEL_ROUTING_ARG_POLICY",
    "MODEL_ROUTING_CHANGED_ACTION",
    "ROUTE_EXTERNAL_ACTION",
    "ROUTE_INTERNAL_ACTION",
    "ModelRouteState",
    "ModelRouteStore",
    "RouteTargetOption",
    "TaskRoutingRow",
    "action_for_lane",
    "build_model_routing_bindings",
    "build_route_provider",
    "ensure_model_route_schema",
    "ne_model_route_overrides",
    "refusal_for_selection",
    "refusal_for_target",
    "route_action_tier",
    "routing_catalog",
]

_LOG = logging.getLogger(__name__)

#: Repoint a task at an IN-VPC (private-vllm) model. ``write.internal.`` ⇒
#: tiers.rego class 1; DECLARED Tier 2 (escalation — see the module docstring).
ROUTE_INTERNAL_ACTION = "write.internal.model.routing.repoint"

#: Repoint a task at a FRONTIER-API model. ``write.external.`` ⇒ tiers.rego
#: class 2: parks for a per-item named human approval; never auto-approves.
ROUTE_EXTERNAL_ACTION = "write.external.model.routing.repoint"

#: The chain record every applied change appends, in the SAME transaction as the
#: state write (C10 — a change that cannot be recorded does not happen).
MODEL_ROUTING_CHANGED_ACTION = "model.routing.changed"

_metadata = sa.MetaData()

#: One row per gateway task. On the AUDIT engine so ``apply`` is atomic with its
#: chain record. ``version`` is an optimistic-concurrency guard: two racing
#: applies cannot both win.
ne_model_route_overrides = sa.Table(
    "ne_model_route_overrides",
    _metadata,
    sa.Column("task", sa.String, primary_key=True),
    sa.Column("model", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("changed_by", sa.String, nullable=False),
    #: The named human who approved this change. NOT NULL: a route that no human
    #: granted is unrepresentable in this table (the approval_store
    #: CheckConstraint discipline).
    sa.Column("approved_by", sa.String, nullable=False),
    sa.Column("approval_id", sa.String, nullable=True),
    sa.Column("changed_via", sa.String, nullable=False),
)


def ensure_model_route_schema(engine: sa.Engine) -> None:
    """Create the route-override table if absent (idempotent, boot-time)."""
    _metadata.create_all(engine, tables=[ne_model_route_overrides])


@dataclass(frozen=True)
class ModelRouteState:
    """One task's runtime route row."""

    task: str
    model: str
    version: int
    changed_at: datetime
    changed_by: str
    approved_by: str


class _ModelRouteArgs(BaseModel):
    """Typed input for BOTH routing actions (reject-by-default, C12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    model: str


class RouteChangeRefused(ActionValidationError):
    """A routing change the governed path refuses, before any state is written.

    An :class:`~ne.runtime.errors.ActionValidationError` subclass on purpose: the
    DETERMINISTIC half of the rule (task routed, model registered, lane/direction,
    no-op) runs in the binding's ``validate_args`` seam, which the
    executor evaluates AFTER guardrails+PDP but BEFORE the human gate — so a
    change that cannot execute is refused with an audited ``tool.validate[denied]``
    and never consumes a human decision (NE-ACTION-VALIDATE-BEFORE-GATE). The
    approval-dependent half (a bound principal, separation of duties) can only be
    evaluated after the gate and therefore lives in the handler.
    """


# --- the server-authoritative rule ------------------------------------------------


def action_for_lane(lane: Lane) -> str:
    """The governed action name a repoint to this lane must ride.

    The tier a change gets derives from what the change DOES, not from which
    endpoint the caller picked (the ``network_egress`` law). This is the ONE
    mapping; the route derives the action from it, and each handler re-checks its
    own direction so the derivation cannot be bypassed by calling the other name.
    """
    return ROUTE_EXTERNAL_ACTION if lane is Lane.FRONTIER_API else ROUTE_INTERNAL_ACTION


def _snapshot_fit_refusal(
    boot: GatewaySettings,
    task: str,
    target: ModelDef,
    snapshot_tokens: int,
) -> str | None:
    """THE current-conversation-snapshot fit rule (NE-CTX-IN-FORCE round 7).

    SPEC PRE-DEMO-REHEARSAL-014: *"A model whose context window cannot safely
    accept the current snapshot is absent/disabled unless a real governed
    compaction operation exists ... There is no silent truncation, new
    Conversation, cache-reset disguise or fallback."* No governed compaction
    operation exists in this deployment, so the SPEC's remedy is the disable.

    ``snapshot_tokens`` is the estimated size of the governing conversation
    snapshot in the admission estimator's units (run_store's boundary-receipt
    arithmetic: history bytes + input + ``CONTINUITY_PROMPT_RESERVE_TOKENS``,
    bytes upper-bounding tokens) — the SAME durable counts the boundary
    transaction froze, never a second estimator. A per-run SELECTION passes the
    caller's own conversation snapshot; a task REPOINT (which governs every
    conversation on the task and names no single one) passes the LARGEST live
    conversation snapshot for the task (:meth:`ModelRouteStore
    .snapshot_tokens_for`). ``None`` at the callers ⇒ no live conversation in
    hand ⇒ no fit check — never "unknown ⇒ allowed": a caller whose snapshot
    authority FAILS raises out of its own read instead of degrading to None.
    Counts-only (C12): no message or turn text ever rides the refusal.

    The snapshot is a per-CONVERSATION fact that grows per turn. A target that
    fails only this check remains a valid target for a fresh Conversation.
    """
    input_budget = boot.context_budget_for(target).input_budget
    if snapshot_tokens > input_budget:
        return (
            f"model {target.name!r}'s context window cannot safely accept the "
            f"current conversation snapshot: snapshot_tokens={snapshot_tokens} "
            f"input_budget={input_budget}. No governed compaction operation "
            "exists and there is no silent truncation (SPEC PRE-DEMO-014), so "
            "the target is disabled for this conversation"
        )
    return None


def _refusal_for_model(
    boot: GatewaySettings,
    task: str,
    model: str,
    *,
    air_gap: bool,
    current: str | None,
    reject_current: bool,
    snapshot_tokens: int | None = None,
) -> str | None:
    """Shared registry/task/AIR_GAP rule for both routing and selection.

    ``reject_current`` is the sole semantic difference: a persistent route change
    must not mint an approval for a no-op, while a per-run selection MUST permit
    the model already serving the task. Keeping that switch inside this one rule
    prevents the picker from growing a second registry truth table.
    """
    boot_route = boot.tasks.get(task)
    if boot_route is None:
        return f"task {task!r} is not routed by this deployment"
    by_name = {m.name: m for m in boot.models}
    target = by_name.get(model)
    if target is None:
        return (
            f"model {model!r} is not in this deployment's model set; a routing "
            "change may only re-select a registered model, never introduce one "
            f"(registered: {sorted(by_name)!r})"
        )
    in_force = current if current is not None else boot_route.model
    if reject_current and model == in_force:
        return f"task {task!r} already routes to {model!r}; nothing to change"
    # NE-CTX-IN-FORCE round 7: the SAME shape of rule for the CURRENT
    # conversation snapshot (SPEC PRE-DEMO-014 — absent/disabled, no governed
    # compaction exists, no silent truncation). ``None`` ⇒ no conversation in
    # hand ⇒ no check; a surface that knows the conversation passes the
    # boundary-receipt estimate and an over-snapshot target is disabled with a
    # counts-only reason instead of being offered and then discarding turns.
    if snapshot_tokens is not None:
        snapshot_refusal = _snapshot_fit_refusal(boot, task, target, snapshot_tokens)
        if snapshot_refusal is not None:
            return snapshot_refusal
    # NE-CTX-IN-FORCE: there is NO budget floor here any more, and its removal is
    # the point of that unit rather than a relaxation smuggled in beside it. The
    # floor refused any target whose input budget was below the BOOT primary's,
    # for one stated reason: ``RuntimeSettings.context_budget`` was derived from
    # the boot primary once and "cannot be re-derived live", so a smaller-window
    # target would have left the trim guard sizing prompts the new model could not
    # hold. It CAN be re-derived live now — ``GatewayModel._turn_budget`` asks the
    # gateway for the in-force model's budget on every single request — so the
    # premise is gone and with it the refusal. Keeping it would have exactly one
    # effect left: forcing every lane to declare the SMALLEST lane's window, which
    # is the defect being fixed (a 1M frontier model serving 18841 input tokens).
    #
    # What still protects a repoint DOWN to a narrow model: the trim guard now
    # trims to that model's own window, and refuses — typed, audited,
    # counts-only — when the current turn cannot fit it. A loud governed refusal
    # naming the real window, instead of a silent overflow at the provider.
    if air_gap and target.lane is Lane.FRONTIER_API:
        # The recovery ORDER is part of the refusal (POST_DEMO.md item 21): an
        # engaged gap freezes every frontier target, so the only path back is
        # lift-then-repoint, and each step is its own Tier-2 approval. Stating
        # it here puts the ordering in the console menu and the 409 detail —
        # the surfaces an operator actually reads — not just in a runbook.
        return (
            f"model {model!r} is on the frontier lane and the air gap is engaged; "
            "the egress guard would refuse every call on this route. To restore "
            "frontier routing, FIRST lift the air gap "
            "(write.external.network.egress.expand, Tier-2 approval), THEN repoint "
            "this task (write.external.model.routing.repoint, its own Tier-2 "
            "approval) — the order cannot be swapped, because this refusal stands "
            "while the gap is engaged"
        )
    return None


def refusal_for_selection(
    boot: GatewaySettings,
    task: str,
    model: str,
    *,
    air_gap: bool = False,
    snapshot_tokens: int | None = None,
) -> str | None:
    """``None`` when ``model`` is safe to select for one call of ``task``.

    This is BD-13's boot-registered task/model and AIR_GAP verdict with exactly
    one deliberate difference from a route *change*: the model currently in force
    remains a valid per-run choice. Availability is a separate measured fact
    owned by :mod:`ne.api.model_selection`.
    """
    return _refusal_for_model(
        boot,
        task,
        model,
        air_gap=air_gap,
        current=None,
        reject_current=False,
        snapshot_tokens=snapshot_tokens,
    )


def refusal_for_target(
    boot: GatewaySettings,
    task: str,
    model: str,
    *,
    air_gap: bool = False,
    current: str | None = None,
    snapshot_tokens: int | None = None,
) -> str | None:
    """``None`` if this repoint is permitted, else the reason it is refused.

    THE single source of the offer/refuse judgement. The catalog calls it to mark
    each candidate ``selectable`` and the handler calls it to refuse — so the menu
    the console renders and the answer the server gives cannot diverge (C7: the
    console computes neither).

    The checks, in order, each a distinct failure the caller can act on:

    1. the task is routed by the BOOT config (a runtime table cannot mint a task);
    2. the model is in the BOOT-composed model set (§2.1 — the property that makes
       live activation safe: no new endpoint can enter at runtime);
    3. it is not already the model in force (a no-op must not mint an approval);
    4. SNAPSHOT fit (round 7, SPEC PRE-DEMO-014) — ``snapshot_tokens`` is the
       estimated size of the conversation snapshot this change must keep
       serving, and a target whose window cannot safely accept it is refused
       BY NAME with a counts-only reason (no governed compaction operation
       exists, and there is no silent truncation). A task repoint governs
       every conversation on the task, so the store/console paths pass the
       LARGEST live conversation snapshot for the task
       (:meth:`ModelRouteStore.snapshot_tokens_for`, read from the same
       durable boundary receipts the per-run selection gate reads). ``None``
       skips the check and means NO live conversation — never "unknown":
       a failed snapshot read raises at the caller instead of degrading.
       NOT the deleted budget-width floor: a narrow target the live snapshots
       fit stays selectable and the trim guard sizes every turn to it.
    5. AIR GAP — a frontier target while the gap is engaged. This is ergonomics
       layered OVER the enforcement, not the enforcement: ``EgressPolicy`` refuses
       that call per attempt regardless of what this function says.
    """
    return _refusal_for_model(
        boot,
        task,
        model,
        air_gap=air_gap,
        current=current,
        reject_current=True,
        snapshot_tokens=snapshot_tokens,
    )


# --- the durable register ---------------------------------------------------------


class ModelRouteStore:
    """Read/apply surface for the runtime route register.

    ``boot`` is the boot-composed :class:`GatewaySettings` the platform froze — it
    supplies the default route and the complete set of routable models. Every
    effective route is DERIVED from it; this store never re-reads
    env / ``ne.toml`` (the ``EgressPostureStore`` law).
    """

    def __init__(
        self,
        engine: sa.Engine,
        boot: GatewaySettings,
        air_gap_provider: Callable[[], bool] | None = None,
        snapshot_tokens_provider: Callable[[str], int | None] | None = None,
    ) -> None:
        self._engine = engine
        self._boot = boot
        self._air_gap_provider = air_gap_provider
        #: NE-CTX-IN-FORCE round 9 (SPEC PRE-DEMO-014 on the REPOINT surface):
        #: ``task -> largest live conversation snapshot estimate`` (the newest
        #: boundary-receipt count per conversation, maxed over the task's live
        #: conversations — AgentRunStore.largest_live_context_snapshot_estimate
        #: _sync). ``None`` provider ⇒ a composition with no Run store, which
        #: genuinely has no conversation snapshots — never "unknown": a
        #: provider that FAILS raises, and every caller lets that refusal
        #: propagate instead of degrading to no check.
        self._snapshot_tokens_provider = snapshot_tokens_provider

    def snapshot_tokens_for(self, task: str) -> int | None:
        """The largest live conversation snapshot a repoint of ``task`` must keep serving.

        Read fresh per judgement (offer, pre-flight, and apply — inside apply's
        transaction, before any durable write) from the same durable boundary
        receipts the per-run selection gate reads. ``None`` means no live
        conversation has a receipt for this task ⇒ no fit check; a failing
        read RAISES out of the judgement (fail closed), it never answers None.
        """
        if self._snapshot_tokens_provider is None:
            return None
        return self._snapshot_tokens_provider(task)

    @property
    def boot_settings(self) -> GatewaySettings:
        return self._boot

    @property
    def boot_routes(self) -> Mapping[str, TaskRoute]:
        return self._boot.tasks

    def models_by_name(self) -> Mapping[str, ModelDef]:
        return {m.name: m for m in self._boot.models}

    def read_state(self) -> dict[str, ModelRouteState]:
        """Every runtime route row. Raises on a store failure — fail-closed
        handling is the PROVIDER's job (:func:`build_route_provider`)."""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.select(ne_model_route_overrides)).all()
        return {
            str(r.task): ModelRouteState(
                task=str(r.task),
                model=str(r.model),
                version=int(r.version),
                changed_at=r.changed_at,
                changed_by=str(r.changed_by),
                approved_by=str(r.approved_by),
            )
            for r in rows
        }

    def effective_routes(self) -> dict[str, TaskRoute]:
        """The routes in force: boot routes with the runtime overrides applied.

        A row naming a task the boot config does not route, or a model the boot
        config does not register, is DROPPED here with a warning rather than
        served — the gateway would refuse it at call time anyway
        (``UnknownRoutedModelError``), and the honest reading of an unresolvable
        row is that this deployment has no such route.
        """
        boot = dict(self._boot.tasks)
        for task, state in self.read_state().items():
            base = boot.get(task)
            if base is None:
                _LOG.warning("runtime route row names unknown task %r; ignoring", task)
                continue
            # Preserve an invalid model name so the gateway's independent
            # enforcement layer raises UnknownRoutedModelError. Silently dropping
            # it here would make that defense unreachable and conceal corruption.
            boot[task] = TaskRoute(model=state.model, fallbacks=base.fallbacks, params=base.params)
        return boot

    def apply(
        self,
        *,
        task: str,
        model: str,
        actor: str,
        approver: str,
        approval_id: str | None,
        via_action: str,
    ) -> str:
        """Apply a route change: state write + ``model.routing.changed`` chain
        record, ONE transaction (C10 — a change that cannot be recorded does not
        happen). Returns the governed-tool-handler JSON summary string.

        The target is re-validated here from FIRST PRINCIPLES against the boot
        settings — never trusting that the route already checked it — so the only
        code path that can write this table has the full rule in front of it.
        """
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_model_route_overrides.c.model, ne_model_route_overrides.c.version)
                .where(ne_model_route_overrides.c.task == task)
                .with_for_update()
            ).first()
            old = str(row.model) if row is not None else None
            base = self._boot.tasks.get(task)
            in_force = old if old is not None else (base.model if base is not None else None)
            refusal = refusal_for_target(
                self._boot,
                task,
                model,
                current=in_force,
                air_gap=self._air_gap_provider() if self._air_gap_provider is not None else False,
                snapshot_tokens=self.snapshot_tokens_for(task),
            )
            if refusal is not None:
                raise RouteChangeRefused(refusal)
            if row is None:
                conn.execute(
                    ne_model_route_overrides.insert().values(
                        task=task,
                        model=model,
                        version=1,
                        changed_at=now,
                        changed_by=actor,
                        approved_by=approver,
                        approval_id=approval_id,
                        changed_via=via_action,
                    )
                )
            else:
                result = conn.execute(
                    ne_model_route_overrides.update()
                    .where(
                        ne_model_route_overrides.c.task == task,
                        ne_model_route_overrides.c.version == int(row.version),
                    )
                    .values(
                        model=model,
                        version=int(row.version) + 1,
                        changed_at=now,
                        changed_by=actor,
                        approved_by=approver,
                        approval_id=approval_id,
                        changed_via=via_action,
                    )
                )
                if result.rowcount != 1:
                    raise RuntimeError("model route changed concurrently; refusing stale update")
            detail: dict[str, object] = {
                "task": task,
                "model": {"old": in_force, "new": model},
                "approved_by": approver,
            }
            if approval_id is not None:
                detail["approval_id"] = approval_id
            record = append_record(
                conn,
                new_audit_event(
                    action=MODEL_ROUTING_CHANGED_ACTION,
                    resource=f"model:routing:{task}",
                    outcome=AuditOutcome.success,
                    actor=actor,
                    detail=json.dumps(detail),
                    provenance={"changed_via": via_action},
                    occurred_at=now,
                ),
            )
        return json.dumps({"applied": True, "task": task, "model": model, "audit_seq": record.seq})


def build_route_provider(store: ModelRouteStore) -> Callable[[], Mapping[str, TaskRoute]]:
    """The per-call route provider for ``LiteLLMGateway(route_provider=...)``.

    FAIL-CLOSED, in the direction that means something for routing: when the
    register cannot be read, the provider returns the BOOT routes — the
    deployment-sanctioned assignment the frozen ``config_hash`` attests — never a
    remembered or partial override. It never raises and never fails open.
    """

    def _current() -> Mapping[str, TaskRoute]:
        try:
            return store.effective_routes()
        except Exception:
            _LOG.warning(
                "model route register unreadable; failing CLOSED to the boot routes",
                exc_info=True,
            )
            return store.boot_routes

    return _current


# --- the catalog (server-computed; the console renders it) ------------------------


@dataclass(frozen=True)
class RouteTargetOption:
    """One candidate target for a task, with the server's verdict on it."""

    model: str
    lane: str
    #: The governed action a repoint to this model would ride, and its tier —
    #: so the console can say what will happen without deciding anything.
    action: str
    tier: int
    selectable: bool
    #: The server's own words when ``selectable`` is False; ``None`` otherwise.
    reason: str | None


@dataclass(frozen=True)
class TaskRoutingRow:
    """One gateway task: what serves it now, what the deployment shipped, and
    every target the SERVER would accept."""

    task: str
    model: str
    lane: str
    default_model: str
    source: str
    changed_by: str | None
    approved_by: str | None
    changed_at: datetime | None
    targets: tuple[RouteTargetOption, ...]


_TIER_BY_ACTION = {
    ROUTE_INTERNAL_ACTION: int(ApprovalTier.TIER_2),
    ROUTE_EXTERNAL_ACTION: int(ApprovalTier.TIER_2),
}

# The demo-only NE_DEMO_UNAPPROVED_ROUTE_REPOINT switch (NE-AIRGAP-NO-APPROVAL,
# owner-authorized for the 2026-08-28 demo ONLY) used to live here: it dropped
# the internal repoint to its tiers.rego class tier and let the handler accept a
# call that consumed no approval, recording a demo-override sentinel as
# ``approved_by``. It was RETIRED after the demo (POST_DEMO.md item 20): an
# approval-bypass path must not survive its one authorized day, however narrow.
# Historical rows on the chain may still carry
# ``approved_by = demo-override:NE_DEMO_UNAPPROVED_ROUTE_REPOINT`` — they are an
# honest record of what happened on 2026-08-28, not a live capability.


def route_action_tier(action: str) -> ApprovalTier:
    """The tier a routing action is declared and reported at.

    Both routing actions are DECLARED Tier 2 — for the internal action that is
    an escalation over its tiers.rego class (see the module docstring). Both the
    binding's ``ToolSpec.tier`` and the catalog's ``RouteTargetOption.tier``
    come through here, so what the console shows is what the gate enforces.
    """
    return ApprovalTier(_TIER_BY_ACTION[action])


def routing_catalog(store: ModelRouteStore, *, air_gap: bool = False) -> tuple[TaskRoutingRow, ...]:
    """Every routed task, ordered by name, entirely server-computed.

    ``targets`` is the C7 load-bearing field: which models a task may be
    repointed to is ``registered ∧ lane ∧ snapshot-fit ∧ not-current ∧ air-gap``,
    a judgement combining the boot model set, the live route register, the
    largest live conversation snapshot and the tier table. A
    console that computed it would be transliterating three server-side facts,
    and the first divergence would be a menu offering a change the server
    refuses.
    """
    boot = store.boot_settings
    by_name = store.models_by_name()
    try:
        state = store.read_state()
    except Exception:
        _LOG.warning("model route register unreadable; reporting the boot routes", exc_info=True)
        state = {}
    rows: list[TaskRoutingRow] = []
    for task in sorted(boot.tasks):
        base = boot.tasks[task]
        live = state.get(task)
        in_force = live.model if live is not None and live.model in by_name else base.model
        current_def = by_name.get(in_force)
        # One durable read per task, not per target: every target is judged
        # against the SAME largest-live-snapshot count.
        snapshot_tokens = store.snapshot_tokens_for(task)
        targets: list[RouteTargetOption] = []
        for name in sorted(by_name):
            target = by_name[name]
            action = action_for_lane(target.lane)
            refusal = refusal_for_target(
                boot,
                task,
                name,
                air_gap=air_gap,
                current=in_force,
                snapshot_tokens=snapshot_tokens,
            )
            targets.append(
                RouteTargetOption(
                    model=name,
                    lane=target.lane.value,
                    action=action,
                    tier=int(route_action_tier(action)),
                    selectable=refusal is None,
                    reason=refusal,
                )
            )
        rows.append(
            TaskRoutingRow(
                task=task,
                model=in_force,
                lane=current_def.lane.value if current_def is not None else "",
                default_model=base.model,
                source="override" if in_force != base.model else "default",
                changed_by=live.changed_by if live is not None else None,
                approved_by=live.approved_by if live is not None else None,
                changed_at=live.changed_at if live is not None else None,
                targets=tuple(targets),
            )
        )
    return tuple(rows)


# --- the two governed actions -----------------------------------------------------


def _refuse_unless_permitted(
    store: ModelRouteStore, action_name: str, expected_lane: Lane, task: str, model: str
) -> None:
    """The DETERMINISTIC half of the rule, shared by ``validate_args`` and the handler.

    Direction FIRST: the tier a change gets derives from what the change DOES, not
    from which endpoint the caller picked, so a frontier target arriving on the
    internal (cheaper-classed) action name is refused before anything else is
    considered — including before the target is judged otherwise permissible.
    """
    target = store.models_by_name().get(model)
    if target is None:
        raise RouteChangeRefused(
            f"model {model!r} is not in this deployment's model set; a routing "
            "change may only re-select a registered model, never introduce one"
        )
    if target.lane is not expected_lane:
        raise RouteChangeRefused(
            f"{action_name} only ever repoints to a {expected_lane.value} model; "
            f"{model!r} is on the {target.lane.value} lane (direction mismatch, "
            "refused) — the tier a change gets derives from what the change does"
        )
    try:
        state = store.read_state().get(task)
    except Exception:  # pragma: no cover - the store is read again under lock at apply
        state = None
    current = state.model if state is not None else None
    refusal = refusal_for_target(
        store.boot_settings,
        task,
        model,
        current=current,
        snapshot_tokens=store.snapshot_tokens_for(task),
    )
    if refusal is not None:
        raise RouteChangeRefused(refusal)


def build_model_routing_bindings(
    store: ModelRouteStore,
    principal_provider: Callable[[], Principal | None],
    approval_provider: Callable[[], object | None],
) -> tuple[ToolBinding, ...]:
    """The two routing actions, as plain :class:`ToolBinding`s.

    ``principal_provider`` and ``approval_provider`` are the executor's sanctioned
    ambient carriers (``ne.runtime.middleware.current_tool_principal`` /
    ``current_tool_approval``), injected per the skill-bindings discipline rather
    than imported here.

    Each handler enforces, before any state is touched:

    * **direction** — the internal action only ever repoints to a private-vllm
      model, the external action only ever to a frontier model. A caller
      smuggling a frontier target through the internal name is refused: the tier a
      change gets derives from what the change DOES, not from which endpoint the
      caller picked;
    * **a bound principal** — the chain record's ``actor`` must be the real
      requester, never a placeholder;
    * **separation of duties** — a Tier-2 change reaching the handler with NO
      consumed approval, or with an approval granted BY the requester, is refused.
      Defence in depth: the decide path already refuses the requester
      (``ne.runtime.approvals.refuse_self_approval``), so this guard is what
      catches a grant that arrived any other way.
    """
    import functools

    import anyio.to_thread

    def _make_handler(action_name: str, *, expected_lane: Lane):
        async def _handler(args: Mapping[str, object]) -> str:
            parsed = _ModelRouteArgs.model_validate(dict(args))
            # The deterministic half already ran in validate_args (pre-gate). It is
            # re-run here from first principles rather than trusted: this is the
            # ONLY code path that writes the register, so it holds the whole rule.
            _refuse_unless_permitted(store, action_name, expected_lane, parsed.task, parsed.model)
            principal = principal_provider()
            if principal is None:
                raise RouteChangeRefused(
                    "no verified principal bound to this governed call; refusing to "
                    "change the model route (fail closed)"
                )
            approval = approval_provider()
            approver = getattr(approval, "approver", None) if approval is not None else None
            if not approver:
                raise RouteChangeRefused(
                    "a model route change requires a named human approval and this "
                    "call consumed none; refusing (fail closed)"
                )
            if approver == principal.id:
                raise RouteChangeRefused(
                    "a model route change may not be approved by the principal who "
                    "requested it (separation of duties); a different human must approve"
                )
            return await anyio.to_thread.run_sync(
                functools.partial(
                    store.apply,
                    task=parsed.task,
                    model=parsed.model,
                    actor=principal.id,
                    approver=str(approver),
                    approval_id=getattr(approval, "approval_id", None),
                    via_action=action_name,
                )
            )

        return _handler

    def _make_validate(action_name: str, *, expected_lane: Lane):
        def _validate(args: Mapping[str, object]) -> None:
            try:
                parsed = _ModelRouteArgs.model_validate(dict(args))
            except Exception as exc:
                raise RouteChangeRefused(str(exc)) from exc
            _refuse_unless_permitted(store, action_name, expected_lane, parsed.task, parsed.model)

        return _validate

    def _audit_detail(args: Mapping[str, object], result: str) -> Mapping[str, object]:
        # Names/shapes only (C12): the task, the target model alias and whether it
        # applied. NEVER an api_base and never a credential — a model ALIAS is a
        # deployment-chosen name, not a secret, and no endpoint appears here.
        out: dict[str, object] = {
            "task": str(args.get("task", "")),
            "model": str(args.get("model", "")),
        }
        try:
            out["applied"] = bool(json.loads(result).get("applied"))
        except Exception:  # pragma: no cover - defensive; result is our own JSON
            out["applied_parse_error"] = True
        return out

    input_schema = {
        "type": "object",
        "properties": {"task": {"type": "string"}, "model": {"type": "string"}},
        "required": ["task", "model"],
    }
    internal = ToolBinding(
        spec=ToolSpec(
            name=ROUTE_INTERNAL_ACTION,
            description=(
                "Repoint a gateway task at a different IN-VPC model. Takes effect on "
                "the next model call, in every process. Requires a named human "
                "approval distinct from the requester (Tier 2)."
            ),
            # ESCALATION over the rego class (write.internal ⇒ 1). See the module
            # docstring: a repoint changes which model answers every subsequent
            # turn on the task, and no other human is in that loop.
            tier=route_action_tier(ROUTE_INTERNAL_ACTION),
            input_schema=input_schema,
        ),
        handler=_make_handler(ROUTE_INTERNAL_ACTION, expected_lane=Lane.PRIVATE_VLLM),
        validate_args=_make_validate(ROUTE_INTERNAL_ACTION, expected_lane=Lane.PRIVATE_VLLM),
        extract_audit_detail=_audit_detail,
    )
    external = ToolBinding(
        spec=ToolSpec(
            name=ROUTE_EXTERNAL_ACTION,
            description=(
                "Repoint a gateway task at a frontier-API model: prompts for this task "
                "will leave the deployment through the governed egress lane. Requires a "
                "named human approval distinct from the requester (Tier 2)."
            ),
            tier=ApprovalTier.TIER_2,
            input_schema=input_schema,
        ),
        handler=_make_handler(ROUTE_EXTERNAL_ACTION, expected_lane=Lane.FRONTIER_API),
        validate_args=_make_validate(ROUTE_EXTERNAL_ACTION, expected_lane=Lane.FRONTIER_API),
        extract_audit_detail=_audit_detail,
    )
    return (internal, external)


#: The reject-by-default C12 arg policy BOTH routing actions share; merged into
#: ``guardrails.tools`` unconditionally pre-freeze (the ``read.trace`` /
#: ``credential_rotation`` always-on pattern in ``platform._merge_tool_policy``).
MODEL_ROUTING_ARG_POLICY = ToolArgPolicy(
    args={
        # ``identifier`` (^[A-Za-z0-9_.-]{1,256}$), not ``text``: a task key and a
        # model ALIAS are both structured names, so the C12 guardrail rejects shell
        # metacharacters and SQL markers in them BEFORE any model judgment, and an
        # undeclared key is rejected by the policy's reject-by-default shape.
        "task": ArgSpec(kind=ToolArgKind.identifier, required=True),
        "model": ArgSpec(kind=ToolArgKind.identifier, required=True),
    }
)
