# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Real-Postgres restart/reopen proof for the canonical Agent Context Ledger."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ne.api.deps import api_state, current_principal
from ne.api.routers import context_ledger
from ne.contracts import Principal
from ne.runtime.run_store import ensure_run_schema
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .test_context_ledger_api import (
    _AGENT,
    _DEPARTMENT,
    _OWNER,
    _PROTECTED_REF_SENTINEL,
    _seed_agent_context,
    _VisibleAgentCatalog,
)

_URL = os.environ.get("NE_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="NE_TEST_POSTGRES_URL is required")


@contextmanager
def _isolated_engine() -> Iterator[tuple[sa.Engine, str]]:
    schema = f"ne_agent_context_{uuid.uuid4().hex}"
    admin = sa.create_engine(_URL)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        ensure_runtime_schema(engine)
        ensure_run_schema(engine)
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def _client(engine: sa.Engine) -> tuple[SqlAlchemySessionStore, TestClient]:
    sessions = SqlAlchemySessionStore(engine)
    state = SimpleNamespace(
        session_store=sessions,
        agent_catalog=_VisibleAgentCatalog(),
        engines=[engine],
    )
    app = FastAPI()
    app.include_router(context_ledger.router)
    app.dependency_overrides[api_state] = lambda: state
    app.dependency_overrides[current_principal] = lambda: Principal(
        id=_OWNER,
        claims={"department": _DEPARTMENT},
    )
    return sessions, TestClient(app)


def test_postgres_context_ledger_survives_engine_restart_and_reopen() -> None:
    with _isolated_engine() as (engine, schema):
        sessions, client = _client(engine)
        conversation_id = _seed_agent_context(engine, sessions)
        path = f"/v1/agents/{_AGENT}/conversations/{conversation_id}/context-ledger"
        before = client.get(path, params={"limit": 2})
        assert before.status_code == 200, before.text
        assert before.json()["resides"] == "Postgres"
        assert before.json()["truncated"] is True
        assert [row["turn_sequence"] for row in before.json()["components"]] == [2, 3]
        assert _PROTECTED_REF_SENTINEL not in before.text

        engine.dispose()
        reopened = sa.create_engine(
            _URL,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        try:
            _sessions, restarted_client = _client(reopened)
            after = restarted_client.get(path, params={"limit": 2})
            assert after.status_code == 200, after.text
            assert after.json() == before.json()
        finally:
            reopened.dispose()
