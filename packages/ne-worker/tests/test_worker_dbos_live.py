# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T3 — the durability proof: SIGKILL-mid-step exactly-once (NE-103c §6.3).

Gated on ``NE_RUNTIME_DBOS_DATABASE_URL`` — the ``live-postgres`` CI lane (direct)
and the ``live-postgres-pooled`` CI lane (through a transaction-mode PgBouncer —
NE-POOLER-TIMEOUTS) both run this. Boots
``python -m ne.worker`` as a SUBPROCESS with the CI stub seams, enqueues a
``chat.turn`` from the TEST process via ``DBOSClient`` (the SAME EnqueueOptions
ne-api uses), SIGKILLs the worker mid-step, restarts it, then releases the blocked
turn and asserts:

* the workflow resumes and completes exactly once (``retrieve_workflow(T)`` returns
  once; a re-enqueue of the SAME ``turn_id`` is a no-op);
* the turn step re-executed from the top (start-marker count == 2 — at-least-once
  step / exactly-once completion, the §5.2-1 semantics asserted EXACTLY);
* the frame stream holds exactly ONE ``done`` in a contract-legal order;
* the DBOS admin server is OFF (localhost:3001 connection refused).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
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


def _worker_env(marker_path: str, *, mode: str = "block") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "NE_WORKER_SEAMS": "ne.worker.testing:ci_seams",
            "NE_WORKER_TEST_MODE": mode,
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
    )
    return env


def _wait_marker(path: str, *, timeout_s: float = 90.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 60.0:
            return
        time.sleep(0.5)
    raise AssertionError(f"worker heartbeat marker {path} never appeared")


def _marker_count(kind: str) -> int:
    engine = sa.create_engine(_SESSION_URL)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text("SELECT count(*) FROM ne_worker_test_markers WHERE kind = :k"),
                    {"k": kind},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _insert_release() -> None:
    from ne.worker.testing import _write_marker

    _write_marker(_SESSION_URL, "release")


def _bound_request(turn_id: str) -> tuple[str, str, str]:
    from ne.api.turn_wire import ProtectedTurnRef, TurnRequest
    from ne.contracts.identity import AuthenticatedRequestContext
    from ne.contracts.types import Principal
    from ne.runtime.private_payloads import (
        CONVERSATION_TURN_TARGET_KIND,
        PrivatePayloadDomain,
        ProtectedContentStatus,
        RunPrivatePayloadCipher,
        StaticPrivatePayloadKeys,
        conversation_turn_content_binding,
    )
    from ne.runtime.run_store import AgentRunStore, ensure_run_schema
    from ne.runtime.store import (
        SqlAlchemySessionStore,
        conversation_request_fingerprint,
        ensure_runtime_schema,
    )

    engine = sa.create_engine(_SESSION_URL)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        principal = Principal(id="user:ada", groups=("eng",), claims={"department": "engineering"})
        trace_id = hashlib.sha256(f"live-turn:{turn_id}".encode()).hexdigest()[:32]

        async def _claim() -> tuple[str, str, str]:
            conversation = await store.create_conversation(
                "console-chat", "engineering", principal.id
            )
            fingerprint = conversation_request_fingerprint(
                conversation.conversation_id,
                principal.id,
                "engineering",
                "console-chat",
                "hello",
                None,
                None,
                trace_id,
                principal.groups,
                dict(principal.claims),
            )
            claim = await store.claim_conversation_attachment(
                conversation.conversation_id,
                principal.id,
                "engineering",
                "console-chat",
                turn_id,
                fingerprint,
                trace_id,
            )
            assert claim.disposition == "owned" and claim.attempt_id is not None
            await store.bind_conversation_execution(
                conversation.conversation_id,
                principal.id,
                turn_id,
                claim.attempt_id,
                fingerprint,
            )
            dispatch_id = f"{turn_id}:{claim.attempt_id}"
            await store.mark_conversation_dispatched(
                conversation.conversation_id,
                principal.id,
                turn_id,
                claim.attempt_id,
                dispatch_id,
            )
            runs = AgentRunStore(
                engine,
                RunPrivatePayloadCipher(
                    StaticPrivatePayloadKeys(
                        current_key_id="worker-live-k1",
                        keys={"worker-live-k1": b"k" * 32},
                    )
                ),
            )
            protected = await runs.claim_protected_payload(
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
                plaintext=b"hello",
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

        return asyncio.run(_claim())
    finally:
        engine.dispose()


def _materialize_terminal(request_json: str, protected_json: str) -> str:
    """Open the exact content-free DBOS result through the sanctioned store seam."""
    from ne.api.turn_wire import ProtectedTerminalPayload, ProtectedTurnRef, TurnRequest
    from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
    from ne.runtime.run_store import AgentRunStore

    request = TurnRequest.model_validate_json(request_json)
    protected = ProtectedTurnRef.model_validate_json(protected_json)
    assert protected.content_status == "source_free"
    engine = sa.create_engine(_SESSION_URL)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(
                current_key_id="worker-live-k1",
                keys={"worker-live-k1": b"k" * 32},
            )
        ),
    )
    try:
        payload = asyncio.run(
            runs.materialize_legacy_conversation_turn(
                owner_principal_id=request.principal_id,
                conversation_id=request.session_id,
                attachment_id=request.turn_id,
                attempt_id=request.claimed_attempt_id,
                slot="terminal",
                protected_ref=protected.protected_ref,
                plaintext_hash=protected.protected_hash,
            )
        )
        return ProtectedTerminalPayload.model_validate_json(payload).output
    finally:
        engine.dispose()


def _clear_markers() -> None:
    from ne.worker.testing import ensure_marker_table

    engine = sa.create_engine(_SESSION_URL)
    try:
        ensure_marker_table(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ne_worker_test_markers"))
    finally:
        engine.dispose()


def test_sigkill_mid_step_exactly_once(tmp_path: Path) -> None:
    from dbos import DBOSClient, EnqueueOptions

    from ne.api.turn_relay import normalize_dbos_url
    from ne.api.turn_wire import TurnFrame

    _clear_markers()
    turn_id = f"turn-{uuid.uuid4()}"
    _session_id, dispatch_id, request = _bound_request(turn_id)

    marker_path = str(tmp_path / "worker.health")
    proc = subprocess.Popen([sys.executable, "-m", "ne.worker"], env=_worker_env(marker_path))
    client = DBOSClient(system_database_url=normalize_dbos_url(_WORKER_DBOS_URL))
    options: EnqueueOptions = {
        "queue_name": "chat-turns",
        "workflow_name": "chat.turn",
        "workflow_id": dispatch_id,
    }
    try:
        _wait_marker(marker_path)
        client.enqueue(options, request)

        # Wait for the step to reach the blocking point (start marker), then SIGKILL.
        deadline = time.time() + 60
        while _marker_count("start") < 1 and time.time() < deadline:
            time.sleep(0.2)
        assert _marker_count("start") == 1, "turn step did not reach the blocking point"
        proc.kill()
        proc.wait(timeout=30)

        # Restart the worker → DBOS recovery re-runs the PENDING workflow from the top.
        proc = subprocess.Popen([sys.executable, "-m", "ne.worker"], env=_worker_env(marker_path))
        _wait_marker(marker_path)
        # The re-run re-enters the blocking step (a SECOND start marker).
        deadline = time.time() + 60
        while _marker_count("start") < 2 and time.time() < deadline:
            time.sleep(0.2)
        assert _marker_count("start") == 2, "the recovered turn did not re-execute the step"

        # Release the turn; it completes exactly once.
        _insert_release()
        handle = client.retrieve_workflow(dispatch_id)
        result = handle.get_result()
        assert "done" not in result
        assert _materialize_terminal(request, result) == "done"
        # A re-enqueue of the SAME turn_id is a no-op (workflow-id idempotency).
        client.enqueue(options, request)
        assert client.retrieve_workflow(dispatch_id).get_result() == result
        assert _marker_count("start") == 2  # no extra execution

        # Exactly one done frame, contract-legal order (token then done, monotonic seq).
        frames = [TurnFrame.model_validate(v) for v in client.read_stream(dispatch_id, "frames")]
        assert [f.type for f in frames][-1] == "done"
        assert sum(1 for f in frames if f.type == "done") == 1
        assert [f.seq for f in frames] == sorted(f.seq for f in frames)

        # The DBOS admin server must be OFF (run_admin_server=False).
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 3001), timeout=2)
    finally:
        proc.kill()
        proc.wait(timeout=30)
