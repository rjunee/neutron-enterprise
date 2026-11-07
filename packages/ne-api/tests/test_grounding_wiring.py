# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GA-G5: the composition-root GroundingPolicy wiring, inline + route-level.

Proves default-off byte-identity (no ontology config, OR verified enabled but
``grounded_agent_enabled`` left False ⇒ ``platform.grounding`` stays ``None`` and
the terminal ``done`` frame is byte-for-byte unchanged), the fail-loud misconfig
guard (grounded-on without verified-on is a boot ``ValueError``), the grounded
happy/fail path (a fabricated draft with zero fetches always falls back,
carrying structural ``grounded`` provenance on the done frame), the per-agent-name
gate (only ``grounded_agent_name`` gets the policy — verified at both the session
level and the full HTTP route level), the live tool restriction (the grounded
agent's SSE ``tool`` frames + in-band model catalog show ONLY ``read.kg.*``
names), the config_hash discipline (moves only with the flag), and the bounded
G4 assist boundary (a bare 2-message, no-catalog completion).

NE-HGATE-3 adds: strictness/profile threading onto ``platform.grounding``
(object identity), the done-frame ``grounded.strictness`` stamp on a real SSE
turn, the B-P0-1 no-request-override guardian (wire models cannot name a tier),
the adopted-candidate FAIL-redraft path through the REAL ``_grounding_assist``
wrapper, the boot fail-loud belt (missing ``grounding-assist`` route ⇒ boot
ValueError; and its pass paths), and — the owner-directed §6.3 test-blindness
fix — the ``TaskRoutingGateway`` NEGATIVE (unrouted assist surfaces an audited
``UnknownTaskError`` degrade, assist_used=False) and POSITIVE (route present ⇒
assist_used=True) regression pair.

Self-contained harness copied from ``test_verified_router_wiring.py`` (NOT
imported from it — only ``conftest`` seams are shared) so this module's
fixtures stay independent of that unit's TOML content.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.sse import turn_event_stream
from ne.api.tool_transit import TURN_TOOL_TRANSITS, TurnTransitChannel
from ne.api.tools import TOOL_NAME, TURN_PRINCIPAL
from ne.api.wire import ChatMessageRequest, CreateSessionRequest
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.grounding_policy import PROFILE_NG1_FROZEN, PROFILE_NG2, StrictnessTier
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_department_session
from ne.runtime.store import ne_agent_sessions

from .conftest import (
    ISSUER,
    AppBuilder,
    FakeTokenVerifier,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    TaskRoutingGateway,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)

_DEPARTMENT = "engineering"
_GROUNDED_AGENT = "grounded-analyst"
_ORDINARY_AGENT = "console-chat"

# --- fixture templates (self-contained; NOT the real corpus; NO [routing] so
# the U3 pre-step never fires) -----------------------------------------------

_TEMPLATES = {
    "accounts_directory.toml": """
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
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
    "entity_by_iri.toml": """
template_id = "entity-by-iri"
version = 1
description = "Fetch one entity's name by its exact IRI."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[args.account]
type = "iri"

[scope]
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
}

#: The SAME accounts_directory template PLUS a ``[routing]`` block, so
#: ``platform.verified_router`` is actually built (non-None) — used ONLY by
#: the Codex-P1 regression test below (a grounded-analyst turn must never be
#: short-circuited by the router). ``entity_by_iri.toml`` stays WITHOUT
#: ``[routing]`` (mirrors test_verified_router_wiring.py's fixture exactly).
_ROUTED_TEMPLATES = {
    "accounts_directory.toml": _TEMPLATES["accounts_directory.toml"]
    + """
[routing]
domain = "accounts-directory"
triggers = ["list every account i am cleared to see"]
guard_terms = ["accounts directory", "cleared to see"]
clarify_max = 2
""",
    "entity_by_iri.toml": _TEMPLATES["entity_by_iri.toml"],
}


def _write_templates(directory: Path, templates: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def _account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acct",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "name": f"{key} Corp",
    }


class _NullSink:
    def emit(self, event: Any) -> None:
        return None


def _seed_store(config_hash: str) -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    store = ValidatingTripleStore(
        OxigraphBackend(),
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash=config_hash,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _seed() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes(
            [
                _account("acc_alpha", ["alpha"]),
                _account("acc_beta", ["beta"]),
                _account("acc_both", ["alpha", "beta"]),
            ]
        )
        await PropertyGraphLift(mem, store).run()

    run(_seed())
    return store


class _EmptyChunkStore:
    async def query(self, **_kw: Any) -> list[Any]:
        return []

    async def get(self, chunk_ids: Any) -> list[Any]:
        return []

    async def get_filtered(self, chunk_ids: Any, **_kw: Any) -> list[Any]:
        return []

    async def upsert_chunks(self, chunks: Any) -> None:
        return None

    async def delete(self, chunk_ids: Any) -> None:
        return None


@dataclass
class PlatformHarness:
    platform: Platform
    gateway: Any


PlatformBuilder = Callable[..., PlatformHarness]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[PlatformBuilder]:
    engines: list[sa.Engine] = []

    def _make(
        *,
        ontology_settings: OntologySettings | None = None,
        seed_store: bool = False,
        gateway: Any | None = None,
        key: str | None = None,
        gateway_tasks: dict[str, Any] | None = None,
        runtime_lane: Lane | None = None,
    ) -> PlatformHarness:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        if seed_store:
            store = _seed_store("s" * 64)
            registry.register("triple_store", "oxigraph", lambda **_kw: store)

        key = key or uuid.uuid4().hex
        runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

        gw = gateway if gateway is not None else ScriptedGateway()
        seams = AppSeams(
            identity_settings=IdentitySettings(
                verifier="fake",
                oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
                principal_claims=("email", "department"),
            ),
            runtime_settings=RuntimeSettings(
                session_database_url=runtime_url,
                lane=runtime_lane.value if runtime_lane is not None else None,
            ),
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
                ],
                # NE-HGATE-3: routed tasks feed the boot fail-loud belt (TaskRoute
                # values reference the configured 'stub' model so GatewaySettings
                # validates). Default {} keeps the belt a no-op (its documented
                # injected-seam semantics), so every existing test is unaffected.
                tasks=gateway_tasks or {},
            ),
            gateway=gw,
            embedder=StaticEmbedder(),
            sparse_encoder=StaticSparseEncoder(),
            chunk_store=_EmptyChunkStore(),
            plugin_registry=registry,
            ontology_settings=ontology_settings,
        )
        platform = build_platform(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
            ),
            seams,
        )
        engines.extend(platform.engines)
        return PlatformHarness(platform=platform, gateway=gw)

    yield _make
    for engine in engines:
        engine.dispose()


def _enabled_settings(
    directory: Path,
    templates: dict[str, str],
    *,
    grounded_agent_enabled: bool = False,
    grounded_assist_enabled: bool = True,
    grounding_strictness: Literal["strict", "standard"] = "strict",
    grounding_gate_profile: Literal["ng1-frozen", "ng2"] = "ng2",
) -> OntologySettings:
    _write_templates(directory, templates)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=grounded_agent_enabled,
        grounded_assist_enabled=grounded_assist_enabled,
        grounding_strictness=grounding_strictness,
        grounding_gate_profile=grounding_gate_profile,
    )


#: Byte-mirror of the NE_GATEWAY_TASKS keys the demo overlay routes
#: (``_gateway_lane_env`` in tools/ne-cli/src/ne/cli/demo/live.py) — minus
#: grounding-assist, which ``_DEMO_TASKS_WITH_ASSIST`` below adds, because the
#: pair exists to drive the HGATE-3 route-present / route-absent contrast.
#: These maps feed ``TaskRoutingGateway`` ONLY (plain dicts — the double calls
#: ``.get``, mirroring the real gateway on ``GatewaySettings.tasks``); they are
#: never handed to ``GatewaySettings``.
#:
#: HAND-MAINTAINED, and nothing compares them to the generator, so the mirror
#: claim is only as true as this edit. Kept in step with
#: NE-DEMO-NO-CPU-FALLBACK (2026-08-27): the sealed branch these mirror routes
#: every customer-text task at ``private-default`` with NO fallback, and
#: the 0.6B CPU lane is gone from the menu outright.
_DEMO_TASKS = {
    "agent": {"model": "private-default"},
    "triage": {"model": "private-default"},
    "reason": {"model": "private-default"},
    "adjudicate": {"model": "frontier-best"},
    "skill-authoring": {"model": "private-default"},
}
#: The §6.2-1 route HGATE-4 ships — the demo map WITH the grounding-assist route.
_DEMO_TASKS_WITH_ASSIST = {
    **_DEMO_TASKS,
    "grounding-assist": {"model": "private-default"},
}


def _assist_audit_events(tmp_path: Path, key: str) -> list[Any]:
    """Read every ``grounding.assist`` audit row from the turn's sqlite audit DB
    (mirrors test_demo_grounded_hallucination_seam.py's audit read)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path}/audit-{key}.db")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.outcome, ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "grounding.assist"
                )
            ).all()
    finally:
        engine.dispose()
    return list(rows)


def _parse_frames(frames: list[bytes]) -> list[tuple[str, dict[str, Any] | None]]:
    """Parse raw SSE frame bytes into ``(event, data)`` pairs (skips keep-alives)."""
    parsed: list[tuple[str, dict[str, Any] | None]] = []
    for raw in frames:
        text = raw.decode()
        if text.startswith(":"):
            continue  # a comment keep-alive line
        lines = text.rstrip("\n").split("\n")
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: ")) if len(lines) > 1 else None
        parsed.append((event, data))
    return parsed


async def _run_turn(
    platform: Platform,
    gateway: Any,
    prompt: str,
    *,
    agent_name: str = _ORDINARY_AGENT,
    grounded: bool = False,
    transits: TurnTransitChannel | None = None,
    requested_model: str | None = None,
    requested_model_lane: Lane | None = None,
) -> tuple[str, list[bytes]]:
    """Open a session (through the composition-root grounding wiring) and drive
    one turn, mirroring ``routers/chat.py``'s name-gated ``grounding=`` threading."""
    principal = Principal(id="user:a", groups=("alpha",), claims={"department": _DEPARTMENT})
    session = await create_department_session(
        agent_name,
        _DEPARTMENT,
        owner_principal_id=principal.id,
        store=platform.session_store,
        gateway=gateway,
        executor=platform.executor,
        settings=platform.runtime_settings,
        # NE-GA-G5 (Codex P1 fix): mirror routers/chat.py:_open EXACTLY — the
        # grounded-analyst never gets the verified_router pre-step (a ROUTE/
        # CLARIFY/REFUSE outcome would short-circuit before the model — and
        # therefore before grounding — ever runs).
        router=None if grounded else platform.verified_router,
        grounding=platform.grounding if grounded else None,
    )
    principal_token = TURN_PRINCIPAL.set(principal)
    transit_token = TURN_TOOL_TRANSITS.set(transits) if transits is not None else None
    try:
        frames = [
            f
            async for f in turn_event_stream(
                session.run(
                    prompt,
                    principal=principal,
                    requested_model=requested_model,
                    requested_model_lane=requested_model_lane,
                ),
                keepalive_s=30.0,
                timeout_s=30.0,
                transits=transits,
            )
        ]
    finally:
        if transit_token is not None:
            TURN_TOOL_TRANSITS.reset(transit_token)
        TURN_PRINCIPAL.reset(principal_token)
    return session.session_id, frames


# --- (a) default-off: grounding is None -------------------------------------


def test_default_off_grounding_is_none(make_platform: PlatformBuilder) -> None:
    h = make_platform()
    assert h.platform.grounding is None
    assert h.platform.grounded_agent_name is None


def test_verified_on_grounded_off_grounding_is_none(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(tmp_path / "verified", _TEMPLATES, grounded_agent_enabled=False)
    h = make_platform(ontology_settings=settings, seed_store=True)
    assert h.platform.grounding is None
    assert h.platform.grounded_agent_name is None


# --- the load-bearing byte-identity pin --------------------------------------


def test_default_off_done_frame_byte_identical(make_platform: PlatformBuilder) -> None:
    gateway = ScriptedGateway([text_completion("the answer")])
    h = make_platform(gateway=gateway)
    assert h.platform.grounding is None

    sid, frames = run(_run_turn(h.platform, gateway, "hi"))
    assert (
        frames[-1]
        == (
            f'event: done\ndata: {{"type":"done","session_id":"{sid}","output":"the answer"}}\n\n'
        ).encode()
    )


# --- fail-loud misconfig guard -----------------------------------------------


def test_grounded_flag_without_verified_raises(make_platform: PlatformBuilder) -> None:
    settings = OntologySettings(grounded_agent_enabled=True)
    with pytest.raises(ValueError, match="NE_ONTOLOGY_GROUNDED_AGENT_ENABLED"):
        make_platform(ontology_settings=settings)


# --- (b) grounded-on FAIL leg + name gate ------------------------------------


def test_grounded_turn_fabricated_draft_falls_back_with_grounded_on_done(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,  # determinism (brief risk #3)
    )
    gateway = ScriptedGateway([text_completion("Initech Global has 73 open orders")])
    h = make_platform(ontology_settings=settings, seed_store=True, gateway=gateway)
    assert h.platform.grounding is not None
    assert h.platform.grounded_agent_name == _GROUNDED_AGENT

    _sid, frames = run(
        _run_turn(h.platform, gateway, "how many open orders does Initech have", grounded=True)
    )
    parsed = _parse_frames(frames)
    done_data = dict(parsed)["done"]
    assert done_data is not None
    grounded = done_data["grounded"]
    assert grounded["fallback"] is True
    assert grounded["assist_used"] is False
    assert grounded["fetches"] == []
    assert "withheld (audited). No verified data was fetched this turn." in done_data["output"]
    assert "Initech" not in done_data["output"]


def test_route_level_name_gate_grounded_vs_ordinary_agent(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    directory = tmp_path / "verified"
    _write_templates(directory, _TEMPLATES)
    settings = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )
    triple_store = _seed_store("t" * 64)
    gateway = ScriptedGateway(
        [text_completion("fabricated grounded draft"), text_completion("console draft")]
    )
    h = make_app(
        gateway=gateway,
        chat_agents=(_ORDINARY_AGENT, _GROUNDED_AGENT),
        ontology_settings=settings,
        triple_store=triple_store,
    )
    headers = h.bearer("user:ada", groups=["alpha"], department=_DEPARTMENT)

    async def _turn(agent_name: str) -> dict[str, Any] | None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": agent_name}, headers=headers
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hello"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            parsed = parse_sse(r.text)
            done = parsed.of_type("done")
            assert len(done) == 1
            return done[0].data

    grounded_done = run(_turn(_GROUNDED_AGENT))
    assert grounded_done is not None
    assert "grounded" in grounded_done

    ordinary_done = run(_turn(_ORDINARY_AGENT))
    assert ordinary_done is not None
    assert "grounded" not in ordinary_done
    assert ordinary_done["output"] == "console draft"


def test_chat_capabilities_advertise_only_composed_and_admitted_grounding(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    configured_agent = "configured-grounding-mode"
    directory = tmp_path / "capabilities"
    _write_templates(directory, _TEMPLATES)
    settings = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_agent_name=configured_agent,
        grounded_assist_enabled=False,
    )
    triple_store = _seed_store("c" * 64)
    admitted = make_app(
        chat_agents=(_ORDINARY_AGENT, configured_agent),
        ontology_settings=settings,
        triple_store=triple_store,
    )

    async def _read(app: Any) -> dict[str, Any]:
        headers = app.bearer("user:ada", groups=["alpha"], department=_DEPARTMENT)
        async with app.client() as client:
            response = await client.get("/api/v1/chat/capabilities", headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

    assert run(_read(admitted)) == {"grounded": {"agent_name": configured_agent}}

    # A policy whose configured agent was not admitted cannot execute through
    # create_session, so it is never advertised as a browser capability.
    reset_config_hash_for_testing()
    not_admitted = make_app(
        chat_agents=(_ORDINARY_AGENT,),
        ontology_settings=settings,
        triple_store=triple_store,
    )
    assert run(_read(not_admitted)) == {"grounded": None}


def test_chat_capabilities_default_off_is_authenticated_and_side_effect_free(
    make_app: AppBuilder,
) -> None:
    h = make_app()

    async def _read(headers: dict[str, str] | None = None) -> tuple[int, Any]:
        async with h.client() as client:
            response = await client.get("/api/v1/chat/capabilities", headers=headers)
            return response.status_code, response.json()

    status, body = run(_read())
    assert status == 401
    assert "detail" in body

    audit_engine = sa.create_engine(h.audit_url)
    runtime_engine = sa.create_engine(h.runtime_url)
    with audit_engine.connect() as conn:
        audit_before = conn.scalar(sa.select(sa.func.count()).select_from(ne_audit_log))
    with runtime_engine.connect() as conn:
        sessions_before = conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_sessions))

    headers = h.bearer("user:ada", groups=["alpha"], department=_DEPARTMENT)
    before = len(h.gateway.requests)
    status, body = run(_read(headers))
    assert status == 200
    assert body == {"grounded": None}
    assert len(h.gateway.requests) == before
    with audit_engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_audit_log)) == audit_before
    with runtime_engine.connect() as conn:
        assert (
            conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_sessions))
            == sessions_before
        )
    audit_engine.dispose()
    runtime_engine.dispose()


def test_grounded_agent_not_short_circuited_by_verified_router(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    """Codex P1 regression: routers/chat.py:_open used to hand EVERY session
    (grounded or not) the same ``verified_router``. A prompt matching a
    ``[routing]`` trigger returns a ``RoutedTurn`` BEFORE the model — and
    therefore before the grounding post-step — ever runs (agent.py:158-182),
    so a grounded-analyst turn would silently carry no ``grounded``
    provenance for exactly the prompts the router matches. The fix: the
    grounded-analyst session never receives the router (``router=None``);
    an ordinary agent on the SAME platform is still routed deterministically.
    """
    directory = tmp_path / "routed"
    _write_templates(directory, _ROUTED_TEMPLATES)
    settings = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )
    triple_store = _seed_store("r" * 64)
    # Only the grounded leg ever reaches the model (the ordinary leg is
    # routed deterministically and never touches the gateway) — one
    # completion is enough for both turns.
    gateway = ScriptedGateway([text_completion("Initech Global has 73 open orders")])
    h = make_app(
        gateway=gateway,
        chat_agents=(_ORDINARY_AGENT, _GROUNDED_AGENT),
        ontology_settings=settings,
        triple_store=triple_store,
    )
    headers = h.bearer("user:ada", groups=["alpha"], department=_DEPARTMENT)
    trigger = "list every account i am cleared to see"

    async def _turn(agent_name: str) -> dict[str, Any] | None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": agent_name}, headers=headers
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": trigger},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            parsed = parse_sse(r.text)
            done = parsed.of_type("done")
            assert len(done) == 1
            return done[0].data

    # Ordinary agent: the SAME routing-eligible prompt is short-circuited by
    # the verified_router pre-step (deterministic 'verified' provenance, zero
    # model tokens) — proving the router is genuinely live on this platform.
    ordinary_done = run(_turn(_ORDINARY_AGENT))
    assert ordinary_done is not None
    assert "verified" in ordinary_done
    assert "grounded" not in ordinary_done

    # Grounded-analyst: the SAME trigger prompt must NOT be routed away from
    # the model — the grounding gate must run and stamp 'grounded'.
    grounded_done = run(_turn(_GROUNDED_AGENT))
    assert grounded_done is not None
    assert "grounded" in grounded_done
    assert "verified" not in grounded_done
    assert gateway.requests, "the grounded turn must reach the model (no router short-circuit)"


# --- (d) tool restriction visible at the wire --------------------------------


def test_grounded_agent_sees_and_calls_only_verified_read_tools(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.kg.accounts_directory", {}),
            text_completion("Initech Global has 73 open orders"),
        ]
    )
    h = make_platform(ontology_settings=settings, seed_store=True, gateway=gateway)
    assert h.platform.grounding is not None

    channel = TurnTransitChannel()
    _sid, frames = run(
        _run_turn(
            h.platform,
            gateway,
            "list every account i am cleared to see",
            agent_name=_GROUNDED_AGENT,
            grounded=True,
            transits=channel,
        )
    )
    parsed = _parse_frames(frames)
    tool_frames = [data for event, data in parsed if event == "tool"]
    assert tool_frames, "expected at least one tool frame"
    for tool_data in tool_frames:
        assert tool_data is not None
        assert tool_data["name"].startswith("read.kg.")

    catalog_texts = [m.content for m in gateway.requests[0].messages if m.role == "system"]
    assert any("read.kg.accounts_directory" in text for text in catalog_texts)
    assert not any(TOOL_NAME in text for text in catalog_texts)

    done_data = dict(parsed)["done"]
    assert done_data is not None
    grounded = done_data["grounded"]
    assert grounded["fetches"][0]["template_id"] == "accounts-directory"


# --- config_hash discipline ---------------------------------------------------


def test_config_hash_moves_only_with_grounded_on(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    directory = tmp_path / "verified"
    _write_templates(directory, _TEMPLATES)
    settings_off = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=False,
    )
    settings_on = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )

    # A shared ``key`` fixes the sqlite database URLs identically across builds
    # so the ONLY composed-config difference under test is the ontology flag
    # (the test_graph_wiring.py ``key=`` precedent).
    a = make_platform(ontology_settings=settings_off, seed_store=True, key="ident")
    b = make_platform(ontology_settings=settings_on, seed_store=True, key="ident")
    assert a.platform.config_hash != b.platform.config_hash

    # Default-off is unaffected: the new OntologySettings fields are stamped into
    # config_hash ONLY when ontology_read_enabled is True (platform.py ~466-467).
    # A T3-ON grounded-OFF deployment DOES take a one-time hash move on upgrade
    # (the stamped OntologySettings model gained fields; model_dump(mode="json")
    # includes defaults) — a real, audited config-surface change, not a bug.
    c = make_platform(key="ident2")
    c2 = make_platform(key="ident2")
    assert c.platform.config_hash == c2.platform.config_hash


# --- G4 assist boundary -------------------------------------------------------


def test_grounded_assist_is_bare_gateway_completion(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
    )
    # Both completions are scripted with content that cannot possibly be
    # grounded (the corpus is EMPTY — zero fetches this turn — so ANY draft
    # containing non-connective prose fails; the assist candidate is scripted
    # to fail identically, proving the assist can only improve, never degrade).
    gateway = ScriptedGateway(
        [
            text_completion("fabricated draft with no fetches"),
            text_completion("Zorblatt Nine shipped nine hundred widgets"),
        ]
    )
    h = make_platform(
        ontology_settings=settings,
        seed_store=True,
        gateway=gateway,
        runtime_lane=Lane.PRIVATE_VLLM,
    )
    assert h.platform.grounding is not None
    assert h.platform.grounding.assist is not None

    _sid, frames = run(_run_turn(h.platform, gateway, "what is going on", grounded=True))
    parsed = _parse_frames(frames)
    done_data = dict(parsed)["done"]
    assert done_data is not None
    grounded = done_data["grounded"]
    assert grounded["fallback"] is True
    assert grounded["assist_used"] is False

    assist_requests = [r for r in gateway.requests if r.task == "grounding-assist"]
    assert len(assist_requests) == 1
    assist_request = assist_requests[0]
    assert [m.role for m in assist_request.messages] == ["system", "user"]
    for message in assist_request.messages:
        assert "Available tools" not in message.content

    # NE-COST-PRINCIPAL: through the REAL platform composition, the run-root
    # principal (_run_turn's Principal(id="user:a", ...)) threads onto BOTH
    # the agent's own draft request AND the grounding-assist request — proving
    # run_principal_scope is actually entered end-to-end, not dead plumbing.
    assert assist_request.principal_id == "user:a"
    draft_requests = [r for r in gateway.requests if r.task != "grounding-assist"]
    assert len(draft_requests) == 1
    assert draft_requests[0].principal_id == "user:a"
    # Legacy run: explicit None on the session root preserves the composed
    # deployment route for both the draft and the direct assist completion.
    assert [request.requested_model for request in gateway.requests] == [None, None]
    assert [request.lane for request in gateway.requests] == [
        Lane.PRIVATE_VLLM,
        Lane.PRIVATE_VLLM,
    ]


# --- NE-HGATE-3 (1): strictness/profile thread onto the policy by identity -----


def test_strictness_and_profile_thread_into_policy(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    standard = _enabled_settings(
        tmp_path / "std",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
        grounding_strictness="standard",
        grounding_gate_profile="ng2",
    )
    h = make_platform(ontology_settings=standard, seed_store=True, key="std")
    assert h.platform.grounding is not None
    assert h.platform.grounding.strictness is StrictnessTier.STANDARD
    assert h.platform.grounding.profile is PROFILE_NG2

    frozen = _enabled_settings(
        tmp_path / "frozen",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
        grounding_strictness="strict",
        grounding_gate_profile="ng1-frozen",
    )
    h2 = make_platform(ontology_settings=frozen, seed_store=True, key="frozen")
    assert h2.platform.grounding is not None
    assert h2.platform.grounding.strictness is StrictnessTier.STRICT
    assert h2.platform.grounding.profile is PROFILE_NG1_FROZEN


# --- NE-HGATE-3 (2): the done frame carries the deployment posture -------------


def test_done_frame_strictness_standard_gate_ng2(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
        grounding_strictness="standard",
        grounding_gate_profile="ng2",
    )
    gateway = ScriptedGateway([text_completion("Initech Global has 73 open orders")])
    h = make_platform(ontology_settings=settings, seed_store=True, gateway=gateway)

    _sid, frames = run(
        _run_turn(h.platform, gateway, "how many open orders does Initech have", grounded=True)
    )
    grounded = dict(_parse_frames(frames))["done"]["grounded"]  # type: ignore[index]
    # 73 is numeral-ungrounded — a FACT class — so STANDARD still withholds; this
    # proves the tier + profile reached the wire (not that STANDARD relaxes facts).
    assert grounded["strictness"] == "standard"
    assert grounded["gate"].startswith("ng-2/")
    assert grounded["fallback"] is True
    assert grounded["assist_used"] is False


# --- NE-HGATE-3 (3): B-P0-1 no request-level strictness override ---------------


def test_no_request_level_strictness_override(make_app: AppBuilder, tmp_path: Path) -> None:
    # (i) structural: no wire field can NAME a tier. Both request models are
    # extra="forbid" (wire.py:49/57), so a smuggled 'strictness' is a LOUD 422.
    assert set(ChatMessageRequest.model_fields) == {"prompt", "requested_model"}
    assert set(CreateSessionRequest.model_fields) == {"agent_name"}

    # (ii) route-level: smuggling 'strictness' into the POST body is a 422; a clean
    # turn stamps the DEPLOYMENT posture (default strict), never a caller value.
    directory = tmp_path / "verified"
    _write_templates(directory, _TEMPLATES)
    settings = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )
    triple_store = _seed_store("g" * 64)
    gateway = ScriptedGateway([text_completion("Initech Global has 73 open orders")])
    h = make_app(
        gateway=gateway,
        chat_agents=(_ORDINARY_AGENT, _GROUNDED_AGENT),
        ontology_settings=settings,
        triple_store=triple_store,
    )
    headers = h.bearer("user:ada", groups=["alpha"], department=_DEPARTMENT)

    async def _drive() -> dict[str, Any] | None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": _GROUNDED_AGENT}, headers=headers
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]
            smuggled = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hello", "strictness": "standard"},
                headers=headers,
            )
            assert smuggled.status_code == 422, smuggled.text
            clean = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hello"},
                headers=headers,
            )
            assert clean.status_code == 200, clean.text
            done = parse_sse(clean.text).of_type("done")
            assert len(done) == 1
            return done[0].data

    done_data = run(_drive())
    assert done_data is not None
    assert done_data["grounded"]["strictness"] == "strict"


# --- NE-HGATE-3 (4): adopted candidate through the REAL _grounding_assist -------


def test_assist_adopted_candidate_through_real_wrapper(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
        grounding_strictness="standard",
        grounding_gate_profile="ng2",
    )
    # Draft fails (73 numeral-ungrounded, a fact class); the assist candidate has
    # NO fact event, so at STANDARD its vocab findings are advisory — it passes the
    # re-check and is ADOPTED.
    gateway = ScriptedGateway(
        [
            text_completion("Initech Global has 73 open orders"),
            text_completion("There is nothing to report."),
        ]
    )
    h = make_platform(
        ontology_settings=settings,
        seed_store=True,
        gateway=gateway,
        runtime_lane=Lane.PRIVATE_VLLM,
    )

    _sid, frames = run(
        _run_turn(
            h.platform,
            gateway,
            "what is going on",
            grounded=True,
            requested_model="stub",
            requested_model_lane=Lane.FRONTIER_API,
        )
    )
    done_data = dict(_parse_frames(frames))["done"]
    assert done_data is not None
    assert done_data["output"] == "There is nothing to report."
    grounded = done_data["grounded"]
    assert grounded["assist_used"] is True
    assert grounded["fallback"] is True

    assist_requests = [r for r in gateway.requests if r.task == "grounding-assist"]
    assert len(assist_requests) == 1
    assist_request = assist_requests[0]
    assert [m.role for m in assist_request.messages] == ["system", "user"]
    user_msg = next(m.content for m in assist_request.messages if m.role == "user")
    assert "rejected by a grounding check" in user_msg
    # The accepted assist authors the user-visible output, so it must carry the
    # same server-resolved selection as the draft. In particular, the selected
    # frontier lane overrides this platform's legacy private runtime lane.
    assert [request.requested_model for request in gateway.requests] == ["stub", "stub"]
    assert [request.lane for request in gateway.requests] == [
        Lane.FRONTIER_API,
        Lane.FRONTIER_API,
    ]


# --- NE-HGATE-3 (5): THE NEGATIVE — unrouted assist surfaces UnknownTaskError ---


def test_unrouted_assist_task_surfaces_unknown_task_error(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
    )
    # The demo tasks map MINUS grounding-assist: exactly the production topology
    # that silently degraded every live grounded turn. Before TaskRoutingGateway,
    # ScriptedGateway.complete ignored request.task and this turn falsely 'proved'
    # the assist worked. gateway_settings.tasks stays {} so the boot belt no-ops —
    # this test isolates the runtime SUSPENDERS.
    gateway = TaskRoutingGateway(
        ScriptedGateway([text_completion("Initech Global has 73 open orders")]),
        tasks=_DEMO_TASKS,
    )
    h = make_platform(
        ontology_settings=settings, seed_store=True, gateway=gateway, key="unrouted-assist"
    )

    _sid, frames = run(_run_turn(h.platform, gateway, "how many open orders", grounded=True))
    done_data = dict(_parse_frames(frames))["done"]
    assert done_data is not None
    grounded = done_data["grounded"]
    assert grounded["assist_used"] is False
    assert grounded["fallback"] is True
    assert "withheld (audited)" in done_data["output"]

    rows = _assist_audit_events(tmp_path, "unrouted-assist")
    assert len(rows) == 1
    assert rows[0].outcome == "error"
    detail = json.loads(rows[0].payload["detail"])
    assert detail == {"phase": "fail-redraft", "adopted": False, "error_class": "UnknownTaskError"}


# --- NE-HGATE-3 (6): THE POSITIVE — route present ⇒ assist adopts ---------------


def test_routed_assist_task_adopts_candidate(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
        grounding_strictness="standard",
        grounding_gate_profile="ng2",
    )
    gateway = TaskRoutingGateway(
        ScriptedGateway(
            [
                text_completion("Initech Global has 73 open orders"),
                text_completion("There is nothing to report."),
            ]
        ),
        tasks=_DEMO_TASKS_WITH_ASSIST,
    )
    h = make_platform(
        ontology_settings=settings, seed_store=True, gateway=gateway, key="routed-assist"
    )

    _sid, frames = run(_run_turn(h.platform, gateway, "how many open orders", grounded=True))
    done_data = dict(_parse_frames(frames))["done"]
    assert done_data is not None
    assert done_data["output"] == "There is nothing to report."
    assert done_data["grounded"]["assist_used"] is True

    rows = _assist_audit_events(tmp_path, "routed-assist")
    assert len(rows) == 1
    assert rows[0].outcome == "success"
    detail = json.loads(rows[0].payload["detail"])
    assert detail == {"phase": "fail-redraft", "adopted": True, "error_class": None}


# --- NE-HGATE-3 (7)/(8): boot fail-loud belt — fail + pass paths ---------------


def test_boot_fail_loud_assist_route_missing(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
    )
    with pytest.raises(ValueError, match="grounding-assist") as excinfo:
        make_platform(
            ontology_settings=settings,
            seed_store=True,
            gateway_tasks={"agent": {"model": "stub"}},
        )
    assert "NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED" in str(excinfo.value)


def test_boot_pass_route_present(make_platform: PlatformBuilder, tmp_path: Path) -> None:
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
    )
    h = make_platform(
        ontology_settings=settings,
        seed_store=True,
        gateway_tasks={"agent": {"model": "stub"}, "grounding-assist": {"model": "stub"}},
    )
    assert h.platform.grounding is not None


def test_boot_pass_assist_disabled_route_missing(
    make_platform: PlatformBuilder, tmp_path: Path
) -> None:
    # assist DISABLED + route missing ⇒ pure hard-gate, untouched (§6.2-2).
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=False,
    )
    h = make_platform(
        ontology_settings=settings,
        seed_store=True,
        gateway_tasks={"agent": {"model": "stub"}},
    )
    assert h.platform.grounding is not None
    assert h.platform.grounding.assist is None


def test_boot_pass_empty_tasks_noop(make_platform: PlatformBuilder, tmp_path: Path) -> None:
    # tasks == {} + assist enabled ⇒ the documented conftest/injected-seam no-op.
    settings = _enabled_settings(
        tmp_path / "verified",
        _TEMPLATES,
        grounded_agent_enabled=True,
        grounded_assist_enabled=True,
    )
    h = make_platform(ontology_settings=settings, seed_store=True)
    assert h.platform.grounding is not None
