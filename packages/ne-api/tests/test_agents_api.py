# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-008 metadata-only Agent catalog HTTP visibility contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ne.api.agent_catalog import AgentDependencySnapshot, reconcile_agent_catalog
from ne.api.agent_manifest import load_agent_manifest
from ne.api.deps import api_state, current_principal
from ne.api.routers import agents
from ne.contracts.types import Principal

_AGENT = "agt_01k3meridiananalyst000000000"


def _client(tmp_path: Path, principal: Principal) -> TestClient:
    path = tmp_path / "registry.toml"
    path.write_text(
        f'''schema_version = 1
generation = 1
[[agents]]
agent_id = "{_AGENT}"
version = 1
label = "ADA-CATALOG-SENTINEL"
description = "Metadata only"
owner_label = "Meridian Operations"
lifecycle = "active"
allowed_strategies = ["agentic"]
default_strategy = "agentic"
instructions = "Answer strictly from governed evidence served this run."
model_policy_id = "agent"
tool_grants = ["read.knowledge.search"]
knowledge_sources = []
skills = []
availability_dependencies = []
[agents.visibility]
all_authenticated = false
principals = ["u-ada"]
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
    catalog = reconcile_agent_catalog(
        sa.create_engine("sqlite://"),
        load_agent_manifest(path),
        AgentDependencySnapshot(
            tools=frozenset(),
            knowledge_sources=frozenset(),
            skills=frozenset(),
            workflow_profiles=frozenset(),
            model_policies=frozenset({"agent"}),
        ),
    )
    app = FastAPI()
    app.include_router(agents.router)
    app.dependency_overrides[api_state] = lambda: SimpleNamespace(agent_catalog=catalog)
    app.dependency_overrides[current_principal] = lambda: principal
    return TestClient(app)


def test_visible_list_and_get_are_metadata_only_private_no_store(tmp_path: Path) -> None:
    client = _client(tmp_path, Principal(id="u-ada"))
    listed = client.get("/v1/agents")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "private, no-store"
    item = listed.json()["items"][0]
    assert item["label"] == "ADA-CATALOG-SENTINEL"
    assert item["available"] is False
    assert item["availability_reasons"] == ["tool-unavailable"]
    assert set(item).isdisjoint(
        {"visibility", "availability_dependencies", "raw", "prompt", "endpoint", "activity"}
    )

    fetched = client.get(f"/v1/agents/{_AGENT}")
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "private, no-store"
    assert fetched.json() == item


def test_hidden_and_random_are_identical_and_filter_precedes_page(tmp_path: Path) -> None:
    client = _client(tmp_path, Principal(id="u-bob"))
    listed = client.get("/v1/agents", params={"limit": 1})
    assert listed.status_code == 200
    assert listed.json() == {"items": [], "next_cursor": None}
    hidden = client.get(f"/v1/agents/{_AGENT}")
    random = client.get("/v1/agents/agt_01k3randomrandomrandom0000000")
    assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
        random.status_code,
        random.content,
        random.headers["cache-control"],
    )


def test_malformed_cursor_is_non_oracular_no_store(tmp_path: Path) -> None:
    response = _client(tmp_path, Principal(id="u-ada")).get(
        "/v1/agents", params={"cursor": "not-a-cursor"}
    )
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"


def test_uncomposed_preflight_answers_503_uniformly_rather_than_the_catalogs_404(
    tmp_path: Path,
) -> None:
    """PRE-011E ships the route ahead of the composition that builds its service.

    The same app that serves the catalog above has NO preflight service on
    ``app.state``.  It must say so — 503 — for a visible Agent and an unknown id
    alike: an empty surface would be a fabricated grant, and a 404 would answer
    an existence question this deployment is in no position to answer.
    """
    client = _client(tmp_path, Principal(id="u-ada"))
    visible = client.get(f"/v1/agents/{_AGENT}/preflight")
    unknown = client.get("/v1/agents/agt_01k3randomrandomrandom0000000/preflight")

    assert (visible.status_code, unknown.status_code) == (503, 503)
    assert visible.content == unknown.content
    assert visible.json() == {"detail": "Agent capability preflight is not configured"}
    assert visible.headers["cache-control"] == "private, no-store"
    # The catalog route on the SAME app is unaffected: this is a missing
    # service, not a broken app.
    assert client.get(f"/v1/agents/{_AGENT}").status_code == 200
