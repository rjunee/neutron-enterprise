# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-1a: default-off byte-parity, enabled registration, steward route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ne.api.platform import AppSeams, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_trace_scope
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.tools import SEARCH_TOOL_NAME, VIEW_TOOL_NAME

from .conftest import (
    AppBuilder,
    FakeTokenVerifier,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    run,
)

ISSUER = "https://issuer.test"
_S = SEARCH_TOOL_NAME
_V = VIEW_TOOL_NAME


def _api_settings(**over: Any) -> ApiSettings:
    return ApiSettings(
        qdrant_url="http://qdrant.test",
        qdrant_collection="t",
        tei_url="http://tei.test",
        **over,
    )


def _seams(tmp_path: Path, *, key: str, signal_url: str = "") -> AppSeams:
    registry = PluginRegistry()
    registry.register("token_verifier", "fake", lambda oidc: FakeTokenVerifier())
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    return AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(
            session_database_url=f"sqlite:///{tmp_path}/runtime-{key}.db"
        ),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/audit-{key}.db"),
        permissions_settings=PermissionsSettings(
            database_url=f"sqlite:///{tmp_path}/perms-{key}.db"
        ),
        flywheel_settings=FlywheelSettings(signal_database_url=signal_url),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=None,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
    )


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


def _tool_names(platform: Any) -> set[str]:
    return {s.name for s in platform.executor.tool_specs}


def test_default_off_byte_parity(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    a = build_platform(_api_settings(), _seams(tmp_path, key="ident"))
    engines.extend(a.engines)
    reset_config_hash_for_testing()
    b = build_platform(_api_settings(), _seams(tmp_path, key="ident"))
    engines.extend(b.engines)

    assert a.config_hash == b.config_hash
    assert _S not in _tool_names(a)
    assert _V not in _tool_names(a)
    assert a.skills_engine is None


def test_enabled_registers_tools_and_policies_and_stamp(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    off = build_platform(_api_settings(), _seams(tmp_path, key="en"))
    engines.extend(off.engines)

    reset_config_hash_for_testing()
    on = build_platform(
        _api_settings(), _seams(tmp_path, key="en", signal_url=f"sqlite:///{tmp_path}/sig.db")
    )
    engines.extend(on.engines)

    # both tools registered on the frozen executor
    assert {_S, _V} <= _tool_names(on)
    # arg policies present in guardrails.tools BEFORE the freeze
    tools = on.govern_settings.guardrails.tools
    assert _S in tools and _V in tools
    # NE-SKILL-1b: the tier-1 forge cron tool policy is registered when enabled,
    # absent when off (a tool absent from guardrails.tools is fail-closed-rejected).
    assert "write.internal.skills.forge_run" in tools
    assert "write.internal.skills.forge_run" not in off.govern_settings.guardrails.tools
    # NE-SKILL-4: the tier-1 gardener cron tool policy is registered when enabled,
    # absent when off (same fail-closed rule as the forge tool).
    assert "write.internal.skills.gardener_run" in tools
    assert "write.internal.skills.gardener_run" not in off.govern_settings.guardrails.tools
    # config_hash MOVED (ne-skills stamped) relative to default-off
    assert on.config_hash != off.config_hash
    assert on.skills_engine is not None


def test_governed_fire_returns_acl_trimmed(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    on = build_platform(
        _api_settings(), _seams(tmp_path, key="fire", signal_url=f"sqlite:///{tmp_path}/sig.db")
    )
    engines.extend(on.engines)
    assert on.skills_engine is not None
    registry = SqlAlchemySkillRegistry(on.skills_engine)
    from ne.contracts.skills import SkillPack, SkillSource

    body = "## When to Use\nx\n## Procedure\nx\n## Pitfalls\nx\n## Verification\nx\n"
    registry.register_pack(
        SkillPack(
            name="global-skill",
            version=1,
            description="a global skill",
            triggers=("do the global thing",),
            body=body,
            source=SkillSource.BUNDLED,
        ),
        origin="bundle",
    )
    principal = Principal(id="user:ada", claims={"department": "eng"})

    async def _fire() -> str:
        with run_trace_scope("t-fire"):
            return await on.executor.execute(
                VIEW_TOOL_NAME, {"name": "global-skill"}, principal=principal
            )

    out = run(_fire())
    assert out.startswith("# global-skill")


_ROLES = ("steward",)


def _seed_skills(state: Any) -> None:
    from ne.contracts.skills import SkillPack, SkillSource

    assert state.skills_engine is not None
    reg = SqlAlchemySkillRegistry(state.skills_engine)
    body = "## When to Use\nx\n## Procedure\nx\n## Pitfalls\nx\n## Verification\nx\n"
    reg.register_pack(
        SkillPack(
            name="alpha",
            version=1,
            description="alpha skill",
            triggers=("use alpha",),
            body=body,
            source=SkillSource.BUNDLED,
        ),
        origin="bundle",
    )
    reg.register_quarantined("pending-one", "manual", reason="pending-forge")


def test_steward_skills_route(make_app: AppBuilder) -> None:
    h = make_app(steward_roles=_ROLES)
    state = get_state(h.app)
    assert state.skills_engine is not None
    _seed_skills(state)

    async def _t() -> None:
        async with h.client() as client:
            # non-steward → 403 (audited); wins over 503.
            r_forbidden = await client.get(
                "/api/v1/steward/skills", headers=h.bearer("user:nobody", groups=[])
            )
            assert r_forbidden.status_code == 403
            # steward → 200 with rows incl. the quarantined pack.
            r_ok = await client.get(
                "/api/v1/steward/skills",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            assert r_ok.status_code == 200
            names = {row["name"]: row for row in r_ok.json()["skills"]}
            assert names["alpha"]["status"] == "active"
            assert names["pending-one"]["status"] == "quarantined"

    run(_t())


def test_steward_skills_503_when_unconfigured(make_app: AppBuilder) -> None:
    # kpi_configured=False ⇒ no flywheel signal DB ⇒ skills_engine None ⇒ 503.
    h = make_app(steward_roles=_ROLES, kpi_configured=False)
    state = get_state(h.app)
    assert state.skills_engine is None

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/steward/skills",
                headers=h.bearer("user:steward", groups=["steward"]),
            )
            assert r.status_code == 503

    run(_t())


# -- PRE-011B: exact hash-verified Agent Skill pins in production composition ----
#
# "After merged 011F delivery" is a REAL state, not a seeded fixture: the worker is
# the sole bundle reconciler (ne/worker/main.py step 2), so these tests drive
# ``reconcile_required_agent_skills`` — 011F's own worker path — against the shipped
# bundle, and only then compose the platform both roots share.

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"
_BUNDLE = _ROOT / "deploy/skills"
SKILL_NAME = "meridian-hardship-draft"
#: The canonical PARSED SkillPack.content_hash the manifest pins.
DECLARED_HASH = "dbc78dc3c2416336b124ac5aab0ed38a0d267496db12f08e3366c3fee5e2fb9d"
#: The RAW SKILL.md file sha256 that 011F's rollout identity binds. A different
#: thing from DECLARED_HASH; both are real and neither substitutes for the other.
RAW_HASH = "cdcf8cc87d6042d06a56afb6ff4b5494c192387c5b9abca4d99a9fae4854221d"
# NE-CS-SPLIT: the Skill pin lives on Meridian Customer Support now.
ANALYST = "Meridian Customer Support"


def _skills_settings() -> Any:
    from ne.skills.config import SkillsSettings

    return SkillsSettings(bundle_dir=str(_BUNDLE))  # type: ignore[call-arg]


def _open_signal_engine(signal_url: str) -> Any:
    from ne.audit import create_engine_with_timeouts, ensure_transactional_sqlite
    from ne.skills.registry import ensure_skills_schema

    engine = create_engine_with_timeouts(signal_url)
    ensure_transactional_sqlite(engine)
    ensure_skills_schema(engine)
    return engine


def _deliver(signal_url: str) -> None:
    """Run PRE-011F's worker reconcile so the shared registry holds the admitted row."""
    from ne.api.skills_delivery import reconcile_required_agent_skills

    engine = _open_signal_engine(signal_url)
    try:
        reconcile_required_agent_skills(
            engine,
            _skills_settings(),
            agent_manifest_path=str(_REGISTRY),
            expected_raw_hash=RAW_HASH,
        )
    finally:
        engine.dispose()


def _compose(
    tmp_path: Path, *, key: str, signal_url: str, manifest_path: Path | None = None
) -> Any:
    reset_config_hash_for_testing()
    return build_platform(
        _api_settings(agent_manifest_path=str(manifest_path or _REGISTRY)),
        _seams(tmp_path, key=key, signal_url=signal_url),
    )


def _analyst(platform: Any) -> Any:
    return {row.definition.label: row for row in platform.agent_catalog.agents}[ANALYST]


def test_delivered_bundle_makes_the_exact_meridian_pin_satisfied_in_composition(
    tmp_path: Path, engines: list[Any]
) -> None:
    """Control: after 011F delivery the declared pin is EXACTLY verified at boot."""
    signal_url = f"sqlite:///{tmp_path}/sig-deliver.db"
    _deliver(signal_url)
    platform = _compose(tmp_path, key="deliver", signal_url=signal_url)
    engines.extend(platform.engines)

    assert platform.agent_skills is not None
    analyst = _analyst(platform)
    # The discriminating half this scope can prove: the SKILL reason is gone.
    assert "skill-unavailable" not in analyst.availability_reasons
    # ...while knowledge_sources is still hardcoded empty OUTSIDE this scope, so
    # full availability is not yet reachable. Stated, not silently tolerated.
    assert "knowledge-unavailable" in analyst.availability_reasons
    assert analyst.available is False


def test_without_delivery_the_meridian_pin_is_unsatisfied_and_the_agent_stays_unavailable(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The negative pole: no admitted row ⇒ no verified pin ⇒ skill-unavailable."""
    signal_url = f"sqlite:///{tmp_path}/sig-nodeliver.db"
    platform = _compose(tmp_path, key="nodeliver", signal_url=signal_url)
    engines.extend(platform.engines)
    analyst = _analyst(platform)
    assert "skill-unavailable" in analyst.availability_reasons
    assert analyst.available is False


def test_a_newer_admitted_version_does_not_satisfy_the_frozen_pin(
    tmp_path: Path, engines: list[Any]
) -> None:
    """THE get_current discriminator: only v4 is admitted; the manifest pins v3.

    A name/current resolution would find the name present and wrongly call the
    pinned version satisfied. The exact read never folds to ``max(version)``, so
    the pin the Agent actually declared is unsatisfied and the Agent stays
    unavailable.
    """
    from ne.contracts.skills import AuthoringStandard, SkillPack
    from ne.skills.packio import pack_from_dir
    from ne.skills.registry import SqlAlchemySkillRegistry

    signal_url = f"sqlite:///{tmp_path}/sig-rollover.db"
    engine = _open_signal_engine(signal_url)
    try:
        shipped = pack_from_dir(_BUNDLE / SKILL_NAME, standard=AuthoringStandard())
        assert isinstance(shipped, SkillPack)
        assert shipped.content_hash == DECLARED_HASH  # v3 is what the manifest pins
        # Admit ONLY v4 — v3 is never registered.
        SqlAlchemySkillRegistry(engine).register_pack(
            shipped.model_copy(update={"version": 4}), origin="bundle"
        )
    finally:
        engine.dispose()

    platform = _compose(tmp_path, key="rollover", signal_url=signal_url)
    engines.extend(platform.engines)
    assert "skill-unavailable" in _analyst(platform).availability_reasons


def test_a_tampered_registry_row_leaves_the_meridian_pin_unsatisfied(
    tmp_path: Path, engines: list[Any]
) -> None:
    """R-28 reaches the availability projection: a tampered row satisfies nothing."""
    import sqlalchemy as sa

    from ne.skills.registry import ne_skill_versions

    signal_url = f"sqlite:///{tmp_path}/sig-tamper.db"
    _deliver(signal_url)
    engine = _open_signal_engine(signal_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_skill_versions.update()
                .where(ne_skill_versions.c.name == SKILL_NAME)
                # stored content_hash deliberately NOT recomputed
                .values(body="TAMPERED")
            )
        assert sa is not None
    finally:
        engine.dispose()

    platform = _compose(tmp_path, key="tamper", signal_url=signal_url)
    engines.extend(platform.engines)
    assert "skill-unavailable" in _analyst(platform).availability_reasons


def test_a_retired_skill_leaves_the_meridian_pin_unsatisfied(
    tmp_path: Path, engines: list[Any]
) -> None:
    signal_url = f"sqlite:///{tmp_path}/sig-retired.db"
    _deliver(signal_url)
    engine = _open_signal_engine(signal_url)
    try:
        SqlAlchemySkillRegistry(engine).append_lifecycle(
            SKILL_NAME, "retired", actor="user:steward"
        )
    finally:
        engine.dispose()

    platform = _compose(tmp_path, key="retired", signal_url=signal_url)
    engines.extend(platform.engines)
    assert "skill-unavailable" in _analyst(platform).availability_reasons


def test_both_process_roots_compose_identical_verified_pins_over_one_registry(
    tmp_path: Path, engines: list[Any]
) -> None:
    """One recipe, two processes: ne-api and ne-worker call THIS build_platform.

    The worker reconciles first (ne/worker/main.py step 2) and then composes; the
    API verifies read-only and composes. Both therefore run this exact function
    against the SAME shared signal database, so the proof that matters is that two
    independent compositions over one delivered registry agree exactly.
    """
    signal_url = f"sqlite:///{tmp_path}/sig-tworoots.db"
    _deliver(signal_url)

    api_platform = _compose(tmp_path, key="root-api", signal_url=signal_url)
    engines.extend(api_platform.engines)
    worker_platform = _compose(tmp_path, key="root-worker", signal_url=signal_url)
    engines.extend(worker_platform.engines)

    api_analyst = _analyst(api_platform)
    worker_analyst = _analyst(worker_platform)
    assert api_analyst.availability_reasons == worker_analyst.availability_reasons
    for analyst in (api_analyst, worker_analyst):
        assert "skill-unavailable" not in analyst.availability_reasons
    assert api_platform.agent_skills is not None
    assert worker_platform.agent_skills is not None


def test_the_activation_service_reads_the_same_registry_that_verified_the_pins(
    tmp_path: Path, engines: list[Any]
) -> None:
    """One source, not two lookups: the service must serve what the snapshot proved.

    A service composed over a different registry would still leave availability
    looking correct while serving nothing — the exact split-brain this control
    exists to forbid.
    """
    from datetime import UTC, datetime

    from ne.permissions.models import PrincipalKind, SyncedPrincipal

    signal_url = f"sqlite:///{tmp_path}/sig-oneregistry.db"
    _deliver(signal_url)
    platform = _compose(tmp_path, key="oneregistry", signal_url=signal_url)
    engines.extend(platform.engines)
    assert platform.agent_skills is not None

    now = datetime(2026, 8, 21, tzinfo=UTC)
    principal = Principal(id="user:ada", claims={"department": "servicing"})
    platform.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id=principal.id, kind=PrincipalKind.USER)],
        source="test",
        now=now,
    )
    platform.perm_store.set_member_groups(
        principal.id, ["department:servicing"], source="test", now=now
    )

    definition = _analyst(platform).definition
    selected = platform.agent_skills.select(definition, principal)
    assert [(p.name, p.version, p.content_hash) for p in selected] == [
        (SKILL_NAME, 3, DECLARED_HASH)
    ]

    # Selection alone only proves the service READS the verified registry. The
    # substrate is a separate binding constructed over an engine, so it could write
    # the materialized surface somewhere else entirely and every assertion above
    # would still pass. Materialize, then read the surface back through a registry
    # built on the PLATFORM's engine: that round trip is what forbids a split brain.
    activation = run(
        platform.agent_skills.activate(definition, principal, surface_key="run:same-registry")
    )
    surface = SqlAlchemySkillRegistry(platform.skills_engine).read_surface("run:same-registry")
    assert surface is not None, "the substrate wrote the surface to a different engine"
    index_text, pack_names = surface
    assert pack_names == (SKILL_NAME,)
    assert index_text == (
        "Available skills (call read.skills.view with a skill name to load its procedure):\n"
        f"- {SKILL_NAME} (v3): Draft a reviewable disaster-hardship outreach message "
        "from governed evidence."
    )
    # bounded: name/version/description only — never the procedure body.
    assert "repossession" not in index_text
    assert activation.index_text == index_text
    assert activation.pins == ((SKILL_NAME, 3, DECLARED_HASH),)


def test_a_stale_manifest_hash_leaves_the_delivered_pack_unmatched_and_unavailable(
    tmp_path: Path, engines: list[Any]
) -> None:
    """The pack EXISTS and is valid; the Agent declares a different hash.

    This is the one case that separates "a pack by this name/version is admitted"
    from "the admitted pack is the artifact this Agent pinned". A composition that
    compared the registry row against itself — rather than against the DECLARED
    hash — would pass every other control in this file and silently accept a stale
    manifest. Delivery uses the REAL manifest, so the registry row is genuinely
    valid and current; only the composed manifest is stale.
    """
    stale_hash = "b" * 64
    assert stale_hash != DECLARED_HASH
    stale_manifest = tmp_path / "stale-registry.toml"
    stale_manifest.write_text(
        _REGISTRY.read_text(encoding="utf-8").replace(DECLARED_HASH, stale_hash),
        encoding="utf-8",
    )
    assert stale_hash in stale_manifest.read_text(encoding="utf-8")

    signal_url = f"sqlite:///{tmp_path}/sig-stale.db"
    _deliver(signal_url)  # the REAL, valid pack is admitted

    # Control: the same delivered registry DOES satisfy the true manifest.
    good = _compose(tmp_path, key="stale-control", signal_url=signal_url)
    engines.extend(good.engines)
    assert "skill-unavailable" not in _analyst(good).availability_reasons

    stale = _compose(tmp_path, key="stale", signal_url=signal_url, manifest_path=stale_manifest)
    engines.extend(stale.engines)
    stale_analyst = _analyst(stale)
    assert "skill-unavailable" in stale_analyst.availability_reasons
    assert stale_analyst.available is False
