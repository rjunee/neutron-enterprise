# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact deterministic Agent profile projection and visibility proof."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from ne.api.agent_profile_projection import AgentProfileProjectionState
from ne.api.routers import agent_profiles
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    MERIDIAN_B2_CURRENT_SOP_PROFILE,
    MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
    MeridianB1WorkflowInput,
    WorkflowExecutionProfile,
)

from .conftest import AppBuilder, run

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"
_AGENT_ID = "agt_01k3hardshipreview0000000000"
_RANDOM_ID = "agt_01k3randomrandomrandom0000000"


def _inner_app(app: Any) -> Any:
    return next(
        cast(Any, route.app)._app for route in app.routes if getattr(route, "path", None) == "/api"
    )


def _alternate_profile() -> WorkflowExecutionProfile:
    return WorkflowExecutionProfile.define(
        workflow_id=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.workflow_id,
        version=2,
        input_type=MeridianB1WorkflowInput,
        fixed_inputs=dict(MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.fixed_inputs),
        fixed_plan=MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
    )


def test_visible_profile_is_the_exact_server_authored_schema_and_fixed_plan(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))
    inner = _inner_app(harness.app)
    projection_state = getattr(inner.state, agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY)
    assert isinstance(projection_state, AgentProfileProjectionState)
    assert projection_state.agent_catalog is inner.state.ne_api.agent_catalog
    assert (
        projection_state.workflow_execution_profiles[
            MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.workflow_id
        ]
        is MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE
    )

    async def exercise() -> None:
        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/agents/{_AGENT_ID}/workflow-profile",
                headers=harness.bearer("u-ops", groups=("ops",)),
            )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        body = response.json()
        assert set(body) == {
            "agent_id",
            "agent_version",
            "agent_config_hash",
            "strategy",
            "workflow_id",
            "workflow_version",
            "workflow_config_hash",
            "input_schema",
            "fixed_inputs",
            "fixed_plan",
        }
        assert (body["agent_id"], body["agent_version"], body["strategy"]) == (
            _AGENT_ID,
            5,
            "deterministic",
        )
        profile = MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE
        assert body["agent_config_hash"] == next(
            row.definition.definition_hash
            for row in projection_state.agent_catalog.agents
            if row.definition.agent_id == _AGENT_ID
        )
        assert (
            body["workflow_id"],
            body["workflow_version"],
            body["workflow_config_hash"],
        ) == (profile.workflow_id, profile.version, profile.config_hash)
        assert body["input_schema"] == profile.canonical_payload()["input_schema"]
        assert body["input_schema"]["required"] == ["max_subjects"]
        assert body["input_schema"]["properties"] == {
            "max_subjects": {"maximum": 5, "minimum": 1, "title": "Max Subjects", "type": "integer"}
        }
        assert body["input_schema"]["additionalProperties"] is False
        assert body["fixed_inputs"] == {
            "counties": ["Columbia", "Jefferson", "Richmond"],
            "payment_window_end": "2026-09-01",
            "payment_window_start": "2026-08-18",
        }
        assert body["fixed_plan"] == [
            step.model_dump(mode="json") for step in MERIDIAN_B1_DISASTER_HARDSHIP_PLAN
        ]
        serialized = response.text
        assert '"prompt"' not in serialized
        assert '"as_of_date"' not in serialized

    run(exercise())


def test_b2_b3_profiles_remain_exact_with_protected_run_storage_enabled(
    make_app: AppBuilder,
) -> None:
    """The production protected-Run composition exposes both new exact profiles."""

    harness = make_app(agent_manifest_path=str(_REGISTRY), protected_transport=True)
    inner = _inner_app(harness.app)
    platform = inner.state.ne_api
    assert platform.agent_run_store is not None
    projection = getattr(inner.state, agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY)
    pairs = (
        ("agt_01k3currentsopreview000000000", MERIDIAN_B2_CURRENT_SOP_PROFILE),
        ("agt_01k3fabricationrefusal000000", MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE),
    )

    async def exercise() -> None:
        async with harness.client() as client:
            headers = harness.bearer("u-ops", groups=("ops",))
            for agent_id, profile in pairs:
                response = await client.get(
                    f"/api/v1/agents/{agent_id}/workflow-profile", headers=headers
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["agent_id"] == agent_id
                assert body["agent_version"] == 2
                assert body["strategy"] == "deterministic"
                assert (
                    body["workflow_id"],
                    body["workflow_version"],
                    body["workflow_config_hash"],
                ) == (profile.workflow_id, profile.version, profile.config_hash)
                assert body["fixed_inputs"] == {}
                assert body["fixed_plan"] == [
                    step.model_dump(mode="json") for step in profile.fixed_plan
                ]
                assert projection.workflow_execution_profiles[profile.workflow_id] is profile

    run(exercise())


def test_hidden_random_and_profile_binding_mismatch_are_indistinguishable(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))

    async def exercise() -> None:
        async with harness.client() as client:
            headers = harness.bearer("u-finance", groups=("finance",))
            hidden = await client.get(
                f"/api/v1/agents/{_AGENT_ID}/workflow-profile", headers=headers
            )
            random = await client.get(
                f"/api/v1/agents/{_RANDOM_ID}/workflow-profile", headers=headers
            )
            assert (
                (
                    hidden.status_code,
                    hidden.content,
                    hidden.headers["cache-control"],
                )
                == (
                    random.status_code,
                    random.content,
                    random.headers["cache-control"],
                )
                == (404, b'{"detail":"Agent profile not found"}', "private, no-store")
            )

            inner = _inner_app(harness.app)
            original = getattr(inner.state, agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY)
            mismatched = _alternate_profile()
            setattr(
                inner.state,
                agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY,
                replace(
                    original,
                    workflow_execution_profiles={mismatched.workflow_id: mismatched},
                ),
            )
            visible_headers = harness.bearer("u-ops", groups=("ops",))
            mismatch = await client.get(
                f"/api/v1/agents/{_AGENT_ID}/workflow-profile", headers=visible_headers
            )
            missing = await client.get(
                f"/api/v1/agents/{_RANDOM_ID}/workflow-profile", headers=visible_headers
            )
            assert (mismatch.status_code, mismatch.content) == (
                missing.status_code,
                missing.content,
            )

            setattr(
                inner.state,
                agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY,
                replace(original, workflow_execution_profiles={}),
            )
            unavailable = await client.get(
                f"/api/v1/agents/{_AGENT_ID}/workflow-profile", headers=visible_headers
            )
            assert (unavailable.status_code, unavailable.content) == (
                missing.status_code,
                missing.content,
            )

    run(exercise())
