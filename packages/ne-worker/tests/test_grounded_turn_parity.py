# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GA-G5: THE KEY inline==enqueue grounded-provenance parity test.

ne-worker inherits the composition-root ``GroundingPolicy`` via the SAME
``build_platform`` recipe ne-api runs (one recipe, two processes — SPEC
§6.2/Q5); there is NO worker-specific grounding wiring. This module proves
that the SAME grounded-analyst turn, run once through the in-process
``DepartmentAgentSession`` (the inline leg) and once through
``execute_turn_step``/``run_turn_workflow`` (the enqueue leg), yields
STRUCTURALLY identical ``AgentRunResult.grounded`` / ``TurnFrame.grounded``
provenance and BYTE-identical terminal SSE frames — AND that an ordinary
agent's enqueue-leg done frame carries no ``grounded`` key at all (the
worker-side name gate, mirroring ``routers/chat.py:_open`` exactly).

Self-contained fixture TOML (NOT the real corpus); the worker-style seams
mirror ``test_verified_turn_parity.py``'s harness (NOT imported from it, per
that module's own self-contained convention).
"""

from __future__ import annotations

import hashlib
import json
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
from ne.contracts.models import Completion, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_trace_scope
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

from .conftest import ScriptedGateway, run, text_completion

pytestmark = pytest.mark.usefixtures("protected_transport_env")

_DEPARTMENT = "engineering"
_GROUNDED_AGENT = "grounded-analyst"
_ORDINARY_AGENT = "console-chat"
_PROMPT = "how many open orders does Initech have"


def _assert_no_source_evidence(platform: Platform) -> None:
    with platform.runtime_engine.connect() as conn:
        for table in (
            ne_agent_evidence_sets,
            ne_agent_evidence_records,
            ne_agent_evidence_locators,
            ne_agent_pending_source_tool_results,
        ):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


# --- fixture template (self-contained; NOT the real corpus; NO [routing] so
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
}

#: The SAME template PLUS a ``[routing]`` block, so ``platform.verified_router``
#: is actually built (non-None) — used ONLY by the Codex-P1 regression test
#: below (the enqueue leg must never be short-circuited by the router either).
_ROUTED_TEMPLATES = {
    "accounts_directory.toml": _TEMPLATES["accounts_directory.toml"]
    + """
[routing]
domain = "accounts-directory"
triggers = ["list every account i am cleared to see"]
guard_terms = ["accounts directory", "cleared to see"]
clarify_max = 2
""",
}
_ROUTED_PROMPT = "list every account i am cleared to see"


def _tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


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


def _grounded_platform(
    tmp_path: Path, gateway: Any, *, templates: dict[str, str] = _TEMPLATES
) -> Platform:
    """Build a Platform with verified answers + the grounded agent ENABLED
    (assist off for determinism). ``templates`` defaults to the routing-free
    fixture (the U3 pre-step never fires); pass ``_ROUTED_TEMPLATES`` for the
    Codex-P1 regression test, which needs a real, non-None ``verified_router``."""
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    store = _seed_store("s" * 64)
    registry.register("triple_store", "oxigraph", lambda **_kw: store)

    key = uuid.uuid4().hex
    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"
    templates_dir = _write_templates(tmp_path / "verified", templates)

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
            grounded_agent_enabled=True,
            grounded_assist_enabled=False,
        ),
    )
    settings = ApiSettings(
        qdrant_url="http://qdrant.test",
        qdrant_collection="t",
        tei_url="http://tei.test",
        chat_agents=(_ORDINARY_AGENT, _GROUNDED_AGENT),
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
    agent_name: str,
    prompt: str,
    trace_id: str | None = None,
) -> TurnRequest:
    principal = _principal()
    exact_trace = trace_id or hashlib.sha256(f"test-turn:{turn_id}".encode()).hexdigest()[:32]
    fingerprint = conversation_request_fingerprint(
        session_id,
        principal.id,
        _DEPARTMENT,
        agent_name,
        prompt,
        None,
        None,
        exact_trace,
        principal.groups,
        dict(principal.claims),
    )
    claim = await platform.session_store.claim_conversation_attachment(
        session_id,
        principal.id,
        _DEPARTMENT,
        agent_name,
        turn_id,
        fingerprint,
        exact_trace,
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
        agent_name=agent_name,
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
        trace_id=exact_trace,
        request_fingerprint=fingerprint,
        idempotency_fingerprint=fingerprint,
        claimed_attempt_id=claim.attempt_id,
    )


async def _inline_result(platform: Platform, gateway: Any, trace_id: str) -> Any:
    principal = _principal()
    session = await create_department_session(
        _GROUNDED_AGENT,
        _DEPARTMENT,
        owner_principal_id=principal.id,
        store=platform.session_store,
        gateway=gateway,
        executor=platform.executor,
        settings=platform.runtime_settings,
        # NE-GA-G5 (Codex P1 fix): mirror the FIXED routers/chat.py:_open /
        # workflows.py:execute_turn_step wiring — a grounded-analyst session
        # never receives the verified_router pre-step, since a ROUTE/CLARIFY/
        # REFUSE outcome would short-circuit before grounding ever runs.
        router=None,
        grounding=platform.grounding,
    )
    token = TURN_PRINCIPAL.set(principal)
    try:
        with run_trace_scope(trace_id):
            return session.session_id, await session.run(_PROMPT, principal=principal)
    finally:
        TURN_PRINCIPAL.reset(token)


def test_enqueue_grounded_turn_carries_identical_provenance(tmp_path: Path) -> None:
    enqueue_trace_id = "0123456789abcdef0123456789abcdef"
    # 4 scripted completions total: 2 per leg (a tool call, then a final draft).
    gateway = ScriptedGateway(
        [
            _tool_call_completion("read.kg.accounts_directory", {}),
            text_completion("Initech Global has 73 open orders"),
            _tool_call_completion("read.kg.accounts_directory", {}),
            text_completion("Initech Global has 73 open orders"),
        ]
    )
    platform = _grounded_platform(tmp_path, gateway)
    assert platform.grounding is not None
    assert platform.grounded_agent_name == _GROUNDED_AGENT

    # INLINE leg (consumes the first 2 scripted completions).
    session_id, inline_result = run(_inline_result(platform, gateway, enqueue_trace_id))
    assert inline_result.grounded is not None
    assert inline_result.grounded.fallback is True
    assert inline_result.grounded.fetches == ()
    assert inline_result.verified is None

    # ENQUEUE leg over the SAME platform (one recipe, two processes; consumes
    # the second 2 scripted completions).
    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    request = run(
        _bound_request(
            platform,
            session_id,
            turn_id="turn-g",
            agent_name=_GROUNDED_AGENT,
            prompt=_PROMPT,
            trace_id=enqueue_trace_id,
        )
    )
    run(run_turn_workflow(request.model_dump_json()))
    done = writer.frames[-1]
    assert done.type == "done"
    assert done.protected_terminal is not None
    terminal = run(_materialized_terminal(platform, request, done.protected_terminal))
    assert terminal.grounded == inline_result.grounded
    assert terminal.verified is None
    assert terminal.citations == ()
    assert terminal.output == inline_result.output
    assert len(gateway.requests) == 4
    _assert_no_source_evidence(platform)


def test_enqueue_ordinary_agent_done_frame_has_no_grounded_key(tmp_path: Path) -> None:
    gateway = ScriptedGateway([text_completion("plain console answer")])
    platform = _grounded_platform(tmp_path, gateway)
    assert platform.grounding is not None

    # Open (and discard) an ordinary session first so a session_id exists to
    # re-derive inside execute_turn_step (get-or-create — matches U4 precedent).
    session_id = run(_ordinary_session_id(platform))

    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    request = run(
        _bound_request(
            platform,
            session_id,
            turn_id="turn-o",
            agent_name=_ORDINARY_AGENT,
            prompt="hello there",
            trace_id="fedcba9876543210fedcba9876543210",
        )
    )
    run(run_turn_workflow(request.model_dump_json()))

    assert [f.type for f in writer.frames] == ["done"]
    done = writer.frames[0]
    assert done.output is None
    assert done.protected_terminal is not None
    assert done.grounded is None
    assert done.trace_id == "fedcba9876543210fedcba9876543210"
    assert run(_materialized_terminal(platform, request, done.protected_terminal)).grounded is None


async def _ordinary_session_id(platform: Platform) -> str:
    session = await create_department_session(
        _ORDINARY_AGENT,
        _DEPARTMENT,
        owner_principal_id="user:a",
        store=platform.session_store,
        gateway=platform.gateway,
        executor=platform.executor,
        settings=platform.runtime_settings,
        router=platform.verified_router,
        grounding=None,
    )
    return session.session_id


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


async def _grounded_session_id(platform: Platform) -> str:
    """Open (without running) a grounded-analyst session to obtain the
    deterministic get-or-create ``session_id`` ``execute_turn_step`` re-derives
    — mirrors ``_ordinary_session_id`` above. Opening never touches the
    gateway (only ``.run`` does)."""
    session = await create_department_session(
        _GROUNDED_AGENT,
        _DEPARTMENT,
        owner_principal_id="user:a",
        store=platform.session_store,
        gateway=platform.gateway,
        executor=platform.executor,
        settings=platform.runtime_settings,
        router=None,
        grounding=platform.grounding,
    )
    return session.session_id


def test_enqueue_grounded_turn_not_short_circuited_by_verified_router(tmp_path: Path) -> None:
    """Codex P1 regression, enqueue leg: ``execute_turn_step`` used to hand the
    grounded-analyst the SAME ``platform.verified_router`` as every other
    agent. A routing-eligible prompt would then return a ``RoutedTurn`` before
    the model — and therefore before the grounding gate — ever ran, silently
    dropping ``grounded`` provenance from the durable done frame. Fixed: the
    grounded session never receives the router; an ordinary agent on the SAME
    routed platform is still short-circuited deterministically.
    """
    # Only the grounded leg reaches the model; the ordinary leg is routed
    # deterministically and never touches the gateway.
    gateway = ScriptedGateway([text_completion("Initech Global has 73 open orders")])
    platform = _grounded_platform(tmp_path, gateway, templates=_ROUTED_TEMPLATES)
    assert platform.verified_router is not None, "the routed fixture must build a live router"
    assert platform.grounding is not None

    set_worker_platform(platform)
    # Ordinary protected execution preserves the same verified router.
    ordinary_session_id = run(_ordinary_session_id(platform))
    ordinary_writer = RecordingFrameWriter("ordinary")
    set_frame_writer_factory(lambda _turn_id: ordinary_writer)
    ordinary_request = run(
        _bound_request(
            platform,
            ordinary_session_id,
            turn_id="turn-routed-ordinary",
            agent_name=_ORDINARY_AGENT,
            prompt=_ROUTED_PROMPT,
        )
    )
    run(run_turn_workflow(ordinary_request.model_dump_json()))
    # NE-TURN-PROGRESS: the routed leg's verified read streams as a `tool`
    # frame first, so the terminal done is addressed by position-from-the-end.
    ordinary_done = ordinary_writer.frames[-1]
    assert ordinary_done.type == "done"
    assert ordinary_done.verified is None
    assert ordinary_done.grounded is None
    assert ordinary_done.output is None
    assert ordinary_done.protected_terminal is not None
    ordinary_terminal = run(
        _materialized_terminal(platform, ordinary_request, ordinary_done.protected_terminal)
    )
    assert ordinary_terminal.verified is None
    assert ordinary_terminal.grounded is None
    assert ordinary_terminal.citations == ()

    # Grounded-analyst: the SAME trigger prompt must NOT be routed away from
    # the model — the grounding gate must run and stamp 'grounded'.
    grounded_writer = RecordingFrameWriter("grounded")
    set_frame_writer_factory(lambda _turn_id: grounded_writer)
    grounded_session_id = run(_grounded_session_id(platform))
    grounded_request = run(
        _bound_request(
            platform,
            grounded_session_id,
            turn_id="turn-routed-grounded",
            agent_name=_GROUNDED_AGENT,
            prompt=_ROUTED_PROMPT,
        )
    )
    run(run_turn_workflow(grounded_request.model_dump_json()))
    grounded_done = grounded_writer.frames[-1]
    assert grounded_done.type == "done"
    assert grounded_done.protected_terminal is not None
    grounded_terminal = run(
        _materialized_terminal(platform, grounded_request, grounded_done.protected_terminal)
    )
    assert grounded_terminal.verified is None
    assert grounded_terminal.grounded is not None
    assert len(gateway.requests) == 1
    _assert_no_source_evidence(platform)
