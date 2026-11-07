# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-runtime — the agent loop, durability, and sessions.

Mirrors the ``GovernSettings`` style (``extra='forbid'`` so a typo in ``ne.toml``
fails loudly rather than silently mis-routing the agent). Every generative call
still travels through the frozen ``ModelGateway`` (C3) — these settings only pick
the routing *task*/*lane* and bound the loop; no provider is ever named here.

config_hash (SPEC §4 / ADR-0003, NE-CKC12): a composition root freezes the
COMPOSED platform hash via ``ne.runtime.freeze_platform_config(...)`` →
``freeze_platform_config_hash`` at process start — one hash spanning ne-runtime +
ne-govern + every caller-passed module's settings, so the event-spine's
non-optional ``config_hash`` stamping covers the whole platform (``new_audit_event``
resolves the frozen hash, and ``ConfigHashNotFrozenError`` propagates if a root
forgot to freeze). The single-module ``freeze_config_hash(RuntimeSettings())``
path remains only as the per-test fixture mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_settings import SettingsConfigDict

from ne.audit.engine import EngineTimeouts
from ne.contracts.config import NeSettings
from ne.contracts.tokens import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_PER_CALL_OVERHEAD,
    ContextBudget,
)


class ApprovalSettings(BaseModel):
    """HITL approval-gate policy knobs (SPEC §4.2 timeout/escalation + promotion).

    Plain frozen-style nested settings (``extra='forbid'`` so a typo in ``ne.toml``
    fails loudly). Durations are seconds. The Tier-3 named-approver list defaults
    EMPTY — an empty list means Tier-3 can NEVER be approved (fail closed, 'no
    delegation below the named approver' per §4.2).
    """

    model_config = ConfigDict(extra="forbid")

    #: SLA before a pending Tier-2 request escalates (default 4h).
    tier2_sla_s: float = 14400.0
    #: SLA before a pending Tier-3 request escalates (default 4h).
    tier3_sla_s: float = 14400.0
    #: Hard expiry after which a request is EXPIRED terminal; re-request required (24h).
    expiry_s: float = 86400.0
    #: Poll cadence of the await loop (seconds); tests inject 0 for a tight loop.
    poll_interval_s: float = 0.5
    #: The exhaustive set of ids that may approve a Tier-3 request. EMPTY ⇒ never.
    tier3_named_approvers: tuple[str, ...] = ()
    #: Promotion-on-evidence window: how many recent terminal decisions to score.
    promotion_window: int = 50
    #: Minimum clean instances over the window before an action is promotion-eligible.
    promotion_min_instances: int = 20
    #: Minimum approval rate over the window for eligibility (SPEC §4.2: ≥ 0.95).
    promotion_min_approval_rate: float = 0.95


class DispatchSettings(BaseModel):
    """Server-side sub-agent dispatch caps (NE-034).

    These bounds are enforced SERVER-SIDE by the
    :class:`~ne.runtime.dispatch.GovernedSubAgentDispatcher` and the
    :class:`~ne.runtime.processes.ProcessRegistry`: a ``SubAgentSpec``'s declared
    ``max_depth`` / ``max_concurrency`` can only NARROW these values, never widen
    them, so a lying spec can shrink its own budget but never exceed the server
    cap. ``extra='forbid'`` so a typo in ``ne.toml`` fails loudly.

    Adding this nested model changes the NE-033 module *subset* config_hash — this
    is expected; the per-test freeze fixture re-freezes ``RuntimeSettings()`` fresh
    for every test, and the composition-root freeze is unaffected (each root
    freezes its own subset until the platform-wide composed-freeze rider lands).
    """

    model_config = ConfigDict(extra="forbid")

    #: Absolute sub-agent tree depth (root session = depth 0). Default 2.
    max_depth: int = 2
    #: Global cap on concurrently-live sub-agents, counted by the ProcessRegistry.
    max_concurrent_subagents: int = 4


class RuntimeSettings(NeSettings):
    """Settings for the governed agent runtime (loop bound, durability, sessions)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_RUNTIME_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Gateway routing task passed on every ``CompletionRequest`` (C3 seam).
    task: str = "agent"
    #: Optional routing lane; coerced to ``ne.contracts.types.Lane`` when set.
    lane: str | None = None
    #: The DEPLOYMENT-level business outcome this runtime's model spend is
    #: attributed to (C8). Stamped onto every ``CompletionRequest`` this runtime
    #: issues, from which the gateway lifts it onto the ``CostEvent`` and the
    #: sibling ``AuditEvent`` — so AI spend is traceable to a named outcome
    #: rather than an aggregated line item.
    #:
    #: DECLARED CONFIGURATION, never per-request: an agent that could choose its
    #: own tag could dress arbitrary spend up as any outcome the buyer is
    #: watching. A per-sub-agent :class:`~ne.runtime.dispatch.AgentDefinition`
    #: may override it; ``None`` on BOTH means the call is emitted UNTAGGED and
    #: reports it as unattributed — there is deliberately no default tag, because
    #: a default would launder untagged spend into a named bucket at the source.
    #:
    #: Adding this field changes the ne-runtime module-subset config_hash —
    #: expected (the ``DispatchSettings`` / ``memory_backend`` precedent); the
    #: per-test freeze fixture re-freezes ``RuntimeSettings()`` fresh.
    kpi_tag: str | None = None
    #: Agent-loop request bound (pydantic-ai ``UsageLimits(request_limit=...)``).
    max_requests: int = 8
    #: Durable-execution engine: ``'off'`` (passthrough) or ``'dbos'`` (DBOS Transact).
    durability: Literal["off", "dbos"] = "off"
    #: Session store engine URL; empty => the caller supplies the engine directly
    #: (tests use ``sqlite:///:memory:``).
    session_database_url: str = ""
    #: Agent-memory backend. ``'episodic'`` (default) = the NE-037 ``AgentMemory``;
    #: ``'temporal-graph'`` = the NE-083 ``TemporalAgentMemory`` facade, loaded via
    #: the ``ne.plugins.memory_provider`` entry point (requires the ne-memory-graph
    #: package). Both satisfy the FROZEN ``MemoryProvider`` Protocol, so flipping
    #: back to ``'episodic'`` is an instant rollback. Adding this field changes the
    #: ne-runtime module-subset config_hash — expected (the ``DispatchSettings``
    #: precedent); the per-test freeze fixture re-freezes ``RuntimeSettings()`` fresh.
    memory_backend: Literal["episodic", "temporal-graph"] = "episodic"
    #: Max most-recent ``append_turn`` batches ``DepartmentAgentSession.run`` loads
    #: per turn (P1-3). ``load_history`` applies this as an ``ORDER BY seq DESC LIMIT``
    #: window (re-reversed to chronological), so history can never grow unbounded and
    #: a long-lived principal-owned conversation cannot brick on
    #: ``context_length_exceeded``.
    #: Adding this field changes the ne-runtime module-subset config_hash — expected
    #: (the DispatchSettings/memory_backend precedent); the per-test freeze fixture
    #: re-freezes ``RuntimeSettings()`` fresh.
    session_history_max_turns: int = 40
    #: The Layer-2 runtime context budget for THIS runtime task's PRIMARY model
    #: (design §3.4). ``None`` ⇒ the trim guard is DISABLED and ``GatewayModel``
    #: keeps its BYTE-IDENTICAL legacy ``request()`` path (no trim, no audit).
    #:
    #: C3-legal transport (design §2.3 single ownership): the SINGLE OWNER of the
    #: budget + estimator params is ``GatewaySettings``. ``ne.api.platform`` ALWAYS
    #: overrides this field (and ``bytes_per_token`` / ``per_call_overhead`` below)
    #: from the composed ``GatewaySettings`` via ``model_copy`` BEFORE the config
    #: hash is frozen — so the runtime trim and the gateway pre-flight derive the
    #: same estimator/arithmetic from one object and can never split-brain. An env
    #: value here is ADVISORY-ONLY for standalone ne-runtime tests; production never
    #: reads it because the platform copy wins.
    #:
    #: Adding this field (and the two below) changes the ne-runtime module-subset
    #: config_hash — EXPECTED, the ``DispatchSettings`` / ``memory_backend`` /
    #: ``session_history_max_turns`` precedent (SPEC-row DoD item e); the per-test
    #: freeze fixture re-freezes ``RuntimeSettings()`` fresh for every test.
    context_budget: ContextBudget | None = None
    #: Estimator divisor for the trim guard (C3-legal transport of
    #: ``GatewaySettings.bytes_per_token``; the platform overrides it — see
    #: :attr:`context_budget`). Defaults to the empirically-corrected
    #: ``ne.contracts.tokens.DEFAULT_BYTES_PER_TOKEN`` (1.5), NEVER the design
    #: prose's original 3.0.
    bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN
    #: Estimator per-call overhead for the trim guard (C3-legal transport of
    #: ``GatewaySettings.per_call_overhead``; the platform overrides it — see
    #: :attr:`context_budget`).
    per_call_overhead: int = DEFAULT_PER_CALL_OVERHEAD
    #: HITL approval-gate policy (env: ``NE_RUNTIME_APPROVALS__TIER2_SLA_S`` etc.).
    approvals: ApprovalSettings = ApprovalSettings()
    #: Server-side sub-agent dispatch caps (env: ``NE_RUNTIME_DISPATCH__MAX_DEPTH`` etc.).
    dispatch: DispatchSettings = DispatchSettings()
    #: Connect/statement/lock/pool timeouts for every runtime engine (P2-16).
    engine_timeouts: EngineTimeouts = EngineTimeouts()
