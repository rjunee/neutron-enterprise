# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-008 production-root composition without Agent execution activation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.routing import iter_route_contexts
from starlette.routing import Mount

from ne.api.agent_catalog import (
    AgentCatalogError,
    AgentDependencySnapshot,
    effective_agent_tool_grants,
    reconcile_agent_catalog,
)
from ne.api.agent_manifest import load_agent_manifest
from ne.api.evidence_projection import EvidenceProjector, RunStoreCurrentEvidenceMaterializer
from ne.api.platform import _compose_workflow_execution_bindings
from ne.api.routers.evidence import EVIDENCE_SERVICE_STATE_KEY
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.skills import AuthoringStandard, SkillPack
from ne.gateway.config import TaskRoute
from ne.runtime.model_continuity import CONTINUITY_PROMPT_RESERVE_TOKENS
from ne.runtime.runner import model_request_limit as adapter_request_limit
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    MERIDIAN_B2_CURRENT_SOP_PROFILE,
    MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
    MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
)
from ne.skills.packio import pack_from_dir

from .conftest import AppBuilder, get_state, run

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"
_SKILL = _ROOT / "deploy/skills/meridian-hardship-draft"


def test_composed_catalog_has_exact_inert_meridian_definitions(make_app: AppBuilder) -> None:
    state = get_state(make_app(agent_manifest_path=str(_REGISTRY)).app)
    by_label = {row.definition.label: row for row in state.agent_catalog.agents}
    assert set(by_label) == {
        "Meridian Analyst",
        "Meridian Customer Support",
        "Meridian Grounded Analyst",
        "Meridian Hardship Review",
        "Meridian Current SOP Review",
        "Meridian Fabrication Refusal",
        "Meridian Overdue Credit Review",
        "Meridian Overdue Credit Review (Deterministic)",
        # NE-ITEM1-INERT-PARK: the operator self-test agent, not a Meridian
        # exhibit. It exists so the tier-2 approval lifecycle can be parked
        # and terminalized with zero outward effect.
        "Governance Self-Test (Inert Write-Back)",
    }

    analyst = by_label["Meridian Analyst"]
    # Version 15 (generation 26): NE-SHAPE-ALLOWANCE-4 re-derived the token
    # budget for the sixth output retry. Budget is definition input exactly as
    # prompt text is, so the hash moves and immutability demands the version
    # move with it. Version 16 (generation 27) is NE-WRAPPED-CALL-FUNDED, the
    # same mechanism one class further: the `wrapped-call` allowance is one
    # corrective round per rung of the ladder, so the request ceiling is
    # `2*max_tool_calls + 7` = 75 and the budget is re-derived again.
    assert analyst.definition.version == 16
    # NE-ANALYST-INSTRUCTIONS: the read-only Analyst pins no Skill, so its
    # system prompt is its own manifest instructions — non-empty by manifest
    # contract, or the registry refuses to load.
    assert analyst.definition.instructions is not None
    assert analyst.definition.instructions.strip()
    # NE-CS-SPLIT: the Analyst is READ-ONLY. The verified-answer lane, granted
    # EXACTLY: the six templates the four demo questions read, plus the seventh
    # (disaster declarations) that only the DR-4830 question needs. Pinned as an
    # exact tuple so a speculative grant is a visible, deliberate edit rather
    # than a quiet widening — and so re-adding a write/draft/skills grant here is
    # a deliberate reversal of the owner's split, not drift.
    assert analyst.definition.tool_grants == (
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.disaster_declarations",
        "read.kg.disputes_directory",
        "read.kg.open_disputes_by_status",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "read.knowledge.graph_path",
        "read.knowledge.search",
    )
    assert analyst.definition.skills == ()
    # The whole lane is OPTIONAL — every read.kg grant and the Tier-3 release —
    # so a deployment without the release keeps an AVAILABLE Analyst that answers
    # what t1/t2 supports, instead of losing the agent to `tool-unavailable`.
    assert analyst.definition.optional_tool_grants == (
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.disaster_declarations",
        "read.kg.disputes_directory",
        "read.kg.open_disputes_by_status",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "read.knowledge.graph_path",
    )

    support = by_label["Meridian Customer Support"]
    assert support.definition.agent_id == "agt_01k3customersupport000000000"
    # Version 4 (generation 24): the pinned Skill advanced to v3, which ended the
    # procedure at the approval-gated send. The trailing CRM note it used to
    # order there could never run — the resume executes the one approved effect
    # and terminalizes — so the step is gone and the registry's prose and
    # per-call budget derivation no longer count it. Grants and budgets did NOT
    # move (see the pins below); only the Skill pin and the prose did.
    # Version 5 (generation 26) is NE-SHAPE-ALLOWANCE-4: `max_input_tokens`
    # re-derived 478376 -> 530959 for the sixth output retry. Version 6
    # (generation 27) is NE-WRAPPED-CALL-FUNDED: re-derived again, 530959 ->
    # 1194989, for the ladder-sized `wrapped-call` class. The CEILINGS did not
    # move in either — `max_steps` stays 23 because it is the subtrahend of the
    # published 180 s approval window.
    assert support.definition.version == 6
    # The drafting-and-outreach half of the split: exactly the Skill's
    # requires_tools plus the real governed action ladder — the Tier-2 send
    # (required: it IS the exhibit) and the Salesforce note + lookup (optional:
    # they bind only where the Salesforce legs are staged) — plus, since
    # NE-CS-WHOLE-LADDER (owner decision 2026-08-28), the Analyst's five
    # hardship verified-answer templates, so one agent establishes eligibility
    # AND runs the draft->approve->send->record ladder. The two dispute
    # templates stay off this agent: unrelated to the hardship ladder.
    assert support.definition.tool_grants == (
        "draft.email.create",
        "read.external.crm.account",
        "read.external.crm.contact",
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.disaster_declarations",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "read.knowledge.graph_path",
        "read.knowledge.search",
        "read.skills.view",
        "write.external.email.send",
        "write.internal.crm.note",
    )
    assert support.definition.optional_tool_grants == (
        "read.external.crm.account",
        "read.external.crm.contact",
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.disaster_declarations",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
        "write.internal.crm.note",
    )
    # NE-CS-WHOLE-LADDER: the verified-answer lane rides the Tier-3 release on
    # this agent exactly as it does on the Analyst — declared, and optional.
    assert tuple(
        (grant.source_id, grant.tiers) for grant in support.definition.knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)), ("meridian-servicing", ("t1", "t2")))
    assert tuple(
        (grant.source_id, grant.tiers) for grant in support.definition.optional_knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)), ("meridian-servicing", ("t2",)))
    assert support.definition.visibility.all_authenticated is False
    # `ops` and never `approver`: the Tier-2 gate grants on any single named
    # approver, so the originating and approving principals must differ.
    assert support.definition.visibility.groups == ("ops",)
    assert tuple(
        (grant.skill_id, grant.version, grant.content_hash) for grant in support.definition.skills
    ) == (
        (
            "meridian-hardship-draft",
            3,
            "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d",
        ),
    )
    assert support.definition.budgets.model_dump() == {
        "max_steps": 23,
        "max_tool_calls": 17,
        "max_input_tokens": 1194989,
        "max_output_tokens": 4000,
    }
    # Tier 3 serves no chunk-store corpus, so it cannot widen a
    # `read.knowledge.search` filter (see ne.api.knowledge_scope).
    assert tuple(
        (grant.source_id, grant.tiers) for grant in analyst.definition.knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)), ("meridian-servicing", ("t1", "t2")))
    assert tuple(
        (grant.source_id, grant.tiers) for grant in analyst.definition.optional_knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)), ("meridian-servicing", ("t2",)))
    # No read.kg grant is an explicit availability dependency: an entry here
    # would re-impose the hard gate the optional wiring exists to avoid.
    assert not any(
        dependency.kind == "tool"
        and dependency.dependency_id
        not in {
            "read.skills.view",
            "read.knowledge.search",
        }
        for dependency in analyst.definition.availability_dependencies
    )
    # NO 011F DELIVERY in this harness: the shared signal database is fresh, so no
    # Agent Skill pin resolves and the Skill dependency is genuinely unsatisfied —
    # on Meridian CUSTOMER SUPPORT now (NE-CS-SPLIT moved the pin there; the
    # Analyst carries no Skill and can no longer be skill-unavailable at all).
    # PRE-011B did NOT weaken this — it made it conditional on delivery, which is
    # why the delivered counterpart below is the discriminating half.
    assert analyst.available is False
    assert "skill-unavailable" not in analyst.availability_reasons
    assert "knowledge-unavailable" in analyst.availability_reasons
    assert support.available is False
    assert "skill-unavailable" in support.availability_reasons
    # The required draft/send bindings ride the write-action selectors, which
    # this default recipe leaves OFF — the agent is honestly tool-unavailable
    # rather than present with a procedure it cannot follow.
    assert "tool-unavailable" in support.availability_reasons

    # NE-GA-RUN2: the gate-scoped agent. EXACTLY the two dispute verified
    # templates — no chunk-store or graph tool — so its entire retrievable
    # corpus is recorded VerifiedAnswers, which is exactly what the grounding
    # gate can see. Pinned as an exact tuple for the same reason the Analyst's
    # grants are: a widened grant here (in particular `read.knowledge.search`,
    # which the gate is blind to) is a deliberate edit, not drift.
    grounded = by_label["Meridian Grounded Analyst"]
    assert grounded.definition.agent_id == "agt_01k3groundedanalyst000000000"
    # Version 3 (generation 23): the NE-GROUND-REL description correction —
    # the gate now enforces semantic row binding, and the description states
    # the spec bar + the disclosed gap. Prose only; the grants/budget pins
    # below did not move. Version 4 (generation 26) is NE-SHAPE-ALLOWANCE-4:
    # this agent's budget is derived by the SAME formula as the Analyst's and
    # was equally unfunded by the sixth output retry (131523 -> 165321).
    # Version 5 (generation 27) is NE-WRAPPED-CALL-FUNDED, the same formula
    # with the ladder-sized `wrapped-call` class in the pool (165321 -> 226437).
    assert grounded.definition.version == 5
    assert grounded.definition.instructions is not None
    assert grounded.definition.instructions.strip()
    assert grounded.definition.tool_grants == (
        "read.kg.disputes_directory",
        "read.kg.open_disputes_by_status",
    )
    assert grounded.definition.optional_tool_grants == ()
    assert grounded.definition.skills == ()
    assert tuple(
        (grant.source_id, grant.tiers) for grant in grounded.definition.knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)),)
    assert grounded.definition.budgets.model_dump() == {
        "max_steps": 10,
        "max_tool_calls": 4,
        "max_input_tokens": 226437,
        "max_output_tokens": 4000,
    }
    # Fresh harness: the Tier-3 release is absent, so the agent is honestly
    # unavailable on its knowledge (and required-tool) dependencies.
    assert grounded.available is False
    assert "knowledge-unavailable" in grounded.availability_reasons

    hardship = by_label["Meridian Hardship Review"]
    assert hardship.definition.agent_id == "agt_01k3hardshipreview0000000000"
    assert hardship.definition.version == 5
    assert hardship.definition.visibility.groups == ("ops",)
    assert hardship.definition.visibility.roles == ()
    assert hardship.definition.allowed_strategies == ("deterministic",)
    assert hardship.definition.default_strategy == "deterministic"
    assert hardship.definition.model_policy_id == "agent.meridian-hardship-review.v3"
    assert hardship.definition.tool_grants == (
        "read.kg.autoloans_for_customer",
        "read.kg.customers_in_counties",
        "read.kg.payments_due_window",
        "read.kg.servicing_notes_for_loan",
    )
    assert hardship.definition.workflow is not None
    assert (
        hardship.definition.workflow.profile_id,
        hardship.definition.workflow.version,
        hardship.definition.workflow.config_hash,
        hardship.definition.workflow.max_subjects,
    ) == (
        "meridian-b1-disaster-hardship",
        1,
        "e90381d94480ee1d01f028380be39595b63c4c632d6aff89738b49a440e667e0",
        5,
    )
    frozen = hardship.definition.workflow.frozen_inputs
    assert frozen.counties == ("Columbia", "Jefferson", "Richmond")
    assert str(frozen.payment_window_start) == "2026-08-18"
    assert str(frozen.payment_window_end) == "2026-09-01"
    assert hardship.definition.budgets.model_dump() == {
        "max_steps": 1,
        "max_tool_calls": 100,
        "max_input_tokens": 8210,
        "max_output_tokens": 1,
    }
    workflow_dependencies = tuple(
        dependency
        for dependency in hardship.definition.availability_dependencies
        if dependency.kind == "workflow-profile"
    )
    assert len(workflow_dependencies) == 1
    assert (
        workflow_dependencies[0].dependency_id,
        workflow_dependencies[0].version,
        workflow_dependencies[0].content_hash,
    ) == (
        hardship.definition.workflow.profile_id,
        hardship.definition.workflow.version,
        hardship.definition.workflow.config_hash,
    )
    assert "workflow-profile-unavailable" not in hardship.availability_reasons

    deterministic = {
        row.definition.workflow.profile_id: row
        for row in state.agent_catalog.agents
        if row.definition.workflow is not None
    }
    expected = {
        "meridian-b1-disaster-hardship": (
            "agt_01k3hardshipreview0000000000",
            MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
            {
                "read.kg.autoloans_for_customer",
                "read.kg.customers_in_counties",
                "read.kg.payments_due_window",
                "read.kg.servicing_notes_for_loan",
            },
        ),
        "meridian-b2-current-sop": (
            "agt_01k3currentsopreview000000000",
            MERIDIAN_B2_CURRENT_SOP_PROFILE,
            {"read.kg.disputes_directory", "read.kg.open_disputes_by_status"},
        ),
        "meridian-b3-fabrication-refusal": (
            "agt_01k3fabricationrefusal000000",
            MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
            {"read.kg.disputes_directory"},
        ),
        "meridian-s9-overdue-provisional-credit": (
            "agt_01k3overduecreditreviewdet00",
            MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
            {"read.kg.overdue_provisional_credit_disputes"},
        ),
    }
    versions = {
        "meridian-b1-disaster-hardship": 5,
        "meridian-b2-current-sop": 2,
        "meridian-b3-fabrication-refusal": 2,
        "meridian-s9-overdue-provisional-credit": 1,
    }
    assert set(deterministic) == set(expected)
    for workflow_id, (agent_id, profile, tools) in expected.items():
        row = deterministic[workflow_id]
        definition = row.definition
        assert definition.agent_id == agent_id
        assert definition.version == versions[workflow_id]
        assert definition.visibility.groups == ("ops",)
        assert set(definition.tool_grants) == tools
        assert definition.workflow is not None
        assert definition.workflow.version == profile.version
        assert definition.workflow.config_hash == profile.config_hash
        assert definition.workflow.max_subjects == 5
        assert definition.workflow.frozen_inputs.canonical_values() == dict(profile.fixed_inputs)
        assert "workflow-profile-unavailable" not in row.availability_reasons

    # S9's AGENTIC half: the chat-surface twin of the deterministic review
    # above. One grant, REQUIRED (unlike the Analyst's optional lane — with the
    # verified read absent this agent can do nothing, so it must be honestly
    # unavailable rather than present and inert), and its own counted budget.
    overdue = by_label["Meridian Overdue Credit Review"]
    assert overdue.definition.agent_id == "agt_01k3overduecreditreview00000"
    # Version 2 (generation 26): NE-SHAPE-ALLOWANCE-4 re-derived its budget for
    # the sixth output retry (114710 -> 146985). Version 3 (generation 27) is
    # NE-WRAPPED-CALL-FUNDED (146985 -> 187221). Its deterministic twin holds at
    # version 1 through both — no model in its answer path, so no output retry
    # to fund.
    assert overdue.definition.version == 3
    assert overdue.definition.allowed_strategies == ("agentic",)
    assert overdue.definition.default_strategy == "agentic"
    assert overdue.definition.model_policy_id == "agent.meridian-analyst.v2"
    assert overdue.definition.instructions is not None
    assert overdue.definition.instructions.strip()
    assert overdue.definition.tool_grants == ("read.kg.overdue_provisional_credit_disputes",)
    assert overdue.definition.optional_tool_grants == ()
    assert overdue.definition.skills == ()
    assert overdue.definition.workflow is None
    assert overdue.definition.visibility.groups == ("ops",)
    assert tuple(
        (grant.source_id, grant.tiers) for grant in overdue.definition.knowledge_sources
    ) == (("meridian-governed-local-t3", ("t3",)),)
    assert overdue.definition.budgets.model_dump() == {
        "max_steps": 9,
        "max_tool_calls": 3,
        "max_input_tokens": 187221,
        "max_output_tokens": 4000,
    }
    assert {
        (dependency.kind, dependency.dependency_id)
        for dependency in overdue.definition.availability_dependencies
    } == {
        ("tool", "read.kg.overdue_provisional_credit_disputes"),
        ("model-policy", "agent.meridian-analyst.v2"),
    }
    # This default recipe registers neither the verified read nor the Tier-3
    # release, so the agent is honestly unavailable on both counts.
    assert overdue.available is False
    assert "tool-unavailable" in overdue.availability_reasons
    assert "knowledge-unavailable" in overdue.availability_reasons


def test_graph_cold_start_has_only_knowledge_debt_then_t1_admits_analyst() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    common = {
        "tools": frozenset(
            {
                "read.knowledge.search",
                "read.skills.view",
                "read.kg.customers_in_counties",
                "read.kg.autoloans_for_customer",
                "read.kg.payments_due_window",
                "read.kg.servicing_notes_for_loan",
                "read.kg.disputes_directory",
                "read.kg.open_disputes_by_status",
                "read.kg.overdue_provisional_credit_disputes",
            }
        ),
        "skills": frozenset(
            {
                (
                    "meridian-hardship-draft",
                    3,
                    "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d",
                )
            }
        ),
        "workflow_profiles": frozenset(
            {
                (
                    "meridian-b1-disaster-hardship",
                    1,
                    "e90381d94480ee1d01f028380be39595b63c4c632d6aff89738b49a440e667e0",
                ),
                (
                    "meridian-b2-current-sop",
                    1,
                    "7209a37006427bdc938c34f8e8c3ce798953a1aefa031bb6c81c5d32ffc97fce",
                ),
                (
                    "meridian-b3-fabrication-refusal",
                    1,
                    "5a70614b9270673416f4f5c521b4d3541a22ff131813d00bf925ddbdc2f6e23a",
                ),
                (
                    "meridian-s9-overdue-provisional-credit",
                    1,
                    "f36c68400c64dbbd49f02f38e52cf3face440f9607485de126de5031a60f4708",
                ),
            }
        ),
        "model_policies": frozenset(
            {"agent.meridian-analyst.v2", "agent.meridian-hardship-review.v3"}
        ),
    }
    cold_dependencies = AgentDependencySnapshot(knowledge_sources=frozenset(), **common)
    cold = reconcile_agent_catalog(sa.create_engine("sqlite://"), loaded, cold_dependencies)
    cold_analyst = next(row for row in cold.agents if row.definition.label == "Meridian Analyst")
    assert cold_analyst.availability_reasons == ("knowledge-unavailable",)

    t1_dependencies = AgentDependencySnapshot(
        knowledge_sources=frozenset({("meridian-servicing", "t1")}), **common
    )
    admitted = reconcile_agent_catalog(sa.create_engine("sqlite://"), loaded, t1_dependencies)
    analyst = next(row for row in admitted.agents if row.definition.label == "Meridian Analyst")
    assert analyst.available is True
    assert effective_agent_tool_grants(analyst.definition, t1_dependencies) == (
        "read.knowledge.search",
    )
    assert {
        row.definition.label: row.availability_reasons
        for row in admitted.agents
        if row.definition.workflow is not None
    } == {
        "Meridian Current SOP Review": ("knowledge-unavailable",),
        "Meridian Fabrication Refusal": ("knowledge-unavailable",),
        "Meridian Hardship Review": ("knowledge-unavailable",),
        "Meridian Overdue Credit Review (Deterministic)": ("knowledge-unavailable",),
    }

    local_dependencies = AgentDependencySnapshot(
        knowledge_sources=frozenset(
            {("meridian-servicing", "t1"), ("meridian-governed-local-t3", "t3")}
        ),
        **common,
    )
    local = reconcile_agent_catalog(sa.create_engine("sqlite://"), loaded, local_dependencies)
    assert all(row.available for row in local.agents if row.definition.workflow is not None)


def test_generation_twenty_two_advances_the_changed_definition_versions(tmp_path: Path) -> None:
    """The shipped registry's CURRENT generation/version/hash triple, pinned.

    This test exists to catch an UNINTENDED Meridian Analyst definition change, so
    the literals below move only with a deliberate one. Generation 9 / version 5 was
    the first token-budget advance (`max_input_tokens` 24000 -> 92650); generation
    10 / version 6 was the second (92650 -> 159923), re-derived once
    `_OUTPUT_RETRIES` raised the model-request ceiling from 11 to 14. Generation 11
    / version 7 re-funds the prompt growth from NE-QUOTE-THE-SOURCE
    (159923 -> 162273): a longer tail is re-paid on every request. Generation 12 /
    version 8 funds the per-class retry allowances NE-CLAIM-RETRY-RESERVE derived
    (162273 -> 231007), which move the request ceiling from 14 to 16. Generation
    13 / version 9 grants the verified-answer lane and re-measures the system
    prompt the larger tool catalog re-sends on every request (231007 -> 245855).
    Generation 14 / version 10 is NE-MULTIHOP-FANOUT: the DR-4830 question is 32
    single-key `read.kg.*` reads deep, so the tool ceiling moves 10 -> 34, the
    request ceiling 16 -> 40, the clock 120s -> 400s, and the token budget is
    re-derived for 40 requests (245855 -> 1219607). Generation 15 / version 11
    grants nothing and revokes nothing: it rewrites catalog DESCRIPTIONS, publishes
    a date window's endpoint semantics in its args schema, and adds the claim-form
    and no-derived-totals rules to the answer-transport tail. The fixed prompt all
    of that lives in is re-sent on every one of the 40 requests, so S 2543 -> 3132,
    and the claim-retry slot is re-derived from the message it now actually carries
    (327 -> 403), together moving the budget 1219607 -> 1243243. PROMPT TEXT IS A
    BUDGET INPUT exactly as a Tool grant is, and this is the line that says so.
    Generation 16 / version 12 is NE-CS-SPLIT (owner decision 2026-08-27): the
    Analyst becomes READ-ONLY — `draft.email.create`, the Skill pin and
    `read.skills.view` move to the new Meridian Customer Support agent — and the
    budget is re-derived DOWN for the smaller prompt (two catalog entries and the
    Skill index gone: S 3132 -> 2916, 1243243 -> 1234603), because prompt text is
    a budget input in both directions.
    Generation 17 / version 13 is NE-ANALYST-INSTRUCTIONS: with the Skill pin
    gone the Analyst's runtime prompt had been the rendered Skill index ALONE, so
    the split composed an empty prompt and every run died at the runtime's
    non-empty-instructions guard. The Analyst now declares its own manifest
    `instructions` (required for any agentic agent with zero Skill pins), and the
    budget is re-derived UP for the larger prompt (508 chars = 174 tokens on all
    40 requests: S 2916 -> 3090, 1234603 -> 1241563).
    Generation 18 / version 14 is NE-ANSWER-THE-QUESTION + NE-GRAPH-ANCHOR: no
    grant moves; the answer-transport tail gains the enumeration-question rule,
    the two dispute catalog entries name what their rows serve, and the shared
    `read.knowledge.graph_path` catalog entry grows its id/class endpoint
    semantics, so the fixed prompt is re-measured (S 3090 -> 3358) and the
    budget re-derived UP for the larger prompt re-sent on all 40 requests
    (1241563 -> 1252283).
    Generation 19 advances Meridian CUSTOMER SUPPORT (version 2 -> 3) and leaves
    the Analyst's definition untouched: NE-CS-WHOLE-LADDER (owner decision
    2026-08-28) grants Customer Support the Analyst's five hardship
    verified-answer templates (and the Tier-3 source they read) so one agent can
    establish eligibility and run the draft->approve->send->record ladder. The
    ceilings are re-counted for the whole-ladder turn (10 -> 17 tool calls,
    16 -> 23 requests) and the token budget re-derived for the measured larger
    catalog (S 1932 -> 3871, 236155 -> 478376).
    Generation 20 changes NO existing definition: it ADDS the S9 side-by-side
    pair (owner decision 2026-08-28) — the agentic Meridian Overdue Credit
    Review (one required verified-read grant, its own absolutely-measured
    budget: S = 2883, 3 tool calls, 9 requests, 114710 input tokens) and its
    deterministic twin (the S9 workflow profile's first manifest binding,
    minimum truthful budget 8210). Both enter at version 1; the Analyst's and
    Customer Support's definitions and hashes must NOT move.
    Generation 21 is NE-GA-RUN2, renumbered by the train-57 collision
    resolution: cut against a pre-train-54 base it claimed generation 19, but
    train-56 spent 19 on NE-CS-WHOLE-LADDER and the S9 pair spent 20, so the
    merged registry lands it as 21. It ADDS the Meridian Grounded Analyst and
    changes nothing else — every OTHER definition hash below is unchanged,
    which is itself the assertion that adding the gated agent did not move the
    agents the demo's other beats run on. Its budget was RE-DERIVED on the
    merged train-57 tree rather than copied from the branch (the real
    renderers here measure the fixed prompt to S = 3026, not the branch
    draft's 2953 — the prompted-output schema block renders 3549 chars, the
    same figure the S9 derivation measures — so max_input_tokens is 131523).
    All thirteen derivations live in `deploy/agents/registry.toml`, and each
    necessarily moves the definition hash a Run binds to.
    Generation 22 advances the GROUNDED ANALYST alone (version 1 -> 2), the
    U11 review's F2 correction: its generation-21 description promised that
    "an answer asserting facts the rows retrieved during the run do not prove
    is withheld", which the gate mechanism does not deliver (wrong-row prose
    pairing passes at every tier — provenance is proven, meaning is not), and
    the deployed Run posture is now pinned STRICT by its own knob
    (NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS). Prose is definition input exactly
    as prompt text is budget input: the hash moves on the corrected sentence
    alone, so immutability demands the version move with it. No grant, budget,
    model policy or instruction changed — every other definition hash below is
    unchanged.
    Generation 25 advances the GOVERNANCE SELF-TEST agent alone (version 1 ->
    2), NE-INERT-BUDGET: `max_steps` 6 -> 23 and nothing else. At 6 the enforced
    turn wall clock was 60 s, and the agent's own two-call ladder — one governed
    `read.knowledge.search`, which this box's CPU-only reranker serves in
    108-207 s — cannot finish inside that, so the agent never reached the Tier-2
    park it exists to prove. 23 is the CEILING the published 180 s approval
    window derives (`test_approval_window_is_honourable.py`), so the parking
    maximum ties rather than moves and the window is unchanged. No grant, model
    policy, token budget or prose-bearing field moved; `max_tool_calls` is
    `min(23, 4)` either way, so the ONLY enforced number that changes is the
    clock.
    Generation 23 advances the GROUNDED ANALYST alone again (version 2 -> 3),
    the NE-GROUND-REL correction (owner ruling 2026-08-29): the gate now
    ENFORCES semantic row binding (the `relation-ungrounded` fact class), so
    the description is corrected to state the spec's bar as the standing
    requirement, name what is mechanically enforced, and disclose the
    remaining swapped-compound gap — instead of the generation-22 narrowed
    "provenance, not meaning" wording. Same shape as 21 -> 22: prose alone
    moves the hash, so the version moves with it.
    Generation 26 is NE-SHAPE-ALLOWANCE-4, and it advances FOUR definitions at
    once — the Analyst (14 -> 15), Customer Support (4 -> 5), the Grounded
    Analyst (3 -> 4) and the agentic Overdue Credit Review (1 -> 2) — because
    all four `max_input_tokens` figures are DERIVED from the output-retry pool
    and `ne.runtime.runner._OUTPUT_RETRY_ALLOWANCES["answer-shape"]` moved 3 ->
    4 on a measured live run.
    Generation 27 is NE-WRAPPED-CALL-FUNDED and advances the SAME four
    (15 -> 16, 5 -> 6, 4 -> 5, 2 -> 3), for the same reason one class further:
    the pool gained `wrapped-call`, whose allowance is not a constant at all but
    one corrective round per rung, so the request ceiling is
    `2*max_tool_calls + 7` — Analyst 41 -> 75, Support 24 -> 41, Grounded
    11 -> 15, Overdue 10 -> 13 — and every budget is re-derived from the tests'
    own formula in `test_agent_execution_caps.py` (1331813 -> 3666637,
    534959 -> 1198989, 169321 -> 230437, 150985 -> 191221; those are
    `max_total_tokens`, i.e. the manifest figures plus the 4000 output half).
    Left unfunded, the Analyst alone would have been 2334824 short and the run
    would have died `budget_exhausted`/`tokens` before reaching the correction
    the class exists to buy. NO CEILING MOVED in either generation: `max_steps`
    is held everywhere (Customer Support's 23 is the subtrahend of the published
    180 s approval window and cannot move; the other three are held with it),
    and the three deterministic definitions and the deterministic S9 twin have
    no output-retry pool to fund, so their hashes must NOT move.
    All fourteen derivations live in `deploy/agents/registry.toml`.
    """
    current_text = _REGISTRY.read_text(encoding="utf-8")
    # The predecessor is the current registry with NE-WRAPPED-CALL-FUNDED undone:
    # the four agentic definitions back at their generation-26 versions and
    # wrapped-call-free token budgets, at the generation immediately below
    # HEAD's.
    # Reconciling it FIRST and the shipped registry SECOND proves the exact
    # upgrade the deployed catalog store performs — same-version redefinition
    # would be refused as immutable drift, so this is also the proof the four
    # version bumps were mandatory.
    #
    # HONEST SCOPE OF THIS PREDECESSOR (U9's disclosure, kept and adapted
    # through the U11 merge and again here): it is MANUFACTURED from the
    # already-current registry, so every definition OTHER than the four is
    # byte-identical to HEAD's by construction. What this pin exercises is
    # therefore "the known changed definitions changed, each with its own
    # version advanced, and nothing else moved" — the real generation 26 -> 27
    # transition. Drift in a definition whose version did NOT move is the guard
    # this shape structurally cannot test; that is covered by
    # `test_a_changed_definition_at_an_unchanged_identity_is_refused` below,
    # which reconciles the REAL shipped registry first and then a genuinely
    # divergent successor.
    previous_text = current_text.replace("\ngeneration = 27\n", "\ngeneration = 26\n", 1)
    # Anchored on each agent's OWN id line, because `version = N` alone is not
    # unique in this file (the deterministic agents and the workflow pins reuse
    # the same small integers).
    for agent_id, current_version, previous_version, current_budget, previous_budget in (
        ("agt_01k3meridiananalyst000000000", 16, 15, 3662637, 1327813),
        ("agt_01k3customersupport000000000", 6, 5, 1194989, 530959),
        ("agt_01k3groundedanalyst000000000", 5, 4, 226437, 165321),
        ("agt_01k3overduecreditreview00000", 3, 2, 187221, 146985),
    ):
        marker = f'agent_id = "{agent_id}"'
        start = previous_text.index(marker)
        end = previous_text.index("\n[[agents]]\n", start)
        block = previous_text[start:end]
        assert block.count(f"\nversion = {current_version}\n") == 1
        assert block.count(f"\nmax_input_tokens = {current_budget}\n") == 1
        rolled = block.replace(
            f"\nversion = {current_version}\n", f"\nversion = {previous_version}\n", 1
        ).replace(
            f"\nmax_input_tokens = {current_budget}\n",
            f"\nmax_input_tokens = {previous_budget}\n",
            1,
        )
        previous_text = previous_text[:start] + rolled + previous_text[end:]
    assert previous_text != current_text
    previous_path = tmp_path / "registry-generation-26.toml"
    previous_path.write_text(previous_text, encoding="utf-8")
    dependencies = AgentDependencySnapshot(
        tools=frozenset(),
        knowledge_sources=frozenset(),
        skills=frozenset(),
        workflow_profiles=frozenset(),
        model_policies=frozenset(),
    )
    engine = sa.create_engine("sqlite://")

    previous = reconcile_agent_catalog(engine, load_agent_manifest(previous_path), dependencies)
    # The predecessor is not merely "different" — it is the generation-26
    # deployment, to the hash. Pinned here so that "the four versions advanced"
    # is a statement about a REAL prior state rather than about whatever the
    # rollback above happened to produce, and so that a semantic drift sneaking
    # into the parts this change did not touch fails HERE rather than silently
    # re-anchoring the transition.
    previous_by_label = {row.definition.label: row.definition for row in previous.agents}
    assert {
        label: (definition.version, definition.definition_hash)
        for label, definition in previous_by_label.items()
        if definition.workflow is None and definition.default_strategy == "agentic"
    } == {
        "Meridian Analyst": (
            15,
            "cfe2d8f8d1331dbba18ed37c1f7bdcb12b543af02342a9318598b8898c12c2c7",
        ),
        "Meridian Customer Support": (
            5,
            "d1068b73b6fa6e1478eb1ba10a6fa6c21b371ca3831128923dcdf02754724052",
        ),
        "Meridian Grounded Analyst": (
            4,
            "3bd01c3d375b07b9a6ce244c3d0d6d494561b016baafd6e8a24fec3848dcea73",
        ),
        "Meridian Overdue Credit Review": (
            2,
            "aa58fdc899e5fb26cfcfeb1353e9fc242f2fd5055c72dc8787217e99b65524cf",
        ),
        # Untouched by this change and by its rollback: the self-test agent's
        # budget is sized from CONTINUITY_PROMPT_RESERVE_TOKENS, not from the
        # output-retry pool, so it neither funds nor needs the sixth retry.
        "Governance Self-Test (Inert Write-Back)": (
            2,
            "1f72606cdbbb007fa0901a751c9849b55e0b8e7cb0d5eafc8a573c916fb02d74",
        ),
    }
    advanced = reconcile_agent_catalog(engine, load_agent_manifest(_REGISTRY), dependencies)

    analyst = next(row for row in advanced.agents if row.definition.label == "Meridian Analyst")
    assert analyst.definition.version == 16
    assert analyst.definition.definition_hash == (
        "bb3f8341666013d46dc7777ae487bf7211e15cc802900d69829a584ac3c27960"
    )
    assert analyst.definition.budgets.max_input_tokens == 3662637
    support = next(
        row for row in advanced.agents if row.definition.label == "Meridian Customer Support"
    )
    assert support.definition.version == 6
    assert support.definition.definition_hash == (
        "1842d8d6c3abf27e395377d29fbd9f32ac3a2ed39b0f4a0cff1e7f558e31f670"
    )
    assert support.definition.budgets.max_input_tokens == 1194989
    grounded = next(
        row for row in advanced.agents if row.definition.label == "Meridian Grounded Analyst"
    )
    assert grounded.definition.version == 5
    assert grounded.definition.definition_hash == (
        "10c7ee214033c2ae64b6f9d31bb39180dd115b74bf0a577f69fa46e3ab6faccf"
    )
    assert grounded.definition.budgets.max_input_tokens == 226437
    overdue = next(
        row for row in advanced.agents if row.definition.label == "Meridian Overdue Credit Review"
    )
    assert overdue.definition.version == 3
    assert overdue.definition.definition_hash == (
        "93e6dee40f34d562697d4a6c2f6b99f5c3144b5b80bd0c9b93e832c95627549c"
    )
    assert overdue.definition.budgets.max_input_tokens == 187221
    overdue_det = next(
        row
        for row in advanced.agents
        if row.definition.label == "Meridian Overdue Credit Review (Deterministic)"
    )
    # UNCHANGED, and that is the assertion: the deterministic twin has no model
    # in its answer path, so no output-retry pool to fund. A generation-27 hash
    # move here would mean the budget re-derivation had reached a definition it
    # has no business touching.
    assert overdue_det.definition.version == 1
    assert overdue_det.definition.definition_hash == (
        "cc328cc41aa8fd791d28653c1b6a733e844adb9fe3044f9ee2adbe14d0ef1a74"
    )
    assert overdue_det.definition.budgets.max_input_tokens == 8210


def test_shared_composition_binds_manifest_agent_to_exact_runtime_profile() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    profiles, bindings = _compose_workflow_execution_bindings(loaded)
    expected = {
        "agt_01k3hardshipreview0000000000": (5, MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE),
        "agt_01k3currentsopreview000000000": (2, MERIDIAN_B2_CURRENT_SOP_PROFILE),
        "agt_01k3fabricationrefusal000000": (2, MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE),
        # Generation 20: the S9 profile, bundled ahead of its agent since the
        # roster reconciliation, is now BOUND by the deterministic Meridian
        # Overdue Credit Review.
        "agt_01k3overduecreditreviewdet00": (1, MERIDIAN_S9_OVERDUE_CREDIT_PROFILE),
    }

    assert profiles == {profile.workflow_id: profile for _version, profile in expected.values()}
    assert set(bindings) >= set(expected)
    for agent_id, (version, profile) in expected.items():
        binding = bindings[agent_id]
        assert binding.agent_version == version
        assert binding.profile_identity == (
            profile.workflow_id,
            profile.version,
            profile.config_hash,
        )


def test_packaged_registries_are_identical_and_carry_the_minimum_truthful_budget() -> None:
    profiles = {
        profile.workflow_id: profile
        for profile in (
            MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
            MERIDIAN_B2_CURRENT_SOP_PROFILE,
            MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
            MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
        )
    }
    registries = (
        _REGISTRY,
        _ROOT / "deploy/helm/neutron-enterprise/files/agents/registry.toml",
        _ROOT / "deploy/helm/neutron-enterprise/charts/ne-api/files/agents/registry.toml",
        _ROOT / "deploy/helm/neutron-enterprise/charts/ne-worker/files/agents/registry.toml",
    )
    canonical_inputs = tuple(
        json.dumps(
            MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.validate_input(
                {"max_subjects": value}
            ).model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        for value in range(1, 6)
    )
    minimum_budget = CONTINUITY_PROMPT_RESERVE_TOKENS + max(map(len, canonical_inputs))
    assert set(map(len, canonical_inputs)) == {18}
    assert minimum_budget == 8210
    source_bytes = _REGISTRY.read_bytes()
    for registry in registries:
        assert registry.read_bytes() == source_bytes
        loaded = load_agent_manifest(registry)
        assert loaded.manifest.generation == 27
        deterministic = [
            definition for definition in loaded.manifest.agents if definition.workflow is not None
        ]
        assert len(deterministic) == 4
        for definition in deterministic:
            assert definition.workflow is not None
            profile = profiles[definition.workflow.profile_id]
            assert definition.budgets.max_input_tokens == minimum_budget
            assert definition.workflow.config_hash == profile.config_hash
            assert definition.workflow.frozen_inputs.canonical_values() == dict(
                profile.fixed_inputs
            )
            dependency = next(
                item
                for item in definition.availability_dependencies
                if item.kind == "workflow-profile"
            )
            assert dependency.content_hash == profile.config_hash


def test_shared_composition_rejects_a_manifest_profile_pin_that_drifted() -> None:
    loaded = load_agent_manifest(_REGISTRY)
    definitions = list(loaded.manifest.agents)
    index = next(
        offset
        for offset, definition in enumerate(definitions)
        if definition.agent_id == "agt_01k3hardshipreview0000000000"
    )
    hardship = definitions[index]
    assert hardship.workflow is not None
    definitions[index] = hardship.model_copy(
        update={"workflow": hardship.workflow.model_copy(update={"config_hash": "0" * 64})}
    )
    drifted = loaded.model_copy(
        update={"manifest": loaded.manifest.model_copy(update={"agents": tuple(definitions)})}
    )

    with pytest.raises(ValueError, match="profile pin does not match"):
        _compose_workflow_execution_bindings(drifted)


def test_bundled_skill_validates_with_standard_and_manifest_pins_exact_hash() -> None:
    pack = pack_from_dir(_SKILL, standard=AuthoringStandard())
    assert isinstance(pack, SkillPack), pack
    assert pack.name == "meridian-hardship-draft"
    assert pack.version == 3
    assert pack.content_hash == "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d"
    assert set(pack.requires_tools) == {
        "draft.email.create",
        "read.knowledge.graph_path",
        "read.knowledge.search",
        "read.skills.view",
    }
    loaded = load_agent_manifest(_REGISTRY)
    # NE-CS-SPLIT: the pin lives on Meridian Customer Support now (the Analyst
    # carries no Skill at all).
    support = next(
        definition
        for definition in loaded.manifest.agents
        if definition.label == "Meridian Customer Support"
    )
    assert support.skills == (
        (
            support.skills[0].model_copy(
                update={
                    "skill_id": pack.name,
                    "version": pack.version,
                    "content_hash": pack.content_hash,
                }
            )
        ),
    )
    skill_dependencies = tuple(
        dependency for dependency in support.availability_dependencies if dependency.kind == "skill"
    )
    assert len(skill_dependencies) == 1
    assert (
        skill_dependencies[0].dependency_id,
        skill_dependencies[0].version,
        skill_dependencies[0].content_hash,
    ) == (pack.name, pack.version, pack.content_hash)


def test_production_agent_routes_are_authenticated_bounded_and_non_cacheable(
    make_app: AppBuilder,
) -> None:
    harness = make_app(agent_manifest_path=str(_REGISTRY))
    ada = harness.bearer("u-ada")
    ops = harness.bearer("u-ops", groups=("ops",))
    unrelated = harness.bearer("u-unrelated", groups=("finance",))

    async def exercise() -> None:
        async with harness.client() as client:
            unauthenticated = await client.get("/api/v1/agents")
            assert unauthenticated.status_code == 401

            listed = await client.get("/api/v1/agents", headers=ada)
            assert listed.status_code == 200
            assert listed.headers["cache-control"] == "private, no-store"
            # NE-GA-RUN2: the id-ordered listing now leads with the Grounded
            # Analyst (agt_01k3grounded... < agt_01k3meridian...), so select
            # the Analyst by label rather than by position.
            labels = [item["label"] for item in listed.json()["items"]]
            assert "Meridian Grounded Analyst" in labels
            analyst = next(
                item for item in listed.json()["items"] if item["label"] == "Meridian Analyst"
            )

            fetched = await client.get(f"/api/v1/agents/{analyst['agent_id']}", headers=ada)
            assert fetched.status_code == 200
            assert fetched.headers["cache-control"] == "private, no-store"

            hidden_id = "agt_01k3hardshipreview0000000000"
            hidden = await client.get(f"/api/v1/agents/{hidden_id}", headers=ada)
            random = await client.get(
                "/api/v1/agents/agt_01k3randomrandomrandom0000000", headers=ada
            )
            assert (
                (hidden.status_code, hidden.content, hidden.headers["cache-control"])
                == (
                    random.status_code,
                    random.content,
                    random.headers["cache-control"],
                )
                == (404, b'{"detail":"Agent not found"}', "private, no-store")
            )
            ops_visible = await client.get(f"/api/v1/agents/{hidden_id}", headers=ops)
            assert ops_visible.status_code == 200
            unrelated_hidden = await client.get(f"/api/v1/agents/{hidden_id}", headers=unrelated)
            assert (
                unrelated_hidden.status_code,
                unrelated_hidden.content,
                unrelated_hidden.headers["cache-control"],
            ) == (404, b'{"detail":"Agent not found"}', "private, no-store")

    run(exercise())

    inner: Any = next(
        cast(Any, route.app)._app
        for route in harness.app.routes
        if isinstance(route, Mount) and route.path == "/api"
    )
    agent_routes: dict[str, set[str]] = {}
    for route in iter_route_contexts(inner.routes):
        if route.path is not None and route.path.startswith("/v1/agents"):
            agent_routes.setdefault(route.path, set()).update(route.methods or ())
    assert {path: frozenset(values) for path, values in agent_routes.items()} == {
        "/v1/agents": frozenset({"GET"}),
        "/v1/agents/updates": frozenset({"GET"}),
        "/v1/agents/{agent_id}": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations": frozenset({"GET", "POST"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}/context-ledger": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}/runs": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}/turns": frozenset({"GET"}),
        "/v1/agents/{agent_id}/preflight": frozenset({"GET"}),
        "/v1/agents/{agent_id}/workflow-profile": frozenset({"GET"}),
        "/v1/agents/{agent_id}/runs": frozenset({"GET", "POST"}),
        "/v1/agents/{agent_id}/runs/{run_id}": frozenset({"GET"}),
        "/v1/agents/{agent_id}/runs/{run_id}/evidence": frozenset({"GET"}),
        "/v1/agents/{agent_id}/runs/{run_id}/evidence/export": frozenset({"GET"}),
        "/v1/agents/{agent_id}/runs/{run_id}/cancel": frozenset({"POST"}),
        "/v1/agents/{agent_id}/runs/{run_id}/events": frozenset({"GET"}),
        "/v1/agents/{agent_id}/runs/{run_id}/retry": frozenset({"POST"}),
    }


def test_model_policy_availability_comes_from_composed_gateway_task_ids(
    make_app: AppBuilder,
) -> None:
    missing = get_state(make_app(agent_manifest_path=str(_REGISTRY)).app)
    analyst = next(
        row for row in missing.agent_catalog.agents if row.definition.label == "Meridian Analyst"
    )
    assert "model-policy-unavailable" in analyst.availability_reasons

    reset_config_hash_for_testing()
    exact = get_state(
        make_app(
            agent_manifest_path=str(_REGISTRY),
            gateway_tasks={"agent.meridian-analyst.v2": TaskRoute(model="stub")},
        ).app
    )
    exact_analyst = next(
        row for row in exact.agent_catalog.agents if row.definition.label == "Meridian Analyst"
    )
    assert "model-policy-unavailable" not in exact_analyst.availability_reasons

    reset_config_hash_for_testing()
    unrelated = get_state(
        make_app(
            agent_manifest_path=str(_REGISTRY),
            gateway_tasks={"unrelated": TaskRoute(model="stub")},
        ).app
    )
    unrelated_analyst = next(
        row for row in unrelated.agent_catalog.agents if row.definition.label == "Meridian Analyst"
    )
    assert "model-policy-unavailable" in unrelated_analyst.availability_reasons


def test_evidence_projection_mount_requires_and_reuses_protected_runtime(
    make_app: AppBuilder,
) -> None:
    def inner(application: object) -> Any:
        return next(
            cast(Any, route.app)._app
            for route in cast(Any, application).routes
            if isinstance(route, Mount) and route.path == "/api"
        )

    unprotected = make_app(agent_manifest_path=str(_REGISTRY)).app
    assert getattr(inner(unprotected).state, EVIDENCE_SERVICE_STATE_KEY) is None

    reset_config_hash_for_testing()
    protected = make_app(agent_manifest_path=str(_REGISTRY), protected_transport=True).app
    state = get_state(protected)
    service = getattr(inner(protected).state, EVIDENCE_SERVICE_STATE_KEY)
    assert isinstance(service, EvidenceProjector)
    assert state.agent_run_store is not None
    reader = cast(Any, service._reader)
    materializer = cast(Any, service._materializer)
    assert reader._engine is state.agent_run_store._engine
    assert isinstance(materializer, RunStoreCurrentEvidenceMaterializer)
    assert materializer._store is state.agent_run_store


def test_delivered_skill_bundle_closes_only_the_skill_dependency(tmp_path: Path) -> None:
    """PRE-011B: after 011F delivery the Skill reason is gone — and NOTHING else is.

    The honest post-011B state for Meridian Analyst is that its Skill dependency is
    closed by an exact hash-verified pin while its Knowledge dependency is still
    hardcoded empty in ``build_platform``, so the Agent remains correctly
    unavailable. Asserting the removal alone would pass on a composition that
    dropped the reason unconditionally, so the undelivered pole above and the four
    failure poles in ``test_skills_platform.py`` carry the other direction.
    """
    from ne.api.platform import build_platform
    from ne.api.skills_delivery import reconcile_required_agent_skills
    from ne.audit import create_engine_with_timeouts, ensure_transactional_sqlite
    from ne.skills.config import SkillsSettings
    from ne.skills.registry import ensure_skills_schema

    from .test_skills_platform import RAW_HASH, _api_settings, _seams

    signal_url = f"sqlite:///{tmp_path}/catalog-signals.db"
    engine = create_engine_with_timeouts(signal_url)
    try:
        ensure_transactional_sqlite(engine)
        ensure_skills_schema(engine)
        reconcile_required_agent_skills(
            engine,
            SkillsSettings(bundle_dir=str(_SKILL.parent)),  # type: ignore[call-arg]
            agent_manifest_path=str(_REGISTRY),
            expected_raw_hash=RAW_HASH,
        )
    finally:
        engine.dispose()

    reset_config_hash_for_testing()
    platform = build_platform(
        _api_settings(agent_manifest_path=str(_REGISTRY)),
        _seams(tmp_path, key="catalog-delivered", signal_url=signal_url),
    )
    try:
        analyst = {row.definition.label: row for row in platform.agent_catalog.agents}[
            "Meridian Analyst"
        ]
        assert analyst.available is False
        assert "skill-unavailable" not in analyst.availability_reasons
        assert "knowledge-unavailable" in analyst.availability_reasons
    finally:
        for composed in platform.engines:
            composed.dispose()


# --- the analyst's token budget is DERIVED, and it is still a budget -----------

#: The three measured/configured figures the Analyst's token budget is derived
#: from, restated here so the pin below is arithmetic a reader can check rather
#: than a literal someone chose.
_SEARCH_TOP_K = 5
"""``ApiSettings.retrieval_top_k`` — chunks one ``read.knowledge.search`` serves."""
_CHUNK_TOKENS = 239
"""One live corpus chunk, MEASURED via TEI ``/tokenize`` over 20 real ``ne_chunks``
rows (mean 700.8 chars), not assumed from a chunker setting."""
_FIXED_PROMPT_TOKENS = 3132
"""What every request re-sends: the in-band tool catalog for the ELEVEN granted
Tools (``ne.runtime.model._catalog_text``), the PromptedOutput schema block for
the structured completion, its answer-transport tail, the Skill index and the
question, at the ~2.93 chars/token the same measurement gives. This is the
registry derivation's own ``S``; the two must not drift, because a floor computed
from a smaller prompt than the one actually sent is not a floor.

NE-LANE-CHOICE / NE-CLAIM-ROW-FORM / NE-WINDOW-ENDPOINTS moved it 2543 -> 3132
without granting or revoking a Tool: they rewrote catalog DESCRIPTIONS, published
a date window's endpoint semantics in its args schema, and added the claim-form
and no-derived-totals rules to the transport tail -- 1723 chars in all. Prompt
TEXT is a budget input exactly as a Tool grant is, and the itemisation lives with
the derivation in ``deploy/agents/registry.toml``."""


def test_the_analyst_token_budget_outlasts_its_own_tool_call_ceiling() -> None:
    """The token budget must not be what stops a run the call ceiling would allow.

    At 24000+4000 it WAS: the demo's DR-4830 question terminated
    `budget_exhausted` having spent 4 of its 10 permitted Tool calls, which makes
    the call ceiling fictional and makes "raise it until the demo works" the only
    available remedy. The budget is instead sized from the ceiling that IS meant to
    bind: cost a run that spends every permitted call on the most expensive Tool (a
    top-k knowledge search over the real corpus) and still writes a full answer.
    """
    _profiles, bindings = _compose_workflow_execution_bindings(load_agent_manifest(_REGISTRY))
    adapter = bindings["agt_01k3meridiananalyst000000000"].agentic_adapter
    assert adapter is not None

    calls = adapter.max_tool_calls
    requests = calls + 1  # exactly how run_model_planned_agent derives its own limit
    search_result = _SEARCH_TOP_K * _CHUNK_TOKENS + 105  # + chunk ids, header, prefix
    tool_call_turn = 80  # the assistant's one-line tool_call JSON incl. step_intent
    # Request k re-sends the fixed prompt plus everything the k-1 prior steps put
    # in history, so the inputs are an arithmetic series over the request count.
    prompt_tokens = requests * _FIXED_PROMPT_TOKENS + (search_result + tool_call_turn) * (
        requests * (requests - 1) // 2
    )
    final_answer = 4000  # the Agent's own max_output_tokens
    required = prompt_tokens + calls * tool_call_turn + final_answer

    assert (search_result, prompt_tokens, required) == (1300, 930720, 937440)
    assert adapter.total_tokens_limit >= required
    # And it is not wildly past THAT ceiling either. The slack is the corrective
    # retries, which this figure deliberately does not model: each one re-pays
    # the whole history AND grows it, so the funded budget is a multiple of the
    # no-retry cost rather than a margin on it. The bound is the retry pool the
    # request ceiling already states, so a budget inflated past what the retries
    # can spend still fails here. The exact arithmetic lives in
    # `test_agent_execution_caps.py`.
    #
    # NE-WRAPPED-CALL-FUNDED made that pool a FUNCTION of the ladder (one
    # corrective round per rung), so it is 40 here, not 6, and the old
    # `(1 + retries // 2) * required` coefficient — calibrated when the pool was
    # a constant 6 — would have become meaningless slack. The ceiling is instead
    # spelled out: a retry can cost at most the deepest request this run makes
    # plus every increment the pool could carry, and it writes at most one full
    # answer. Deliberately CRUDE (every retry charged the largest possible
    # increment, which no real sequence reaches), so it bounds rather than
    # re-derives; the exact figure is pinned in `test_agent_execution_caps.py`.
    retries = adapter_request_limit(calls) - requests
    assert retries == 40
    deepest_request = _FIXED_PROMPT_TOKENS + calls * (search_result + tool_call_turn)
    worst_increment = final_answer + 403  # the largest correction the pool carries
    worst_retry = deepest_request + retries * worst_increment + final_answer
    assert adapter.total_tokens_limit < required + retries * worst_retry


def test_the_analyst_is_still_bounded_after_the_token_budget_raise() -> None:
    """THE CONTROL: re-sizing the ceilings must not un-bound the Agent.

    NE-MULTIHOP-FANOUT moved the call ceiling 10 -> 34 and the clock 120s -> 400s
    because the DR-4830 question is 32 single-key `read.kg.*` reads deep. Raised
    is not removed: a genuinely runaway run is still stopped by the call, request,
    wall-clock and parked-write ceilings, which is where the stop belonged all
    along. Without this half, the pin above would pass equally against simply
    deleting the budgets.
    """
    _profiles, bindings = _compose_workflow_execution_bindings(load_agent_manifest(_REGISTRY))
    adapter = bindings["agt_01k3meridiananalyst000000000"].agentic_adapter
    assert adapter is not None
    assert adapter.max_tool_calls == 34
    assert adapter.wall_clock_seconds == 400.0
    # NE-CS-SPLIT: the read-only Analyst has zero non-read grants left, so it
    # may park zero writes — the drafting agent carries the write surface now.
    assert adapter.max_parked_writes == 0
    # Finite, and still the exact sum of the manifest's two halves — the token
    # ceiling was raised, not removed and not decoupled from the registry.
    definition = next(
        item
        for item in load_agent_manifest(_REGISTRY).manifest.agents
        if item.agent_id == "agt_01k3meridiananalyst000000000"
    )
    assert adapter.total_tokens_limit == (
        definition.budgets.max_input_tokens + definition.budgets.max_output_tokens
    )
    # The output half is held at what the serving lane can actually serve.
    assert definition.budgets.max_output_tokens == 4000


def test_a_changed_definition_at_an_unchanged_identity_is_refused(tmp_path: Path) -> None:
    """The drift guard, exercised against a REAL prior state rather than a made-up one.

    This is the check the generation pin above structurally cannot make. Its
    "previous" manifest is cut from the current one, so every definition it
    carries already equals HEAD's; presenting it can only ever prove that adding
    an agent works.

    Here the ALREADY-SHIPPED registry is reconciled first -- that is the real
    prior state, byte-for-byte, not a reconstruction -- and only then is a
    divergent successor offered. Both refusals matter and they are different
    guards:

      * a semantic change WITHOUT a generation bump is "immutable Agent registry
        generation drift", refused before any per-agent comparison; and
      * a semantic change WITH a generation bump, but leaving some agent's
        ``version`` where it was, is "immutable Agent definition drift".

    This is the failure a branch that edits a budget or a grant and forgets to
    mint a new generation would hit AT BOOT, in both process roots, which is why
    it belongs in a test and not in a reviewer's head.
    """
    current_text = _REGISTRY.read_text(encoding="utf-8")
    dependencies = AgentDependencySnapshot(
        tools=frozenset(),
        knowledge_sources=frozenset(),
        skills=frozenset(),
        workflow_profiles=frozenset(),
        model_policies=frozenset(),
    )
    engine = sa.create_engine("sqlite://")
    # The real shipped manifest is the prior state.
    reconcile_agent_catalog(engine, load_agent_manifest(_REGISTRY), dependencies)

    # A definition-bearing edit: Customer Support's tool ceiling. Deliberately a
    # BUDGET rather than a comment, so the SEMANTIC hash moves, not just the raw
    # bytes (a comment-only edit is a different, weaker refusal).
    # Newline-anchored: the same text also appears inside the derivation COMMENT
    # a few lines above the declaration, and editing prose is not a drift test.
    assert current_text.count("\nmax_tool_calls = 17\n") == 1
    drifted = current_text.replace("\nmax_tool_calls = 17\n", "\nmax_tool_calls = 16\n", 1)
    assert drifted != current_text

    same_generation = tmp_path / "registry-same-generation.toml"
    same_generation.write_text(drifted, encoding="utf-8")
    with pytest.raises(AgentCatalogError, match="immutable Agent registry generation drift"):
        reconcile_agent_catalog(engine, load_agent_manifest(same_generation), dependencies)

    # Bumping the generation is not enough on its own: the CHANGED agent must
    # also advance its own version, or the definition it is bound to has moved
    # under every Run that already recorded its hash. (Written against
    # generation 21; re-anchored to 25 by NE-INERT-BUDGET, to 26 by
    # NE-SHAPE-ALLOWANCE-4 and to 27 by NE-WRAPPED-CALL-FUNDED, which re-derives
    # the four agentic token budgets for the ladder-sized `wrapped-call` class.
    # The shipped generation is whatever HEAD's registry says, advanced by one.)
    assert drifted.count("\ngeneration = 27\n") == 1
    advanced = drifted.replace("\ngeneration = 27\n", "\ngeneration = 28\n", 1)
    advanced_path = tmp_path / "registry-advanced-generation.toml"
    advanced_path.write_text(advanced, encoding="utf-8")
    with pytest.raises(AgentCatalogError, match="immutable Agent definition drift"):
        reconcile_agent_catalog(engine, load_agent_manifest(advanced_path), dependencies)

    # Positive control, so the two refusals above are not merely "any edit
    # fails": the SAME edit with the agent's own version advanced is ACCEPTED.
    versioned = advanced.replace(
        'agent_id = "agt_01k3customersupport000000000"\nversion = 6',
        'agent_id = "agt_01k3customersupport000000000"\nversion = 7',
        1,
    )
    assert versioned != advanced, "the version line moved; re-read the registry"
    versioned_path = tmp_path / "registry-versioned.toml"
    versioned_path.write_text(versioned, encoding="utf-8")
    accepted = reconcile_agent_catalog(engine, load_agent_manifest(versioned_path), dependencies)
    support = next(
        row for row in accepted.agents if row.definition.label == "Meridian Customer Support"
    )
    assert (support.definition.version, support.definition.budgets.max_tool_calls) == (7, 16)
