# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-LG-DROPIN — the governed demo composition root + stdio entry.

Runnable as ``python -m ne.mcp.demo_server`` (platform ``.venv``). It composes a
:class:`~ne.mcp.server.GovernedMcpServer` over the ONE C6 governed executor using
EXISTING factories only (no new governance code) and serves it over stdio, so a
customer-side LangGraph agent (in its own isolated venv) can connect as an MCP
client and have EVERY tool call governed by construction.

The seam this exercises is load-bearing and already merged: ``GovernedMcpServer``
holds ZERO handlers and forwards every ``call_tool`` straight to the C6
:class:`~ne.runtime.middleware.GovernedToolExecutor` (guardrails → PDP → HITL →
audit). An agent whose entire toolset is served here therefore has NO ungoverned
path to any enterprise effect (C6/C12).

Tool catalog (the C7 grant is exactly the first three; ``read.kb.export`` is
REGISTERED but NEVER granted — the visible trim leg):

  * ``read.kb.search``          — demo-local Tier-0 read over a seeded corpus.
  * ``read.kb.export``          — demo-local Tier-0 read, registered-but-ungranted.
  * ``write.internal.crm.note`` — the Tier-1 reference action (autonomous
    internal write; NE-CS-SEND ``1df6a113`` moved it down from Tier 2).
  * ``write.external.ticket.update`` — the Tier-3 reference action (the rejected leg).

STDIO DISCIPLINE: stdout is the MCP wire. Any stray ``print`` to stdout corrupts
the protocol, so the demo connector stubs record calls in a list and print applied
writes to STDERR ONLY. This module imports ``mcp`` only via ``GovernedMcpServer`` /
``ne.mcp._sdk`` (C3 chokepoint) — never bare ``import mcp``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from ne.audit import (
    PostgresAuditSink,
    create_engine_with_timeouts,
    ensure_audit_schema,
)
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern import create_pdp
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.mcp.server import GovernedMcpServer
from ne.runtime import (
    ToolsetScopedExecutor,
    create_approval_gate,
    create_tool_executor,
    freeze_platform_config,
)
from ne.runtime.action_tools import (
    ActionDedupStore,
    build_action_bindings,
    ensure_action_dedup_schema,
)
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_shared_guardrail_state
from ne.runtime.middleware import (
    GovernedToolExecutor,
    current_tool_approval,
    current_tool_principal,
)
from ne.runtime.reference_actions.crm_note import (
    CRM_NOTE_ARG_POLICY,
    crm_note_definition,
)
from ne.runtime.reference_actions.ticket_update import (
    TICKET_UPDATE_ARG_POLICY,
    ticket_update_definition,
)
from ne.runtime.tools import ToolBinding

#: Env: REQUIRED sqlite file path for the shared audit + approval + dedup stores.
DB_ENV = "NE_LG_DEMO_DB"
#: Env: the principal id the served adapter fixes at construction (default below).
PRINCIPAL_ENV = "NE_LG_DEMO_PRINCIPAL"
DEFAULT_PRINCIPAL = "user:lg-demo"

#: The exact C7 grant advertised to the agent — the ungranted read.kb.export is
#: registered on the executor but absent here, so it is invisible in list_tools and
#: fail-closed on call (ToolsetScopedExecutor).
GRANTED_TOOLS: tuple[str, ...] = (
    "read.kb.search",
    "write.internal.crm.note",
    "write.external.ticket.update",
)

#: A tiny seeded corpus for the demo-local knowledge reads (no external system).
_DEMO_CORPUS: dict[str, str] = {
    "acme corp account history": (
        "Acme Corp — enterprise account since 2021. 3 open opportunities; last QBR "
        "flagged a renewal risk on the analytics tier. Primary contact: J. Rivera."
    ),
    "default": "No knowledge-base entry matched the query.",
}


class _DemoCrmNoteClient:
    """A demo Salesforce write stub (records calls; prints to STDERR only).

    Structurally matches ``ne.runtime.reference_actions.crm_note.CrmNoteClient``.
    This is a DEMO CONNECTOR STUB — it performs no real Salesforce write.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        self.calls.append((sobject, dict(fields)))
        note_id = f"note-{len(self.calls):04d}"
        print(
            f"[demo connector stub] CRM write applied: {sobject} "
            f"parent={fields.get('ParentId')} title={fields.get('Title')!r} -> {note_id}",
            file=sys.stderr,
            flush=True,
        )
        return note_id


class _DemoTicketClient:
    """A demo SQL write stub (records calls; prints to STDERR only).

    Structurally matches ``ne.runtime.reference_actions.ticket_update.TicketWriteClient``.
    This is a DEMO CONNECTOR STUB — it performs no real ticket write.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def update_record(
        self,
        *,
        table: str,
        key_column: str,
        key_value: int | str,
        fields: Mapping[str, str],
    ) -> int:
        self.calls.append(
            {
                "table": table,
                "key_column": key_column,
                "key_value": key_value,
                "fields": dict(fields),
            }
        )
        print(
            f"[demo connector stub] ticket write applied: {table}.{key_column}={key_value} "
            f"status={fields.get('status')!r}",
            file=sys.stderr,
            flush=True,
        )
        return 1


@dataclass(frozen=True)
class DemoComposition:
    """The composed demo objects (so tests can drive + assert on the stubs)."""

    server: GovernedMcpServer
    executor: GovernedToolExecutor
    scoped: ToolsetScopedExecutor
    sink: PostgresAuditSink
    gate: DurableApprovalGate
    engine: sa.Engine
    crm_client: _DemoCrmNoteClient
    ticket_client: _DemoTicketClient


def _demo_govern_settings() -> GovernSettings:
    """Guardrail config allowlisting exactly the demo's tool argument shapes.

    The two KB reads get explicit arg policies; the two write actions merge their
    canonical ``*_ARG_POLICY`` shapes (a tool absent from ``guardrails.tools`` is
    fail-closed by the deterministic ToolArgGuardrail before any decision). A
    generous rate rule keeps the multi-step demo from tripping the limiter.
    """
    return GovernSettings(
        guardrails=GuardrailSettings(
            rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
            tools={
                "read.kb.search": ToolArgPolicy(
                    args={"query": ArgSpec(kind=ToolArgKind.text, required=True)}
                ),
                "read.kb.export": ToolArgPolicy(args={}),
                "write.internal.crm.note": CRM_NOTE_ARG_POLICY,
                "write.external.ticket.update": TICKET_UPDATE_ARG_POLICY,
            },
        )
    )


def _kb_search_binding() -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        query = str(args.get("query", "")).strip().lower()
        return _DEMO_CORPUS.get(query, _DEMO_CORPUS["default"])

    return ToolBinding(
        spec=ToolSpec(
            name="read.kb.search",
            description="Search the demo knowledge base for an account summary.",
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        handler=_handler,
    )


def _kb_export_binding() -> ToolBinding:
    """A Tier-0 read that is REGISTERED but NEVER granted — the visible C7 trim leg."""

    async def _handler(args: Mapping[str, object]) -> str:  # pragma: no cover - never granted
        return "full-kb-export"

    return ToolBinding(
        spec=ToolSpec(
            name="read.kb.export",
            description="Export the entire knowledge base (registered but ungranted).",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {}},
        ),
        handler=_handler,
    )


def build_demo_server(
    db_path: Path,
    principal_id: str,
    *,
    registry: PluginRegistry | None = None,
) -> DemoComposition:
    """Compose the governed demo server over ``db_path`` (existing factories only).

    Does NOT freeze the platform config — the ``__main__`` entry freezes once; tests
    freeze their own module-subset hash via the ne-mcp conftest autouse fixture.
    """
    gs = _demo_govern_settings()

    engine = create_engine_with_timeouts(f"sqlite:///{db_path}")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)

    gate = create_approval_gate(sink=sink, engine=engine)

    ensure_action_dedup_schema(engine)
    dedup = ActionDedupStore(engine)

    crm_client = _DemoCrmNoteClient()
    ticket_client = _DemoTicketClient()
    bindings: tuple[ToolBinding, ...] = (
        _kb_search_binding(),
        _kb_export_binding(),
        *build_action_bindings(
            [
                # NE-CRM-NOTE-PROVENANCE: the executor's verified carriers are
                # injected so the written note names its approver + approval id
                # (composition-root read of the sanctioned ambient carriers,
                # same discipline as ne-api's network-egress bindings).
                crm_note_definition(
                    crm_client,
                    approval_provider=current_tool_approval,
                    principal_provider=current_tool_principal,
                ),
                ticket_update_definition(ticket_client),
            ],
            dedup=dedup,
        ),
    )

    ledger, counter = create_shared_guardrail_state()
    executor = create_tool_executor(
        bindings,
        sink=sink,
        approval_gate=gate,
        govern_settings=gs,
        registry=registry,
        ledger=ledger,
        counter=counter,
    )
    pdp = create_pdp(gs, sink=sink, registry=registry)
    scoped = ToolsetScopedExecutor(
        executor,
        granted=GRANTED_TOOLS,
        sink=sink,
        scope="lg-dropin-demo",
        pdp=pdp,
    )
    server = GovernedMcpServer(
        scoped,
        principal=Principal(id=principal_id),
        server_name="neutron-lg-dropin-demo",
    )
    return DemoComposition(
        server=server,
        executor=executor,
        scoped=scoped,
        sink=sink,
        gate=gate,
        engine=engine,
        crm_client=crm_client,
        ticket_client=ticket_client,
    )


def _main() -> int:
    db = os.environ.get(DB_ENV)
    if not db:
        print(
            f"{DB_ENV} is required (path to the shared sqlite store); refusing to start.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    principal_id = os.environ.get(PRINCIPAL_ENV) or DEFAULT_PRINCIPAL

    # Freeze the COMPOSED platform config_hash ONCE at process start (ADR-0003),
    # before constructing anything that emits an event. GovernSettings() reads
    # NE_GOVERN_OPA_URL from the environment so the served PDP points at the live OPA.
    freeze_platform_config(settings=RuntimeSettings(), govern_settings=_demo_govern_settings())

    comp = build_demo_server(Path(db), principal_id)

    import asyncio

    asyncio.run(comp.server.run_stdio())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
