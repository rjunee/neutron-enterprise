# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GA-G7: the grounded-hallucination demo seam.

Proves: (a) the wrapper's routing table in isolation (marked turns are
intercepted with NO call to the inner gateway; unmarked turns delegate
byte-transparently; ``stream`` delegates); (b) the fail-loud misconfig guard
(the flag requires the grounded agent enabled); (c)/(d) default-off byte-parity
(identity + config_hash) and the honest hash move when the seam is on; (e)/(f)/(g)
the deliverable-(b) e2e proof at the ROUTE level: a real, unmodified
``ne.govern.grounding`` gate refuses the seam's fabricated draft LIVE — fallback,
audited ``grounding.ungrounded-claim``, withheld-notice output, zero inner-gateway
completions for the marked turn — with the flag OFF proving the marked prompt
reaches the inner gateway unmodified instead. NE-HGATE-3 adds a tier x profile
parametrization pinning the hardened assist junk withheld under EVERY combination
via a ``numeral-ungrounded`` FACT-class finding (§5.3 beat-8 invariant).

Self-contained harness copied from ``test_grounding_wiring.py`` (that module's own
stated precedent: copy, don't import; reuse conftest ``ScriptedGateway``/
``parse_sse``/``run``/``tool_call_completion``/``text_completion`` + its
``_TEMPLATES`` shape with an ``accounts-directory`` template and a seeded oxigraph
store).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import sqlalchemy as sa

from ne.api import create_app
from ne.api.demo_seams import (
    DEMO_GROUNDED_ASSIST_JUNK,
    DEMO_GROUNDED_HALLUCINATION_MARKER,
    DEMO_HALLUCINATED_DRAFT,
    DemoGroundedHallucinationGateway,
)
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
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
from ne.contracts.models import CompletionRequest
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import ChatMessage, Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.grounding import GroundingCorpus, check_grounding
from ne.govern.grounding_policy import (
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    GateProfile,
    StrictnessTier,
    apply_strictness,
)
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
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
    parse_sse,
    run,
    text_completion,
)

_GROUNDED_AGENT = "grounded-analyst"
_ORDINARY_AGENT = "console-chat"
_MARKED_PROMPT = f"{DEMO_GROUNDED_HALLUCINATION_MARKER} how many orders does each account have?"

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
}


def _write_templates(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in _TEMPLATES.items():
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
        await mem.upsert_nodes([_account("acc_alpha", ["alpha"])])
        await PropertyGraphLift(mem, store).run()

    run(_seed())
    return store


def _enabled_settings(
    directory: Path,
    *,
    grounded_assist_enabled: bool = False,
) -> OntologySettings:
    _write_templates(directory)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
        grounded_assist_enabled=grounded_assist_enabled,
    )


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


# --- (a) direct wrapper-routing unit tests over a recorded inner gateway -----

_Role = Literal["system", "user", "assistant", "tool"]


def _msg(role: _Role, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _request(messages: tuple[ChatMessage, ...], *, task: str = "agent") -> CompletionRequest:
    return CompletionRequest(messages=messages, task=task)


def test_wrapper_marked_fresh_user_prompt_calls_accounts_tool_no_inner_call() -> None:
    inner = ScriptedGateway([text_completion("must never be reached")])
    gw = DemoGroundedHallucinationGateway(inner)
    req = _request((_msg("system", "sys"), _msg("user", _MARKED_PROMPT)))

    completion = run(gw.complete(req))

    payload = json.loads(completion.text)
    assert payload["tool_call"]["name"] == "read.kg.accounts_directory"
    assert payload["tool_call"]["arguments"] == {}
    assert inner.requests == []


def test_wrapper_marked_accounts_return_yields_hallucinated_draft_no_inner_call() -> None:
    inner = ScriptedGateway([text_completion("must never be reached")])
    gw = DemoGroundedHallucinationGateway(inner)
    req = _request(
        (
            _msg("system", "sys"),
            _msg("user", _MARKED_PROMPT),
            _msg("tool", "read.kg.accounts_directory returned: {}"),
        )
    )

    completion = run(gw.complete(req))

    assert completion.text == DEMO_HALLUCINATED_DRAFT
    assert inner.requests == []


def test_wrapper_marked_grounding_assist_returns_junk_no_inner_call() -> None:
    inner = ScriptedGateway([text_completion("must never be reached")])
    gw = DemoGroundedHallucinationGateway(inner)
    req = _request(
        (_msg("system", "sys"), _msg("user", _MARKED_PROMPT)),
        task="grounding-assist",
    )

    completion = run(gw.complete(req))

    assert completion.text == DEMO_GROUNDED_ASSIST_JUNK
    assert inner.requests == []


@pytest.mark.parametrize("profile", [PROFILE_NG1_FROZEN, PROFILE_NG2], ids=["ng1-frozen", "ng2"])
@pytest.mark.parametrize("tier", list(StrictnessTier))
def test_assist_junk_withheld_at_every_tier_profile(
    tier: StrictnessTier, profile: GateProfile
) -> None:
    """Design A-P1-4 / §5.3: the hardened assist junk is withheld at EVERY
    tier x profile, AND via a ``numeral-ungrounded`` FACT-class finding (424242 can
    never ground) — tier-proof by the ENFORCEMENT matrix, not a vocab accident. A
    numeral-free junk would be ADOPTED at STANDARD (advisory-only findings) and
    break beat 8's refuse-to-hallucinate invariant."""
    verdict = check_grounding(
        DEMO_GROUNDED_ASSIST_JUNK,
        GroundingCorpus.from_answers([]),
        _MARKED_PROMPT,
        profile=profile,
    )
    assert apply_strictness(verdict, tier).withhold is True
    assert any(f.reason == "numeral-ungrounded" for f in verdict.findings)


def test_wrapper_unmarked_requests_of_each_shape_delegate_to_inner() -> None:
    inner = ScriptedGateway([text_completion("a"), text_completion("b"), text_completion("c")])
    gw = DemoGroundedHallucinationGateway(inner)

    fresh = _request((_msg("system", "sys"), _msg("user", "how many open orders?")))
    assert run(gw.complete(fresh)).text == "a"

    tool_return = _request(
        (
            _msg("system", "sys"),
            _msg("user", "how many open orders?"),
            _msg("tool", "read.kg.accounts_directory returned: {}"),
        )
    )
    assert run(gw.complete(tool_return)).text == "b"

    assist = _request(
        (_msg("system", "sys"), _msg("user", "how many open orders?")),
        task="grounding-assist",
    )
    assert run(gw.complete(assist)).text == "c"

    assert len(inner.requests) == 3


def test_wrapper_stream_delegates_to_inner() -> None:
    class _StreamingInner:
        def __init__(self) -> None:
            self.stream_calls: list[CompletionRequest] = []

        async def complete(self, request: CompletionRequest) -> Any:
            raise NotImplementedError

        def stream(self, request: CompletionRequest) -> Any:
            self.stream_calls.append(request)
            return "sentinel-iterator"

    inner = _StreamingInner()
    gw = DemoGroundedHallucinationGateway(inner)
    req = _request((_msg("user", "hi"),))

    result = gw.stream(req)

    assert result == "sentinel-iterator"
    assert inner.stream_calls == [req]


# --- (b)/(c)/(d) composition-root wiring: misconfig + default-off byte-parity -


@dataclass
class _PlatformBuild:
    platform: Platform


def _build_platform(
    tmp_path: Path,
    *,
    key: str,
    gateway: Any,
    ontology_settings: OntologySettings | None = None,
    seed_store: bool = False,
    demo_grounded_hallucination: bool = False,
) -> _PlatformBuild:
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    if seed_store:
        store = _seed_store("a" * 64)
        registry.register("triple_store", "oxigraph", lambda **_kw: store)

    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "department"),
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
        gateway=gateway,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        ontology_settings=ontology_settings,
    )
    platform = build_platform(
        _api_settings(demo_grounded_hallucination=demo_grounded_hallucination),
        seams,
    )
    return _PlatformBuild(platform=platform)


def test_seam_flag_without_grounded_agent_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NE_API_DEMO_GROUNDED_HALLUCINATION"):
        _build_platform(
            tmp_path,
            key="misconfig",
            gateway=ScriptedGateway(),
            ontology_settings=None,
            demo_grounded_hallucination=True,
        )


def test_seam_off_gateway_unwrapped_and_hash_byte_identical(
    tmp_path: Path, engines: list[Any]
) -> None:
    settings = _enabled_settings(tmp_path / "verified")

    gw_default = ScriptedGateway()
    default = _build_platform(
        tmp_path, key="ident", gateway=gw_default, ontology_settings=settings, seed_store=True
    )
    engines.extend(default.platform.engines)

    gw_off = ScriptedGateway()
    explicit_off = _build_platform(
        tmp_path,
        key="ident",
        gateway=gw_off,
        ontology_settings=settings,
        seed_store=True,
        demo_grounded_hallucination=False,
    )
    engines.extend(explicit_off.platform.engines)

    assert default.platform.config_hash == explicit_off.platform.config_hash
    assert default.platform.gateway is gw_default
    assert explicit_off.platform.gateway is gw_off


def test_seam_on_hash_moves(tmp_path: Path, engines: list[Any]) -> None:
    settings = _enabled_settings(tmp_path / "verified")

    off = _build_platform(
        tmp_path,
        key="ident",
        gateway=ScriptedGateway(),
        ontology_settings=settings,
        seed_store=True,
    )
    engines.extend(off.platform.engines)

    on = _build_platform(
        tmp_path,
        key="ident",
        gateway=ScriptedGateway(),
        ontology_settings=settings,
        seed_store=True,
        demo_grounded_hallucination=True,
    )
    engines.extend(on.platform.engines)

    assert off.platform.config_hash != on.platform.config_hash


# --- (e)/(f)/(g) route-level e2e: the REAL gate refuses the seam's fabrication


@dataclass
class _AppBuild:
    app: Any
    verifier: FakeTokenVerifier
    audit_url: str


def _build_app(
    tmp_path: Path,
    *,
    key: str,
    gateway: Any,
    ontology_settings: OntologySettings,
    demo_grounded_hallucination: bool,
) -> _AppBuild:
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    store = _seed_store("a" * 64)
    registry.register("triple_store", "oxigraph", lambda **_kw: store)

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
        gateway=gateway,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        ontology_settings=ontology_settings,
    )
    app = create_app(
        _api_settings(
            chat_agents=(_ORDINARY_AGENT, _GROUNDED_AGENT),
            demo_grounded_hallucination=demo_grounded_hallucination,
        ),
        seams=seams,
    )
    return _AppBuild(app=app, verifier=verifier, audit_url=audit_url)


async def _drive_marked_turn(app: Any, verifier: FakeTokenVerifier) -> dict[str, Any]:
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
        token = verifier.mint("user:a", groups=["alpha"], department="engineering")
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/api/v1/chat/sessions", json={"agent_name": _GROUNDED_AGENT}, headers=headers
        )
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        r = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            json={"prompt": _MARKED_PROMPT},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parsed = parse_sse(r.text)
        done = parsed.of_type("done")
        assert len(done) == 1
        data = done[0].data
        assert data is not None
        return data


def _assert_one_ungrounded_claim_block(audit_url: str) -> None:
    engine = sa.create_engine(audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.payload).where(ne_audit_log.c.action == "guardrail.block")
            ).all()
        payloads = [json.dumps(r.payload) for r in rows]
        assert sum("grounding.ungrounded-claim" in p for p in payloads) == 1
    finally:
        engine.dispose()


def test_seam_on_e2e_refuses_fabrication_live(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path / "verified", grounded_assist_enabled=False)
    inner = ScriptedGateway([text_completion("this text must never be reached")])
    built = _build_app(
        tmp_path,
        key="seam-on",
        gateway=inner,
        ontology_settings=settings,
        demo_grounded_hallucination=True,
    )

    done_data = run(_drive_marked_turn(built.app, built.verifier))

    grounded = done_data["grounded"]
    assert grounded["fallback"] is True
    assert grounded["assist_used"] is False
    output = done_data["output"]
    assert "withheld" in output.lower()
    assert "accounts-directory@v1" in output
    assert "initech" not in output.lower()
    assert "accounts-directory" in {f["template_id"] for f in grounded["fetches"]}
    assert inner.requests == []

    state = get_state(built.app)
    _assert_one_ungrounded_claim_block(built.audit_url)
    for engine in state.engines:
        engine.dispose()


def test_seam_on_e2e_with_assist_enabled_assist_used_false(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path / "verified", grounded_assist_enabled=True)
    inner = ScriptedGateway([text_completion("this text must never be reached")])
    built = _build_app(
        tmp_path,
        key="seam-on-assist",
        gateway=inner,
        ontology_settings=settings,
        demo_grounded_hallucination=True,
    )

    done_data = run(_drive_marked_turn(built.app, built.verifier))

    grounded = done_data["grounded"]
    assert grounded["fallback"] is True
    # The seam's grounding-assist junk fails the re-check too — the assist can
    # only ever IMPROVE the outcome, never redeem a fabrication (H6).
    assert grounded["assist_used"] is False
    output = done_data["output"]
    assert "withheld" in output.lower()
    assert "initech" not in output.lower()
    assert inner.requests == []

    state = get_state(built.app)
    _assert_one_ungrounded_claim_block(built.audit_url)
    for engine in state.engines:
        engine.dispose()


def test_flag_off_marked_prompt_reaches_inner(tmp_path: Path) -> None:
    settings = _enabled_settings(tmp_path / "verified", grounded_assist_enabled=False)
    inner = ScriptedGateway([text_completion("a benign draft with nothing fancy")])
    built = _build_app(
        tmp_path,
        key="seam-off",
        gateway=inner,
        ontology_settings=settings,
        demo_grounded_hallucination=False,
    )

    run(_drive_marked_turn(built.app, built.verifier))

    assert inner.requests, "flag OFF: the marked prompt must reach the inner gateway unmodified"

    for engine in get_state(built.app).engines:
        engine.dispose()
