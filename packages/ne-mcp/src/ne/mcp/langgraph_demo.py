# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-LG-DROPIN — the platform-side driver for the LangGraph governed drop-in demo.

Runnable as ``python -m ne.mcp.langgraph_demo`` (platform ``.venv``). It:

  1. builds the ISOLATED hash-pinned sidecar venv (``packages/ne-mcp/.langgraph-venv``)
     that carries the customer's langgraph + langchain-mcp-adapters closure — the
     SAME ``ne.envbuild.ensure_sidecar_venv`` mechanism ``ne.eval.ragas_oracle`` uses
     for its ragas venv (mirrored EXACTLY: a monkeypatchable module-level ``_run``,
     full ``--require-hashes`` closure, AIR_GAP fail-closed ``pre_lock_check``);
  2. launches the customer-side LangGraph agent (``langgraph_env/agent.py``) INSIDE
     that venv as a subprocess. The agent's MCP client spawns the governed server
     (``python -m ne.mcp.demo_server``) over stdio, so every tool call is C6-governed;
  3. polls the shared :class:`~ne.runtime.approvals.DurableApprovalGate` over the same
     SQLite store, prints each approval card, and records the human decision with a
     NAMED approver (``--auto`` approves the Tier-2 CRM note and rejects the Tier-3
     ticket update unattended);
  4. prints the C10 audit receipts for the run and verifies the hash chain.

This module imports NOTHING from langgraph/langchain — that closure lives ONLY in
the sidecar venv. It never imports ``mcp`` directly (C3): the governed server is
reached only as a subprocess the agent spawns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

from ne.audit import (
    PostgresAuditSink,
    create_engine_with_timeouts,
    ensure_audit_schema,
)
from ne.audit.store import ne_audit_log
from ne.envbuild import ensure_sidecar_venv, venv_python
from ne.mcp.demo_server import DB_ENV, DEFAULT_PRINCIPAL, PRINCIPAL_ENV
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.runtime import create_approval_gate, freeze_platform_config
from ne.runtime.approvals import DurableApprovalGate, PendingApproval

_PKG = Path("packages") / "ne-mcp"
_LG_ENV_DIR = _PKG / "langgraph_env"
_VENV_DIR = _PKG / ".langgraph-venv"

# The isolated-venv rebuild lock timeout (a cold build of the langgraph closure can
# take a while on a slow link) — mirrors the ragas oracle.
_ENV_LOCK_TIMEOUT_S = 1800

# Overrides for deployments/CI runners that vendor the payload elsewhere.
_ENV_DIR_OVERRIDE_VAR = "NE_LG_ENV_DIR"
_VENV_DIR_OVERRIDE_VAR = "NE_LG_VENV_DIR"

# The env var carrying the JSON-encoded server spawn command to the agent, plus the
# CRM-note tool name (used to route the Tier-2 approval vs the Tier-3 rejection).
_SERVER_CMD_VAR = "NE_LG_SERVER_CMD"
_CRM_NOTE_TOOL = "write.internal.crm.note"
_TICKET_TOOL = "write.external.ticket.update"


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until a directory containing ``uv.lock`` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")


def _resolve_dirs() -> tuple[Path, Path]:
    env_override = os.environ.get(_ENV_DIR_OVERRIDE_VAR)
    venv_override = os.environ.get(_VENV_DIR_OVERRIDE_VAR)
    root = _find_repo_root(Path(__file__))
    env_dir = Path(env_override) if env_override else root / _LG_ENV_DIR
    venv_dir = Path(venv_override) if venv_override else root / _VENV_DIR
    return env_dir, venv_dir


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Module global so tests can monkeypatch it (mirrors ragas_oracle._run)."""
    subprocess.run(cmd, check=True, env=env)


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP ``pre_lock_check`` hook for ``venv_dir`` (C2).

    Mirrors ``ne.eval.ragas_oracle._air_gap_check``: fires AFTER the unlocked
    up-to-date fast-path but BEFORE any mkdir/lock/rmtree/subprocess. Reaching the
    hook means the venv is absent or stale — a build would ``uv pip install`` egress,
    forbidden under AIR_GAP, so fail typed+closed before any side effect.
    """

    def _check() -> None:
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and {name} at {venv_dir} is missing or stale; "
                "runtime uv pip install egress (pypi.org/files.pythonhosted.org) is "
                "forbidden — pre-build the venv at image build/pre-staging time."
            )

    return _check


def ensure_langgraph_env_at(env_dir: Path, venv_dir: Path) -> None:
    """Create/refresh the isolated hash-pinned langgraph demo venv if needed.

    Delegates to :func:`ne.envbuild.ensure_sidecar_venv` with a FULL hash-pinned
    closure install (``--require-hashes``, NO ``--no-deps`` — the ragas shape) and
    the AIR_GAP fail-closed ``pre_lock_check`` hook (C2). An already-current venv is
    a silent no-op; a missing/stale venv under AIR_GAP raises a typed
    :class:`~ne.net.airgap.AirGapViolationError` before any uv egress.
    """
    ensure_sidecar_venv(
        env_dir=env_dir,
        venv_dir=venv_dir,
        install_args=(),
        pre_lock_check=_air_gap_check(venv_dir, "the isolated langgraph demo venv"),
        lock_timeout_s=_ENV_LOCK_TIMEOUT_S,
        name="the isolated langgraph demo venv",
        run=_run,
    )


def ensure_langgraph_env() -> tuple[Path, Path]:
    """Resolve + ensure the default langgraph demo venv; return ``(env_dir, venv_dir)``."""
    env_dir, venv_dir = _resolve_dirs()
    ensure_langgraph_env_at(env_dir, venv_dir)
    return env_dir, venv_dir


# --- the driver flow --------------------------------------------------------


def _print_card(pending: PendingApproval) -> None:
    print(
        "\n"
        "  ┌─ APPROVAL REQUIRED ──────────────────────────────────────\n"
        f"  │ action:      {pending.action}\n"
        f"  │ tier:        {pending.tier}\n"
        f"  │ principal:   {pending.principal_id}\n"
        f"  │ requested:   {pending.requested_at.isoformat()}\n"
        f"  │ request_id:  {pending.request_id}\n"
        "  └──────────────────────────────────────────────────────────",
        flush=True,
    )


async def _decide(
    gate: DurableApprovalGate,
    pending: PendingApproval,
    *,
    approver: str,
    auto: bool,
) -> None:
    """Record a NAMED human decision for one pending request.

    The Tier-2 CRM note is APPROVED (the write executes only after this); the Tier-3
    ticket update is REJECTED (zero effects). In interactive mode the operator is
    prompted; ``--auto`` applies the scripted decision unattended.
    """
    _print_card(pending)
    if pending.action == _CRM_NOTE_TOOL:
        approve = True if auto else _prompt_yes_no(f"approve {pending.action}?")
    elif pending.action == _TICKET_TOOL:
        approve = False if auto else _prompt_yes_no(f"approve {pending.action}?")
    else:
        approve = False if auto else _prompt_yes_no(f"approve {pending.action}?")

    if approve:
        await gate.approve(pending.request_id, approver=approver)
        print(f"  → APPROVED by {approver}\n", flush=True)
    else:
        await gate.reject(pending.request_id, approver=approver, reason="demo rejection")
        print(f"  → REJECTED by {approver}\n", flush=True)


def _prompt_yes_no(question: str) -> bool:
    try:
        answer = input(f"  {question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


async def _approval_loop(
    gate: DurableApprovalGate,
    proc: asyncio.subprocess.Process,
    *,
    approver: str,
    auto: bool,
) -> None:
    decided: set[str] = set()
    while proc.returncode is None:
        for pending in await gate.list_pending():
            if pending.request_id in decided:
                continue
            decided.add(pending.request_id)
            await _decide(gate, pending, approver=approver, auto=auto)
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.25)
        except TimeoutError:
            continue


def _print_receipts(engine: sa.Engine) -> None:
    print("\n=== C10 audit receipts (this run) ===", flush=True)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                ne_audit_log.c.seq,
                ne_audit_log.c.action,
                ne_audit_log.c.outcome,
                ne_audit_log.c.actor,
                ne_audit_log.c.resource,
                ne_audit_log.c.trace_id,
            ).order_by(ne_audit_log.c.seq)
        ).all()
    for r in rows:
        print(
            f"  seq={r.seq:>3}  {r.action or ''!s:<22} "
            f"{r.outcome or ''!s:<8} actor={r.actor or ''!s:<18} "
            f"{r.resource or ''!s}  trace={r.trace_id or '-'}",
            flush=True,
        )


async def _amain(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Freeze the composed platform config ONCE before any event-emitting object.
    freeze_platform_config()

    print("Building the isolated langgraph sidecar venv (first run only)…", flush=True)
    env_dir, venv_dir = ensure_langgraph_env()
    agent_path = env_dir / "agent.py"
    python = venv_python(venv_dir)

    # Driver-side durable stores over the SAME sqlite the server writes to.
    engine = create_engine_with_timeouts(f"sqlite:///{db_path}")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    gate = create_approval_gate(sink=sink, engine=engine)

    server_cmd = json.dumps([sys.executable, "-m", "ne.mcp.demo_server"])
    child_env = {
        **os.environ,
        _SERVER_CMD_VAR: server_cmd,
        DB_ENV: str(db_path),
        PRINCIPAL_ENV: os.environ.get(PRINCIPAL_ENV, DEFAULT_PRINCIPAL),
        "NE_GOVERN_OPA_URL": os.environ.get("NE_GOVERN_OPA_URL", "http://localhost:8181"),
    }
    agent_argv = [str(python), str(agent_path)]
    if args.model:
        agent_argv.append("--model")

    print(f"Launching the customer LangGraph agent in {venv_dir}…\n", flush=True)
    proc = await asyncio.create_subprocess_exec(*agent_argv, env=child_env)

    await _approval_loop(gate, proc, approver=args.approver, auto=args.auto)
    await proc.wait()

    _print_receipts(engine)

    verification = await sink.verify()
    print(
        f"\nC10 chain verification: valid={verification.valid} "
        f"(checked {verification.checked} records)",
        flush=True,
    )
    if not verification.valid:
        print("FAIL: audit chain did not verify", file=sys.stderr, flush=True)
        return 1
    if proc.returncode != 0:
        print(
            f"FAIL: the agent exited non-zero ({proc.returncode}); its scripted "
            "governance checks did not all hold",
            file=sys.stderr,
            flush=True,
        )
        return proc.returncode or 1
    print("\nDemo complete: governed, approved, audited, chain-verified. ✔", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangGraph governed drop-in demo (NE-LG-DROPIN).")
    parser.add_argument(
        "--db",
        default=str(Path(tempfile.mkdtemp(prefix="ne-lg-demo-")) / "lg-demo.db"),
        help="Path to the shared sqlite store (audit + approvals). Default: a fresh tempdir.",
    )
    parser.add_argument(
        "--approver",
        default="demo-operator@corp.example",
        help="The NAMED approver id recorded on the Tier-2 approval / Tier-3 rejection.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Unattended: auto-approve the Tier-2 CRM note, auto-reject the Tier-3 ticket.",
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="Drive the agent with a react-style LLM against a LOCAL OpenAI-compatible "
        "endpoint (NE_LG_MODEL_BASE_URL). Owner/demo-rig only; never CI; no frontier keys.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
