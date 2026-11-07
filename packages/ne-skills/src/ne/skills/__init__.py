# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Skill Forge read side (ne-skills) — registry, discovery tools, ONE-ACL, active set.

The stack-agnostic core: this package's RUNTIME closure is ne-contracts + pydantic
+ sqlalchemy + pyyaml + anyio ONLY. It imports NO provider SDK and NO
pydantic-ai/dbos/ne.runtime (D-10 boundary, enforced by
``tests/test_import_boundary.py`` and the ``.importlinter`` C3 rosters). Governance,
the executor, and the model path are all composition-root concerns; ne-skills ships
the pure registry + filters + handler closures the roots wire up.
"""

from __future__ import annotations

from ne.skills.active_set import ActiveSetComputer, render_index
from ne.skills.apply import AppliedSkill, apply_skill_candidate
from ne.skills.authoring import (
    SKILL_AUTHOR_AGENT_NAME,
    SKILL_AUTHOR_INSTRUCTIONS,
    parse_author_output,
    render_author_task,
)
from ne.skills.config import SkillsSettings
from ne.skills.errors import (
    PrincipalUnboundError,
    SkillApplyConfigError,
    SkillApplyError,
    SkillApplyGateError,
    SkillApplyIntegrityError,
    SkillApplyLabelError,
    SkillApplyVersionError,
    SkillAuthoringParseError,
    SkillAuthoringUnavailableError,
    SkillError,
    SkillEvalGateError,
    SkillForgeAuditError,
    SkillNotFoundError,
)
from ne.skills.evals import (
    SKILL_TRIGGER_SUITE_NAME,
    SkillTriggerEvalSuite,
    build_eval_ref,
    verify_skill_eval_ref,
    verify_skill_proposal_payload,
)
from ne.skills.forge import ForgeOutcome, SkillForge, ensure_forge_schema, ne_skill_forge_cursor
from ne.skills.gardener import (
    GardenerOutcome,
    GardenerReport,
    HashDriftFlag,
    NearDupPair,
    SkillGardener,
    StaleProposalFlag,
    TriggerOverlap,
    UnknownToolsFlag,
)
from ne.skills.governed_forge import (
    FORGE_TOOL_NAME,
    forge_tool_handler,
    forge_tool_spec,
    scheduled_forge_job,
)
from ne.skills.governed_gardener import (
    GARDENER_TOOL_NAME,
    gardener_tool_handler,
    gardener_tool_spec,
    scheduled_gardener_job,
)
from ne.skills.packio import pack_from_dir, parse_skill_md, serialize_pack
from ne.skills.reconcile import ReconcileReport, reconcile_bundle_dir
from ne.skills.registry import (
    LibraryRow,
    SqlAlchemySkillRegistry,
    ensure_skills_schema,
    skills_metadata,
)
from ne.skills.retirement import (
    FailureFlag,
    RetirementCandidate,
    failure_flags,
    restore_skill,
    retire_skill,
    retirement_candidates,
)
from ne.skills.signature import workflow_signature
from ne.skills.standard import (
    StandardViolation,
    lint_body,
    lint_description,
    normalize_trigger,
    trigger_overlap_violations,
    validate_pack_fields,
)
from ne.skills.substrate import AuthorDispatch, PydanticAiSkillSubstrate
from ne.skills.suppression import SuppressionDecision, suppression_decisions
from ne.skills.telemetry import FoldOutcome, attribute_outcomes, fold_invocations
from ne.skills.tools import (
    SEARCH_TOOL_NAME,
    VIEW_TOOL_NAME,
    build_search_handler,
    build_skill_tool_specs,
    build_view_handler,
    score_pack,
    search_packs,
)
from ne.skills.visibility import GroupExpander, match_labels, visible
from ne.skills.worthiness import ensure_birth_schema

__all__ = [
    "FORGE_TOOL_NAME",
    "GARDENER_TOOL_NAME",
    "SEARCH_TOOL_NAME",
    "SKILL_AUTHOR_AGENT_NAME",
    "SKILL_AUTHOR_INSTRUCTIONS",
    "SKILL_TRIGGER_SUITE_NAME",
    "VIEW_TOOL_NAME",
    "ActiveSetComputer",
    "AppliedSkill",
    "AuthorDispatch",
    "FailureFlag",
    "FoldOutcome",
    "ForgeOutcome",
    "GardenerOutcome",
    "GardenerReport",
    "GroupExpander",
    "HashDriftFlag",
    "LibraryRow",
    "NearDupPair",
    "PrincipalUnboundError",
    "PydanticAiSkillSubstrate",
    "ReconcileReport",
    "RetirementCandidate",
    "SkillApplyConfigError",
    "SkillApplyError",
    "SkillApplyGateError",
    "SkillApplyIntegrityError",
    "SkillApplyLabelError",
    "SkillApplyVersionError",
    "SkillAuthoringParseError",
    "SkillAuthoringUnavailableError",
    "SkillError",
    "SkillEvalGateError",
    "SkillForge",
    "SkillForgeAuditError",
    "SkillGardener",
    "SkillNotFoundError",
    "SkillTriggerEvalSuite",
    "SkillsSettings",
    "SqlAlchemySkillRegistry",
    "StaleProposalFlag",
    "StandardViolation",
    "SuppressionDecision",
    "TriggerOverlap",
    "UnknownToolsFlag",
    "apply_skill_candidate",
    "attribute_outcomes",
    "build_eval_ref",
    "build_search_handler",
    "build_skill_tool_specs",
    "build_view_handler",
    "ensure_birth_schema",
    "ensure_forge_schema",
    "ensure_skills_schema",
    "failure_flags",
    "fold_invocations",
    "forge_tool_handler",
    "forge_tool_spec",
    "gardener_tool_handler",
    "gardener_tool_spec",
    "lint_body",
    "lint_description",
    "match_labels",
    "ne_skill_forge_cursor",
    "normalize_trigger",
    "pack_from_dir",
    "parse_author_output",
    "parse_skill_md",
    "reconcile_bundle_dir",
    "render_author_task",
    "render_index",
    "restore_skill",
    "retire_skill",
    "retirement_candidates",
    "scheduled_forge_job",
    "scheduled_gardener_job",
    "score_pack",
    "search_packs",
    "serialize_pack",
    "skills_metadata",
    "suppression_decisions",
    "trigger_overlap_violations",
    "validate_pack_fields",
    "verify_skill_eval_ref",
    "verify_skill_proposal_payload",
    "visible",
    "workflow_signature",
]
