# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-VAF-U4: THE KEY inline==enqueue verified-provenance parity test.

ne-worker inherits the composition-root ``VerifiedRouter`` via the SAME
``build_platform`` recipe ne-api runs (one recipe, two processes — SPEC
§6.2/Q5); there is NO worker-specific router wiring. This module proves that
the SAME routed prompt, run once through the in-process ``DepartmentAgentSession``
(the inline leg) and once through ``execute_turn_step``/``run_turn_workflow``
(the enqueue leg), yields STRUCTURALLY identical ``AgentRunResult.verified`` /
``TurnFrame.verified`` provenance and BYTE-identical terminal SSE frames.

Self-contained fixture TOML (NOT the real corpus — U5's job); the worker-style
seams mirror ``packages/ne-worker/tests/conftest.py``'s ``sqlite_seams`` pattern
without modifying that module.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.tools import TURN_PRINCIPAL
from ne.api.turn_wire import ProtectedTerminalPayload, ProtectedTurnRef, TurnRequest
from ne.audit.config import AuditSettings
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.factory import create_department_session
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_store import (
    ne_agent_evidence_locators,
    ne_agent_evidence_records,
    ne_agent_evidence_sets,
    ne_agent_pending_source_tool_results,
)
from ne.runtime.store import conversation_request_fingerprint
from ne.worker.relay import RecordingFrameWriter
from ne.worker.runtime_state import (
    reset_worker_state_for_testing,
    set_frame_writer_factory,
    set_worker_platform,
)
from ne.worker.testing import (
    ISSUER,
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)
from ne.worker.workflows import run_turn_workflow

from .conftest import ScriptedGateway, run

pytestmark = pytest.mark.usefixtures("protected_transport_env")

_DEPARTMENT = "engineering"
_AGENT = "console-chat"
_ROUTED_PROMPT = "list every account i am cleared to see"


def _assert_no_source_evidence(platform: Platform) -> None:
    with platform.runtime_engine.connect() as conn:
        for table in (
            ne_agent_evidence_sets,
            ne_agent_evidence_records,
            ne_agent_evidence_locators,
            ne_agent_pending_source_tool_results,
        ):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


# --- fixture template (self-contained; NOT the real corpus) -----------------

_ROUTED_TEMPLATES = {
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

[routing]
domain = "accounts-directory"
triggers = ["list every account i am cleared to see"]
guard_terms = ["accounts directory", "cleared to see"]
clarify_max = 2
""",
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


def _verified_platform(tmp_path: Path, gateway: Any) -> Platform:
    """Build a Platform with verified answers ENABLED + ONE [routing] template."""
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    store = _seed_store("s" * 64)
    registry.register("triple_store", "oxigraph", lambda **_kw: store)

    key = uuid.uuid4().hex
    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"
    templates_dir = _write_templates(tmp_path / "verified", _ROUTED_TEMPLATES)

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-worker"),
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
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
        ontology_settings=OntologySettings(
            verified_answers_enabled=True,
            verified_templates_dir=templates_dir,
            include_ontology_graph=False,
            backend="oxigraph",
        ),
    )
    settings = ApiSettings(
        qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
    )
    return build_platform(settings, seams)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_config_hash_for_testing()
    reset_worker_state_for_testing()


def _principal() -> Principal:
    return Principal(id="user:a", groups=("alpha",), claims={"department": _DEPARTMENT})


async def _bound_request(
    platform: Platform,
    session_id: str,
    *,
    turn_id: str,
    prompt: str,
) -> TurnRequest:
    principal = _principal()
    trace_id = hashlib.sha256(f"test-turn:{turn_id}".encode()).hexdigest()[:32]
    fingerprint = conversation_request_fingerprint(
        session_id,
        principal.id,
        _DEPARTMENT,
        _AGENT,
        prompt,
        None,
        None,
        trace_id,
        principal.groups,
        dict(principal.claims),
    )
    claim = await platform.session_store.claim_conversation_attachment(
        session_id,
        principal.id,
        _DEPARTMENT,
        _AGENT,
        turn_id,
        fingerprint,
        trace_id,
    )
    assert claim.disposition == "owned" and claim.attempt_id is not None
    await platform.session_store.bind_conversation_execution(
        session_id, principal.id, turn_id, claim.attempt_id, fingerprint
    )
    assert platform.agent_run_store is not None
    ref = await platform.agent_run_store.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=principal.id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=conversation_turn_content_binding(
            conversation_id=session_id,
            attachment_id=turn_id,
            attempt_id=claim.attempt_id,
            slot="input",
        ),
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=prompt.encode(),
    )
    return TurnRequest.from_principal(
        turn_id=turn_id,
        session_id=session_id,
        agent_name=_AGENT,
        department=_DEPARTMENT,
        protected_input=ProtectedTurnRef(
            protected_ref=ref.row_id,
            protected_hash=ref.plaintext_hash,
            content_status="source_free",
        ),
        authenticated_context=AuthenticatedRequestContext(
            principal_id=principal.id,
            authorization_groups=("alpha",),
            department=_DEPARTMENT,
            issuer="https://issuer.test",
            expires_at=4_102_444_800,
        ),
        principal=principal,
        trace_id=trace_id,
        request_fingerprint=fingerprint,
        idempotency_fingerprint=fingerprint,
        claimed_attempt_id=claim.attempt_id,
    )


async def _inline_result(platform: Platform, gateway: Any) -> Any:
    principal = _principal()
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
        return session.session_id, await session.run(_ROUTED_PROMPT, principal=principal)
    finally:
        TURN_PRINCIPAL.reset(token)


async def _materialized_terminal(
    platform: Platform, request: TurnRequest, pointer: ProtectedTurnRef
) -> ProtectedTerminalPayload:
    assert platform.agent_run_store is not None
    payload = await platform.agent_run_store.materialize_legacy_conversation_turn(
        owner_principal_id=request.principal_id,
        conversation_id=request.session_id,
        attachment_id=request.turn_id,
        attempt_id=request.claimed_attempt_id,
        slot="terminal",
        protected_ref=pointer.protected_ref,
        plaintext_hash=pointer.protected_hash,
    )
    return ProtectedTerminalPayload.model_validate_json(payload)


def test_enqueue_routed_turn_carries_identical_provenance(tmp_path: Path) -> None:
    gateway = ScriptedGateway()
    platform = _verified_platform(tmp_path, gateway)
    assert platform.verified_router is not None

    # INLINE leg.
    session_id, inline_result = run(_inline_result(platform, gateway))
    assert inline_result.verified is None
    assert inline_result.grounded is None

    # ENQUEUE leg over the SAME platform (one recipe, two processes).
    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    request = run(
        _bound_request(
            platform,
            session_id,
            turn_id="turn-p",
            prompt=_ROUTED_PROMPT,
        )
    )
    run(run_turn_workflow(request.model_dump_json()))

    # Pre-010G legacy attachment turns have no canonical Run context, so the
    # deterministic router refuses before publishing a source Tool frame.
    assert [f.type for f in writer.frames] == ["done"]
    done = writer.frames[-1]
    assert done.output is None
    assert done.protected_terminal is not None
    assert done.verified is None
    terminal = run(_materialized_terminal(platform, request, done.protected_terminal))
    assert terminal.verified is None
    assert terminal.grounded is None
    assert terminal.citations == ()
    assert terminal.output == inline_result.output
    assert gateway.requests == []
    _assert_no_source_evidence(platform)


def test_enqueue_non_domain_prompt_unchanged(tmp_path: Path) -> None:
    gateway = ScriptedGateway()
    platform = _verified_platform(tmp_path, gateway)
    assert platform.verified_router is not None

    session_id, _warmup_result = run(_inline_result(platform, gateway))

    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    request = run(
        _bound_request(
            platform,
            session_id,
            turn_id="turn-q",
            prompt="hello there",
        )
    )
    run(run_turn_workflow(request.model_dump_json()))

    assert [f.type for f in writer.frames] == ["done"]
    done = writer.frames[0]
    assert done.output is None
    assert done.protected_terminal is not None
    assert done.verified is None
    terminal = run(_materialized_terminal(platform, request, done.protected_terminal))
    assert terminal.verified is None
    assert len(gateway.requests) == 1
