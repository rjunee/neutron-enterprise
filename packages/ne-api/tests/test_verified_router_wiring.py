# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-VAF-U4: the composition-root VerifiedRouter wiring, inline SSE surface.

Proves default-off byte-identity (no ontology config, OR verified enabled but
zero ``[routing]`` templates ⇒ ``platform.verified_router`` stays ``None`` and
the terminal ``done`` frame is byte-for-byte unchanged) AND the routed happy
path (a fixture ``[routing]`` template routes deterministically, the model is
NEVER consulted, and the done frame carries structural ``verified``
provenance). The real corpus ``[routing]`` entry is U5's job — this module
uses only a tmp-TOML fixture and never touches ``tools/ne-cli``.

Self-contained harness copied from ``test_verified_answer_tools.py`` (NOT
imported from it — only ``conftest`` seams are shared) so this module's
fixtures stay independent of that unit's TOML content.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa

from ne.api.citation_capture import citation_capture_scope
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.sse import turn_event_stream
from ne.api.tools import TURN_PRINCIPAL
from ne.audit.config import AuditSettings
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
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_trace_scope
from ne.runtime.factory import create_department_session, rehydrate_department_session
from ne.runtime.store import ne_agent_conversation_turns

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    run,
    text_completion,
)

_DEPARTMENT = "engineering"
_AGENT = "console-chat"

# --- fixture templates (self-contained; NOT the real corpus) ----------------

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

#: The SAME accounts_directory template PLUS a ``[routing]`` block (this
#: unit's fixture only — the real corpus [routing] entry is U5's job).
#: ``entity_by_iri.toml`` stays WITHOUT ``[routing]`` (it carries args, which
#: would fail U1's zero-arg load gate).
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


AppBuilder = Callable[..., PlatformHarness]


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[AppBuilder]:
    engines: list[sa.Engine] = []

    def _make(
        *,
        ontology_settings: OntologySettings | None = None,
        seed_store: bool = False,
        gateway: Any | None = None,
    ) -> PlatformHarness:
        reset_config_hash_for_testing()
        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
        if seed_store:
            store = _seed_store("s" * 64)
            registry.register("triple_store", "oxigraph", lambda **_kw: store)

        key = uuid.uuid4().hex
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
    tmp_path: Path, templates: dict[str, str], subdir: str = "verified"
) -> OntologySettings:
    directory = _write_templates(tmp_path / subdir, templates)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
    )


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


async def _run_turn(platform: Platform, gateway: Any, prompt: str) -> tuple[str, list[bytes]]:
    """Open a session (through the composition-root router) and drive one turn."""
    principal = Principal(id="user:a", groups=("alpha",), claims={"department": _DEPARTMENT})
    session = await create_department_session(
        _AGENT,
        _DEPARTMENT,
        owner_principal_id=principal.id,
        store=platform.session_store,
        gateway=gateway,
        executor=platform.executor,
        settings=platform.runtime_settings,
        router=platform.verified_router,
    )
    token = TURN_PRINCIPAL.set(principal)
    try:
        frames = [
            f
            async for f in turn_event_stream(
                session.run(prompt, principal=principal), keepalive_s=30.0, timeout_s=30.0
            )
        ]
    finally:
        TURN_PRINCIPAL.reset(token)
    return session.session_id, frames


# --- (1)/(2) default-off: router is None ------------------------------------


def test_default_off_verified_router_is_none(make_platform: AppBuilder) -> None:
    h = make_platform()
    assert h.platform.verified_router is None


def test_platform_composes_one_canonical_reader_evaluator_and_protected_guard(
    make_platform: AppBuilder,
) -> None:
    platform = make_platform().platform
    assert platform.source_authority_reader is not None
    assert platform.source_authority_evaluator is not None
    assert platform.source_authority_evaluator._authority is platform.source_authority_reader
    assert cast(Any, platform.current_retriever)._evaluator is platform.source_authority_evaluator
    assert platform.source_guard is not None
    assert platform.gate._source_guard is platform.source_guard
    assert platform.current_document_authorizer is not None
    assert not hasattr(platform, "source_authority_writer")


def test_no_routing_templates_router_is_none(make_platform: AppBuilder, tmp_path: Path) -> None:
    h = make_platform(ontology_settings=_enabled_settings(tmp_path, _TEMPLATES), seed_store=True)
    assert h.platform.verified_router is None


# --- (3) the load-bearing byte-identity pin ---------------------------------


def test_default_off_done_frame_byte_identical(make_platform: AppBuilder) -> None:
    gateway = ScriptedGateway([text_completion("the answer")])
    h = make_platform(gateway=gateway)
    assert h.platform.verified_router is None

    sid, frames = run(_run_turn(h.platform, gateway, "hi"))
    assert (
        frames[-1]
        == (
            f'event: done\ndata: {{"type":"done","session_id":"{sid}","output":"the answer"}}\n\n'
        ).encode()
    )


# --- (4) the routed happy path -----------------------------------------------


def test_routed_inline_turn_emits_structural_verified(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    gateway = ScriptedGateway()
    h = make_platform(
        ontology_settings=_enabled_settings(tmp_path, _ROUTED_TEMPLATES),
        seed_store=True,
        gateway=gateway,
    )
    assert h.platform.verified_router is not None
    assert h.platform.verified_router._source_guard is h.platform.source_guard

    _sid, frames = run(_run_turn(h.platform, gateway, "list every account i am cleared to see"))
    parsed = _parse_frames(frames)
    events = [event for event, _ in parsed]
    assert events == ["token", "done"]
    token_delta = dict(parsed)["token"]["delta"]  # type: ignore[index]
    done_data = dict(parsed)["done"]
    assert done_data is not None
    verified = done_data["verified"]
    assert verified["template_id"] == "accounts-directory"
    assert verified["config_hash"] == h.platform.config_hash
    assert verified["row_count"] == 2  # acc_alpha + acc_both under groups=('alpha',)
    assert done_data["output"] == token_delta  # deterministic render, no model text
    assert gateway.requests == []  # the model was NEVER consulted


def test_routed_verified_idempotent_replay_preserves_exact_terminal_envelope(
    make_platform: AppBuilder, tmp_path: Path
) -> None:
    gateway = ScriptedGateway()
    h = make_platform(
        ontology_settings=_enabled_settings(tmp_path, _ROUTED_TEMPLATES),
        seed_store=True,
        gateway=gateway,
    )
    principal = Principal(id="user:a", groups=("alpha",), claims={"department": _DEPARTMENT})

    async def _drive() -> None:
        session = await create_department_session(
            _AGENT,
            _DEPARTMENT,
            owner_principal_id=principal.id,
            store=h.platform.session_store,
            gateway=gateway,
            executor=h.platform.executor,
            settings=h.platform.runtime_settings,
            router=h.platform.verified_router,
        )

        async def frames_for(current) -> list[bytes]:  # type: ignore[no-untyped-def]
            with citation_capture_scope() as citations:

                async def run_turn():  # type: ignore[no-untyped-def]
                    result = await current.run(
                        "list every account i am cleared to see",
                        principal=principal,
                        attachment_id="verified-idempotency",
                    )
                    citations.restore(current.take_replay_citations("verified-idempotency"))
                    return result

                async def publish() -> None:
                    await current.publish_turn(
                        "verified-idempotency", citations=citations.snapshot()
                    )

                with run_trace_scope("0123456789abcdef0123456789abcdef"):
                    frames = [
                        frame
                        async for frame in turn_event_stream(
                            run_turn(),
                            keepalive_s=30.0,
                            timeout_s=30.0,
                            message_id="verified-idempotency",
                            before_done=publish,
                        )
                    ]
                current.finalize_turn("verified-idempotency")
                return frames

        token = TURN_PRINCIPAL.set(principal)
        try:
            first = await frames_for(session)
            restarted = await rehydrate_department_session(
                session.session_id,
                _AGENT,
                _DEPARTMENT,
                owner_principal_id=principal.id,
                store=h.platform.session_store,
                gateway=gateway,
                executor=h.platform.executor,
                settings=h.platform.runtime_settings,
                router=h.platform.verified_router,
            )
            assert restarted is not None
            replay = await frames_for(restarted)
        finally:
            TURN_PRINCIPAL.reset(token)
        assert replay == first
        parsed = _parse_frames(replay)
        assert dict(parsed)["done"]["verified"]["template_id"] == "accounts-directory"  # type: ignore[index]
        assert gateway.requests == []
        with h.platform.session_store.engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_conversation_turns.c.publication_state).where(
                    ne_agent_conversation_turns.c.conversation_id == session.session_id
                )
            ).all()
        assert rows == [("published",)]

    run(_drive())


# --- (5) non-domain prompt still takes the model path ------------------------


def test_non_domain_prompt_takes_model_path(make_platform: AppBuilder, tmp_path: Path) -> None:
    gateway = ScriptedGateway([text_completion("I don't know that.")])
    h = make_platform(
        ontology_settings=_enabled_settings(tmp_path, _ROUTED_TEMPLATES),
        seed_store=True,
        gateway=gateway,
    )
    assert h.platform.verified_router is not None

    _sid, frames = run(_run_turn(h.platform, gateway, "what is the weather like today"))
    parsed = _parse_frames(frames)
    done_data = dict(parsed)["done"]
    assert done_data is not None
    assert "verified" not in done_data
    assert len(gateway.requests) == 1
