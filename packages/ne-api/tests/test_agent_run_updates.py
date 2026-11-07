# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Principal, visibility, status, window, cursor, and disclosure controls for Run updates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from ne.api.deps import api_state, current_principal
from ne.api.routers import agent_run_updates as updates_router
from ne.contracts.types import Principal
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import AgentRunStore, ensure_run_schema, ne_agent_runs
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import AppBuilder, run

OWNER = "user:ada"
DEPARTMENT = "operations"
VISIBLE_AGENT = "agt_visible"
OTHER_AGENT = "agt_other"
START = datetime(2026, 8, 22, 12, tzinfo=UTC)
END = START + timedelta(hours=2)


class _Catalog:
    def __init__(self, visible: dict[str, str]) -> None:
        self._visible = visible

    def get_visible(self, agent_id: str, _principal: Principal):
        label = self._visible.get(agent_id)
        if label is None:
            return None
        return SimpleNamespace(definition=SimpleNamespace(label=label))


def _environment(
    principal: Principal | None = None,
) -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore, TestClient]:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="updates-k1", keys={"updates-k1": b"k" * 32})
        ),
    )
    state = SimpleNamespace(
        agent_catalog=_Catalog({VISIBLE_AGENT: "Meridian Analyst"}),
        engines=[engine],
    )
    app = FastAPI()
    app.include_router(updates_router.router)
    app.dependency_overrides[api_state] = lambda: state
    app.dependency_overrides[current_principal] = lambda: (
        principal or Principal(id=OWNER, claims={"department": DEPARTMENT})
    )
    return engine, sessions, runs, TestClient(app)


async def _create_run(
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    *,
    status: str,
    updated_at: datetime,
    owner: str = OWNER,
    department: str = DEPARTMENT,
    agent_id: str = VISIBLE_AGENT,
    run_id: str | None = None,
) -> str:
    conversation = await sessions.create_conversation(agent_id, department, owner)
    run_id = run_id or f"run-{uuid4()}"
    terminal = status in {
        "completed",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
    }
    with runs._engine.begin() as connection:
        connection.execute(
            ne_agent_runs.insert().values(
                run_id=run_id,
                conversation_id=conversation.conversation_id,
                owner_principal_id=owner,
                department=department,
                conversation_agent_name=agent_id,
                agent_id=agent_id,
                agent_version=1,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="capability-receipt",
                sequence=1,
                display_label="PRIVATE-INPUT-SENTINEL",
                status=status,
                output_verdict="unavailable" if terminal else None,
                version=1,
                created_at=updated_at - timedelta(seconds=1),
                updated_at=updated_at,
                terminal_at=updated_at if terminal else None,
            )
        )
    return run_id


def _params(view: str, *, start: datetime = START, end: datetime = END, limit: int = 25):
    return {
        "view": view,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "limit": limit,
    }


def test_attention_filters_owner_department_and_current_agent_visibility_before_limit() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, groups=("role:ops",), claims={"department": DEPARTMENT})
    )

    async def seed() -> str:
        await _create_run(
            sessions,
            runs,
            status="failed",
            updated_at=START + timedelta(minutes=59),
            owner="user:foreign",
        )
        await _create_run(
            sessions,
            runs,
            status="context_exhausted",
            updated_at=START + timedelta(minutes=58),
            department="finance",
        )
        await _create_run(
            sessions,
            runs,
            status="budget_exhausted",
            updated_at=START + timedelta(minutes=57),
            agent_id=OTHER_AGENT,
        )
        return await _create_run(
            sessions,
            runs,
            status="waiting_approval",
            updated_at=START + timedelta(minutes=56),
        )

    visible_run_id = asyncio.run(seed())
    response = client.get("/v1/agents/updates", params=_params("attention", limit=1))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["next_cursor"] is None
    assert [item["run_id"] for item in body["items"]] == [visible_run_id]
    assert body["items"][0]["status"] == "waiting_approval"
    assert body["items"][0]["reason"] == "Approval required"
    assert body["items"][0]["agent_label"] == "Meridian Analyst"
    assert set(body["items"][0]) == {
        "run_id",
        "agent_id",
        "conversation_id",
        "agent_label",
        "conversation_title",
        "status",
        "reason",
        "updated_at",
    }
    assert "PRIVATE-INPUT-SENTINEL" not in response.text
    engine.dispose()


def test_attention_admits_only_exact_actionable_states_and_never_infers_cancelling() -> None:
    engine, sessions, runs, client = _environment()

    async def seed() -> dict[str, str]:
        created: dict[str, str] = {}
        for offset, status in enumerate(
            (
                "waiting_approval",
                "failed",
                "context_exhausted",
                "budget_exhausted",
                "cancelling",
                "cancelled",
                "running",
                "completed",
            )
        ):
            created[status] = await _create_run(
                sessions,
                runs,
                status=status,
                updated_at=START + timedelta(minutes=offset),
            )
        return created

    created = asyncio.run(seed())
    response = client.get("/v1/agents/updates", params=_params("attention"))
    assert response.status_code == 200
    assert {item["status"] for item in response.json()["items"]} == {
        "waiting_approval",
        "failed",
        "context_exhausted",
        "budget_exhausted",
    }
    assert created["cancelling"] not in {item["run_id"] for item in response.json()["items"]}
    engine.dispose()


def test_recent_is_completed_only_and_uses_updated_at_half_open_utc_window() -> None:
    engine, sessions, runs, client = _environment()

    async def seed() -> tuple[str, str]:
        at_start = await _create_run(sessions, runs, status="completed", updated_at=START)
        inside = await _create_run(
            sessions,
            runs,
            status="completed",
            updated_at=START + timedelta(hours=1),
        )
        await _create_run(sessions, runs, status="failed", updated_at=START + timedelta(minutes=90))
        await _create_run(
            sessions, runs, status="completed", updated_at=START - timedelta(microseconds=1)
        )
        await _create_run(sessions, runs, status="completed", updated_at=END)
        return at_start, inside

    at_start, inside = asyncio.run(seed())
    response = client.get("/v1/agents/updates", params=_params("recent"))
    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["items"]] == [inside, at_start]
    assert all(item["status"] == "completed" for item in response.json()["items"])
    assert all(item["reason"] == "Run completed" for item in response.json()["items"])

    offset = client.get(
        "/v1/agents/updates",
        params={**_params("recent"), "window_start": "2026-08-22T13:00:00+01:00"},
    )
    naive = client.get(
        "/v1/agents/updates",
        params={**_params("recent"), "window_start": "2026-08-22T12:00:00"},
    )
    reversed_window = client.get(
        "/v1/agents/updates",
        params=_params("recent", start=END, end=START),
    )
    assert offset.status_code == naive.status_code == reversed_window.status_code == 422
    engine.dispose()


def test_cursor_is_bound_to_view_and_exact_window_and_orders_updated_at_then_run_id() -> None:
    engine, sessions, runs, client = _environment()

    async def seed() -> None:
        for minute in (10, 20, 30):
            await _create_run(
                sessions,
                runs,
                status="completed",
                updated_at=START + timedelta(minutes=minute),
            )
        for run_id in ("run-tie-a", "run-tie-z"):
            await _create_run(
                sessions,
                runs,
                status="completed",
                updated_at=START + timedelta(minutes=40),
                run_id=run_id,
            )

    asyncio.run(seed())
    first = client.get("/v1/agents/updates", params=_params("recent", limit=1))
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    assert first.json()["items"][0]["run_id"] == "run-tie-z"
    second = client.get(
        "/v1/agents/updates", params={**_params("recent", limit=1), "cursor": cursor}
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["run_id"] == "run-tie-a"

    wrong_view = client.get(
        "/v1/agents/updates", params={**_params("attention", limit=1), "cursor": cursor}
    )
    changed_window = client.get(
        "/v1/agents/updates",
        params={
            **_params("recent", end=END + timedelta(seconds=1), limit=1),
            "cursor": cursor,
        },
    )
    malformed = client.get(
        "/v1/agents/updates", params={**_params("recent", limit=1), "cursor": "not-a-cursor"}
    )
    assert wrong_view.status_code == changed_window.status_code == malformed.status_code == 404
    engine.dispose()


def test_missing_department_and_unavailable_projection_fail_closed() -> None:
    engine, _sessions, _runs, client = _environment(Principal(id=OWNER))
    assert client.get("/v1/agents/updates", params=_params("recent")).status_code == 422
    engine.dispose()

    app = FastAPI()
    app.include_router(updates_router.router)
    app.dependency_overrides[current_principal] = lambda: Principal(
        id=OWNER, claims={"department": DEPARTMENT}
    )
    app.dependency_overrides[api_state] = lambda: SimpleNamespace(
        agent_catalog=_Catalog({VISIBLE_AGENT: "Meridian Analyst"}), engines=[]
    )
    unavailable = TestClient(app).get("/v1/agents/updates", params=_params("recent"))
    assert unavailable.status_code == 503


def test_production_composition_mounts_static_updates_before_dynamic_agent_id(
    make_app: AppBuilder,
) -> None:
    harness = make_app(protected_transport=True)

    async def exercise() -> None:
        async with harness.client() as client:
            response = await client.get(
                "/api/v1/agents/updates",
                params=_params("recent"),
                headers=harness.bearer(OWNER, department=DEPARTMENT),
            )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {
            "view": "recent",
            "window_start": START.isoformat().replace("+00:00", "Z"),
            "window_end": END.isoformat().replace("+00:00", "Z"),
            "items": [],
            "next_cursor": None,
        }

    run(exercise())
