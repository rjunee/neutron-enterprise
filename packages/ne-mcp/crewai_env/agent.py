# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""NE-CREWAI-DROPIN — the CUSTOMER-SIDE CrewAI agent (runs ONLY in .crewai-venv).

This file is NEVER imported by platform code or tests. It runs inside the isolated
hash-pinned sidecar venv and imports ONLY crewai / mcp / pydantic + stdlib — NEVER
``ne.*``. Its ENTIRE enterprise toolset is the platform's governed MCP tools, served
by ``ne.mcp.demo_server`` over stdio, so every tool call it makes is C6-governed by
construction: there is no ungoverned enterprise tool for it to reach.

The customer's DIRECT mcp-SDK client SPAWNS the governed server per the MCP stdio
convention — the server command + shared-state env arrive via ``NE_CREWAI_SERVER_CMD``
/ ``NE_LG_DEMO_DB`` / ``NE_GOVERN_OPA_URL``. (``NE_LG_DEMO_DB`` is the SHARED
``ne.mcp.demo_server`` env contract, reused byte-unchanged by the CrewAI driver; the
string is duplicated here because ``ne.*`` must not be imported in this venv.)

We build CrewAI ``BaseTool`` proxies against the mcp SDK DIRECTLY. crewai-tools'
``MCPServerAdapter`` is NOT used — crewai-tools ships pymupdf (AGPL-3.0) in its base
deps and is barred from the sidecar; the direct proxy needs nothing crewai-tools has.

WHY deterministic mode scripts ``BaseTool.run()`` instead of ``Crew.kickoff()``:
CrewAI has NO LLM-free agent loop and NO public step-pump (``Crew.kickoff`` is a single
run-to-completion call that requires a live model — ne-rtswap-design §7 fact 4). So the
hermetic, zero-egress CI path drives the REAL crewai ``BaseTool`` proxy objects
directly with scripted inputs. The governance claim is UNAFFECTED: the proxies' ONLY
capability is the governed MCP pipe, so whatever CrewAI's dispatch layer would do around
them (its own tool-hook gaps included) is on the OTHER side of the process boundary and
cannot reach an enterprise effect except through the C6 chokepoint. ``--model`` (manual
demo-rig only) drives a real ``Crew.kickoff`` over the SAME governed toolset.

Deterministic mode (default; what the live test runs — NO LLM, zero egress) enforces its
OWN success contract:

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
import threading
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

_SERVER_CMD_VAR = "NE_CREWAI_SERVER_CMD"
# The SHARED ne.mcp.demo_server env contract (string duplicated — ne.* is barred here).
_DB_VAR = "NE_LG_DEMO_DB"

_KB_SEARCH = "read.kb.search"
_CRM_NOTE = "write.internal.crm.note"
_TICKET_UPDATE = "write.external.ticket.update"
_UNREGISTERED = "shell.exec"

# JSON-schema-type -> python-type map for the generated pydantic args_schema.
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

# The read timeout is LOAD-BEARING: the Tier-2 crm.note call blocks server-side on a
# human approval, which must not trip a client read timeout (approvals land in <2s in
# the live test; 300s is a generous ceiling).
_READ_TIMEOUT = timedelta(seconds=300)
# The synchronous call_tool future ceiling (a hair above the read timeout).
_CALL_RESULT_TIMEOUT_S = 320.0

# Module-level bridge the proxy tools' _run() reaches (set by _run_* before any proxy
# is invoked; the proxies' ONLY capability is this governed pipe).
_BRIDGE: _McpBridge | None = None


class _McpBridge:
    """A sync facade over the async mcp stdio client, run in a background thread.

    CrewAI's ``BaseTool.run`` is synchronous, so the async ``ClientSession`` lives in a
    dedicated event loop on a background thread; ``call_tool`` marshals across via
    ``run_coroutine_threadsafe``. ``start()`` blocks until the session has initialised
    and the toolset is discovered (or re-raises the startup exception).
    """

    def __init__(self, cmd: list[str], server_env: dict[str, str]) -> None:
        self._cmd = cmd
        self._server_env = server_env
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_exc: BaseException | None = None
        self.tools: list[Any] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._startup_exc is not None:
            raise self._startup_exc

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._amain())
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_exc = exc
                self._ready.set()

    async def _amain(self) -> None:
        params = StdioServerParameters(
            command=self._cmd[0],
            args=list(self._cmd[1:]),
            env={**get_default_environment(), **self._server_env},
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT) as session,
        ):
            await session.initialize()
            self._session = session
            self._loop = asyncio.get_running_loop()
            self.tools = list((await session.list_tools()).tools)
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._session is None or self._loop is None:
            raise RuntimeError("bridge is not started")
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        return future.result(timeout=_CALL_RESULT_TIMEOUT_S)

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=30)


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts) if parts else repr(result)


def _build_args_schema(mcp_tool: Any) -> type:
    from pydantic import create_model

    schema: dict[str, Any] = getattr(mcp_tool, "inputSchema", None) or {}
    props: dict[str, Any] = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    fields: dict[str, Any] = {}
    for pname, pspec in props.items():
        ptype = (pspec or {}).get("type") if isinstance(pspec, dict) else None
        py: type = _TYPE_MAP.get(ptype, object) if isinstance(ptype, str) else object
        if pname in required:
            fields[pname] = (py, ...)
        else:
            fields[pname] = (py | None, None)
    model_name = f"{str(mcp_tool.name).replace('.', '_')}_Args"
    return create_model(model_name, **fields)


def _make_proxy_tool_class() -> type:
    """Build the CrewAI ``BaseTool`` proxy class (crewai imported LAZILY)."""
    from crewai.tools import BaseTool

    class GovernedMcpProxyTool(BaseTool):
        def _run(self, **kwargs: Any) -> str:
            # Drop optional args the caller did not supply (args_schema model_dump fills
            # them as None) so the governed arg guardrail sees exactly what was passed.
            args = {k: v for k, v in kwargs.items() if v is not None}
            if _BRIDGE is None:
                raise RuntimeError("no MCP bridge is active")
            result = _BRIDGE.call_tool(self.name, args)
            # LOAD-BEARING (the mirror of the LG agent's handle_tool_errors=False): a
            # governed refusal (unapproved / rejected / denied) comes back isError and
            # MUST surface as an exception, never a success-looking string.
            if bool(getattr(result, "isError", False)):
                raise RuntimeError(_result_text(result))
            return _result_text(result)

    return GovernedMcpProxyTool


def _build_proxy(tool_cls: type, mcp_tool: Any) -> Any:
    return tool_cls(
        name=str(mcp_tool.name),
        description=str(getattr(mcp_tool, "description", None) or mcp_tool.name),
        args_schema=_build_args_schema(mcp_tool),
    )


def _server_spec() -> tuple[list[str], dict[str, str]]:
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
        "NE_LG_DEMO_PRINCIPAL": os.environ.get("NE_LG_DEMO_PRINCIPAL", "user:crewai-demo"),
    }
    return cmd, server_env


def _run_deterministic() -> int:
    global _BRIDGE
    cmd, server_env = _server_spec()
    bridge = _McpBridge(cmd, server_env)
    bridge.start()
    _BRIDGE = bridge

    diagnosis: list[str] = []
    research_ok = False
    write_ok = False
    rogue_unknown_blocked = False
    rogue_tier3_blocked = False
    try:
        # Machine-readable line the live test parses: the discovered toolset == the C7
        # grant (exactly 3), proven across the venv/process boundary.
        print(f"NE_CREWAI_TOOLS={len(bridge.tools)}", flush=True)
        tool_cls = _make_proxy_tool_class()
        proxies = {str(t.name): _build_proxy(tool_cls, t) for t in bridge.tools}

        # Beat 1 — research (Tier-0 read; executes, audited).
        research_text = ""
        try:
            research_text = str(proxies[_KB_SEARCH].run(query="acme corp account history"))
            research_ok = True
        except Exception as exc:
            diagnosis.append(f"research (read.kb.search) failed unexpectedly: {exc!r}")

        # Beat 2 — writeback (Tier-2; BLOCKS on HITL, resolves after a NAMED approval).
        body = f"Automated account research summary.\n\n{research_text[:3000]}"
        try:
            proxies[_CRM_NOTE].run(
                request_id="crewai-demo-note-1",
                record_id="001A00000LgDemo01",
                title="Account research summary",
                body=body,
            )
            write_ok = True
        except Exception as exc:
            diagnosis.append(f"write.internal.crm.note failed after approval: {exc!r}")

        # Beat 3 — rogue unknown: a raw call of an UNREGISTERED tool (still the governed
        # server). It MUST come back isError (audited-denied at the toolset boundary).
        try:
            result = bridge.call_tool(_UNREGISTERED, {"cmd": "cat /etc/passwd"})
            rogue_unknown_blocked = bool(getattr(result, "isError", False))
            if not rogue_unknown_blocked:
                diagnosis.append("shell.exec (unregistered) was NOT rejected — governance bypass!")
        except Exception:
            rogue_unknown_blocked = True

        # Beat 4 — rogue Tier-3: the operator REJECTS it, so the proxy MUST raise (zero
        # effect). A success here would be a governance failure.
        try:
            proxies[_TICKET_UPDATE].run(
                request_id="crewai-demo-ticket-1",
                ticket_id=1,
                status="resolved",
                public_reply="auto-close attempt",
            )
            diagnosis.append("write.external.ticket.update SUCCEEDED despite rejection!")
        except Exception:
            rogue_tier3_blocked = True
    finally:
        bridge.close()

    print(
        "agent scorecard: "
        f"research_ok={research_ok} write_ok={write_ok} "
        f"shell_exec_blocked={rogue_unknown_blocked} tier3_rejected={rogue_tier3_blocked}",
        flush=True,
    )
    passed = research_ok and write_ok and rogue_unknown_blocked and rogue_tier3_blocked
    if not passed:
        for line in diagnosis or ["governance contract not satisfied"]:
            print(f"  DIAGNOSIS: {line}", file=sys.stderr, flush=True)
        return 1
    return 0


def _run_model() -> int:
    """The `--model` "wow" path: a real ``Crew.kickoff`` over the SAME governed tools.

    LOCAL OpenAI-compatible endpoint only (NE_CREWAI_MODEL_BASE_URL) — never CI, never a
    frontier key. Not part of the deterministic contract; a manual demo-rig check.
    """
    global _BRIDGE
    from crewai import LLM, Agent, Crew, Task

    cmd, server_env = _server_spec()
    bridge = _McpBridge(cmd, server_env)
    bridge.start()
    _BRIDGE = bridge
    try:
        print(f"NE_CREWAI_TOOLS={len(bridge.tools)}", flush=True)
        base_url = os.environ.get("NE_CREWAI_MODEL_BASE_URL")
        if not base_url:
            raise SystemExit(
                "--model requires NE_CREWAI_MODEL_BASE_URL (a LOCAL OpenAI-compatible endpoint)"
            )
        llm = LLM(
            model=os.environ.get("NE_CREWAI_MODEL_NAME", "openai/local"),
            base_url=base_url,
            api_key=os.environ.get("NE_CREWAI_MODEL_API_KEY", "local"),
        )
        tool_cls = _make_proxy_tool_class()
        proxies = [_build_proxy(tool_cls, t) for t in bridge.tools]
        agent = Agent(
            role="Account researcher",
            goal="Research an account, then file a governed CRM note about it.",
            backstory="You act only through Neutron's governed tools.",
            tools=proxies,
            llm=llm,
        )
        task = Task(
            description=(
                "Research the Acme Corp account, then file a CRM note summarizing what you found."
            ),
            expected_output="A confirmation that the governed CRM note was filed.",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task])
        result = crew.kickoff()
        print(result, flush=True)
    finally:
        bridge.close()
    return 0


def main() -> int:
    if "--model" in sys.argv[1:]:
        return _run_model()
    return _run_deterministic()


if __name__ == "__main__":
    raise SystemExit(main())
