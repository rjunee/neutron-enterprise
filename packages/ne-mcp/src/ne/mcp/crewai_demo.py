# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CREWAI-DROPIN — the platform-side driver for the CrewAI governed drop-in demo.

Runnable as ``python -m ne.mcp.crewai_demo`` (platform ``.venv``). It is the SECOND
governed out-of-process drop-in (NE-RTSWAP re-scoped phase (b)) and mirrors
``ne.mcp.langgraph_demo`` file-for-file — the ONLY substantive difference is the
sidecar payload it builds (``crewai_env/`` -> ``.crewai-venv``). It:

  1. builds the ISOLATED hash-pinned sidecar venv (``packages/ne-mcp/.crewai-venv``)
     that carries the customer's crewai closure (chromadb -> posthog, lancedb,
     instructor, mismatched pydantic pin — ALL of it, none of it ever in the platform
     lock) — the SAME ``ne.envbuild.ensure_sidecar_venv`` mechanism the ragas and
     langgraph sidecars use (a monkeypatchable module-level ``_run``, a full
     ``--require-hashes`` closure, an AIR_GAP fail-closed ``pre_lock_check``);
  2. launches the customer-side CrewAI agent (``crewai_env/agent.py``) INSIDE that
     venv as a subprocess. The agent's DIRECT mcp-SDK client spawns the governed
     server (``python -m ne.mcp.demo_server``) over stdio, so every tool call is
     C6-governed;
  3. polls the shared :class:`~ne.runtime.approvals.DurableApprovalGate` over the same
     SQLite store, prints each approval card, and records the human decision with a
     NAMED approver (``--auto`` approves the Tier-2 CRM note and rejects the Tier-3
     ticket update unattended);
  4. prints the C10 audit receipts for the run and verifies the hash chain.

REUSE (DECIDED, no new governance code): the governed composition root is
``ne.mcp.demo_server``, REUSED BYTE-UNCHANGED — it is already framework-agnostic,
parameterised by its ``NE_LG_DEMO_DB`` / ``NE_LG_DEMO_PRINCIPAL`` env contract. This
CrewAI driver adopts that shared env contract verbatim (renaming those vars or the
``neutron-lg-dropin-demo`` server_name would touch merged NE-LG-DROPIN surface, out of
scope). Audit rows are distinguished by the principal ``user:crewai-demo``.

This module imports NOTHING from crewai — that closure lives ONLY in the sidecar venv.
It never imports ``mcp`` directly (C3): the governed server is reached only as a
subprocess the agent spawns.
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
from ne.mcp.demo_server import DB_ENV, PRINCIPAL_ENV
from ne.net.airgap import AirGapViolationError, air_gap_engaged
from ne.runtime import create_approval_gate, freeze_platform_config
from ne.runtime.approvals import DurableApprovalGate, PendingApproval

_PKG = Path("packages") / "ne-mcp"
_CREWAI_ENV_DIR = _PKG / "crewai_env"
_VENV_DIR = _PKG / ".crewai-venv"

# The isolated-venv rebuild lock timeout. A cold build of the crewai closure is HEAVY
# (chromadb pulls onnxruntime/grpcio/kubernetes; crewai-cli vendors a ~35 MB uv binary
# + textual), so a first build downloads hundreds of MB — the 1800s ceiling covers it.
_ENV_LOCK_TIMEOUT_S = 1800

# Overrides for deployments/CI runners that vendor the payload elsewhere.
_ENV_DIR_OVERRIDE_VAR = "NE_CREWAI_ENV_DIR"
_VENV_DIR_OVERRIDE_VAR = "NE_CREWAI_VENV_DIR"

# The env var carrying the JSON-encoded server spawn command to the agent, plus the
# CRM-note tool name (used to route the Tier-2 approval vs the Tier-3 rejection).
_SERVER_CMD_VAR = "NE_CREWAI_SERVER_CMD"
_CRM_NOTE_TOOL = "write.internal.crm.note"
_TICKET_TOOL = "write.external.ticket.update"

# The default demo principal (distinguishes CrewAI audit rows from the LangGraph demo's
# user:lg-demo without renaming the shared demo_server env contract).
_CREWAI_PRINCIPAL = "user:crewai-demo"


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
    env_dir = Path(env_override) if env_override else root / _CREWAI_ENV_DIR
    venv_dir = Path(venv_override) if venv_override else root / _VENV_DIR
    return env_dir, venv_dir


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Module global so tests can monkeypatch it (mirrors langgraph_demo._run)."""
    subprocess.run(cmd, check=True, env=env)


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP ``pre_lock_check`` hook for ``venv_dir`` (C2).

    Mirrors ``ne.mcp.langgraph_demo._air_gap_check``: fires AFTER the unlocked
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


def ensure_crewai_env_at(env_dir: Path, venv_dir: Path) -> None:
    """Create/refresh the isolated hash-pinned crewai demo venv if needed.

    Delegates to :func:`ne.envbuild.ensure_sidecar_venv` with a FULL hash-pinned
    closure install (``--require-hashes``, NO ``--no-deps`` — the ragas/langgraph
    shape) and the AIR_GAP fail-closed ``pre_lock_check`` hook (C2). An already-current
    venv is a silent no-op; a missing/stale venv under AIR_GAP raises a typed
    :class:`~ne.net.airgap.AirGapViolationError` before any uv egress.
    """
    ensure_sidecar_venv(
        env_dir=env_dir,
        venv_dir=venv_dir,
        install_args=(),
        pre_lock_check=_air_gap_check(venv_dir, "the isolated crewai demo venv"),
        lock_timeout_s=_ENV_LOCK_TIMEOUT_S,
        name="the isolated crewai demo venv",
        run=_run,
    )


def ensure_crewai_env() -> tuple[Path, Path]:
    """Resolve + ensure the default crewai demo venv; return ``(env_dir, venv_dir)``."""
    env_dir, venv_dir = _resolve_dirs()
    ensure_crewai_env_at(env_dir, venv_dir)
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
    principal: str,
) -> None:
    """Decide only THIS run's pending approvals.

    ``list_pending()`` returns every open request in the (possibly shared) store, so
    the loop MUST scope to this demo run's principal before deciding — a shared
    ``--db`` could otherwise carry another principal's pending Tier-2 write that this
    scripted operator would blindly approve, or an unrelated Tier-3 it would reject.
    Filtering on ``principal_id`` pens each decision to the agent this driver launched.
    """
    decided: set[str] = set()
    while proc.returncode is None:
        for pending in await gate.list_pending():
            if pending.request_id in decided:
                continue
            if pending.principal_id != principal:
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

    print("Building the isolated crewai sidecar venv (first run only)…", flush=True)
    env_dir, venv_dir = ensure_crewai_env()
    agent_path = env_dir / "agent.py"
    python = venv_python(venv_dir)

    # Driver-side durable stores over the SAME sqlite the server writes to.
    engine = create_engine_with_timeouts(f"sqlite:///{db_path}")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    gate = create_approval_gate(sink=sink, engine=engine)

    # The principal the agent subprocess runs as; the approval loop scopes its decisions
    # to exactly this principal so a shared --db can't leak another run's pending approvals.
    demo_principal = os.environ.get(PRINCIPAL_ENV, _CREWAI_PRINCIPAL)

    server_cmd = json.dumps([sys.executable, "-m", "ne.mcp.demo_server"])
    child_env = {
        **os.environ,
        _SERVER_CMD_VAR: server_cmd,
        DB_ENV: str(db_path),
        PRINCIPAL_ENV: demo_principal,
        "NE_GOVERN_OPA_URL": os.environ.get("NE_GOVERN_OPA_URL", "http://localhost:8181"),
        # C2 telemetry suppression: crewai-core ships an OTLP exporter and chromadb
        # ships posthog INSIDE the sidecar. These pen those channels shut for the demo.
        # The sidecar is OUTSIDE the trust boundary; a hardened deploy additionally pens
        # it with C2 egress policy — deployment posture, not a property this unit builds.
        "CREWAI_DISABLE_TELEMETRY": "true",
        "OTEL_SDK_DISABLED": "true",
        "ANONYMIZED_TELEMETRY": "False",
    }
    agent_argv = [str(python), str(agent_path)]
    if args.model:
        agent_argv.append("--model")

    print(f"Launching the customer CrewAI agent in {venv_dir}…\n", flush=True)
    proc = await asyncio.create_subprocess_exec(*agent_argv, env=child_env)

    await _approval_loop(
        gate, proc, approver=args.approver, auto=args.auto, principal=demo_principal
    )
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
    parser = argparse.ArgumentParser(description="CrewAI governed drop-in demo (NE-CREWAI-DROPIN).")
    parser.add_argument(
        "--db",
        default=str(Path(tempfile.mkdtemp(prefix="ne-crewai-demo-")) / "crewai-demo.db"),
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
        help="Drive the agent with a real Crew.kickoff against a LOCAL OpenAI-compatible "
        "endpoint (NE_CREWAI_MODEL_BASE_URL). Owner/demo-rig only; never CI; no frontier keys.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
