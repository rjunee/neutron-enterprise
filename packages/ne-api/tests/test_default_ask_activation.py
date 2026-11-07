# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from ne.api.app import _dispatch_pending_agent_runs_once
from ne.api.routers import chat
from ne.api.settings import ApiSettings
from ne.api.turn_relay import DbosAgentRunEnqueuer


def test_default_ask_generation_is_exact_and_default_off() -> None:
    field = ApiSettings.model_fields["default_ask_generation"]
    assert field.default == ""
    assert "default-ask/canonical-run/v1" in str(field.annotation)


def test_canonical_chat_branch_precedes_every_legacy_attachment_writer() -> None:
    source = inspect.getsource(chat.send_message)
    branch = source.index("if state.default_ask_submission is not None")
    assert branch < source.index("claim_conversation_attachment")
    assert branch < source.index("bind_conversation_execution")
    legacy_start = source.index("request_fingerprint = conversation_request_fingerprint")
    assert "claim_conversation_attachment" not in source[branch:legacy_start]
    assert "Never delete or fall back" in source


def test_agent_run_dbos_identity_is_the_durable_dispatch_id() -> None:
    source = inspect.getsource(DbosAgentRunEnqueuer.enqueue)
    assert '"workflow_id": value.dispatch_id' in source
    assert '"workflow_name": "agent.run"' in source
    resume_source = inspect.getsource(DbosAgentRunEnqueuer.enqueue_approval_resume)
    assert '"workflow_id": value.resume_id' in resume_source
    assert '"workflow_name": "agent.run.approval.resume"' in resume_source


def test_dispatch_reconciler_reenqueues_exact_persisted_workflow_ids() -> None:
    run_request = SimpleNamespace(dispatch_id="dispatch-1")
    resume_request = SimpleNamespace(resume_id="resume-1")

    class Store:
        async def load_pending_workflow_requests(self) -> tuple[object, ...]:
            return (run_request,)

    class Checkpoints:
        async def load_pending_resume_requests(self) -> tuple[object, ...]:
            return (resume_request,)

    class Enqueuer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def enqueue(self, request: object) -> None:
            self.calls.append(("run", request))

        async def enqueue_approval_resume(self, request: object) -> None:
            self.calls.append(("resume", request))

    enqueuer = Enqueuer()
    count = asyncio.run(
        _dispatch_pending_agent_runs_once(
            run_store=Store(),
            enqueuer=enqueuer,  # type: ignore[arg-type]
            approval_checkpoints=Checkpoints(),
        )
    )
    assert count == 2
    assert enqueuer.calls == [("run", run_request), ("resume", resume_request)]
