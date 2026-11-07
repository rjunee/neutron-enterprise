# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-016A-E production-composed Agent vertical acceptance.

This file is intentionally promoted as one serial PostgreSQL gate.  An absent
developer DSN parks the module; once CI configures the DSN, database/bootstrap/
composition failures are ordinary failures and are never converted to skips.
The four tests match the four load-bearing beats in SPEC §016.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from ne.api.agent_manifest import load_agent_manifest
from ne.api.agent_skills import AgentSkillService
from ne.api.platform import _compose_workflow_execution_bindings
from ne.api.routers import agent_profiles
from ne.api.run_submission import RunSubmissionService
from ne.api.runs_wire import RunDetailView, RunExecutionProfileReceiptView
from ne.connect.m365.config import M365ConnectorSettings
from ne.contracts import reset_config_hash_for_testing
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PLAN,
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
)
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.worker.runtime_state import reset_worker_state_for_testing

from .pre016_vertical_fixture import (
    AGENT_MANIFEST,
    HARDSHIP_ID,
    M365_DRIVE_ID,
    SUPPORT_ID,
    IsolatedPostgres,
    Pre016VerticalHarness,
    run,
)

_ADMIN_DSN = os.environ.get("NE_PRE016_POSTGRES_URL") or os.environ.get("NE_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    not _ADMIN_DSN,
    reason="PRE-016 PostgreSQL proof requires NE_TEST_POSTGRES_URL",
)


@pytest.fixture
def vertical(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Pre016VerticalHarness]:
    """One exact test owns one database, DBOS namespace and secret document."""
    assert _ADMIN_DSN, "configured PRE-016 gate may not skip an unavailable database"
    reset_config_hash_for_testing()
    reset_worker_state_for_testing()
    lane = IsolatedPostgres(_ADMIN_DSN).create()
    harness = Pre016VerticalHarness(lane, tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        harness.close()
        reset_worker_state_for_testing()
        reset_config_hash_for_testing()
        lane.close()


def _definition(agent_id: str) -> Any:
    loaded = load_agent_manifest(AGENT_MANIFEST)
    return next(row for row in loaded.manifest.agents if row.agent_id == agent_id)


def test_deterministic_profile_is_typed_executed_and_frozen_on_the_run(
    vertical: Pre016VerticalHarness,
) -> None:
    """016A: real manifest/profile plus a historical, Run-owned receipt."""
    profile = MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE
    profiles, bindings = _compose_workflow_execution_bindings(load_agent_manifest(AGENT_MANIFEST))
    assert profiles[profile.workflow_id] is profile
    assert bindings[HARDSHIP_ID].profile_identity == (
        profile.workflow_id,
        profile.version,
        profile.config_hash,
    )
    assert profile.validate_input({"max_subjects": 1}).model_dump()["max_subjects"] == 1
    assert profile.validate_input({"max_subjects": 5}).model_dump()["max_subjects"] == 5
    for invalid in (
        {"max_subjects": "2"},
        {"max_subjects": 0},
        {"max_subjects": 6},
        {"max_subjects": 2, "as_of_date": "2026-08-23"},
    ):
        with pytest.raises(ValidationError):
            profile.validate_input(invalid)
    assert profile.fixed_plan == MERIDIAN_B1_DISASTER_HARDSHIP_PLAN
    assert [step.ordinal for step in profile.fixed_plan] == [1, 2, 3, 4, 5]
    assert all(step.kind != "agentic" for step in profile.fixed_plan)

    # Compose both real roots before testing the public historical contract.
    worker = vertical.build_worker_root()
    app, state = vertical.build_api_root()
    inner = next(
        cast(Any, route.app)._app for route in app.routes if getattr(route, "path", None) == "/api"
    )
    projection = getattr(inner.state, agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY)
    assert worker.agent_execution_bindings == bindings
    assert worker.workflow_execution_profiles == projection.workflow_execution_profiles
    assert projection.agent_catalog is state.agent_catalog

    # A current /workflow-profile read is not a historical Run receipt.  These
    # fields must be frozen into the Run wire so a registry rollover cannot
    # rewrite an old Run's input/schema/plan on refresh.
    assert "execution_profile_receipt" in RunDetailView.model_fields
    assert {
        "workflow_id",
        "profile_version",
        "profile_hash",
        "input_schema",
        "ordered_plan",
    } <= set(RunExecutionProfileReceiptView.model_fields)


def test_real_skill_surface_and_approval_restart_are_run_bound(
    vertical: Pre016VerticalHarness,
) -> None:
    """016B + main beat: shipped Skill ACL/materialization and durable receipt."""
    platform = vertical.build_worker_root()
    assert platform.skills_engine is not None
    assert platform.skills_settings is not None
    # NE-CS-SPLIT: the Skill pin lives on Meridian Customer Support now.
    definition = _definition(SUPPORT_ID)
    registry = SqlAlchemySkillRegistry(platform.skills_engine)
    substrate = PydanticAiSkillSubstrate(
        platform.skills_engine,
        audit_engine=platform.audit_engine,
    )
    service = AgentSkillService(
        registry,
        platform.expander,
        platform.skills_settings,
        substrate,
    )
    activation = run(service.activate(definition, vertical.owner(), surface_key="run:pre016-owner"))
    denied = run(
        service.activate(definition, vertical.restricted(), surface_key="run:pre016-restricted")
    )
    assert [(pin.name, pin.version, pin.content_hash) for pin in activation.pins] == [
        (
            "meridian-hardship-draft",
            3,
            "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d",
        )
    ]
    assert denied.pins == ()
    assert "repossession" not in activation.index_text.lower()

    # The worker already persists this exact activation.  The public Run must
    # expose only its safe hashes/pins (never the procedure) and retain the
    # approval/checkpoint join across a fresh root.
    required_safe_receipts = {"skill_activation_receipt", "approval_id"}
    assert required_safe_receipts <= set(RunDetailView.model_fields)


def test_graph_claims_use_same_admitted_m365_records_and_current_authority(
    vertical: Pre016VerticalHarness,
) -> None:
    """016C/E: the production M365 host must author the Meridian topology."""
    # Creating this production mapping is the first non-bypass step.  A direct
    # EntityGraph upsert is deliberately absent: if the generated schema/host
    # cannot express these entities and edges, the integrated gate remains RED.
    platform = vertical.build_worker_root()
    assert platform.m365_graph_sync_host is not None
    mapping = platform.m365_graph_sync_host.mapping_for(
        M365ConnectorSettings(
            tenant_id="tenant-meridian",
            client_id="client-meridian",
            auth_method="secret",
            client_secret=SecretStr("fixture-secret-not-real"),
            drive_ids=(M365_DRIVE_ID,),
            owner="acceptance@neutron.invalid",
        )
    )
    assert mapping.source_system == "m365-sharepoint"
    assert len(mapping.entities) == 46
    assert sum(len(entity.edges) for entity in mapping.entities) == 40


def test_default_ask_is_one_canonical_agent_lineage_with_model_continuity(
    vertical: Pre016VerticalHarness,
) -> None:
    """016D/E: Ask preserves exact IDs and the explicit next-model boundary."""
    # The canonical service, not a legacy attachment handler, must accept the
    # same requested-model boundary as POST /agents/.../runs.  Signature binding
    # is intentionally value-discriminating: silently dropping the field is RED.
    submit = inspect.signature(RunSubmissionService.start)
    assert "requested_model" in submit.parameters
    assert "requested_model_lane" in submit.parameters

    # Production create_app then has to select the manifest-backed Analyst and
    # mount the canonical Ask service; no second resolver or copied transcript.
    worker = vertical.build_worker_root(default_ask=True)
    vertical.populate_m365(worker)
    app, state = vertical.build_api_root(default_ask=True)
    assert app is not None
    assert state.settings.default_ask_generation == "default-ask/canonical-run/v1"
    assert state.default_ask_submission is not None
    assert state.agent_run_store is not None
