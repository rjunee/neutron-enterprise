# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE flagship test (spec §5.3-4): HITL through HTTP end-to-end.

A governed chat turn whose tier-2 ``write.external.*`` tool call is routed to
``require-approval`` blocks INSIDE the REAL ``DurableApprovalGate``; the pending
row is listable over ``GET /v1/approvals/pending``; and an HTTP ``approve`` by a
DISTINCT approver principal (empty body) RELEASES the turn — the SSE ``done`` frame
arrives carrying the post-approval output. Two AsyncClient instances over the same
app drive the concurrent SSE read + approve (mirrors test_sse.py's streaming
pattern).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.config import ApprovalSettings
from ne.runtime.tools import ToolBinding

from .conftest import AppBuilder, parse_sse, run, text_completion, tool_call_completion

_ACTION = "write.external.demo.send"


async def _demo_handler(_args: Mapping[str, object]) -> str:
    return "ok"


def _binding() -> ToolBinding:
    return ToolBinding(
        spec=ToolSpec(
            name=_ACTION,
            description="demo",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        ),
        handler=_demo_handler,
    )


def test_hitl_through_http_releases_the_turn(make_app: AppBuilder) -> None:
    h = make_app(
        approver_roles=("approvers",),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        extra_tool_bindings=(_binding(),),
        extra_tool_policies={
            _ACTION: ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.text, required=True)})
        },
        completions=[
            tool_call_completion(_ACTION, {"to": "ops"}),
            text_completion("sent"),
        ],
    )

    async def _t() -> None:
        loop = asyncio.get_running_loop()
        ada = h.bearer("user:ada", groups=["eng"], department="engineering")
        appr = h.bearer("user:approver", groups=["approvers"])
        async with h.client() as writer, h.client() as approver:
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]

            # Start the SSE turn concurrently — it blocks in the gate until approved.
            task = asyncio.create_task(
                writer.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={"prompt": "send it"},
                    headers=ada,
                )
            )

            # Poll the pending queue (bounded) until the tier-2 row appears.
            deadline = loop.time() + 10.0
            pending = None
            while loop.time() < deadline:
                r = await approver.get("/api/v1/approvals/pending", headers=appr)
                assert r.status_code == 200
                match = [x for x in r.json()["approvals"] if x["action"] == _ACTION]
                if match:
                    pending = match[0]
                    break
                await asyncio.sleep(0.05)
            assert pending is not None, "the tier-2 approval never appeared in the queue"
            # The pending row's principal is the turn's VERIFIED bearer.
            assert pending["principal_id"] == "user:ada"

            # Owner ruling 2026-07-31: the approver reads WHAT they are being
            # asked to approve, over HTTP, BEFORE deciding — the disclosure
            # chain (middleware -> store -> gate -> API) proven end-to-end on
            # the REAL spine, not a seeded fixture.
            payload_resp = await approver.get(
                f"/api/v1/approvals/{pending['request_id']}/payload", headers=appr
            )
            assert payload_resp.status_code == 200
            payload_body = payload_resp.json()
            assert payload_body["material_parameters"] == {"to": "ops"}
            assert payload_body["target_resource"] == f"tool:{_ACTION}"
            assert "external-facing" in payload_body["reversibility"]

            # Approve over HTTP with an EMPTY body by the DISTINCT approver.
            resp = await approver.post(
                f"/api/v1/approvals/{pending['request_id']}/approve", json={}, headers=appr
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "approved"
            assert body["approver"] == "user:approver"

            # The approval RELEASES the turn — the done frame carries 'sent'.
            sse = await task
            assert sse.status_code == 200
            parsed = parse_sse(sse.text)
            done = parsed.of_type("done")
            assert done, "no done frame — the turn was not released by the approval"
            assert done[0].data is not None
            assert done[0].data["output"] == "sent"

    run(_t())
