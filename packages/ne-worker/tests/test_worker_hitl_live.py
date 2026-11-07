# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T4 — the flagship cross-process HITL e2e (NE-103c §6.3).

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` — the ``live-postgres`` CI lane (direct)
and the ``live-postgres-pooled`` CI lane (through a transaction-mode PgBouncer —
NE-POOLER-TIMEOUTS) both run this. A worker turn
hits a tier-2 tool → ``require-approval`` → the worker's gate blocks in
``await_outcome``. A SECOND process (this test, simulating ne-api) builds its OWN
gate over the SAME Postgres, sees the worker-created pending row via ``list_pending``,
approves it, and the blocked turn releases to ``done``. Finally: a worker-emitted
audit row and a test-process-emitted audit row carry the SAME ``config_hash`` — the
§6.2 boot cross-check made executable (both processes freeze the identical composed
mapping via ``build_platform`` under identical env).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_URL = os.environ.get("NE_RUNTIME_DBOS_DATABASE_URL")


def _reachable(url: str) -> bool:
    try:
        engine = sa.create_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


if not (_URL and _reachable(_URL)):
    pytest.skip(
        "pending-live-verification: set NE_RUNTIME_DBOS_DATABASE_URL (the live-postgres "
        f"and live-postgres-pooled CI lanes both run this) (url {_URL!r} unset/unreachable)",
        allow_module_level=True,
    )

assert _URL is not None


def _swap_db(url: str, db: str) -> str:
    base, _, _old = url.rpartition("/")
    return f"{base}/{db}"


_SESSION_URL = _swap_db(_URL, "postgres")
_WORKER_DBOS_URL = _swap_db(_URL, "ne_worker_dbos")


def _env(marker_path: str) -> dict[str, str]:
    return {
        "NE_WORKER_SEAMS": "ne.worker.testing:ci_seams",
        "NE_WORKER_TEST_MODE": "hitl",
        "NE_WORKER_DBOS_DATABASE_URL": _WORKER_DBOS_URL,
        "NE_WORKER_HEARTBEAT_PATH": marker_path,
        "NE_RUNTIME_DURABILITY": "dbos",
        "NE_RUNTIME_SESSION_DATABASE_URL": _SESSION_URL,
        "NE_AUDIT_DATABASE_URL": _SESSION_URL,
        "NE_PERMISSIONS_DATABASE_URL": _SESSION_URL,
        "NE_API_QDRANT_URL": "http://qdrant.invalid",
        "NE_API_QDRANT_COLLECTION": "t",
        "NE_API_TEI_URL": "http://tei.invalid",
        "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID": "worker-live-k1",
        "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
        "NE_AGENT_TRANSPORT_GENERATION": "agent-transport-v1",
    }


def _wait_marker(path: str, *, timeout_s: float = 90.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 60.0:
            return
        time.sleep(0.5)
    raise AssertionError(f"worker heartbeat marker {path} never appeared")


async def _bound_request(platform: object, turn_id: str) -> tuple[str, str, str]:
    from ne.api.turn_wire import ProtectedTurnRef, TurnRequest
    from ne.contracts.identity import AuthenticatedRequestContext
    from ne.contracts.types import Principal
    from ne.runtime.private_payloads import PrivatePayloadDomain, ProtectedContentStatus
    from ne.runtime.run_store import (
        CONVERSATION_TURN_TARGET_KIND,
        conversation_turn_content_binding,
    )
    from ne.runtime.store import conversation_request_fingerprint

    principal = Principal(id="user:ada", groups=("eng",), claims={"department": "engineering"})
    conversation = await platform.session_store.create_conversation(  # type: ignore[attr-defined]
        "console-chat", "engineering", principal.id
    )
    trace_id = hashlib.sha256(f"live-turn:{turn_id}".encode()).hexdigest()[:32]
    fingerprint = conversation_request_fingerprint(
        conversation.conversation_id,
        principal.id,
        "engineering",
        "console-chat",
        "please write the note",
        None,
        None,
        trace_id,
        principal.groups,
        dict(principal.claims),
    )
    claim = await platform.session_store.claim_conversation_attachment(  # type: ignore[attr-defined]
        conversation.conversation_id,
        principal.id,
        "engineering",
        "console-chat",
        turn_id,
        fingerprint,
        trace_id,
    )
    assert claim.disposition == "owned" and claim.attempt_id is not None
    await platform.session_store.bind_conversation_execution(  # type: ignore[attr-defined]
        conversation.conversation_id,
        principal.id,
        turn_id,
        claim.attempt_id,
        fingerprint,
    )
    dispatch_id = f"{turn_id}:{claim.attempt_id}"
    await platform.session_store.mark_conversation_dispatched(  # type: ignore[attr-defined]
        conversation.conversation_id,
        principal.id,
        turn_id,
        claim.attempt_id,
        dispatch_id,
    )
    protected = await platform.agent_run_store.claim_protected_payload(  # type: ignore[attr-defined]
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=principal.id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=conversation_turn_content_binding(
            conversation_id=conversation.conversation_id,
            attachment_id=turn_id,
            attempt_id=claim.attempt_id,
            slot="input",
        ),
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=b"please write the note",
    )
    request = TurnRequest.from_principal(
        turn_id=turn_id,
        session_id=conversation.conversation_id,
        agent_name="console-chat",
        department="engineering",
        protected_input=ProtectedTurnRef(
            protected_ref=protected.row_id,
            protected_hash=protected.plaintext_hash,
            content_status=ProtectedContentStatus.SOURCE_FREE.value,
        ),
        authenticated_context=AuthenticatedRequestContext(
            principal_id=principal.id,
            authorization_groups=("eng",),
            department="engineering",
            issuer="https://issuer.test",
            expires_at=4_102_444_800,
        ),
        principal=principal,
        trace_id=trace_id,
        request_fingerprint=fingerprint,
        idempotency_fingerprint=fingerprint,
        claimed_attempt_id=claim.attempt_id,
    ).model_dump_json()
    return conversation.conversation_id, dispatch_id, request


def _distinct_config_hashes() -> set[str]:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT DISTINCT config_hash FROM ne_audit_log")).fetchall()
        return {str(r[0]) for r in rows}
    finally:
        engine.dispose()


def test_cross_process_hitl_release_and_config_hash_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dbos import DBOSClient, EnqueueOptions

    from ne.api.platform import build_platform
    from ne.api.settings import ApiSettings
    from ne.api.turn_relay import normalize_dbos_url
    from ne.contracts import reset_config_hash_for_testing
    from ne.worker.testing import ci_seams

    # Freeze the SAME composed mapping in THIS process (simulating ne-api) via the
    # SAME build_platform + ci_seams under the SAME env — the config_hash cross-check.
    for key, value in _env(str(tmp_path / "unused.health")).items():
        monkeypatch.setenv(key, value)
    reset_config_hash_for_testing()
    platform = build_platform(
        ApiSettings(
            qdrant_url="http://qdrant.invalid", qdrant_collection="t", tei_url="http://tei.invalid"
        ),
        ci_seams(),
    )
    test_hash = platform.config_hash

    turn_id = f"turn-{uuid.uuid4()}"
    _session_id, dispatch_id, request = asyncio.run(_bound_request(platform, turn_id))

    marker_path = str(tmp_path / "worker.health")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ne.worker"], env={**os.environ, **_env(marker_path)}
    )
    client = DBOSClient(system_database_url=normalize_dbos_url(_WORKER_DBOS_URL))
    options: EnqueueOptions = {
        "queue_name": "chat-turns",
        "workflow_name": "chat.turn",
        "workflow_id": dispatch_id,
    }
    try:
        _wait_marker(marker_path)
        client.enqueue(options, request)

        # Poll THIS process's gate (over the same Postgres) until the worker-created
        # tier-2 approval appears, then approve it here.
        async def _approve() -> None:
            deadline = time.time() + 90
            while time.time() < deadline:
                pending = await platform.gate.list_pending()
                if pending:
                    await platform.gate.approve(pending[0].request_id, approver="user:boss")
                    return
                await asyncio.sleep(0.5)
            raise AssertionError("no worker-created approval appeared")

        asyncio.run(_approve())

        # The blocked turn releases as a content-free protected result.  Open it
        # only through the exact Conversation terminal slot in the sanctioned
        # store; plaintext must never cross the DBOS result boundary.
        from ne.api.turn_wire import ProtectedTerminalPayload, ProtectedTurnRef, TurnRequest

        result = client.retrieve_workflow(dispatch_id).get_result()
        assert "done" not in result
        protected = ProtectedTurnRef.model_validate_json(result)
        request_model = TurnRequest.model_validate_json(request)
        assert platform.agent_run_store is not None
        terminal = asyncio.run(
            platform.agent_run_store.materialize_legacy_conversation_turn(
                owner_principal_id=request_model.principal_id,
                conversation_id=request_model.session_id,
                attachment_id=request_model.turn_id,
                attempt_id=request_model.claimed_attempt_id,
                slot="terminal",
                protected_ref=protected.protected_ref,
                plaintext_hash=protected.protected_hash,
            )
        )
        assert ProtectedTerminalPayload.model_validate_json(terminal).output == "done"

        # config_hash parity: every audit row (worker- + test-emitted) shares ONE hash.
        hashes = _distinct_config_hashes()
        assert hashes == {test_hash}, f"config_hash fork: {hashes} != {{{test_hash}}}"
    finally:
        proc.kill()
        proc.wait(timeout=30)
