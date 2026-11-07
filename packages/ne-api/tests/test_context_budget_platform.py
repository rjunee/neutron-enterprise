# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 platform threading (design §3.4 / §2.2 boot check).

``build_platform`` derives the runtime context budget ONCE from the composed
``GatewaySettings`` (the routed runtime task's PRIMARY model), copies the estimator
params into ``RuntimeSettings`` via ``model_copy``, and leaves an UNROUTED
composition at ``context_budget=None`` (byte-identical default-off). It wires the
§2.2 fail-loud ``validate_tool_catalog_budgets`` boot check, and the boot check's
catalog text is BYTE-IDENTICAL to what a live ``GatewayModel`` appends in-band.

config_hash: the new ``RuntimeSettings`` fields move the ne-runtime module-subset
hash — EXPECTED (the DispatchSettings precedent, DoD e) — but two identical
compositions still freeze the SAME hash (re-freeze idempotence).
"""

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
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.errors import GatewayConfigError
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel, catalog_text_for_tool_specs
from ne.runtime.runner import build_governed_tool

from .conftest import (
    FakeTokenVerifier,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
)

ISSUER = "https://issuer.test"


def _api_settings(**over: Any) -> ApiSettings:
    return ApiSettings(
        qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test", **over
    )


def _seams(tmp_path: Path, *, key: str, gs: GatewaySettings) -> AppSeams:
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
        flywheel_settings=FlywheelSettings(signal_database_url=""),
        gateway_settings=gs,
        gateway=ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=None,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
    )


def _model(name: str, *, window: int) -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model="openai/stub",
        provider="stub",
        lane=Lane.FRONTIER_API,
        context_window=window,
    )


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


def test_routed_task_derives_budget_and_copies_estimator_params(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    primary = _model("primary", window=32768)
    gs = GatewaySettings(
        models=[primary, _model("fallback", window=200000)],
        tasks={"agent": TaskRoute(model="primary", fallbacks=("fallback",))},
        bytes_per_token=2.0,
        per_call_overhead=11,
    )
    platform = build_platform(_api_settings(), _seams(tmp_path, key="routed", gs=gs))
    engines.extend(platform.engines)

    rt = platform.runtime_settings
    assert rt.context_budget == gs.context_budget_for(primary)
    assert rt.bytes_per_token == 2.0
    assert rt.per_call_overhead == 11


def test_unrouted_task_leaves_budget_none(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    gs = GatewaySettings(models=[_model("stub", window=32768)], tasks={})
    platform = build_platform(_api_settings(), _seams(tmp_path, key="unrouted", gs=gs))
    engines.extend(platform.engines)
    assert platform.runtime_settings.context_budget is None
    # Estimator params still copied (harmless defaults) — advisory only when off.
    assert platform.runtime_settings.bytes_per_token == gs.bytes_per_token


def test_catalog_boot_check_fails_loud_for_too_small_window(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    # A window whose input budget (~376) is below the 512-token floor: the governed
    # tool catalog can never fit, so build_platform must fail LOUD naming the task.
    tiny = _model("tiny", window=1600)
    gs = GatewaySettings(models=[tiny], tasks={"agent": TaskRoute(model="tiny")})
    with pytest.raises(GatewayConfigError, match="agent"):
        platform = build_platform(_api_settings(), _seams(tmp_path, key="tiny", gs=gs))
        engines.extend(platform.engines)


def test_catalog_text_byte_parity_with_live_loop(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    gs = GatewaySettings(
        models=[_model("primary", window=32768)], tasks={"agent": TaskRoute(model="primary")}
    )
    platform = build_platform(_api_settings(), _seams(tmp_path, key="parity", gs=gs))
    engines.extend(platform.engines)

    specs = platform.executor.tool_specs
    derived = catalog_text_for_tool_specs(specs)

    # The live loop builds a ToolDefinition per spec via build_governed_tool and
    # GatewayModel appends _render_catalog(tool_defs) in-band. Reproduce that exact
    # derivation and assert byte-identity.
    principal = Principal(id="user:parity", claims={"department": "eng"})
    tool_defs = [build_governed_tool(platform.executor, spec, principal).tool_def for spec in specs]
    model = GatewayModel(ScriptedGateway(), RuntimeSettings(), on_unknown_tool=_noop_hook)
    live_catalog = model._render_catalog(tool_defs)  # pyright: ignore[reportPrivateUsage]
    assert derived == live_catalog


def test_config_hash_is_deterministic_across_identical_builds(
    tmp_path: Path, engines: list[Any]
) -> None:
    # The new RuntimeSettings fields (DoD e) MOVE the module-subset hash vs pre-unit,
    # but two identical compositions still freeze the SAME hash (re-freeze idempotence).
    def _build(key: str) -> Any:
        reset_config_hash_for_testing()
        gs = GatewaySettings(
            models=[_model("primary", window=32768)], tasks={"agent": TaskRoute(model="primary")}
        )
        return build_platform(_api_settings(), _seams(tmp_path, key=key, gs=gs))

    # SAME key ⇒ identical composed settings (incl. the session DB URL, which is a
    # config_hash input); the hash must be stable across the two freezes.
    a = _build("hashsame")
    engines.extend(a.engines)
    b = _build("hashsame")
    engines.extend(b.engines)
    assert a.config_hash == b.config_hash
    assert a.runtime_settings.context_budget is not None  # routed ⇒ budget present


async def _noop_hook(name: str, args: Any) -> None:  # pragma: no cover - unused
    return None


def _manifest_toml(*, generation: int, version: int, description: str) -> str:
    return f"""schema_version = 1
generation = {generation}
[[agents]]
agent_id = "agt_01k3wedgeproof00000000000000"
version = {version}
label = "Wedge Proof Agent"
description = "{description}"
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
"""


def test_a_release_that_fails_the_fit_check_leaves_the_registry_head_unchanged(
    tmp_path: Path, engines: list[Any]
) -> None:
    """NE-CTX-IN-FORCE round 6, finding 1 — no wedged deployment.

    ``build_platform`` used to reconcile the Agent registry FIRST and run
    ``validate_tool_catalog_budgets`` after, so a release combining a new
    manifest generation with a model window too small for its catalog had
    already committed the new registry head when validation raised. The app
    then failed to boot AND rolling back to the previous manifest failed too —
    its generation was now stale ("stale Agent registry generation") — a wedged
    deployment with no in-band way out.

    Validation now precedes reconciliation, so the failing release must leave
    the registry head byte-unchanged and the previous manifest bootable. Both
    halves are asserted by execution below, against the REAL registry head row
    the reconciler writes.
    """
    import sqlalchemy as sa

    from ne.api.agent_catalog import ne_agent_registry_head

    manifest_v1 = tmp_path / "registry-gen1.toml"
    manifest_v1.write_text(
        _manifest_toml(generation=1, version=1, description="Generation one"), encoding="utf-8"
    )
    manifest_v2 = tmp_path / "registry-gen2.toml"
    manifest_v2.write_text(
        _manifest_toml(generation=2, version=2, description="Generation two"), encoding="utf-8"
    )

    def _good_settings() -> GatewaySettings:
        return GatewaySettings(
            models=[_model("primary", window=32768)], tasks={"agent": TaskRoute(model="primary")}
        )

    # Boot 1 — yesterday's good release: manifest generation 1, adequate window.
    reset_config_hash_for_testing()
    first = build_platform(
        _api_settings(agent_manifest_path=str(manifest_v1)),
        _seams(tmp_path, key="wedge", gs=_good_settings()),
    )
    engines.extend(first.engines)

    registry_engine = sa.create_engine(f"sqlite:///{tmp_path}/runtime-wedge.db")
    engines.append(registry_engine)

    def _head() -> tuple[int, str, str]:
        with registry_engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_registry_head).where(ne_agent_registry_head.c.singleton_key == 1)
            ).one()
        return (
            int(row.generation),
            str(row.manifest_raw_hash),
            str(row.manifest_semantic_hash),
        )

    committed = _head()
    assert committed[0] == 1

    # Boot 2 — the bad release: manifest generation 2 PLUS a window whose input
    # budget (~376) can never hold the catalog + the 512 floor. Validation must
    # fail LOUD — and must fail BEFORE the registry head moves.
    reset_config_hash_for_testing()
    bad = GatewaySettings(
        models=[_model("tiny", window=1600)], tasks={"agent": TaskRoute(model="tiny")}
    )
    with pytest.raises(GatewayConfigError, match="agent"):
        wedged = build_platform(
            _api_settings(agent_manifest_path=str(manifest_v2)),
            _seams(tmp_path, key="wedge", gs=bad),
        )
        engines.extend(wedged.engines)

    # THE assertion of this round: the head is byte-unchanged — generation 1,
    # same fingerprints — not merely "an error was raised".
    assert _head() == committed

    # And the previous manifest is still BOOTABLE: the rollback the wedge used
    # to refuse with "stale Agent registry generation" now comes up clean.
    reset_config_hash_for_testing()
    rollback = build_platform(
        _api_settings(agent_manifest_path=str(manifest_v1)),
        _seams(tmp_path, key="wedge", gs=_good_settings()),
    )
    engines.extend(rollback.engines)
    assert _head() == committed
