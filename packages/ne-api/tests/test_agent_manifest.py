# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-008 operator manifest parsing, canonical hashes, and strict shape."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from ne.api.agent_manifest import AgentManifest, AgentManifestError, load_agent_manifest


def _manifest(
    *,
    version: int = 1,
    label: str = "Analyst",
    skills: str = "[]",
    instructions: str | None = "Answer strictly from governed evidence served this run.",
) -> str:
    instructions_line = "" if instructions is None else f'instructions = "{instructions}"\n'
    return f'''schema_version = 1
generation = 1

[[agents]]
agent_id = "agt_01k3meridiananalyst000000000"
version = {version}
label = "{label}"
description = "Governed Meridian analysis"
owner_label = "Meridian Operations"
lifecycle = "active"
allowed_strategies = ["agentic"]
default_strategy = "agentic"
{instructions_line}model_policy_id = "agent"
tool_grants = ["read.knowledge.search", "read.skills.view"]
knowledge_sources = [{{ source_id = "meridian-servicing", tiers = ["t1", "t2"] }}]
skills = {skills}
availability_dependencies = [{{ kind = "tool", dependency_id = "read.skills.view" }}]

[agents.visibility]
all_authenticated = false
principals = ["u-ada"]
groups = []
roles = []
departments = ["servicing"]

[agents.budgets]
max_steps = 8
max_tool_calls = 6
max_input_tokens = 12000
max_output_tokens = 2000
'''


def test_unconfigured_manifest_is_honestly_empty() -> None:
    loaded = load_agent_manifest("")
    assert loaded.configured is False
    assert loaded.manifest.agents == ()
    assert loaded.raw_hash is None
    assert loaded.semantic_hash is None


def test_configured_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AgentManifestError, match="configured Agent manifest is unavailable"):
        load_agent_manifest(str(tmp_path / "missing.toml"))


def test_manifest_is_extra_forbid_and_visibility_is_required(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    path.write_text(
        _manifest().replace("[agents.visibility]", "unknown = true\n"), encoding="utf-8"
    )
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(str(path))

    path.write_text(_manifest().split("[agents.visibility]", 1)[0], encoding="utf-8")
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(str(path))


def test_raw_and_semantic_hashes_discriminate_bytes_and_fields(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    path.write_text(_manifest(), encoding="utf-8")
    baseline = load_agent_manifest(str(path))
    assert baseline.raw_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    path.write_text("# comment-only raw change\n" + _manifest(), encoding="utf-8")
    raw_changed = load_agent_manifest(str(path))
    assert raw_changed.raw_hash != baseline.raw_hash
    assert raw_changed.semantic_hash == baseline.semantic_hash

    path.write_text(_manifest(label="Different"), encoding="utf-8")
    semantic_changed = load_agent_manifest(str(path))
    assert semantic_changed.semantic_hash != baseline.semantic_hash


def test_optional_capability_subsets_are_canonical_validated_and_hashed(tmp_path: Path) -> None:
    optional = _manifest().replace(
        'knowledge_sources = [{ source_id = "meridian-servicing", tiers = ["t1", "t2"] }]',
        'optional_tool_grants = ["read.skills.view"]\n'
        'knowledge_sources = [{ source_id = "meridian-servicing", tiers = ["t1", "t2"] }]\n'
        'optional_knowledge_sources = [{ source_id = "meridian-servicing", tiers = ["t2"] }]',
    )
    path = tmp_path / "registry.toml"
    path.write_text(optional, encoding="utf-8")
    loaded = load_agent_manifest(path)
    definition = loaded.manifest.agents[0]
    assert definition.optional_tool_grants == ("read.skills.view",)
    assert definition.optional_knowledge_sources[0].tiers == ("t2",)
    assert (
        loaded.semantic_hash
        != load_agent_manifest(_write_manifest(tmp_path / "plain.toml", _manifest())).semantic_hash
    )

    document = tomllib.loads(
        optional.replace(
            'optional_tool_grants = ["read.skills.view"]',
            'optional_tool_grants = ["missing.tool"]',
        )
    )
    with pytest.raises(ValidationError, match="optional Tool grants must be a subset"):
        AgentManifest.model_validate(document)

    document = tomllib.loads(optional.replace('tiers = ["t2"] }]', 'tiers = ["t3"] }]', 1))
    with pytest.raises(ValidationError, match="optional Knowledge grants must be a subset"):
        AgentManifest.model_validate(document)


def _write_manifest(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_duplicate_agent_id_and_invalid_default_strategy_fail(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    duplicate = _manifest() + _manifest().split("[[agents]]", 1)[1].join(("[[agents]]", ""))
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(str(path))


def _skill(skill_id: str, version: int, digest: str) -> str:
    return f'{{ skill_id = "{skill_id}", version = {version}, content_hash = "{digest}" }}'


@pytest.mark.parametrize("second_version", [1, 2])
def test_a_second_grant_for_one_skill_id_is_rejected_at_an_equal_or_a_higher_version(
    tmp_path: Path, second_version: int
) -> None:
    """A Run resolves Skills by name, so a second grant for one id has no answer.

    ``second_version = 1`` is the exact-pair case the superseded
    ``(skill_id, version)`` check used to catch, and proves the id check subsumes it.
    """
    skills = "[{}, {}]".format(
        _skill("meridian-hardship-draft", 1, "a" * 64),
        _skill("meridian-hardship-draft", second_version, "b" * 64),
    )
    document = tomllib.loads(_manifest(skills=skills))
    with pytest.raises(ValidationError, match="duplicate Skill id 'meridian-hardship-draft'"):
        AgentManifest.model_validate(document)

    path = tmp_path / "duplicate-skill-id.toml"
    path.write_text(_manifest(skills=skills), encoding="utf-8")
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(path)


def test_distinct_skill_ids_load_and_come_back_canonically_sorted(tmp_path: Path) -> None:
    skills = "[{}, {}]".format(
        _skill("zeta-pack", 3, "a" * 64),
        _skill("alpha-pack", 9, "b" * 64),
    )
    path = tmp_path / "distinct-skill-ids.toml"
    path.write_text(_manifest(skills=skills), encoding="utf-8")
    loaded = load_agent_manifest(path)
    assert tuple((grant.skill_id, grant.version) for grant in loaded.manifest.agents[0].skills) == (
        ("alpha-pack", 9),
        ("zeta-pack", 3),
    )


@pytest.mark.parametrize(
    "dependency",
    [
        '{ kind = "tool", dependency_id = "read.skills.view", version = 1 }',
        '{ kind = "knowledge", dependency_id = "meridian", content_hash = "' + "a" * 64 + '" }',
        '{ kind = "model-policy", dependency_id = "agent", version = 1, content_hash = "'
        + "a" * 64
        + '" }',
        '{ kind = "skill", dependency_id = "skill.one" }',
        '{ kind = "workflow-profile", dependency_id = "workflow.one", version = 1 }',
    ],
)
def test_dependency_kind_rejects_forbidden_or_missing_pins(tmp_path: Path, dependency: str) -> None:
    path = tmp_path / "invalid-dependency.toml"
    path.write_text(
        _manifest().replace(
            'availability_dependencies = [{ kind = "tool", dependency_id = "read.skills.view" }]',
            f"availability_dependencies = [{dependency}]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(path)


def test_dependency_kind_accepts_only_exact_pin_shapes(tmp_path: Path) -> None:
    digest = "a" * 64
    skill_dependency = (
        '  { kind = "skill", dependency_id = "skill.one", version = 1, '
        f'content_hash = "{digest}" }},'
    )
    workflow_dependency = (
        '  { kind = "workflow-profile", dependency_id = "workflow.one", version = 2, '
        f'content_hash = "{digest}" }},'
    )
    dependencies = f"""[
  {{ kind = "tool", dependency_id = "tool.one" }},
  {{ kind = "knowledge", dependency_id = "knowledge.one" }},
  {{ kind = "model-policy", dependency_id = "model.one" }},
{skill_dependency}
{workflow_dependency}
]"""
    path = tmp_path / "valid-dependencies.toml"
    path.write_text(
        _manifest().replace(
            'availability_dependencies = [{ kind = "tool", dependency_id = "read.skills.view" }]',
            f"availability_dependencies = {dependencies}",
        ),
        encoding="utf-8",
    )
    loaded = load_agent_manifest(path)
    assert tuple(item.kind for item in loaded.manifest.agents[0].availability_dependencies) == (
        "knowledge",
        "model-policy",
        "skill",
        "tool",
        "workflow-profile",
    )

    path.write_text(
        _manifest().replace('default_strategy = "agentic"', 'default_strategy = "deterministic"'),
        encoding="utf-8",
    )
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(str(path))


# --- NE-ANALYST-INSTRUCTIONS ---------------------------------------------------
# An agentic Agent's runtime prompt is its own `instructions` plus the rendered
# Skill index, and `render_index` renders zero pins as the empty string by
# design. An agentic Agent with neither would therefore be admitted and then die
# on EVERY run at the runtime's non-empty-instructions guard — the exact live
# failure after NE-CS-SPLIT un-pinned the Analyst's last Skill. The manifest is
# where that misconfiguration fails: at load, legibly, before admission.


def test_an_agentic_agent_with_no_skill_pins_must_declare_instructions(tmp_path: Path) -> None:
    document = tomllib.loads(_manifest(instructions=None))
    with pytest.raises(
        ValidationError,
        match="pins no Skills and declares no instructions",
    ):
        AgentManifest.model_validate(document)

    # The same registry text through the loader fails closed as any invalid
    # manifest does — the boot path, not just direct validation.
    path = tmp_path / "uninstructed-agentic.toml"
    path.write_text(_manifest(instructions=None), encoding="utf-8")
    with pytest.raises(AgentManifestError, match="invalid Agent manifest"):
        load_agent_manifest(str(path))


def test_declared_instructions_are_carried_verbatim_and_move_the_semantic_hash(
    tmp_path: Path,
) -> None:
    text = "Answer strictly from governed evidence served this run."
    loaded = load_agent_manifest(_write_manifest(tmp_path / "a.toml", _manifest()))
    assert loaded.manifest.agents[0].instructions == text

    reworded = load_agent_manifest(
        _write_manifest(
            tmp_path / "b.toml",
            _manifest(instructions="Different governed guidance."),
        )
    )
    assert reworded.semantic_hash != loaded.semantic_hash
    assert reworded.manifest.agents[0].definition_hash != loaded.manifest.agents[0].definition_hash


def test_whitespace_only_instructions_are_refused(tmp_path: Path) -> None:
    document = tomllib.loads(_manifest(instructions="   "))
    with pytest.raises(ValidationError, match="non-empty trimmed text"):
        AgentManifest.model_validate(document)


def test_a_skill_pinned_agent_may_omit_instructions(tmp_path: Path) -> None:
    """The Skill index composes that Agent's prompt, so prose stays optional."""
    skills = '[{ skill_id = "meridian-hardship-draft", version = 1, content_hash = "%s" }]' % (
        "a" * 64
    )
    loaded = load_agent_manifest(
        _write_manifest(tmp_path / "pinned.toml", _manifest(skills=skills, instructions=None))
    )
    assert loaded.manifest.agents[0].instructions is None
