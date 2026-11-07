# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-008 durable Agent definition history and availability truth."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError

import ne.api.agent_catalog as agent_catalog_module
from ne.api.agent_catalog import (
    AgentCatalogError,
    AgentDependencySnapshot,
    decode_agent_cursor,
    effective_agent_tool_grants,
    encode_agent_cursor,
    reconcile_agent_catalog,
)
from ne.api.agent_manifest import LoadedAgentManifest, load_agent_manifest
from ne.contracts.types import Principal

_AGENT = "agt_01k3meridiananalyst000000000"


def _set_visibility(
    path: Path,
    *,
    principals: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
) -> None:
    text = path.read_text(encoding="utf-8")

    def toml_array(values: tuple[str, ...]) -> str:
        return "[" + ", ".join(f'"{value}"' for value in values) + "]"

    path.write_text(
        text.replace(
            'principals = ["u-ada"]\ngroups = []\nroles = []',
            f"principals = {toml_array(principals)}\n"
            f"groups = {toml_array(groups)}\nroles = {toml_array(roles)}",
        ),
        encoding="utf-8",
    )


def _write(
    path: Path,
    *,
    version: int = 1,
    generation: int = 1,
    label: str = "Analyst",
    agents: bool = True,
) -> None:
    head = f"schema_version = 1\ngeneration = {generation}\n"
    if not agents:
        path.write_text(head, encoding="utf-8")
        return
    path.write_text(
        head
        + f'''\n[[agents]]
agent_id = "{_AGENT}"
version = {version}
label = "{label}"
description = "Governed analysis"
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


def _deps(
    *,
    tools: tuple[str, ...] = ("read.knowledge.search",),
    knowledge: tuple[tuple[str, str], ...] = (),
) -> AgentDependencySnapshot:
    return AgentDependencySnapshot(
        tools=frozenset(tools),
        knowledge_sources=frozenset(knowledge),
        skills=frozenset(),
        workflow_profiles=frozenset(),
        model_policies=frozenset({"agent"}),
    )


def _count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def test_optional_graph_is_not_required_for_availability_or_effective_grants(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.toml"
    _write(path)
    text = path.read_text(encoding="utf-8").replace(
        'tool_grants = ["read.knowledge.search"]\nknowledge_sources = []',
        'tool_grants = ["read.knowledge.graph_path", "read.knowledge.search"]\n'
        'optional_tool_grants = ["read.knowledge.graph_path"]\n'
        'knowledge_sources = [{ source_id = "meridian", tiers = ["t1", "t2"] }]\n'
        'optional_knowledge_sources = [{ source_id = "meridian", tiers = ["t2"] }]',
    )
    path.write_text(text, encoding="utf-8")
    definition = load_agent_manifest(path).manifest.agents[0]
    off = _deps(
        tools=("read.knowledge.search", "read.knowledge.graph_path"),
        knowledge=(("meridian", "t1"),),
    )
    row = reconcile_agent_catalog(sa.create_engine("sqlite://"), load_agent_manifest(path), off)
    assert row.agents[0].available is True
    assert effective_agent_tool_grants(definition, off) == ("read.knowledge.search",)
    on = _deps(
        tools=("read.knowledge.search", "read.knowledge.graph_path"),
        knowledge=(("meridian", "t1"), ("meridian", "t2")),
    )
    assert effective_agent_tool_grants(definition, on) == (
        "read.knowledge.graph_path",
        "read.knowledge.search",
    )


def test_same_version_is_idempotent_but_drift_and_lower_version_fail(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    loaded = load_agent_manifest(str(path))
    first = reconcile_agent_catalog(engine, loaded, _deps())
    assert first.get_visible(_AGENT, Principal(id="u-ada")) is not None
    assert _count(engine, "ne_agent_definition_versions") == 1

    reconcile_agent_catalog(engine, loaded, _deps())
    assert _count(engine, "ne_agent_definition_versions") == 1

    _write(path, label="Changed", generation=2)
    with pytest.raises(AgentCatalogError, match="immutable Agent definition drift"):
        reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())

    _write(path, version=2, generation=2)
    reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())
    assert _count(engine, "ne_agent_definition_versions") == 2

    _write(path, version=1, generation=3)
    with pytest.raises(AgentCatalogError, match="stale Agent definition version"):
        reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())


def test_removal_marks_unavailable_without_deleting_history(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())

    _write(path, agents=False, generation=2)
    removed = reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())
    row = removed.get_visible(_AGENT, Principal(id="u-ada"))
    assert row is not None
    assert row.available is False
    assert row.availability_reasons == ("definition-removed",)
    assert _count(engine, "ne_agent_definition_versions") == 1


def test_missing_dependency_is_safe_unavailable_and_never_substituted(tmp_path: Path) -> None:
    engine = sa.create_engine("sqlite://")
    path = tmp_path / "registry.toml"
    _write(path)
    catalog = reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps(tools=()))
    row = catalog.get_visible(_AGENT, Principal(id="u-ada"))
    assert row is not None
    assert row.available is False
    assert row.availability_reasons == ("tool-unavailable",)
    assert row.definition.tool_grants == ("read.knowledge.search",)


def test_knowledge_availability_requires_every_exact_source_tier_pair(tmp_path: Path) -> None:
    path = tmp_path / "registry.toml"
    _write(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "knowledge_sources = []",
            'knowledge_sources = [{ source_id = "meridian", tiers = ["t1", "t2"] }]',
        ),
        encoding="utf-8",
    )
    only_t1 = reconcile_agent_catalog(
        sa.create_engine("sqlite://"),
        load_agent_manifest(path),
        _deps(knowledge=(("meridian", "t1"),)),
    ).get_visible(_AGENT, Principal(id="u-ada"))
    assert only_t1 is not None
    assert only_t1.availability_reasons == ("knowledge-unavailable",)

    both = reconcile_agent_catalog(
        sa.create_engine("sqlite://"),
        load_agent_manifest(path),
        _deps(knowledge=(("meridian", "t1"), ("meridian", "t2"))),
    ).get_visible(_AGENT, Principal(id="u-ada"))
    assert both is not None
    assert both.available is True
    assert both.availability_reasons == ()


@pytest.mark.parametrize(
    ("missing", "expected_reason"),
    [
        ("tool", "tool-unavailable"),
        ("knowledge", "knowledge-unavailable"),
        ("model-policy", "model-policy-unavailable"),
        ("skill", "skill-unavailable"),
        ("workflow-profile", "workflow-profile-unavailable"),
    ],
)
def test_each_explicit_dependency_kind_uses_its_exact_composed_identity(
    tmp_path: Path, missing: str, expected_reason: str
) -> None:
    digest = "a" * 64
    skill_dependency = (
        '  { kind = "skill", dependency_id = "skill.one", version = 1, '
        f'content_hash = "{digest}" }},'
    )
    workflow_dependency = (
        '  { kind = "workflow-profile", dependency_id = "workflow.one", version = 2, '
        f'content_hash = "{digest}" }},'
    )
    path = tmp_path / f"dependencies-{missing}.toml"
    _write(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "availability_dependencies = []",
            f"""availability_dependencies = [
  {{ kind = "tool", dependency_id = "tool.one" }},
  {{ kind = "knowledge", dependency_id = "knowledge.one" }},
  {{ kind = "model-policy", dependency_id = "model.one" }},
{skill_dependency}
{workflow_dependency}
]""",
        ),
        encoding="utf-8",
    )
    tools = frozenset({"read.knowledge.search", "tool.one"})
    knowledge_sources = frozenset({("knowledge.one", "t1")})
    skills = frozenset({("skill.one", 1, digest)})
    workflow_profiles = frozenset({("workflow.one", 2, digest)})
    model_policies = frozenset({"agent", "model.one"})
    if missing == "tool":
        tools = frozenset({"read.knowledge.search"})
    elif missing == "knowledge":
        knowledge_sources = frozenset()
    elif missing == "model-policy":
        model_policies = frozenset({"agent"})
    elif missing == "skill":
        skills = frozenset()
    else:
        workflow_profiles = frozenset()
    row = reconcile_agent_catalog(
        sa.create_engine("sqlite://"),
        load_agent_manifest(path),
        AgentDependencySnapshot(
            tools=tools,
            knowledge_sources=knowledge_sources,
            skills=skills,
            workflow_profiles=workflow_profiles,
            model_policies=model_policies,
        ),
    ).get_visible(_AGENT, Principal(id="u-ada"))
    assert row is not None
    assert row.availability_reasons == (expected_reason,)


@pytest.mark.parametrize(
    ("kind", "available_identity", "expected_reason"),
    [
        ("skill", ("skill.one", 2, "a" * 64), "skill-unavailable"),
        ("skill", ("skill.one", 1, "b" * 64), "skill-unavailable"),
        (
            "workflow-profile",
            ("workflow.one", 3, "a" * 64),
            "workflow-profile-unavailable",
        ),
        (
            "workflow-profile",
            ("workflow.one", 2, "b" * 64),
            "workflow-profile-unavailable",
        ),
    ],
)
def test_pinned_dependencies_require_exact_version_and_hash(
    tmp_path: Path,
    kind: str,
    available_identity: tuple[str, int, str],
    expected_reason: str,
) -> None:
    digest = "a" * 64
    path = tmp_path / f"pinned-{kind}.toml"
    _write(path)
    dependency_id = "skill.one" if kind == "skill" else "workflow.one"
    dependency = (
        f'{{ kind = "{kind}", dependency_id = "{dependency_id}", '
        f'version = {1 if kind == "skill" else 2}, content_hash = "{digest}" }}'
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "availability_dependencies = []",
            f"availability_dependencies = [{dependency}]",
        ),
        encoding="utf-8",
    )
    row = reconcile_agent_catalog(
        sa.create_engine("sqlite://"),
        load_agent_manifest(path),
        AgentDependencySnapshot(
            tools=frozenset({"read.knowledge.search"}),
            knowledge_sources=frozenset(),
            skills=frozenset({available_identity}) if kind == "skill" else frozenset(),
            workflow_profiles=(
                frozenset({available_identity}) if kind == "workflow-profile" else frozenset()
            ),
            model_policies=frozenset({"agent"}),
        ),
    ).get_visible(_AGENT, Principal(id="u-ada"))
    assert row is not None
    assert row.availability_reasons == (expected_reason,)


@pytest.mark.parametrize(
    ("lifecycle", "expected_available", "expected_reasons"),
    [
        ("active", True, ()),
        ("deprecated", True, ()),
        ("retired", False, ("lifecycle-retired",)),
    ],
)
def test_lifecycle_has_explicit_availability_semantics(
    tmp_path: Path,
    lifecycle: str,
    expected_available: bool,
    expected_reasons: tuple[str, ...],
) -> None:
    path = tmp_path / f"{lifecycle}.toml"
    _write(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'lifecycle = "active"', f'lifecycle = "{lifecycle}"'
        ),
        encoding="utf-8",
    )
    catalog = reconcile_agent_catalog(
        sa.create_engine("sqlite://"), load_agent_manifest(path), _deps()
    )
    row = catalog.get_visible(_AGENT, Principal(id="u-ada"))
    assert row is not None
    assert row.available is expected_available
    assert row.availability_reasons == expected_reasons


def test_visibility_is_deny_default_and_applied_before_pagination(tmp_path: Path) -> None:
    engine = sa.create_engine("sqlite://")
    path = tmp_path / "registry.toml"
    _write(path)
    catalog = reconcile_agent_catalog(engine, load_agent_manifest(str(path)), _deps())

    assert catalog.list_visible(Principal(id="u-bob"), after=None, limit=1) == ((), None)
    rows, cursor = catalog.list_visible(Principal(id="u-ada"), after=None, limit=1)
    assert [row.definition.agent_id for row in rows] == [_AGENT]
    assert cursor is None
    assert catalog.get_visible(_AGENT, Principal(id="u-bob")) is None
    assert catalog.get_visible("agt_01k3randomrandomrandom0000000", Principal(id="u-bob")) is None


def test_role_and_group_selectors_have_disjoint_principal_group_namespaces(
    tmp_path: Path,
) -> None:
    role_path = tmp_path / "roles.toml"
    _write(role_path)
    _set_visibility(role_path, roles=("ops",))
    role_catalog = reconcile_agent_catalog(
        sa.create_engine("sqlite://"), load_agent_manifest(role_path), _deps()
    )
    bare_group = Principal(id="u-bob", groups=("ops",))
    assert role_catalog.get_visible(_AGENT, bare_group) is None
    assert role_catalog.get_visible("agt_01k3randomrandomrandom0000000", bare_group) is None
    assert role_catalog.get_visible(_AGENT, Principal(id="u-bob", groups=("role:ops",)))

    group_path = tmp_path / "groups.toml"
    _write(group_path)
    _set_visibility(group_path, groups=("ops",))
    group_catalog = reconcile_agent_catalog(
        sa.create_engine("sqlite://"), load_agent_manifest(group_path), _deps()
    )
    assert group_catalog.get_visible(_AGENT, bare_group) is not None
    assert group_catalog.get_visible(_AGENT, Principal(id="ops")) is None


def test_interleaved_hidden_rows_do_not_consume_opaque_visible_pages(tmp_path: Path) -> None:
    ids = tuple(f"agt_{index:024d}" for index in range(1, 5))

    def block(agent_id: str, visible: bool) -> str:
        return f'''[[agents]]
agent_id = "{agent_id}"
version = 1
label = "Agent {agent_id}"
description = "Pagination proof"
owner_label = "Operations"
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
all_authenticated = {str(visible).lower()}
principals = []
groups = []
roles = []
departments = []
[agents.budgets]
max_steps = 1
max_tool_calls = 0
max_input_tokens = 1
max_output_tokens = 1
'''

    path = tmp_path / "paged.toml"
    path.write_text(
        "schema_version = 1\ngeneration = 1\n"
        + "\n".join(block(agent_id, agent_id != ids[1]) for agent_id in ids),
        encoding="utf-8",
    )
    catalog = reconcile_agent_catalog(
        sa.create_engine("sqlite://"), load_agent_manifest(path), _deps(tools=())
    )
    principal = Principal(id="u-reader")
    seen: list[str] = []
    cursor: str | None = None
    while True:
        after = decode_agent_cursor(cursor) if cursor is not None else None
        page, after = catalog.list_visible(principal, after=after, limit=1)
        seen.extend(row.definition.agent_id for row in page)
        if after is None:
            break
        cursor = encode_agent_cursor(after)
        assert after not in cursor
    assert seen == [ids[0], ids[2], ids[3]]
    assert len(seen) == len(set(seen))
    assert ids[1] not in seen


def test_concurrent_first_reconcile_is_one_immutable_definition(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    loaded = load_agent_manifest(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        catalogs = list(
            pool.map(lambda _: reconcile_agent_catalog(engine, loaded, _deps()), range(8))
        )
    assert all(len(catalog.agents) == 1 for catalog in catalogs)
    assert _count(engine, "ne_agent_definition_versions") == 1
    assert _count(engine, "ne_agent_active_definitions") == 1


def test_same_semantics_different_manifest_bytes_fail_without_overwrite(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    first_path = tmp_path / "first.toml"
    second_path = tmp_path / "second.toml"
    _write(first_path)
    second_path.write_bytes(b"# byte-different\n" + first_path.read_bytes())
    first = load_agent_manifest(first_path)
    second = load_agent_manifest(second_path)
    assert first.semantic_hash == second.semantic_hash
    assert first.raw_hash != second.raw_hash
    reconcile_agent_catalog(engine, first, _deps())
    with pytest.raises(AgentCatalogError, match="byte fingerprint mismatch"):
        reconcile_agent_catalog(engine, second, _deps())
    with engine.connect() as conn:
        head_raw = conn.execute(
            sa.text("SELECT manifest_raw_hash FROM ne_agent_registry_head WHERE singleton_key = 1")
        ).scalar_one()
        active_raw = conn.execute(
            sa.text("SELECT DISTINCT manifest_raw_hash FROM ne_agent_active_definitions")
        ).scalar_one()
    assert head_raw == active_raw == hashlib.sha256(first_path.read_bytes()).hexdigest()

    second_path.write_text(
        second_path.read_text(encoding="utf-8").replace("generation = 1", "generation = 2"),
        encoding="utf-8",
    )
    with pytest.raises(AgentCatalogError, match="no semantic transition"):
        reconcile_agent_catalog(engine, load_agent_manifest(second_path), _deps())


def test_concurrent_byte_distinct_first_roots_choose_one_global_head(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'shared-concurrent.db'}")
    paths = (tmp_path / "a.toml", tmp_path / "b.toml")
    _write(paths[0])
    paths[1].write_bytes(b"# other-root\n" + paths[0].read_bytes())
    loaded = tuple(load_agent_manifest(path) for path in paths)

    def reconcile(item: LoadedAgentManifest) -> str:
        try:
            reconcile_agent_catalog(engine, item, _deps())
        except AgentCatalogError as exc:
            assert "byte fingerprint mismatch" in str(exc)
            return "refused"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(reconcile, loaded))
    assert outcomes == ["accepted", "refused"]
    assert _count(engine, "ne_agent_registry_head") == 1
    assert _count(engine, "ne_agent_definition_versions") == 1


def test_same_generation_semantic_or_disjoint_roots_cannot_replace_live_head(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'shared-generation.db'}")
    first_path = tmp_path / "first.toml"
    changed_path = tmp_path / "changed.toml"
    disjoint_path = tmp_path / "disjoint.toml"
    _write(first_path)
    _write(changed_path, label="Other semantics")
    _write(disjoint_path)
    disjoint_path.write_text(
        disjoint_path.read_text(encoding="utf-8").replace(
            _AGENT, "agt_01k3disjointagent00000000000"
        ),
        encoding="utf-8",
    )
    reconcile_agent_catalog(engine, load_agent_manifest(first_path), _deps())
    for path in (changed_path, disjoint_path):
        with pytest.raises(AgentCatalogError, match="registry generation drift"):
            reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
    assert _count(engine, "ne_agent_definition_versions") == 1


def test_same_generation_rejects_stale_active_pointer_and_missing_absent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry.toml"
    pointer_engine = sa.create_engine(f"sqlite:///{tmp_path / 'stale-pointer.db'}")
    _write(path)
    reconcile_agent_catalog(pointer_engine, load_agent_manifest(path), _deps())
    _write(path, version=2, generation=2)
    current = load_agent_manifest(path)
    reconcile_agent_catalog(pointer_engine, current, _deps())
    with pointer_engine.begin() as conn:
        old = conn.execute(
            sa.text(
                "SELECT version, definition_hash FROM ne_agent_definition_versions "
                "WHERE agent_id = :agent_id ORDER BY version LIMIT 1"
            ),
            {"agent_id": _AGENT},
        ).one()
        conn.execute(
            sa.text(
                "UPDATE ne_agent_active_definitions SET version = :version, "
                "definition_hash = :definition_hash WHERE agent_id = :agent_id"
            ),
            {"version": old.version, "definition_hash": old.definition_hash, "agent_id": _AGENT},
        )
    with pytest.raises(AgentCatalogError, match="active/absent state is inconsistent"):
        reconcile_agent_catalog(pointer_engine, current, _deps())

    absent_engine = sa.create_engine(f"sqlite:///{tmp_path / 'missing-absent.db'}")
    _write(path)
    reconcile_agent_catalog(absent_engine, load_agent_manifest(path), _deps())
    _write(path, agents=False, generation=2)
    absent = load_agent_manifest(path)
    reconcile_agent_catalog(absent_engine, absent, _deps())
    with absent_engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM ne_agent_active_definitions WHERE agent_id = :agent_id"),
            {"agent_id": _AGENT},
        )
    with pytest.raises(AgentCatalogError, match="active/absent state is inconsistent"):
        reconcile_agent_catalog(absent_engine, absent, _deps())

    stale_absent_engine = sa.create_engine(f"sqlite:///{tmp_path / 'stale-absent.db'}")
    _write(path)
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("all_authenticated = false", "all_authenticated = true")
        .replace('principals = ["u-ada"]', "principals = []"),
        encoding="utf-8",
    )
    reconcile_agent_catalog(stale_absent_engine, load_agent_manifest(path), _deps())
    _write(path, version=2, generation=2)
    scoped_v2 = load_agent_manifest(path)
    reconcile_agent_catalog(stale_absent_engine, scoped_v2, _deps())
    _write(path, agents=False, generation=3)
    removed = load_agent_manifest(path)
    reconcile_agent_catalog(stale_absent_engine, removed, _deps())
    with stale_absent_engine.begin() as conn:
        v1 = conn.execute(
            sa.text(
                "SELECT version, definition_hash FROM ne_agent_definition_versions "
                "WHERE agent_id = :agent_id ORDER BY version LIMIT 1"
            ),
            {"agent_id": _AGENT},
        ).one()
        conn.execute(
            sa.text(
                "UPDATE ne_agent_active_definitions SET version = :version, "
                "definition_hash = :definition_hash WHERE agent_id = :agent_id"
            ),
            {"version": v1.version, "definition_hash": v1.definition_hash, "agent_id": _AGENT},
        )

    projected = False
    read_catalog = agent_catalog_module._read_catalog

    def observe_projection(*args, **kwargs):
        nonlocal projected
        projected = True
        return read_catalog(*args, **kwargs)

    monkeypatch.setattr(agent_catalog_module, "_read_catalog", observe_projection)
    with pytest.raises(AgentCatalogError, match="active/absent state is inconsistent"):
        reconcile_agent_catalog(stale_absent_engine, removed, _deps())
    assert projected is False


def test_default_off_can_enable_once_but_configured_cannot_return_off(tmp_path: Path) -> None:
    configured_engine = sa.create_engine(f"sqlite:///{tmp_path / 'configured-first.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    reconcile_agent_catalog(configured_engine, load_agent_manifest(path), _deps())
    with pytest.raises(AgentCatalogError, match="configured-state mismatch"):
        reconcile_agent_catalog(configured_engine, load_agent_manifest(""), _deps())

    default_engine = sa.create_engine(f"sqlite:///{tmp_path / 'default-first.db'}")
    assert reconcile_agent_catalog(default_engine, load_agent_manifest(""), _deps()).agents == ()
    enabled = reconcile_agent_catalog(default_engine, load_agent_manifest(path), _deps())
    assert enabled.get_visible(_AGENT, Principal(id="u-ada")) is not None
    with pytest.raises(AgentCatalogError, match="configured-state mismatch"):
        reconcile_agent_catalog(default_engine, load_agent_manifest(""), _deps())


def test_concurrent_default_off_and_configured_boot_converge_to_configured(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'concurrent-enable.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    configured = load_agent_manifest(path)
    default_off = load_agent_manifest("")

    def reconcile(loaded: LoadedAgentManifest) -> str:
        try:
            reconcile_agent_catalog(engine, loaded, _deps())
        except AgentCatalogError as exc:
            return str(exc)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(reconcile, (default_off, configured)))

    assert results.count("ok") >= 1
    assert all(result == "ok" or "configured-state mismatch" in result for result in results)
    catalog = reconcile_agent_catalog(engine, configured, _deps())
    assert catalog.get_visible(_AGENT, Principal(id="u-ada")) is not None
    with pytest.raises(AgentCatalogError, match="configured-state mismatch"):
        reconcile_agent_catalog(engine, default_off, _deps())


def test_malformed_lookalike_schema_and_history_mutation_fail_closed(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'malformed.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """CREATE TABLE ne_agent_definition_versions (
            agent_id VARCHAR(64) NOT NULL, version INTEGER NOT NULL,
            definition_hash VARCHAR(64) NOT NULL, definition_json TEXT NOT NULL,
            first_manifest_semantic_hash VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (agent_id, version))"""
        )
    path = tmp_path / "registry.toml"
    _write(path)
    with pytest.raises(AgentCatalogError, match="schema"):
        reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())

    sound = sa.create_engine(f"sqlite:///{tmp_path / 'sound.db'}")
    reconcile_agent_catalog(sound, load_agent_manifest(path), _deps())
    with sound.begin() as conn, pytest.raises(DatabaseError, match="append-only"):
        conn.execute(sa.text("UPDATE ne_agent_definition_versions SET definition_json = '{}'"))


def test_sqlite_replaces_same_name_noop_append_only_triggers(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'triggers.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    loaded = load_agent_manifest(path)
    reconcile_agent_catalog(engine, loaded, _deps())
    with engine.begin() as conn:
        for operation in ("update", "delete"):
            conn.exec_driver_sql(f"DROP TRIGGER trg_agent_definitions_no_{operation}")
            conn.exec_driver_sql(
                f"CREATE TRIGGER trg_agent_definitions_no_{operation} "
                f"BEFORE {operation.upper()} ON ne_agent_definition_versions BEGIN SELECT 1; END"
            )
    reconcile_agent_catalog(engine, loaded, _deps())
    statements = (
        "UPDATE ne_agent_definition_versions SET definition_json = '{}'",
        "DELETE FROM ne_agent_definition_versions",
    )
    for statement in statements:
        with engine.begin() as conn, pytest.raises(DatabaseError, match="append-only"):
            conn.execute(sa.text(statement))
    assert _count(engine, "ne_agent_definition_versions") == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"definition_hash": "g" * 64},
        {"definition_hash": "a" * 63},
        {"first_manifest_semantic_hash": "z" * 64},
        {"agent_id": " "},
        {"definition_json": " "},
        {"definition_json": "{not-json"},
        {"definition_json": "[]"},
    ],
)
def test_sqlite_definition_shape_rejects_invalid_direct_inserts(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / (hashlib.sha256(repr(overrides).encode()).hexdigest() + '.db')}"
    )
    path = tmp_path / "registry.toml"
    _write(path)
    reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
    values: dict[str, object] = {
        "agent_id": "agt_01k3directinsert000000000000",
        "version": 1,
        "definition_hash": "a" * 64,
        "definition_json": "{}",
        "first_manifest_semantic_hash": "b" * 64,
    }
    values.update(overrides)
    with engine.begin() as conn, pytest.raises(DatabaseError):
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


@pytest.mark.parametrize("mutation", ["agent_id", "version", "hash", "invalid_json"])
def test_stored_definition_identity_is_recomputed_before_removed_history_is_served(
    tmp_path: Path, mutation: str
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / f'integrity-{mutation}.db'}")
    path = tmp_path / "registry.toml"
    _write(path)
    reconcile_agent_catalog(engine, load_agent_manifest(path), _deps())
    _write(path, agents=False, generation=2)
    removed = load_agent_manifest(path)
    reconcile_agent_catalog(engine, removed, _deps())
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_agent_definitions_no_update")
        stored = conn.execute(
            sa.text("SELECT definition_json FROM ne_agent_definition_versions")
        ).scalar_one()
        if mutation == "invalid_json":
            changed = "{not-json"
        else:
            payload = json.loads(stored)
            if mutation == "agent_id":
                payload["agent_id"] = "agt_01k3forgedidentity0000000000"
            elif mutation == "version":
                payload["version"] = 2
            else:
                payload["label"] = "forged label"
            changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        conn.execute(
            sa.text("UPDATE ne_agent_definition_versions SET definition_json = :changed"),
            {"changed": changed},
        )
    with pytest.raises(AgentCatalogError, match="stored definition"):
        reconcile_agent_catalog(engine, removed, _deps())
