# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-LIVE-SEAMS — the default-off demo seams at the composition root.

Proves at the ne-api composition root: (1) default-off byte-parity (the demo
ApiSettings fields defaulted vs explicit-off produce the SAME config_hash AND the SAME
executor tool menu — NE-GA-G7 adds ``demo_grounded_hallucination`` to this check; its
own dedicated wiring/e2e proof lives in ``test_demo_grounded_hallucination_seam.py``);
(2) the governed TIER-2 ``write.external.demo.send`` binding is
registered through the frozen executor with a merged guardrail policy and stamps a
DIFFERENT config_hash; (3) the retired production graph-fixture setting is absent and
the generated demo overlay composes an honestly disabled graph tier; (4) the optional
ontology lift fails loud without its prerequisites and, with an injected test graph, a
verified template returns rows lifted from the snapshot; (5) the loop-safe coroutine
driver works under a running loop (the ne-worker composition path); (6) the
NE-ONTOLOGY-FLYWHEEL projection tripwire passes with the demo tool + verified answers on.

The governed spine is REAL; only the network edges + the graph store are faked. No
pytest-asyncio (repo policy): async is driven via the conftest ``run`` helper.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ne.api import create_app
from ne.api.demo_seams import DEMO_WRITEBACK_ACTION, run_boot_coroutine
from ne.api.platform import AppSeams, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import ApprovalTier, Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.runtime.config import RuntimeSettings

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    run,
)

_ACCOUNTS_DIRECTORY_TEMPLATE = """
template_id = "accounts-directory"
version = 1
description = "Every account visible to the caller, with its name."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
"""


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _seams(
    tmp_path: Path,
    *,
    key: str,
    chunks: tuple[Any, ...] = (),
    brain_graph_settings: BrainGraphSettings | None = None,
    graph_store: Any | None = None,
    ontology_settings: Any | None = None,
    gateway: Any | None = None,
) -> tuple[AppSeams, FakeTokenVerifier]:
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
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
        gateway=gateway if gateway is not None else ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(chunks),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        brain_graph_settings=brain_graph_settings,
        graph_store=graph_store,
        ontology_settings=ontology_settings,
    )
    return seams, verifier


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


def _tool_names(platform: Any) -> set[str]:
    return {s.name for s in platform.executor.tool_specs}


# --- (1) default-off byte-parity ---------------------------------------------
def test_demo_default_off_config_hash_and_tool_menu_byte_identical(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams_a, _ = _seams(tmp_path, key="ident")
    default = build_platform(_api_settings(), seams_a)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_b, _ = _seams(tmp_path, key="ident")
    explicit_off = build_platform(
        _api_settings(
            demo_meridian_fixture_path="",
            demo_ontology_lift=False,
            demo_writeback_tool=False,
            demo_grounded_hallucination=False,
        ),
        seams_b,
    )
    engines.extend(explicit_off.engines)

    assert default.config_hash == explicit_off.config_hash
    assert _tool_names(default) == _tool_names(explicit_off)
    assert DEMO_WRITEBACK_ACTION not in _tool_names(default)


# --- (2) tier-2 write-back registration + hash delta -------------------------
def test_demo_writeback_tool_registered_governed_tier2(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    seams_d, _ = _seams(tmp_path, key="ident")
    default = build_platform(_api_settings(), seams_d)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_w, _ = _seams(tmp_path, key="ident")
    withtool = build_platform(_api_settings(demo_writeback_tool=True), seams_w)
    engines.extend(withtool.engines)

    # Registered on the frozen executor as a TIER-2 spec.
    assert DEMO_WRITEBACK_ACTION in _tool_names(withtool)
    spec = next(s for s in withtool.executor.tool_specs if s.name == DEMO_WRITEBACK_ACTION)
    assert spec.tier is ApprovalTier.TIER_2
    # Present in the merged guardrail policy (else it would be fail-closed-rejected).
    assert DEMO_WRITEBACK_ACTION in withtool.govern_settings.guardrails.tools
    # The config_hash moves (honestly stamped) vs default-off.
    assert withtool.config_hash != default.config_hash


# --- (3) production demo fixture removal ------------------------------------
def test_demo_graph_fixture_setting_is_not_public_configuration() -> None:
    assert "demo_graph_fixture_path" not in ApiSettings.model_fields


def test_generated_demo_overlay_composes_honest_graph_disabled_404(tmp_path: Path) -> None:
    """The real generated overlay cannot resurrect the retired fixture T2 path."""
    from ne.brain_graph.config import BrainGraphSettings
    from ne.cli.demo import live

    seed_dir = live._stage_demo_seed(tmp_path)
    overlay_path = live._write_demo_overlay(tmp_path, seed_dir)

    import yaml

    doc = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    api_env = {
        str(key): str(value) for key, value in doc["services"]["ne-api"]["environment"].items()
    }
    worker_env = {
        str(key): str(value) for key, value in doc["services"]["ne-worker"]["environment"].items()
    }
    for retired in (
        "NE_BRAIN_GRAPH_ENABLED",
        "NE_BRAIN_GRAPH_SOURCES",
        "NE_API_DEMO_ONTOLOGY_LIFT",
    ):
        assert retired not in api_env
        assert retired not in worker_env

    graph_env = {key: value for key, value in api_env.items() if key.startswith("NE_BRAIN_GRAPH_")}
    with patch.dict(os.environ, graph_env, clear=True):
        api_settings = _api_settings()
        graph_settings = BrainGraphSettings()
    assert graph_settings.enabled is False

    reset_config_hash_for_testing()
    seams, verifier = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=graph_settings,
        graph_store=None,
    )
    app = create_app(api_settings, seams=seams)
    state = get_state(app)

    import httpx

    async def _t() -> None:
        token = verifier.mint("u-reader", groups=["all-employees"], department="sales")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            response = await client.get(
                "/api/v1/graph/neighborhood",
                params={"entity": "any", "hops": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "graph tier not enabled"}

    run(_t())
    for engine in state.engines:
        engine.dispose()


# --- (4) ontology lift prerequisites + positive lift -------------------------
def test_demo_ontology_lift_without_ontology_is_loud(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    seams, _ = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=InMemoryGraphStore(),
    )
    with pytest.raises(ValueError, match="ontology read surface"):
        build_platform(_api_settings(demo_ontology_lift=True), seams)


def _ontology_settings(tmp_path: Path) -> Any:
    from ne.brain_ontology.config import OntologySettings

    directory = tmp_path / "verified-templates"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "accounts_directory.toml").write_text(
        _ACCOUNTS_DIRECTORY_TEMPLATE, encoding="utf-8"
    )
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
    )


def test_demo_ontology_lift_without_graph_is_loud(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    seams, _ = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        ontology_settings=_ontology_settings(tmp_path),  # verified on, graph OFF
    )
    with pytest.raises(ValueError, match="graph tier enabled"):
        build_platform(_api_settings(demo_ontology_lift=True), seams)


def _accounts_snapshot_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()

    async def _seed() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "acct1",
                    "Account",
                    ("all-employees",),
                    name="Acme",
                    id="acct1",
                    source_status="no_source",
                ),
                make_node(
                    "acct2",
                    "Account",
                    ("all-employees",),
                    name="Beta",
                    id="acct2",
                    source_status="no_source",
                ),
            ]
        )

    run(_seed())
    return store


def test_demo_lift_with_both_yields_verified_rows(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    seams, verifier = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=_accounts_snapshot_store(),
        ontology_settings=_ontology_settings(tmp_path),
    )
    app = create_app(
        _api_settings(demo_ontology_lift=True, chat_agents=("console-chat",)),
        seams=seams,
    )
    state = get_state(app)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-emp", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    state.perm_store.set_member_groups(
        "u-emp", ["all-employees"], source="test", now=datetime.now(UTC)
    )

    import httpx

    async def _t() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            token = verifier.mint("u-emp", groups=["all-employees"], department="engineering")
            r = await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory", "args": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            answer = body["answer"]
            assert len(answer["rows"]) >= 1
            assert all(c.startswith("urn:ne:entity") for c in answer["citations"])
            assert answer["config_hash"]
            assert answer["ontology_version"]
            assert body["replay_match"] is True

    run(_t())
    for engine in state.engines:
        engine.dispose()


# --- (5) loop-safe coroutine driver ------------------------------------------
def test_run_coroutine_works_under_running_loop(tmp_path: Path) -> None:
    async def _drive() -> Any:
        # Simulate the ne-worker path: drive a value-returning coroutine while an
        # event loop already owns the current thread.
        async def _value() -> int:
            return 2

        return run_boot_coroutine(_value())

    assert run(_drive()) == 2


def test_run_coroutine_returns_value_no_loop() -> None:
    async def _co() -> int:
        return 41

    assert run_boot_coroutine(_co()) == 41


# --- (6) projection tripwire with demo tool + verified answers on ------------
def test_demo_writeback_and_verified_answers_pass_projection_tripwire(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams, _ = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=_accounts_snapshot_store(),
        ontology_settings=_ontology_settings(tmp_path),
    )
    # Boot must NOT raise the NE-ONTOLOGY-FLYWHEEL drift tripwire (RuntimeError) when
    # the demo write-back tool AND verified answers are both on + the lift runs.
    platform = build_platform(
        _api_settings(demo_writeback_tool=True, demo_ontology_lift=True), seams
    )
    engines.extend(platform.engines)
    assert DEMO_WRITEBACK_ACTION in _tool_names(platform)
    assert platform.verified_curation is not None


# --- (4b) NE-LIFT-FIX: a required-edge entity (Order.account, minCount>=1) must
# land in T3 on the DEMO BOOT lift. With the old append-only ``.run()`` the Order
# node's literal batch failed SHACL before its ACCOUNT edge landed and the whole
# entity was quarantined (zero Order rows); ``.reconcile()`` validates the node +
# edges TOGETHER per ACL graph, so the orders template returns the row.
_ORDERS_DIRECTORY_TEMPLATE = """
template_id = "orders-directory"
version = 1
description = "Every order visible to the caller, with its account and status."
sparql = \"\"\"
SELECT ?order ?account ?status WHERE {
  #BINDINGS#
  ?order a <https://neutron.enterprise/schema/operational/Order> .
  ?order <https://neutron.enterprise/schema/operational/account> ?account .
  ?order <https://neutron.enterprise/schema/operational/status> ?status .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["order", "account", "status"]
citation_var = "order"
"""


def _orders_snapshot_store() -> InMemoryGraphStore:
    store = _accounts_snapshot_store()

    async def _seed() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "p1",
                    "Person",
                    ("all-employees",),
                    id="p1",
                    given_name="Alice",
                    family_name="Ng",
                    email="alice@acme.com",
                    source_status="no_source",
                ),
                make_node(
                    "ord1",
                    "Order",
                    ("all-employees",),
                    id="ord1",
                    order_number="ORD-0001",
                    status="placed",
                    currency="USD",
                    total_amount="100.50",
                    placed_at="2026-01-02T00:00:00+00:00",
                    source_status="no_source",
                ),
            ]
        )
        await store.upsert_edges(
            [
                # BOTH of Order's SHACL-required (minCount>=1) edges — the exact
                # shape class the append-only .run() quarantined WHOLE.
                make_edge("ACCOUNT", "ord1", "acct1", ("all-employees",))
                | {"source_status": "no_source"},
                make_edge("PLACED_BY", "ord1", "p1", ("all-employees",))
                | {"source_status": "no_source"},
            ]
        )

    run(_seed())
    return store


def test_demo_lift_required_edge_order_lands_in_t3(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    ontology_settings = _ontology_settings(tmp_path)
    (tmp_path / "verified-templates" / "orders_directory.toml").write_text(
        _ORDERS_DIRECTORY_TEMPLATE, encoding="utf-8"
    )
    seams, verifier = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=_orders_snapshot_store(),
        ontology_settings=ontology_settings,
    )
    app = create_app(
        _api_settings(demo_ontology_lift=True, chat_agents=("console-chat",)),
        seams=seams,
    )
    state = get_state(app)
    state.perm_store.upsert_principals(
        [SyncedPrincipal(principal_id="u-emp", kind=PrincipalKind.USER)],
        source="test",
        now=datetime.now(UTC),
    )
    state.perm_store.set_member_groups(
        "u-emp", ["all-employees"], source="test", now=datetime.now(UTC)
    )

    import httpx

    async def _t() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            token = verifier.mint("u-emp", groups=["all-employees"], department="engineering")
            r = await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "orders-directory", "args": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            answer = r.json()["answer"]
            # THE regression bit: the required-edge Order row actually landed.
            assert len(answer["rows"]) == 1
            row = dict(zip(answer["columns"], answer["rows"][0], strict=True))
            assert row["order"].endswith("ord1")
            assert row["account"].endswith("acct1")
            assert row["status"] == "placed"

    run(_t())
    for engine in state.engines:
        engine.dispose()


# --- (7) 015A0-AUTHORITY-T3: the stamped Meridian fixture-load seam ------------

_MERIDIAN_FIXTURE = (
    Path(__file__).resolve().parents[3] / "tools" / "meridian" / "fixtures" / "meridian-v1"
)


def test_meridian_seam_returns_the_builders_exact_release_descriptor() -> None:
    """The boot seam surfaces the stamped release, it does not discard it.

    The composition root must bind the descriptor this load actually produced rather
    than recompute a second one, so the seam returns the report; and the descriptor it
    returns must be byte-equal to what the loader's own builder derives from the same
    checked-in generator bytes.
    """
    from ne.api.demo_seams import run_demo_meridian_fixture_load
    from ne.brain_ontology import build_meridian_release, build_oxigraph_store
    from ne.brain_ontology.config import OntologySettings

    store = build_oxigraph_store(OntologySettings(include_ontology_graph=False))
    report = run_demo_meridian_fixture_load(_MERIDIAN_FIXTURE, store)
    assert report.release == build_meridian_release(_MERIDIAN_FIXTURE).descriptor
    assert report.fixture_hash == report.release.manifest_hash


def test_meridian_seam_logs_a_governed_local_t3_contract_and_never_m365_lineage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The seam's provenance line names ``governed_local_t3`` and the exact contract hash.

    A local fixture row must never be described with M365 provenance: that producer's
    source kind must not appear anywhere in this seam's output.
    """
    from ne.api.demo_seams import run_demo_meridian_fixture_load
    from ne.brain_ontology import build_oxigraph_store, meridian_source_contract_identity
    from ne.brain_ontology.config import OntologySettings

    store = build_oxigraph_store(OntologySettings(include_ontology_graph=False))
    with caplog.at_level(logging.INFO, logger="ne.api.demo_seams"):
        report = run_demo_meridian_fixture_load(_MERIDIAN_FIXTURE, store)
    contract = meridian_source_contract_identity(report.release)
    assert contract.source_kind == "governed_local_t3"
    messages = [record.getMessage() for record in caplog.records]
    stamped = [m for m in messages if "release_descriptor=" in m]
    assert len(stamped) == 1
    assert f"source_kind={contract.source_kind}" in stamped[0]
    assert f"source_contract={contract.identity_hash}" in stamped[0]
    assert f"release_descriptor={report.release.descriptor_hash}" in stamped[0]
    assert "m365" not in stamped[0].lower()
