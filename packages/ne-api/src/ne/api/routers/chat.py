# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chat routes (C6 surface, spec §1.2) — session open + the turn-then-stream SSE.

``department`` is NEVER client-supplied: it is derived from the verified bearer's
``principal.claims['department']`` (propagated via the AMENDMENT A1 configurable
allowlist) and re-enforced fail-closed inside ``DepartmentAgentSession.run``
(agent.py:103-108, defense in depth). ``agent_name`` is validated against the
``chat_agents`` allowlist. The turn runs over THE process-wide governed executor
(C6) constructed in the composition root.

R3 conversation lookup resolves one exact principal-owned id and rechecks its
owner, department, and configured agent; a foreign id can never match (no
existence leak) — a miss is a 404.

NE-103c — TWO execution modes, byte-identical to the browser (design §2/§4):

* ``inline`` (default): NE-104a's in-process turn multiplexed with keep-alives.
* ``enqueue``: mint ``turn_id`` server-side, build the ``TurnRequest`` EXCLUSIVELY
  from the verified-bearer principal + the matched session plus the server-authorized
  model alias/lane (never trust a client lane), enqueue the durable ``chat.turn``
  workflow, emit a redacted
  ``api.chat.turn_enqueued`` surface record (ids only — spec §4.8), then relay the
  worker's frame stream through ne-api's SOLE SSE encoder.

ALL pre-stream validation (department fail-closed, R3 session match, 404) is
IDENTICAL across both modes and runs BEFORE any enqueue.

Idempotency-Key (Finding B, NE-INV-CHAT-IDEMPOTENCY): an OPTIONAL client request header mirroring
``GovernedMcpServer``'s ``TURN_META_KEY`` escape hatch (NE-SCOPE-MCP/U2,
``packages/ne-mcp/src/ne/mcp/server.py``). A transport-level retry (timeout, proxy
retry, double-click) of the SAME logical turn can otherwise mint a second, unrelated
``turn_id`` -> a second ``action_scope`` -> a governed write is not deduplicated
across the two turns (NE-050's dedup key is derived from the ambient scope).

When the header value matches ``_IDEMPOTENCY_KEY_RE`` (the exact charset/length
shape ``TURN_META_KEY`` validates, C12 — opaque, never task text), ``turn_id`` is
derived deterministically via :func:`_mint_turn_id`: conversation- AND
principal-prefixed, so a client-chosen token cannot collide across either ownership
dimension. Absent or
invalid, ``turn_id`` falls back BYTE-IDENTICALLY to today's ``f"turn-{uuid.uuid4()}"``
(fail-safe, never a 4xx — this is convenience, not a security boundary, matching the
MCP precedent's choice). Applies IDENTICALLY to both chat modes (one mint point in
``send_message`` feeds both). Pinned by ``tests/test_chat_action_scope.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from starlette.responses import Response, StreamingResponse

from ne.api.authz import emit_surface_audit, require_role
from ne.api.citation_capture import citation_capture_scope
from ne.api.conversation_ledger import (
    MAX_CONVERSATION_TURNS,
    AvailableProtectedConversationTurnView,
    ConversationTurnTooLargeError,
    LegacyUnavailableConversationTurnView,
    ProtectedBindingView,
    ProtectedConversationTurnView,
    WithheldProtectedConversationTurnView,
    build_conversation_ledger,
    build_protected_conversation_ledger,
    encoded_ledger,
    encoded_protected_ledger,
)
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.model_selection import AuthorizedModelSelection
from ne.api.routers.models import authorize_model_choice
from ne.api.run_credential_floor import (
    RUN_CREDENTIAL_FLOOR_BOUND_SECONDS,
    credential_too_short_detail,
)
from ne.api.sse import relay_event_stream, turn_event_stream
from ne.api.tool_transit import TURN_TOOL_TRANSITS, TurnTransitChannel
from ne.api.tools import TURN_PRINCIPAL
from ne.api.turn_wire import (
    PROTECTED_TRANSPORT_GENERATION,
    LegacyTurnRequest,
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnFrame,
    TurnRequest,
    citations_frame,
    done_frame,
    token_frame,
)
from ne.api.wire import (
    ChatCapabilitiesView,
    ChatGroundingModeView,
    ChatMessageRequest,
    ChatSessionListView,
    ChatSessionMetadataView,
    ChatSessionView,
    CreateSessionRequest,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import AgentRunResult
from ne.contracts.types import Principal
from ne.runtime import action_scope
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.correlation import run_trace_scope
from ne.runtime.factory import create_department_session, rehydrate_department_session
from ne.runtime.model_continuity import CONTINUITY_PROMPT_RESERVE_TOKENS
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_store import ProtectedTurnUnavailable
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    require_authenticated_context,
)
from ne.runtime.store import (
    AttachmentRequestMismatchError,
    PrincipalConversation,
    ProtectedConversationTurn,
    PublishedConversationReplay,
    conversation_request_fingerprint,
)
from ne.schema import AuditOutcome

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_logger = logging.getLogger(__name__)

#: Opaque, charset-bounded client idempotency tokens ONLY — never model- or
#: task-derived text (C12 PII discipline). Byte-identical shape to ne-mcp's
#: ``_TURN_TOKEN_RE`` (``packages/ne-mcp/src/ne/mcp/server.py``, NE-SCOPE-MCP/U2),
#: the precedent this header mirrors for chat's HTTP transport.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
_CONVERSATION_RESOURCE = "chat:conversation"
_DEFAULT_ASK_ADAPTER_NAME = "console-chat"
_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
_IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def _netstring_encode(*parts: str) -> bytes:
    """Length-prefix each part before concatenation so the byte string fed to the
    hash is an UNAMBIGUOUS encoding of the tuple (Codex C1 fix).

    A plain ``":"``-joined ``f"{a}:{b}:{c}"`` is ambiguous whenever a component may
    itself contain ``":"`` — and ``principal_id`` legitimately does (e.g.
    ``"user:ada"``, per ``Principal``'s id shape used throughout this codebase).
    That ambiguity lets two DISTINCT ``(session_id, principal_id, idempotency_key)``
    triples collide on the same joined string and therefore the same digest — e.g.
    ``principal_id="user:ada", idempotency_key="retry"`` collides with
    ``principal_id="user", idempotency_key="ada:retry"`` under naive joining. A
    netstring-style ``len(part):part`` prefix per component makes the encoding
    injective: the length prefix is decoded before the delimiter is ever
    interpreted, so no choice of ``":"``-containing (or any-content) part can shift
    a byte from one field into another.
    """
    encoded = bytearray()
    for part in parts:
        raw = part.encode()
        encoded += f"{len(raw)}:".encode()
        encoded += raw
    return bytes(encoded)


def _mint_turn_id(session_id: str, principal_id: str, idempotency_key: str | None) -> str:
    """Mint the per-turn id: client-derived when a valid ``Idempotency-Key`` is
    supplied, else a fresh ``uuid4`` (byte-identical to pre-NE-INV-CHAT-IDEMPOTENCY
    behavior).

    Validation is FAIL-SAFE, not fail-closed (deliberate for THIS check only — it is
    a convenience escape hatch, not a security boundary, matching the MCP
    ``TURN_META_KEY`` precedent's choice, NE-SCOPE-MCP/U2): an absent or invalid
    header NEVER rejects the request, it just falls through to the uuid4 default. An
    invalid value is logged by LENGTH ONLY — the raw client-controlled value is NEVER
    logged (C12).

    The derived id is session- AND principal-prefixed (``sha256`` over a
    length-prefixed, UNAMBIGUOUS encoding of ``(session_id, principal_id, token)`` —
    see :func:`_netstring_encode`): conversations are principal-owned, and the
    redundant principal component preserves that ownership boundary even if a
    future storage adapter changes the id namespace. This
    mirrors ne-mcp's per-CONNECTION scope prefix (a connection binds one principal) —
    NE-SCOPE-MCP/U2's collision-prevention reasoning, carried over verbatim.

    The derived shape (``turn-`` + 32 hex chars) and the ``uuid4`` shape (``turn-`` +
    dashed hex) are disjoint namespaces — the two mint paths can never collide.

    Findings ref: invariant-audit Finding B.
    Pinned by: packages/ne-api/tests/test_chat_action_scope.py.
    """
    if idempotency_key is not None:
        if _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            digest = hashlib.sha256(
                _netstring_encode(session_id, principal_id, idempotency_key)
            ).hexdigest()
            return f"turn-{digest[:32]}"
        _logger.warning(
            "ignoring invalid Idempotency-Key header (len=%s); minting a fresh turn id",
            len(idempotency_key),
        )
    return f"turn-{uuid.uuid4()}"


def _trace_id_for_turn(turn_id: str) -> str:
    """Derive the opaque 32-hex run trace from the server-owned turn identity.

    The one-way, domain-separated derivation keeps an Idempotency-Key retry on
    the same selection/execution trace in BOTH inline and enqueue modes without
    accepting a client-supplied trace id. A fresh turn id still yields a fresh
    trace, and no prompt/model text enters the derivation.
    """
    return hashlib.sha256(_netstring_encode("bd15-chat-trace", turn_id)).hexdigest()[:32]


def _grounded_surface_is_executable(state: ApiState) -> bool:
    """True only while the grounded agent's ONE execution path can actually run.

    The grounded analyst executes on the legacy session path
    (``DepartmentAgentSession.run`` -> ``ne.runtime.grounding.run_grounding_gate``):
    the recorder-bound verified-read toolset, the whole-answer NG gate, the
    withheld-notice fallback, and the ``GroundedProvenance`` on the ``done`` frame
    all live there. The canonical Run path composes no ``GroundingPolicy`` at all
    (``BoundedAgenticAdapter`` -> ``run_model_planned_agent`` takes no policy), and
    there is no grounded Agent in the catalog and no grounded ``ExecutionStrategy``.

    PRE-010G retired the legacy Conversation attachment writer *in the database* —
    ``ne_block_legacy_turn_when_default_ask_active`` / the SQLite twin, installed by
    ``ne.runtime.run_store._install_default_ask_fence``. That trigger is a BEFORE
    INSERT with no exemptions: once canonical default Ask is activated, the legacy
    path cannot complete a single turn, so a grounded submission dies on the trigger
    with a 500 rather than running the gate.

    NE-GA-G8 read that retirement as an API-level policy the grounded name could be
    exempted from, and routed grounded turns back onto the legacy writer. It is not a
    policy, and the trigger predates NE-GA-G8. So the honest boundary is the
    trigger's own: **the grounded surface exists exactly while the legacy writer
    does**. Under canonical activation it is advertised to nobody and admitted from
    nobody — an unavailable capability, never a downgraded one and never a 500.

    Deliberately NOT done here: aliasing the grounded name onto canonical Ask. That
    would serve an ungrounded answer under a control labelled "Grounding on", which
    is the one outcome worse than the capability being absent. Restoring the toggle
    under canonical Ask means porting ``GroundingPolicy`` onto the Run execution
    path; that is a migration, not a routing decision this function can make.

    Every term is deployment configuration composed at the root, never request data.
    """
    grounded_name = state.grounded_agent_name
    return (
        state.default_ask_submission is None
        and state.grounding is not None
        and grounded_name is not None
        and grounded_name in state.settings.chat_agents
    )


def _require_department(principal: Principal) -> str:
    department = principal.claims.get("department") or None
    if not department:
        # Fail closed: a principal with no department claim cannot chat (422).
        raise HTTPException(status_code=422, detail="principal has no department claim")
    return department


async def _open(
    agent_name: str, department: str, owner_principal_id: str, state: ApiState
) -> DepartmentAgentSession:
    # NE-GA-G5 (Codex P1 fix): a grounded-analyst turn must ALWAYS reach the
    # grounding post-step. The NE-VAF-U4 verified_router is a PRE-model
    # short-circuit (agent.py:158-182) — on a ROUTE/CLARIFY/REFUSE outcome it
    # returns before the model (and therefore before grounding) ever runs, so
    # handing the grounded-analyst session a router would silently drop
    # GroundedProvenance for exactly the turns the router matches. The grounded
    # agent's tools are the verified reads anyway (same registry), so nothing
    # is lost by not pre-routing it — every other agent_name is unaffected.
    is_grounded_agent = agent_name == state.grounded_agent_name
    return await create_department_session(
        agent_name,
        department,
        owner_principal_id=owner_principal_id,
        store=state.session_store,
        gateway=state.gateway,
        executor=state.executor,
        settings=state.runtime_settings,
        router=None if is_grounded_agent else state.verified_router,
        # NE-GA-G5: only the ONE configured grounded-agent name gets the
        # grounding post-step + verified-reads-only toolset restriction;
        # every other agent_name is byte-identically unaffected.
        grounding=state.grounding if is_grounded_agent else None,
        # D-2 (NE-SKILL-1b): the platform sink durably records the session.run
        # terminal event so the Skill Forge can fold interactive sessions.
        sink=state.sink,
        source_guard=state.source_guard,
    )


async def resolve_session(
    session_id: str, principal_id: str, department: str, state: ApiState
) -> DepartmentAgentSession | None:
    """Resolve only an exact owner/id/department row and configured agent."""
    conversation = await state.session_store.get_conversation(session_id, principal_id, department)
    if conversation is None or conversation.agent_name not in state.settings.chat_agents:
        return None
    is_grounded_agent = conversation.agent_name == state.grounded_agent_name
    return await rehydrate_department_session(
        session_id,
        conversation.agent_name,
        department,
        owner_principal_id=principal_id,
        store=state.session_store,
        gateway=state.gateway,
        executor=state.executor,
        settings=state.runtime_settings,
        router=None if is_grounded_agent else state.verified_router,
        grounding=state.grounding if is_grounded_agent else None,
        sink=state.sink,
        source_guard=state.source_guard,
    )


@router.post("/v1/chat/sessions")
async def create_session(
    body: CreateSessionRequest,
    principal: _Principal,
    state: _State,
) -> ChatSessionView:
    if body.agent_name not in state.settings.chat_agents:
        raise HTTPException(status_code=422, detail="unknown agent_name")
    if (
        getattr(state.settings, "default_ask_generation", "")
        and state.default_ask_submission is None
    ):
        # Configured-but-cold is not the legacy mode: the first governed sync
        # must establish source authority before canonical Ask may write.
        raise HTTPException(status_code=503, detail="canonical default Ask is bootstrapping")
    department = _require_department(principal)
    if state.default_ask_submission is not None:
        # PRE-010G retires the legacy adapters as writers.  The ordinary Ask
        # adapter now mints the Conversation under the exact canonical Agent id
        # that admission will freeze into its Run; an alternate legacy adapter
        # may not silently reopen the old path after activation.  The grounded
        # analyst is no exception: its only execution path is the legacy session,
        # and the database fence has already retired that writer, so admitting it
        # here would mint a Conversation whose every turn 500s on the trigger.
        # ``/v1/chat/capabilities`` stops advertising the mode in the same state,
        # so the console never offers a request that lands on this 422.
        if body.agent_name != _DEFAULT_ASK_ADAPTER_NAME:
            raise HTTPException(status_code=422, detail="unknown agent_name")
        canonical_agent_id = state.default_ask_submission.activation.default_agent_id
        conversation = await state.session_store.create_conversation(
            canonical_agent_id,
            department,
            principal.id,
        )
        return ChatSessionView(
            session_id=conversation.conversation_id,
            agent_name=canonical_agent_id,
        )
    session = await _open(body.agent_name, department, principal.id, state)
    return ChatSessionView(session_id=session.session_id, agent_name=body.agent_name)


def _metadata_view(conversation: PrincipalConversation) -> ChatSessionMetadataView:
    return ChatSessionMetadataView(
        session_id=conversation.conversation_id,
        agent_name=conversation.agent_name,
        department=conversation.department,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        last_sequence=conversation.last_sequence,
    )


async def _protect_legacy_input(
    *,
    state: ApiState,
    principal: Principal,
    conversation_id: str,
    attachment_id: str,
    attempt_id: str,
    prompt: str,
) -> tuple[ProtectedTurnRef, AuthenticatedRequestContext]:
    """Seal a pre-010G source-free input before it crosses a process boundary."""
    if state.agent_transport_generation != PROTECTED_TRANSPORT_GENERATION:
        raise HTTPException(status_code=503, detail="protected Agent transport is not ready")
    store = state.agent_run_store
    if store is None:
        raise HTTPException(status_code=503, detail="protected Agent transport is not ready")
    try:
        context = require_authenticated_context()
    except MissingAuthenticatedContextError as exc:
        raise HTTPException(status_code=401, detail="authenticated context is required") from exc
    now = int(time.time())
    if (
        context.principal_id != principal.id
        or (
            context.department is not None
            and context.department != principal.claims.get("department")
        )
        or context.expires_at <= now
    ):
        raise HTTPException(status_code=401, detail="authenticated context is unavailable")
    # ADMISSION, not disclosure: this context is handed to a durable turn
    # workflow that will make protected disclosures minutes from now against
    # this frozen `expires_at`. A credential that cannot cover that window is
    # refused here rather than killing the turn mid-flight.
    #
    # ⚠ THE DEPLOYMENT BOUND RATHER THAN A PER-AGENT FLOOR, because this leg
    # cannot derive one: `agent_name` here is a pre-010G chat adapter, not a
    # registry agent with a manifest, so there are no budgets to read. The
    # bound over the registry is the fail-closed choice -- never LESS
    # restrictive than the per-agent floor would have been.
    remaining = context.expires_at - now
    if remaining < RUN_CREDENTIAL_FLOOR_BOUND_SECONDS:
        raise HTTPException(
            status_code=401,
            detail=credential_too_short_detail(
                remaining_seconds=remaining,
                required_seconds=RUN_CREDENTIAL_FLOOR_BOUND_SECONDS,
            ),
        )
    binding = conversation_turn_content_binding(
        conversation_id=conversation_id,
        attachment_id=attachment_id,
        attempt_id=attempt_id,
        slot="input",
    )
    row = await store.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=principal.id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=binding,
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=prompt.encode("utf-8"),
    )
    return (
        ProtectedTurnRef(
            protected_ref=row.row_id,
            protected_hash=row.plaintext_hash,
            content_status=ProtectedContentStatus.SOURCE_FREE.value,
        ),
        context,
    )


async def _materialize_protected_terminal(
    frame: TurnFrame,
    *,
    state: ApiState,
    owner_principal_id: str,
    conversation_id: str,
    attachment_id: str,
    attempt_id: str,
    trace_id: str,
    message_id: str,
) -> AsyncIterator[TurnFrame]:
    """Expand one exact protected terminal only after request-context admission."""
    pointer = frame.protected_terminal
    if pointer is None:
        if state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION and frame.type in {
            "token",
            "citations",
            "done",
        }:
            raise ProtectedTurnUnavailable
        yield frame
        return
    store = state.agent_run_store
    if store is None or state.agent_transport_generation != PROTECTED_TRANSPORT_GENERATION:
        raise ProtectedTurnUnavailable
    # The router dependency bound the verifier-authored context. Requiring it again
    # here keeps target-load after authentication even on joined relay/retry paths.
    context = require_authenticated_context()
    if context.principal_id != owner_principal_id or context.expires_at <= int(time.time()):
        raise ProtectedTurnUnavailable
    plaintext = await store.materialize_legacy_conversation_turn(
        owner_principal_id=owner_principal_id,
        conversation_id=conversation_id,
        attachment_id=attachment_id,
        attempt_id=attempt_id,
        slot="terminal",
        protected_ref=pointer.protected_ref,
        plaintext_hash=pointer.protected_hash,
    )
    terminal = ProtectedTerminalPayload.model_validate_json(plaintext)
    seq = frame.seq
    if terminal.output:
        yield token_frame(seq, terminal.output)
        seq += 1
    if terminal.citations:
        yield citations_frame(seq, [dict(item) for item in terminal.citations])
        seq += 1
    yield done_frame(
        seq,
        frame.session_id or conversation_id,
        terminal.output,
        verified=terminal.verified,
        grounded=terminal.grounded,
        # Correlation is server-owned outer-envelope state. The protected body is
        # allowed to omit these fields (the production worker does) and cannot
        # replace them with conflicting inner substitutions.
        trace_id=trace_id,
        message_id=message_id,
    )


@router.get("/v1/chat/sessions")
async def list_sessions(
    response: Response, principal: _Principal, state: _State
) -> ChatSessionListView:
    response.headers["Cache-Control"] = "private, no-store"
    department = _require_department(principal)
    rows = await state.session_store.list_conversations(principal.id, department)
    return ChatSessionListView(
        sessions=[
            _metadata_view(row) for row in rows if row.agent_name in state.settings.chat_agents
        ]
    )


@router.get("/v1/chat/sessions/{session_id}")
async def get_session(
    session_id: str, response: Response, principal: _Principal, state: _State
) -> ChatSessionMetadataView:
    response.headers["Cache-Control"] = "private, no-store"
    department = _require_department(principal)
    row = await state.session_store.get_conversation(session_id, principal.id, department)
    if row is None or row.agent_name not in state.settings.chat_agents:
        raise HTTPException(status_code=404, detail="unknown session")
    return _metadata_view(row)


@router.get("/v1/chat/capabilities")
async def read_chat_capabilities(
    _principal: _Principal,
    state: _State,
) -> ChatCapabilitiesView:
    """Advertise the optional grounded session only when it is executable.

    The browser receives the configured agent mapping instead of inferring a
    conventional name.  Every half is required: a composed grounding policy,
    admission through the same allowlist ``create_session`` enforces, and a live
    legacy writer to execute on (see :func:`_grounded_surface_is_executable`).
    A partial/misconfigured deployment — and every deployment running canonical
    default Ask — therefore serves ``null``, and the console renders no grounding
    control rather than offering one whose every turn would fail.
    """
    grounded_name = state.grounded_agent_name
    grounded = (
        ChatGroundingModeView(agent_name=grounded_name)
        if grounded_name is not None and _grounded_surface_is_executable(state)
        else None
    )
    return ChatCapabilitiesView(grounded=grounded)


@router.get("/v1/chat/conversations")
async def read_conversations(
    principal: _Principal,
    state: _State,
    limit: Annotated[int, Query(ge=1, le=MAX_CONVERSATION_TURNS)] = 50,
) -> Response:
    """Governed ops disclosure of complete retained conversation turns.

    Authorization runs before the store read. Each returned body is audited
    before any response bytes leave the process, with reader as the audit actor
    and only subject/session/trace identifiers in detail. A sink failure therefore
    withholds the entire response. Bodies never enter the audit chain.
    """
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.chat.conversation_unauthorized",
        resource=_CONVERSATION_RESOURCE,
        http_detail="not authorized",
    )
    if state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION:
        department = _require_department(principal)
        page = await state.session_store.list_protected_conversation_turns(
            department=department, limit=limit
        )
        turns: list[ProtectedConversationTurnView] = []
        for turn in page.turns:
            if turn.agent_name not in state.settings.chat_agents:
                continue
            statuses = {turn.input.status, turn.terminal.status}
            bindings = {
                "input": ProtectedBindingView(
                    status=turn.input.status,
                    protected_ref=turn.input.protected_ref,
                    protected_hash=turn.input.plaintext_hash,
                ),
                "terminal": ProtectedBindingView(
                    status=turn.terminal.status,
                    protected_ref=turn.terminal.protected_ref,
                    protected_hash=turn.terminal.plaintext_hash,
                ),
            }
            common = {
                "session_id": turn.conversation_id,
                "agent_name": turn.agent_name,
                "department": turn.department,
                "seq": turn.turn_sequence,
                "principal_id": turn.owner_principal_id,
                "trace_id": turn.trace_id,
                "created_at": turn.created_at,
                **bindings,
            }
            unavailable_common = {
                **common,
                "input": ProtectedBindingView(
                    status="legacy_unavailable",
                    protected_ref=turn.input.protected_ref,
                    protected_hash=turn.input.plaintext_hash,
                ),
                "terminal": ProtectedBindingView(
                    status="legacy_unavailable",
                    protected_ref=turn.terminal.protected_ref,
                    protected_hash=turn.terminal.plaintext_hash,
                ),
            }
            if "legacy_unavailable" in statuses:
                projected = LegacyUnavailableConversationTurnView(**unavailable_common)
            elif "source_bound_container" in statuses:
                projected = WithheldProtectedConversationTurnView(**common)
            else:
                store = state.agent_run_store
                if store is None:
                    raise HTTPException(status_code=503, detail="protected ledger unavailable")
                try:
                    prompt = await store.materialize_legacy_conversation_turn(
                        owner_principal_id=turn.owner_principal_id,
                        conversation_id=turn.conversation_id,
                        attachment_id=turn.attachment_id,
                        attempt_id=turn.attempt_id,
                        slot="input",
                        protected_ref=turn.input.protected_ref,
                        plaintext_hash=turn.input.plaintext_hash,
                    )
                    terminal_raw = await store.materialize_legacy_conversation_turn(
                        owner_principal_id=turn.owner_principal_id,
                        conversation_id=turn.conversation_id,
                        attachment_id=turn.attachment_id,
                        attempt_id=turn.attempt_id,
                        slot="terminal",
                        protected_ref=turn.terminal.protected_ref,
                        plaintext_hash=turn.terminal.plaintext_hash,
                    )
                    terminal = ProtectedTerminalPayload.model_validate_json(terminal_raw)
                    messages = [
                        {"role": "user", "content": prompt.decode("utf-8")},
                        {"role": "assistant", "content": terminal.output},
                    ]
                    projected = AvailableProtectedConversationTurnView(**common, messages=messages)
                except (ProtectedTurnUnavailable, UnicodeDecodeError, ValueError):
                    # A missing/tampered/foreign protected row is one unavailable
                    # content item, not an oracle that changes the whole response.
                    projected = LegacyUnavailableConversationTurnView(**unavailable_common)
            turns.append(projected)
        try:
            protected_view = build_protected_conversation_ledger(
                turns,
                total_turns=page.total_turns,
            )
        except ConversationTurnTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        for turn in protected_view.turns:
            await emit_surface_audit(
                state.sink,
                action="api.chat.conversation_turn_read",
                resource=_CONVERSATION_RESOURCE,
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={
                    "session_id": turn.session_id,
                    "seq": str(turn.seq),
                    "attribution": turn.attribution,
                    "subject_principal_id": turn.principal_id,
                    "trace_id": turn.trace_id,
                    "content_state": turn.content_state,
                },
            )
        return Response(
            content=encoded_protected_ledger(protected_view),
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )

    page = await state.session_store.list_conversation_turns(limit=limit)
    try:
        view = build_conversation_ledger(page)
    except ConversationTurnTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    for turn in view.turns:
        detail = {
            "session_id": turn.session_id,
            "seq": str(turn.seq),
            "attribution": turn.attribution,
        }
        if turn.principal_id is not None:
            detail["subject_principal_id"] = turn.principal_id
        if turn.trace_id is not None:
            detail["trace_id"] = turn.trace_id
        await emit_surface_audit(
            state.sink,
            action="api.chat.conversation_turn_read",
            resource=_CONVERSATION_RESOURCE,
            outcome=AuditOutcome.success,
            actor=principal.id,
            detail=detail,
        )

    return Response(
        content=encoded_ledger(view),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/v1/chat/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatMessageRequest,
    principal: _Principal,
    state: _State,
    idempotency_key: _IdempotencyKey = None,
) -> StreamingResponse:
    department = _require_department(principal)

    if (
        getattr(state.settings, "default_ask_generation", "")
        and state.default_ask_submission is None
    ):
        # Refuse retained legacy sessions too; no old writer reopens while the
        # canonical activation fence waits for its source-authority dependency.
        raise HTTPException(status_code=503, detail="canonical default Ask is bootstrapping")

    keepalive_s = state.settings.sse_keepalive_s
    timeout_s = state.settings.turn_timeout_s

    # PRE-010G: branch before the first legacy claim/writer/loader call.  Both
    # source-free and source-bound Ask use this exact canonical boundary.  Reading
    # the STORED Conversation identity is not a legacy claim/writer/loader call —
    # the canonical branch still precedes every one of those.  A retained
    # grounded Conversation is NOT exempted onto the legacy writer here: the
    # database fence retired that writer, so the exemption only ever produced a
    # 500.  It falls to the id check below and is an unknown session.
    conversation = (
        await state.session_store.get_conversation(session_id, principal.id, department)
        if state.default_ask_submission is not None
        else None
    )
    if state.default_ask_submission is not None:
        activation = state.default_ask_submission.activation
        if conversation is None or conversation.agent_name != activation.default_agent_id:
            raise HTTPException(status_code=404, detail="unknown session")
        # NE-INV-CHAT-IDEMPOTENCY (Finding B): mint once from the exact stored
        # Conversation identity; the same id binds admission and dispatch.
        turn_id = _mint_turn_id(conversation.conversation_id, principal.id, idempotency_key)
        trace_id = _trace_id_for_turn(turn_id)
        catalog_row = state.agent_catalog.get_visible(activation.default_agent_id, principal)
        if catalog_row is None or not catalog_row.available or state.agent_preflight is None:
            raise HTTPException(status_code=503, detail="canonical default Ask is unavailable")
        preflight = await state.agent_preflight.preflight(
            agent_id=catalog_row.definition.agent_id, principal=principal
        )
        if preflight is None or preflight.capabilities is None or not preflight.available:
            raise HTTPException(status_code=503, detail="canonical default Ask is unavailable")
        strategies = tuple(catalog_row.definition.allowed_strategies)
        if not strategies:
            raise HTTPException(status_code=503, detail="canonical default Ask has no strategy")
        # THE EXPLICIT MODEL CHOICE RIDES THE CANONICAL PATH TOO. This is the
        # same shared selection authority the legacy branch below and the
        # workflow routes call, with the same typed refusals (403 air-gap /
        # 409 unavailable-by-name / 503 evidence) BEFORE any durable write —
        # a refusal here leaves no Run and no claimed turn. Dropping
        # ``body.requested_model`` on this branch was the defect that made the
        # Ask picker decorative: the field parsed, authorized nothing, and the
        # run silently executed as request_mode=auto on the gateway default.
        # Admission (run_submission._authorize_model) re-authorizes the pair
        # under the Agent's model policy and refuses a lane that drifted.
        # NE-CTX-IN-FORCE round 7 (SPEC PRE-DEMO-014): an EXPLICIT switch is
        # gated against the CURRENT conversation snapshot, read from the newest
        # boundary receipt — the durable counts admission itself froze. A model
        # whose window cannot safely accept it refuses BY NAME here (no
        # compaction operation exists; there is no silent truncation), instead
        # of authorizing and then failing the admitted turn. Omitted model
        # (automatic policy) passes None: routing then serves what fits and the
        # trim layer's typed, audited refusal governs the turn — SPEC's
        # automatic-policy clause, not this gate, covers that path.
        snapshot_tokens = (
            await state.agent_run_store.latest_context_snapshot_estimate(
                conversation_id=conversation.conversation_id,
                owner_principal_id=principal.id,
            )
            if body.requested_model is not None and state.agent_run_store is not None
            else None
        )
        selection = await authorize_model_choice(
            state=state,
            principal=principal,
            task=state.runtime_settings.task,
            requested_model=body.requested_model,
            trace_id=trace_id,
            snapshot_tokens=snapshot_tokens,
        )
        try:
            accepted = await state.default_ask_submission.submit(
                principal=principal,
                conversation_id=conversation.conversation_id,
                expected_version=conversation.last_sequence,
                agent_id=catalog_row.definition.agent_id,
                agent_version=catalog_row.definition.version,
                strategy=strategies[0],
                config_hash=state.config_hash,
                capability_snapshot_hash=preflight.capabilities.capability_hash,
                input_text=body.prompt,
                idempotency_key=idempotency_key or turn_id,
                turn_id=turn_id,
                attachment_id=turn_id,
                trace_id=trace_id,
                producer_snapshot_hash=activation.producer_snapshot_hash,
                requested_model=selection.model if selection is not None else None,
                requested_model_lane=selection.lane if selection is not None else None,
            )
        except Exception as exc:
            from ne.api.default_ask_submission import DefaultAskUnavailable
            from ne.api.run_submission import (
                RunCredentialTooShort,
                RunSubmissionConflict,
                RunSubmissionNotFound,
            )

            if isinstance(exc, RunSubmissionNotFound):
                raise HTTPException(
                    status_code=404, detail="canonical default Ask unavailable"
                ) from exc
            if isinstance(exc, RunCredentialTooShort):
                # Ordered BEFORE the conflict arm it subclasses: the caller can
                # fix a short credential and cannot fix a 409.
                raise HTTPException(status_code=401, detail=exc.detail) from exc
            if isinstance(exc, (RunSubmissionConflict, DefaultAskUnavailable)):
                raise HTTPException(
                    status_code=409, detail="canonical default Ask conflict"
                ) from exc
            raise
        if state.agent_run_enqueuer is None:
            raise HTTPException(status_code=503, detail="canonical Agent Run queue is unavailable")
        try:
            await state.agent_run_enqueuer.enqueue(accepted.workflow_request)
        except Exception as exc:
            # Admission is durable by design. Never delete or fall back to the
            # legacy attachment writer; an exact retry reuses the same identities.
            raise HTTPException(
                status_code=503, detail="canonical Agent Run enqueue failed"
            ) from exc

        async def _accepted() -> AsyncIterator[bytes]:
            payload = json.dumps(
                {
                    "type": "accepted",
                    "agent_id": activation.default_agent_id,
                    "conversation_id": conversation.conversation_id,
                    "run_id": accepted.submission.run_id,
                    "attempt_id": accepted.submission.attempt_id,
                    "status": accepted.submission.status,
                },
                separators=(",", ":"),
            )
            yield f"event: accepted\ndata: {payload}\n\n".encode()

        return StreamingResponse(_accepted(), media_type="text/event-stream", headers=_SSE_HEADERS)

    matched = await resolve_session(session_id, principal.id, department, state)
    if matched is None:
        raise HTTPException(status_code=404, detail="unknown session")

    # NE-INV-CHAT-IDEMPOTENCY (Finding B): mint the turn id ONCE here — uuid4 by
    # default, or derived from a valid client Idempotency-Key — and share it
    # verbatim with both legacy modes below (see _mint_turn_id + module docstring).
    turn_id = _mint_turn_id(matched.session_id, principal.id, idempotency_key)
    trace_id = _trace_id_for_turn(turn_id)
    request_fingerprint = conversation_request_fingerprint(
        matched.session_id,
        principal.id,
        department,
        matched.agent_name,
        body.prompt,
        body.requested_model,
        None,
        trace_id,
        principal.groups,
        dict(principal.claims),
    )
    try:
        while True:
            claim = await state.session_store.claim_conversation_attachment(
                matched.session_id,
                principal.id,
                department,
                matched.agent_name,
                turn_id,
                request_fingerprint,
                trace_id,
            )
            if (
                claim.disposition == "inflight"
                and claim.dispatch_id is not None
                and state.turn_enqueuer is not None
                and state.turn_reader is not None
            ):
                return _joined_enqueue_stream(
                    state,
                    dispatch_id=claim.dispatch_id,
                    owner_principal_id=principal.id,
                    conversation_id=matched.session_id,
                    attachment_id=turn_id,
                    attempt_id=claim.attempt_id,
                    trace_id=trace_id,
                    keepalive_s=keepalive_s,
                    timeout_s=timeout_s,
                )
            if claim.disposition != "inflight":
                break
            await asyncio.sleep(0.02)
    except AttachmentRequestMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail="idempotency key conflicts with the original request",
        ) from exc
    if (
        claim.disposition in {"published", "prepared", "staged"}
        and state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION
    ):
        if claim.attempt_id is None:
            raise HTTPException(status_code=503, detail="protected conversation replay unavailable")
        protected = await state.session_store.protected_conversation_turn(
            matched.session_id,
            principal.id,
            department,
            matched.agent_name,
            turn_id,
            claim.attempt_id,
        )
        if protected is None:
            # Never adopt/publish a legacy staged/prepared plaintext envelope in
            # protected mode. Every unavailable recovery disposition collapses
            # to the same neutral response as a missing protected publication.
            raise HTTPException(status_code=404, detail="conversation turn unavailable")
        await emit_surface_audit(
            state.sink,
            action="api.chat.turn_replayed",
            resource="chat:turn",
            outcome=AuditOutcome.success,
            actor=principal.id,
            detail={
                "turn_id": turn_id,
                "session_id": matched.session_id,
                "agent_name": matched.agent_name,
            },
        )
        return _published_protected_replay_stream(
            protected,
            state=state,
            trace_id=trace_id,
            keepalive_s=keepalive_s,
            timeout_s=timeout_s,
        )
    if claim.disposition in {"published", "prepared", "staged"}:
        recovery_citations: list[dict[str, str]] = []
        if claim.attempt_id is not None and claim.disposition == "staged":
            await matched.recover_staged_turn(
                principal=principal,
                attachment_id=turn_id,
                idempotency_fingerprint=request_fingerprint,
                claimed_attempt_id=claim.attempt_id,
                trace_id=trace_id,
            )
            recovery_citations = [dict(item) for item in matched.take_replay_citations(turn_id)]
        elif claim.attempt_id is not None and claim.disposition == "prepared":
            await matched.adopt_prepared_turn(
                principal=principal,
                attachment_id=turn_id,
                idempotency_fingerprint=request_fingerprint,
                claimed_attempt_id=claim.attempt_id,
                trace_id=trace_id,
            )
            prepared = await state.session_store.prepared_conversation_replay(
                matched.session_id,
                principal.id,
                department,
                matched.agent_name,
                turn_id,
                request_fingerprint,
            )
            if prepared is None:  # pragma: no cover - locked row invariant
                raise HTTPException(status_code=409, detail="turn is already in progress")
            recovery_citations = [dict(item) for item in prepared.citations]
        if claim.attempt_id is not None and claim.disposition in {"prepared", "staged"}:
            await matched.publish_turn(turn_id, citations=recovery_citations)
            matched.finalize_turn(turn_id)
        replay = await state.session_store.published_conversation_replay(
            matched.session_id,
            principal.id,
            department,
            matched.agent_name,
            turn_id,
            request_fingerprint,
        )
        if replay is None:  # pragma: no cover - same locked row invariant
            raise HTTPException(status_code=409, detail="turn is already in progress")
    else:
        replay = None
    if replay is not None:
        # A published attachment is durable terminal truth.  Resolve it after
        # exact owner/department/agent admission but before consulting volatile
        # model catalog/PDP/lifecycle evidence or enqueueing new work.  The
        # original execution already emitted session.run; this names the distinct
        # delivery without pretending a second model/tool run happened.
        await emit_surface_audit(
            state.sink,
            action="api.chat.turn_replayed",
            resource="chat:turn",
            outcome=AuditOutcome.success,
            actor=principal.id,
            detail={
                "turn_id": turn_id,
                "session_id": matched.session_id,
                "agent_name": matched.agent_name,
            },
        )
        return _published_replay_stream(
            replay,
            session_id=matched.session_id,
            turn_id=turn_id,
            trace_id=trace_id,
            keepalive_s=keepalive_s,
            timeout_s=timeout_s,
        )
    if claim.attempt_id is None:  # pragma: no cover - claim disposition invariant
        raise HTTPException(status_code=409, detail="turn is already in progress")
    try:
        # NE-CTX-IN-FORCE round 8 (finding 2): the round-7 snapshot-fit gate
        # covered only the canonical default-Ask branch above; this legacy
        # branch also holds a conversation, so an explicit switch here is gated
        # against the SAME current-snapshot rule (SPEC PRE-DEMO-014 — no silent
        # truncation) instead of authorizing with no snapshot at all. The
        # estimate mirrors the boundary-receipt arithmetic: the stored bytes of
        # the published turns the run will reload (measured as stored, no
        # second estimator) + this prompt + the continuity reserve, bytes
        # upper-bounding tokens. ``None`` here is NEVER "unknown ⇒ allowed":
        # ``published_context_bytes`` returns ``None`` only when the
        # conversation has NO published turn — genuinely no snapshot to check
        # (a fresh session keeps its model-switch beat) — and a store failure
        # raises out of this try, aborting the claimed turn, rather than
        # degrading to no check.
        legacy_snapshot_tokens: int | None = None
        if body.requested_model is not None:
            published_bytes = await state.session_store.published_context_bytes(
                matched.session_id,
                principal.id,
                department,
                matched.agent_name,
                limit=max(1, state.runtime_settings.session_history_max_turns),
            )
            if published_bytes is not None:
                legacy_snapshot_tokens = (
                    published_bytes + len(body.prompt) + CONTINUITY_PROMPT_RESERVE_TOKENS
                )
        selection = await authorize_model_choice(
            state=state,
            principal=principal,
            task=state.runtime_settings.task,
            requested_model=body.requested_model,
            trace_id=trace_id,
            snapshot_tokens=legacy_snapshot_tokens,
        )
    except BaseException:
        with contextlib.suppress(LookupError):
            await state.session_store.abort_conversation_turn(
                matched.session_id,
                principal.id,
                turn_id,
                claim.attempt_id,
            )
        raise

    execution_fingerprint = conversation_request_fingerprint(
        matched.session_id,
        principal.id,
        department,
        matched.agent_name,
        body.prompt,
        selection.model if selection is not None else None,
        None if selection is None else selection.lane.value,
        trace_id,
        principal.groups,
        dict(principal.claims),
    )
    await state.session_store.bind_conversation_execution(
        matched.session_id,
        principal.id,
        turn_id,
        claim.attempt_id,
        execution_fingerprint,
    )

    if state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION:
        if state.turn_enqueuer is None or state.turn_reader is None:
            raise HTTPException(status_code=503, detail="protected Agent transport is not ready")
        protected_input, authenticated_context = await _protect_legacy_input(
            state=state,
            principal=principal,
            conversation_id=matched.session_id,
            attachment_id=turn_id,
            attempt_id=claim.attempt_id,
            prompt=body.prompt,
        )
        return await _enqueue_stream(
            matched,
            department,
            principal,
            state,
            keepalive_s,
            timeout_s,
            turn_id,
            trace_id,
            selection,
            request_fingerprint,
            execution_fingerprint,
            claim.attempt_id,
            body.prompt,
            protected_input=protected_input,
            authenticated_context=authenticated_context,
        )

    if state.turn_enqueuer is not None and state.turn_reader is not None:
        # Enqueue mode: ne-worker's step binds the SAME transit channel and streams
        # each REAL governed transit as a `tool` TurnFrame while the turn runs
        # (NE-TURN-PROGRESS, ne.worker.workflows), relayed here through ne-api's
        # sole SSE encoder — per-hop progress, never fabricated (design §0 law 5).
        return await _enqueue_stream(
            matched,
            department,
            principal,
            state,
            keepalive_s,
            timeout_s,
            turn_id,
            trace_id,
            selection,
            request_fingerprint,
            execution_fingerprint,
            claim.attempt_id,
            body.prompt,
            protected_input=None,
            authenticated_context=None,
        )

    async def _streamer() -> AsyncIterator[bytes]:
        # Set the turn principal AND the NE-137 tool-transit channel from the VERIFIED
        # bearer BEFORE the turn task is created (the ContextVars copy into it); reset
        # both in finally (client abort included). This runs in the streaming task's
        # context, so the observing sink publishes THIS turn's governed transits.
        #
        # NE-SCOPE-CHAT (ne-action-scope-wiring design §3.1): the turn id is minted
        # ONCE in send_message (uuid4 by default, Idempotency-Key-derived when
        # supplied) and shared verbatim with enqueue mode, so both chat modes carry
        # identical per-turn action_scope semantics (pinned by
        # tests/test_chat_action_scope.py).
        channel = TurnTransitChannel()
        principal_token = TURN_PRINCIPAL.set(principal)
        transit_token = TURN_TOOL_TRANSITS.set(channel)
        try:
            with (
                citation_capture_scope() as citation_capture,
                action_scope(turn_id),
                run_trace_scope(trace_id),
            ):
                run_prepared = False

                async def _run() -> AgentRunResult:
                    nonlocal run_prepared
                    result = await matched.run(
                        body.prompt,
                        principal=principal,
                        requested_model=(selection.model if selection is not None else None),
                        requested_model_lane=(selection.lane if selection is not None else None),
                        attachment_id=turn_id,
                        request_fingerprint=execution_fingerprint,
                        idempotency_fingerprint=request_fingerprint,
                        claimed_attempt_id=claim.attempt_id,
                        served_citations=citation_capture.snapshot,
                    )
                    run_prepared = True
                    citation_capture.restore(matched.take_replay_citations(turn_id))
                    return result

                async def _publish() -> None:
                    await matched.publish_turn(
                        turn_id,
                        citations=citation_capture.snapshot(),
                    )

                terminal_completed = False
                try:
                    async for frame in turn_event_stream(
                        _run(),
                        keepalive_s=keepalive_s,
                        timeout_s=timeout_s,
                        transits=channel,
                        trace_id=trace_id,
                        link_plain_trace=selection is not None,
                        message_id=turn_id,
                        before_done=_publish,
                    ):
                        if frame.startswith(b"event: done\n"):
                            terminal_completed = True
                            matched.finalize_turn(turn_id)
                        yield frame
                finally:
                    if run_prepared and not terminal_completed:
                        with contextlib.suppress(LookupError):
                            await matched.abort_turn(turn_id)
        finally:
            TURN_TOOL_TRANSITS.reset(transit_token)
            TURN_PRINCIPAL.reset(principal_token)

    return StreamingResponse(
        _streamer(),
        status_code=200,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _joined_enqueue_stream(
    state: ApiState,
    *,
    dispatch_id: str,
    owner_principal_id: str,
    conversation_id: str,
    attachment_id: str,
    attempt_id: str | None,
    trace_id: str,
    keepalive_s: float,
    timeout_s: float,
) -> StreamingResponse:
    """Join one already-claimed attempt's durable frame stream without re-enqueue."""
    assert state.turn_reader is not None
    reader = state.turn_reader

    async def _relay() -> AsyncIterator[bytes]:
        async def _frames() -> AsyncIterator[TurnFrame]:
            if attempt_id is None:
                raise ProtectedTurnUnavailable
            async for current in reader.frames(dispatch_id):
                async for materialized in _materialize_protected_terminal(
                    current,
                    state=state,
                    owner_principal_id=owner_principal_id,
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    attempt_id=attempt_id,
                    trace_id=trace_id,
                    message_id=attachment_id,
                ):
                    yield materialized

        source = (
            _frames()
            if state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION
            else reader.frames(dispatch_id)
        )
        async for frame in relay_event_stream(
            source,
            keepalive_s=keepalive_s,
            timeout_s=timeout_s,
            trace_id=trace_id,
        ):
            yield frame

    return StreamingResponse(
        _relay(),
        status_code=200,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _published_replay_stream(
    replay: PublishedConversationReplay,
    *,
    session_id: str,
    turn_id: str,
    trace_id: str,
    keepalive_s: float,
    timeout_s: float,
) -> StreamingResponse:
    """Serve one stored terminal envelope without re-entering execution seams."""

    async def _streamer() -> AsyncIterator[bytes]:
        with citation_capture_scope() as citations:
            citations.restore(replay.citations)

            async def _result() -> AgentRunResult:
                return AgentRunResult(
                    output=replay.output,
                    session_id=session_id,
                    verified=replay.verified,
                    grounded=replay.grounded,
                )

            async for frame in turn_event_stream(
                _result(),
                keepalive_s=keepalive_s,
                timeout_s=timeout_s,
                trace_id=trace_id,
                link_plain_trace=replay.trace_linked,
                message_id=turn_id,
            ):
                yield frame

    return StreamingResponse(
        _streamer(),
        status_code=200,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _published_protected_replay_stream(
    replay: ProtectedConversationTurn,
    *,
    state: ApiState,
    trace_id: str,
    keepalive_s: float,
    timeout_s: float,
) -> StreamingResponse:
    """Serve an idempotent replay only through its exact protected terminal."""

    async def _source() -> AsyncIterator[TurnFrame]:
        pointer = ProtectedTurnRef(
            protected_ref=replay.terminal.protected_ref,
            protected_hash=replay.terminal.plaintext_hash,
            content_status=replay.terminal.status,
        )
        async for frame in _materialize_protected_terminal(
            done_frame(0, replay.conversation_id, protected_terminal=pointer),
            state=state,
            owner_principal_id=replay.owner_principal_id,
            conversation_id=replay.conversation_id,
            attachment_id=replay.attachment_id,
            attempt_id=replay.attempt_id,
            trace_id=trace_id,
            message_id=replay.attachment_id,
        ):
            yield frame

    async def _streamer() -> AsyncIterator[bytes]:
        async for frame in relay_event_stream(
            _source(),
            keepalive_s=keepalive_s,
            timeout_s=timeout_s,
            trace_id=trace_id,
        ):
            yield frame

    return StreamingResponse(
        _streamer(),
        status_code=200,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _enqueue_stream(
    session: DepartmentAgentSession,
    department: str,
    principal: Principal,
    state: ApiState,
    keepalive_s: float,
    timeout_s: float,
    turn_id: str,
    trace_id: str,
    selection: AuthorizedModelSelection | None,
    request_fingerprint: str,
    execution_fingerprint: str,
    claimed_attempt_id: str,
    prompt: str,
    *,
    protected_input: ProtectedTurnRef | None,
    authenticated_context: AuthenticatedRequestContext | None,
) -> StreamingResponse:
    """Enqueue the turn as a durable workflow and relay its frame stream (design §4.4).

    The ``TurnRequest`` principal fields come ONLY from the verified bearer
    (``from_principal``); the body carried only the prompt. ``turn_id`` is minted by
    ``send_message`` (server ``uuid4`` default, or derived from a valid client
    Idempotency-Key — NE-INV-CHAT-IDEMPOTENCY) and remains BOTH the DBOS workflow id
    and the frame-stream key: a retried POST carrying the same key re-enqueues the
    SAME workflow id, idempotent at the DBOS layer too.
    """
    assert state.turn_enqueuer is not None
    assert state.turn_reader is not None
    protected_mode = state.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION
    if protected_mode:
        if protected_input is None or authenticated_context is None:
            raise HTTPException(status_code=503, detail="protected Agent transport is not ready")
        request: TurnRequest | LegacyTurnRequest = TurnRequest.from_principal(
            turn_id=turn_id,
            session_id=session.session_id,
            agent_name=session.agent_name,
            department=department,
            protected_input=protected_input,
            authenticated_context=authenticated_context,
            principal=principal,
            trace_id=trace_id,
            requested_model=selection.model if selection is not None else None,
            requested_model_lane=selection.lane if selection is not None else None,
            request_fingerprint=execution_fingerprint,
            idempotency_fingerprint=request_fingerprint,
            claimed_attempt_id=claimed_attempt_id,
        )
    else:
        if protected_input is not None or authenticated_context is not None:
            raise HTTPException(status_code=503, detail="legacy Agent transport is not ready")
        request = LegacyTurnRequest.from_principal(
            turn_id=turn_id,
            session_id=session.session_id,
            agent_name=session.agent_name,
            department=department,
            prompt=prompt,
            principal=principal,
            trace_id=trace_id,
            requested_model=selection.model if selection is not None else None,
            requested_model_lane=selection.lane if selection is not None else None,
            request_fingerprint=execution_fingerprint,
            idempotency_fingerprint=request_fingerprint,
            claimed_attempt_id=claimed_attempt_id,
        )
    dispatch_id = f"{turn_id}:{claimed_attempt_id}"
    try:
        await state.turn_enqueuer.enqueue(dispatch_id, request.model_dump_json())
        await state.session_store.mark_conversation_dispatched(
            session.session_id,
            principal.id,
            turn_id,
            claimed_attempt_id,
            dispatch_id,
        )
    except BaseException:
        with contextlib.suppress(LookupError):
            await state.session_store.abort_conversation_turn(
                session.session_id,
                principal.id,
                turn_id,
                claimed_attempt_id,
            )
        raise
    # Redacted surface record — ids only, NEVER the prompt (spec §4.8).
    await emit_surface_audit(
        state.sink,
        action="api.chat.turn_enqueued",
        resource="chat:turn",
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "turn_id": turn_id,
            "session_id": session.session_id,
            "agent_name": session.agent_name,
        },
    )
    reader = state.turn_reader

    async def _relay() -> AsyncIterator[bytes]:
        async def _frames() -> AsyncIterator[TurnFrame]:
            async for current in reader.frames(dispatch_id):
                async for materialized in _materialize_protected_terminal(
                    current,
                    state=state,
                    owner_principal_id=principal.id,
                    conversation_id=session.session_id,
                    attachment_id=turn_id,
                    attempt_id=claimed_attempt_id,
                    trace_id=trace_id,
                    message_id=turn_id,
                ):
                    yield materialized

        async for frame in relay_event_stream(
            _frames(),
            keepalive_s=keepalive_s,
            timeout_s=timeout_s,
            trace_id=trace_id,
        ):
            yield frame

    return StreamingResponse(
        _relay(),
        status_code=200,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
