# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Promoted real-Postgres PRE-008 history/restart/concurrency proof."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError

from ne.api.agent_catalog import AgentCatalogError, AgentDependencySnapshot, reconcile_agent_catalog
from ne.api.agent_manifest import load_agent_manifest

_URL = os.environ.get("NE_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="NE_TEST_POSTGRES_URL is required")
_AGENT = "agt_01k3meridiananalyst000000000"


def _write(
    path: Path,
    *,
    version: int = 1,
    generation: int = 1,
    label: str = "Analyst",
    empty: bool = False,
) -> None:
    if empty:
        path.write_text(f"schema_version = 1\ngeneration = {generation}\n", encoding="utf-8")
        return
    path.write_text(
        f'''schema_version = 1
generation = {generation}
[[agents]]
agent_id = "{_AGENT}"
version = {version}
label = "{label}"
description = "Postgres catalog proof"
owner_label = "Meridian Operations"
lifecycle = "active"
allowed_strategies = ["agentic"]
default_strategy = "agentic"
instructions = "Answer strictly from governed evidence served this run."
model_policy_id = "agent"
tool_grants = []
knowledge_sources = []
skills = []
availability_dependencies = []
[agents.visibility]
all_authenticated = true
principals = []
groups = []
roles = []
departments = []
[agents.budgets]
max_steps = 8
max_tool_calls = 6
max_input_tokens = 12000
max_output_tokens = 2000
''',
        encoding="utf-8",
    )


def _deps() -> AgentDependencySnapshot:
    return AgentDependencySnapshot(
        tools=frozenset(),
        knowledge_sources=frozenset(),
        skills=frozenset(),
        workflow_profiles=frozenset(),
        model_policies=frozenset({"agent"}),
    )


@contextmanager
def _isolated_engine() -> Iterator[sa.Engine]:
    schema = f"ne_agent_catalog_{uuid.uuid4().hex}"
    admin = sa.create_engine(_URL)
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def test_postgres_concurrent_restart_append_and_removal_preserve_history(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    with _isolated_engine() as engine:
        _write(path)
        loaded = load_agent_manifest(path)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: reconcile_agent_catalog(engine, loaded, _deps()), range(8)))
        engine.dispose()

        restarted = engine
        _write(path, version=2, generation=2)
        reconcile_agent_catalog(restarted, load_agent_manifest(path), _deps())
        _write(path, empty=True, generation=3)
        removed = reconcile_agent_catalog(restarted, load_agent_manifest(path), _deps())
        assert len(removed.agents) == 1
        assert removed.agents[0].available is False
        with restarted.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM ne_agent_definition_versions")
                ).scalar_one()
                == 2
            )


def test_postgres_generation_eight_advance_makes_generation_seven_rollback_stale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.toml"
    with _isolated_engine() as engine:
        _write(path, version=1, generation=7)
        generation_seven = load_agent_manifest(path)
        reconcile_agent_catalog(engine, generation_seven, _deps())

        _write(path, version=2, generation=8)
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())

        with pytest.raises(AgentCatalogError, match="stale Agent registry generation"):
            reconcile_agent_catalog(engine, generation_seven, _deps())


def test_postgres_definition_history_update_delete_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    with _isolated_engine() as engine:
        _write(path)
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
        with engine.connect() as conn:
            before = conn.execute(
                sa.text(
                    "SELECT agent_id, version, definition_hash, definition_json, "
                    "first_manifest_semantic_hash FROM ne_agent_definition_versions"
                )
            ).one()
        for statement in (
            "UPDATE ne_agent_definition_versions SET definition_json = '{}'",
            "DELETE FROM ne_agent_definition_versions",
        ):
            with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
                conn.execute(sa.text(statement))
        with engine.connect() as conn:
            after = conn.execute(
                sa.text(
                    "SELECT agent_id, version, definition_hash, definition_json, "
                    "first_manifest_semantic_hash FROM ne_agent_definition_versions"
                )
            ).one()
        assert after == before

        _write(path, version=2, generation=2)
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
        _write(path, generation=3, empty=True)
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM ne_agent_definition_versions")
                ).scalar_one()
                == 2
            )


def test_postgres_definition_shape_rejects_invalid_direct_inserts(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    with _isolated_engine() as engine:
        _write(path)
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
        invalid = (
            {"version": 0},
            {"definition_hash": "g" * 64},
            {"definition_hash": "a" * 63},
            {"first_manifest_semantic_hash": "z" * 64},
            {"agent_id": " "},
            {"definition_json": " "},
            {"definition_json": "{not-json"},
            {"definition_json": "[]"},
        )
        for index, overrides in enumerate(invalid):
            values: dict[str, object] = {
                "agent_id": f"agt_{index:024d}",
                "version": 1,
                "definition_hash": "a" * 64,
                "definition_json": "{}",
                "first_manifest_semantic_hash": "b" * 64,
            }
            values.update(overrides)
            with pytest.raises(DatabaseError), engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO ne_agent_definition_versions "
                        "(agent_id, version, definition_hash, definition_json, "
                        "first_manifest_semantic_hash) VALUES "
                        "(:agent_id, :version, :definition_hash, :definition_json, "
                        ":first_manifest_semantic_hash)"
                    ),
                    values,
                )
