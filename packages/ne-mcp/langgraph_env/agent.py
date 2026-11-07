# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""NE-LG-DROPIN — the CUSTOMER-SIDE LangGraph agent (runs ONLY in .langgraph-venv).

This file is NEVER imported by platform code or tests. It runs inside the isolated
hash-pinned sidecar venv and imports ONLY langgraph / langchain_mcp_adapters /
langchain_core (+ langchain_openai under ``--model``) + stdlib — NEVER ``ne.*``,
NEVER bare ``mcp``. Its ENTIRE enterprise toolset is the platform's governed MCP
tools, served by ``ne.mcp.demo_server`` over stdio, so every tool call it makes is
C6-governed by construction: there is no ungoverned tool for it to reach.

The customer's MCP client (``MultiServerMCPClient``) SPAWNS the governed server per
the MCP stdio convention — the server command + shared-state env arrive via
``NE_LG_SERVER_CMD`` / ``NE_LG_DEMO_DB`` / ``NE_GOVERN_OPA_URL``.

Deterministic mode (default; what the live test runs — NO LLM, zero egress) drives a
REAL ``StateGraph`` through a scripted multi-step task and enforces its OWN success
contract:

    exit 0  IFF  (1) read.kb.search succeeded
             AND (2) write.internal.crm.note succeeded (only after a NAMED approval)
             AND (3) the unregistered shell.exec came back isError (audited-denied)
             AND (4) the Tier-3 write.external.ticket.update was REJECTED (zero effect)
    else exit 1 with a stderr diagnosis.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from typing import TypedDict

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph

_SERVER_CMD_VAR = "NE_LG_SERVER_CMD"
_DB_VAR = "NE_LG_DEMO_DB"
_SERVER_NAME = "neutron"

_KB_SEARCH = "read.kb.search"
_CRM_NOTE = "write.internal.crm.note"
_TICKET_UPDATE = "write.external.ticket.update"
_UNREGISTERED = "shell.exec"


def _build_client() -> MultiServerMCPClient:
    raw = os.environ.get(_SERVER_CMD_VAR)
    if not raw:
        raise SystemExit(
            f"{_SERVER_CMD_VAR} is required (JSON list: [python, -m, ne.mcp.demo_server])"
        )
    cmd = json.loads(raw)
    db = os.environ.get(_DB_VAR)
    if not db:
        raise SystemExit(f"{_DB_VAR} is required (path to the shared sqlite store)")
    server_env = {
        _DB_VAR: db,
        "NE_GOVERN_OPA_URL": os.environ.get("NE_GOVERN_OPA_URL", "http://localhost:8181"),
        "NE_LG_DEMO_PRINCIPAL": os.environ.get("NE_LG_DEMO_PRINCIPAL", "user:lg-demo"),
    }
    return MultiServerMCPClient(
        {
            _SERVER_NAME: {
                "command": cmd[0],
                "args": list(cmd[1:]),
                "transport": "stdio",
                "env": server_env,
                # The HITL block on the Tier-2 write must not trip a client read
                # timeout (approvals land in <2s in the live test; 300s is a generous
                # ceiling). read_timeout_seconds defaults to None/no-timeout anyway.
                "session_kwargs": {"read_timeout_seconds": timedelta(seconds=300)},
            }
        },
        # LOAD-BEARING for the governance beats: with the default (True) a governed
        # isError (an unapproved/rejected/denied call) is CAUGHT and returned as a
        # STRING, so a rejected Tier-3 write would look like a success to the agent.
        # False lets the ToolException PROPAGATE, so a governed refusal is an
        # exception the scripted contract can detect.
        handle_tool_errors=False,
    )


class _State(TypedDict, total=False):
    research_text: str
    research_ok: bool
    write_ok: bool
    rogue_unknown_blocked: bool
    rogue_tier3_blocked: bool
    diagnosis: list[str]


def _run_deterministic() -> int:
    client = _build_client()

    async def scenario() -> _State:
        tools = await client.get_tools()
        by_name = {t.name: t for t in tools}
        # Machine-readable line the live test parses: the discovered toolset ==
        # the C7 grant (exactly 3), proven across the venv/process boundary.
        print(f"NE_LG_TOOLS={len(tools)}", flush=True)

        async def research(state: _State) -> _State:
            diag = list(state.get("diagnosis", []))
            try:
                text = await by_name[_KB_SEARCH].ainvoke({"query": "acme corp account history"})
                return {"research_text": str(text), "research_ok": True, "diagnosis": diag}
            except Exception as exc:
                diag.append(f"research (read.kb.search) failed unexpectedly: {exc!r}")
                return {"research_text": "", "research_ok": False, "diagnosis": diag}

        async def writeback(state: _State) -> _State:
            diag = list(state.get("diagnosis", []))
            summary = (state.get("research_text") or "no research")[:3000]
            body = f"Automated account research summary.\n\n{summary}"
            try:
                # BLOCKS on HITL — resolves only after the driver records a NAMED approval.
                await by_name[_CRM_NOTE].ainvoke(
                    {
                        "request_id": "lg-demo-note-1",
                        "record_id": "001A00000LgDemo01",
                        "title": "Account research summary",
                        "body": body,
                    }
                )
                return {"write_ok": True, "diagnosis": diag}
            except Exception as exc:
                diag.append(f"write.internal.crm.note failed after approval: {exc!r}")
                return {"write_ok": False, "diagnosis": diag}

        async def rogue_unknown(state: _State) -> _State:
            diag = list(state.get("diagnosis", []))
            # Raw session call of an UNREGISTERED tool — still the governed server
            # (adapters API, never bare mcp). It MUST come back isError (audited-denied).
            try:
                async with client.session(_SERVER_NAME) as session:
                    result = await session.call_tool(_UNREGISTERED, {"cmd": "cat /etc/passwd"})
                blocked = bool(getattr(result, "isError", False))
                if not blocked:
                    diag.append("shell.exec (unregistered) was NOT rejected — governance bypass!")
                return {"rogue_unknown_blocked": blocked, "diagnosis": diag}
            except Exception:
                return {"rogue_unknown_blocked": True, "diagnosis": diag}

        async def rogue_tier3(state: _State) -> _State:
            diag = list(state.get("diagnosis", []))
            # The Tier-3 ticket update — the operator REJECTS it, so it MUST fail
            # with zero effect (adapters surface the isError as an exception).
            try:
                await by_name[_TICKET_UPDATE].ainvoke(
                    {
                        "request_id": "lg-demo-ticket-1",
                        "ticket_id": 1,
                        "status": "resolved",
                        "public_reply": "auto-close attempt",
                    }
                )
                diag.append("write.external.ticket.update SUCCEEDED despite rejection!")
                return {"rogue_tier3_blocked": False, "diagnosis": diag}
            except Exception:
                return {"rogue_tier3_blocked": True, "diagnosis": diag}

        graph: StateGraph = StateGraph(_State)
        graph.add_node("research", research)
        graph.add_node("writeback", writeback)
        graph.add_node("rogue_unknown", rogue_unknown)
        graph.add_node("rogue_tier3", rogue_tier3)
        graph.add_edge(START, "research")
        graph.add_edge("research", "writeback")
        graph.add_edge("writeback", "rogue_unknown")
        graph.add_edge("rogue_unknown", "rogue_tier3")
        graph.add_edge("rogue_tier3", END)
        app = graph.compile()
        return await app.ainvoke({"diagnosis": []})  # type: ignore[return-value]

    final: _State = asyncio.run(scenario())

    research_ok = bool(final.get("research_ok"))
    write_ok = bool(final.get("write_ok"))
    rogue_unknown_blocked = bool(final.get("rogue_unknown_blocked"))
    rogue_tier3_blocked = bool(final.get("rogue_tier3_blocked"))

    print(
        "agent scorecard: "
        f"research_ok={research_ok} write_ok={write_ok} "
        f"shell_exec_blocked={rogue_unknown_blocked} tier3_rejected={rogue_tier3_blocked}",
        flush=True,
    )
    passed = research_ok and write_ok and rogue_unknown_blocked and rogue_tier3_blocked
    if not passed:
        for line in final.get("diagnosis", []) or ["governance contract not satisfied"]:
            print(f"  DIAGNOSIS: {line}", file=sys.stderr, flush=True)
        return 1
    return 0


def _run_model() -> int:
    """The `--model` "wow" path: a react-style agent over the SAME governed tools.

    LOCAL OpenAI-compatible endpoint only (NE_LG_MODEL_BASE_URL) — never CI, never a
    frontier key. Not part of the deterministic contract; a manual demo-rig check.
    """
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    client = _build_client()

    async def scenario() -> None:
        tools = await client.get_tools()
        print(f"NE_LG_TOOLS={len(tools)}", flush=True)
        base_url = os.environ.get("NE_LG_MODEL_BASE_URL")
        if not base_url:
            raise SystemExit(
                "--model requires NE_LG_MODEL_BASE_URL (a LOCAL OpenAI-compatible endpoint)"
            )
        model = ChatOpenAI(
            base_url=base_url,
            api_key=os.environ.get("NE_LG_MODEL_API_KEY", "local"),
            model=os.environ.get("NE_LG_MODEL_NAME", "local"),
        )
        agent = create_react_agent(model, tools)
        result = await agent.ainvoke(
            {
                "messages": [
                    (
                        "user",
                        "Research the Acme Corp account, then file a CRM note "
                        "summarizing what you found.",
                    )
                ]
            }
        )
        for message in result["messages"]:
            print(getattr(message, "content", message), flush=True)

    asyncio.run(scenario())
    return 0


def main() -> int:
    if "--model" in sys.argv[1:]:
        return _run_model()
    return _run_deterministic()


if __name__ == "__main__":
    raise SystemExit(main())
