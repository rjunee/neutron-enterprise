# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-memory-graph`` CLI — stdlib argparse (the ``ne.brain_graph.cli`` house style).

Verbs:

* ``serve``     — ensure the sidecar venv and run the Graphiti service (subprocess);
* ``llm-facade``— run the frontier-lane OpenAI-compat facade over a real gateway;
* ``backfill``  — replay the full episodic log (``--agent`` / ``--all``);
* ``catch-up``  — replay only the per-agent watermark gap.

Composition is behind module-level hooks so tests can drive the verbs over the
in-memory store + fake client/gateway without a live Postgres or model provider.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.memory_graph.backfill import backfill, catch_up, ensure_watermark_schema
from ne.memory_graph.client import MemoryGraphClient
from ne.memory_graph.config import MemoryGraphSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.memory_store import SqlAlchemyEpisodicStore, ensure_memory_schema

__all__ = ["main"]


def load_memory_settings() -> MemoryGraphSettings:
    """Load the facade settings (overridable in tests)."""
    return MemoryGraphSettings()


def load_runtime_settings() -> RuntimeSettings:
    """Load ne-runtime settings for the shared session/memory engine (overridable)."""
    return RuntimeSettings()


def build_engine(settings: RuntimeSettings) -> sa.Engine:
    """Build the episodic engine over ``session_database_url`` (overridable)."""
    if not settings.session_database_url:
        raise SystemExit(
            "ne-memory-graph: set NE_RUNTIME_SESSION_DATABASE_URL (the episodic log URL)"
        )
    engine = sa.create_engine(settings.session_database_url)
    ensure_transactional_sqlite(engine)
    ensure_memory_schema(engine)
    ensure_watermark_schema(engine)
    return engine


def build_store(engine: sa.Engine) -> SqlAlchemyEpisodicStore:
    """Build the episodic store (overridable in tests)."""
    return SqlAlchemyEpisodicStore(engine)


def build_client(settings: MemoryGraphSettings) -> MemoryGraphClient:
    """Build the sidecar client (overridable in tests)."""
    return MemoryGraphClient(settings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ne-memory-graph", description="Temporal-graph agent memory."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Graphiti sidecar service (subprocess)")
    serve.add_argument("--port", type=int, default=8093)

    facade = sub.add_parser("llm-facade", help="run the frontier-lane OpenAI-compat facade")
    facade.add_argument("--port", type=int, default=None)

    bf = sub.add_parser("backfill", help="replay the full episodic log into the graph")
    bf.add_argument("--agent", dest="agent", default=None)
    bf.add_argument("--all", dest="all_agents", action="store_true")
    bf.add_argument("--throttle-s", dest="throttle_s", type=float, default=0.0)

    cu = sub.add_parser("catch-up", help="replay the per-agent watermark gap")
    cu.add_argument("--agent", dest="agent", default=None)
    cu.add_argument("--all", dest="all_agents", action="store_true")
    cu.add_argument("--throttle-s", dest="throttle_s", type=float, default=0.0)
    return parser


def _serve(port: int) -> int:
    from ne.memory_graph.sidecar import GraphitiSidecar

    sidecar = GraphitiSidecar()
    sidecar.ensure()
    proc = sidecar.start(dict(os.environ), port=port)
    return proc.wait()


def compose_gateway(engine: sa.Engine) -> object:
    """Build a real ``LiteLLMGateway`` (the ne-api ``_default_gateway`` recipe).

    Overridable in tests. litellm stays confined to ``ne.gateway._litellm`` (the C3
    chokepoint is untouched — ne.memory_graph legitimately imports ne.gateway).

    NE-162b: wires the real fail-closed ``RedactionStage`` (mirroring ne-api's
    ``_default_gateway``) so the facade's frontier lane egresses REDACTED instead
    of being REFUSED by NE-162's runtime check. Construction stays boot-safe: both
    the presidio isolation-venv build and the env-dir resolution are deferred to
    the first ``redact()`` call (the NE-162 air-gap-boot precedent), so this never
    touches the venv or requires a repo checkout at boot.
    """
    from ne.events.outbox import ensure_outbox_schema
    from ne.gateway.config import GatewaySettings
    from ne.gateway.cost import OutboxCostSink
    from ne.gateway.egress import build_egress_guard
    from ne.gateway.gateway import LiteLLMGateway
    from ne.govern import GovernSettings, create_redactor

    ensure_outbox_schema(engine)
    return LiteLLMGateway(
        GatewaySettings(),
        OutboxCostSink(engine),
        audit_sink=None,  # type: ignore[arg-type]
        egress_guard=build_egress_guard(),
        redactor=create_redactor(GovernSettings()),
    )


def _llm_facade(port: int | None) -> int:
    import uvicorn

    from ne.contracts.models import ModelGateway
    from ne.memory_graph.llm_facade import create_llm_facade_app

    memory_settings = load_memory_settings()
    if not memory_settings.service_token:
        print(
            "ne-memory-graph: NE_MEMORY_GRAPH_SERVICE_TOKEN is required for the llm-facade",
            file=sys.stderr,
        )
        return 2
    runtime_settings = load_runtime_settings()
    engine = build_engine(runtime_settings)
    gateway: ModelGateway = compose_gateway(engine)  # type: ignore[assignment]
    app = create_llm_facade_app(
        gateway,
        service_token=memory_settings.service_token,
        task=memory_settings.llm_task,
    )
    uvicorn.run(
        app,
        host=memory_settings.llm_facade_host,
        port=port if port is not None else memory_settings.llm_facade_port,
    )
    return 0


def _agents_arg(agent: str | None, all_agents: bool) -> list[str] | None:
    if all_agents:
        return None
    if agent:
        return [agent]
    raise SystemExit("ne-memory-graph: pass --agent <id> or --all")


def _backfill(agent: str | None, all_agents: bool, throttle_s: float) -> int:
    engine = build_engine(load_runtime_settings())
    store = build_store(engine)
    client = build_client(load_memory_settings())
    report = asyncio.run(
        backfill(store, client, agent_ids=_agents_arg(agent, all_agents), throttle_s=throttle_s)
    )
    for a in report.agents:
        print(f"agent={a.agent_id} replayed={a.replayed} skipped={a.skipped} failed={a.failed}")
    return 0


def _catch_up(agent: str | None, all_agents: bool, throttle_s: float) -> int:
    engine = build_engine(load_runtime_settings())
    store = build_store(engine)
    client = build_client(load_memory_settings())
    report = asyncio.run(
        catch_up(
            store,
            client,
            engine,
            agent_ids=_agents_arg(agent, all_agents),
            throttle_s=throttle_s,
        )
    )
    for a in report.agents:
        print(f"agent={a.agent_id} replayed={a.replayed} skipped={a.skipped} failed={a.failed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args.port)
    if args.command == "llm-facade":
        return _llm_facade(args.port)
    if args.command == "backfill":
        return _backfill(args.agent, args.all_agents, args.throttle_s)
    if args.command == "catch-up":
        return _catch_up(args.agent, args.all_agents, args.throttle_s)
    return 1  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
