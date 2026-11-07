# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CLEAN2: the pysqlite dual-append recipe holds on ne-runtime engines.

Proves the SPEC §11 stranding gap is closed at every ne-runtime dual-append
engine site — the create_approval_gate factory engine, the store/scheduler
constructors, and (end-to-end) that a forced post-SAVEPOINT failure inside
``expire_approval_step`` leaves the request 'pending' with zero outbox/evidence
rows. The strand probe is duplicated locally (cross-package imports are banned).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

import ne.runtime.approvals_durable as ad
from ne.events import append as real_append
from ne.events.outbox import ne_outbox_events
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.factory import create_approval_gate
from ne.runtime.memory_store import SqlAlchemyEpisodicStore
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.processes import ProcessRegistry
from ne.runtime.run_store import AgentRunStore, ensure_run_schema
from ne.runtime.scheduler import CronScheduler, ensure_scheduler_schema
from ne.runtime.store import SqlAlchemySessionStore

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_guardrails,
    make_pdp,
    make_principal,
    standard_bindings,
)


class _Boom(Exception):
    pass


def _strand_probe(engine: sa.Engine) -> int:
    """begin_nested() write + released SAVEPOINT + failing outer leg; count survivors."""
    md = sa.MetaData()
    probe = sa.Table("ne_clean2_probe", md, sa.Column("id", sa.Integer, primary_key=True))
    md.create_all(engine)
    try:
        with engine.begin() as conn:
            with conn.begin_nested():
                conn.execute(probe.insert().values(id=1))
            raise _Boom()
    except _Boom:
        pass
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(probe)).scalar_one())


def _url(tmp_path: Path, name: str = "rt.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def test_create_approval_gate_engine_transactional(tmp_path: Path) -> None:
    """The create_approval_gate factory engine passes the strand probe."""
    gate = create_approval_gate(
        sink=RecordingAuditSink(),
        settings=RuntimeSettings(session_database_url=_url(tmp_path)),
    )
    engine = gate._store.engine  # type: ignore[attr-defined]  # private attr acceptable in tests
    assert _strand_probe(engine) == 0


def test_create_approval_gate_composes_the_run_checkpoint_store(tmp_path: Path) -> None:
    engine = sa.create_engine(_url(tmp_path, "run-approval.db"))
    ensure_run_schema(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    gate = create_approval_gate(sink=RecordingAuditSink(), engine=engine, run_store=runs)
    checkpoints = gate._run_approval_checkpoints  # type: ignore[attr-defined]
    assert checkpoints is not None
    assert checkpoints._engine is engine
    assert checkpoints._approvals is gate._store


def test_store_constructors_patch_external_engines(tmp_path: Path) -> None:
    """Store/scheduler constructors patch a handed-in external engine defensively."""
    # SqlAlchemyApprovalStore.__init__ over a PLAIN (un-recipe'd) engine.
    plain = sa.create_engine(_url(tmp_path, "approval.db"))
    store = SqlAlchemyApprovalStore(plain)
    assert _strand_probe(store.engine) == 0

    # from_settings variants build + patch their own engine.
    session_store = SqlAlchemySessionStore.from_settings(
        RuntimeSettings(session_database_url=_url(tmp_path, "session.db"))
    )
    assert _strand_probe(session_store.engine) == 0

    episodic_store = SqlAlchemyEpisodicStore.from_settings(
        RuntimeSettings(session_database_url=_url(tmp_path, "memory.db"))
    )
    assert _strand_probe(episodic_store.engine) == 0

    # CronScheduler.__init__ over a PLAIN file engine (no manual recipe).
    sink = RecordingAuditSink()
    sched_engine = sa.create_engine(_url(tmp_path, "sched.db"))
    ensure_scheduler_schema(sched_engine)
    pdp = make_pdp(sink)
    executor = GovernedToolExecutor(
        standard_bindings(ToolLog()),
        pdp=pdp,
        guardrails=make_guardrails(sink),
        approval_gate=NamedApproverGate("alice"),
        sink=sink,
    )
    scheduler = CronScheduler(
        engine=sched_engine,
        definitions={
            "researcher": AgentDefinition(
                name="researcher", instructions="Research.", toolset=("read.docs.search",)
            )
        },
        executor=executor,
        pdp=pdp,
        gateway=ScriptedGateway([]),
        registry=ProcessRegistry(),
        sink=sink,
        settings=RuntimeSettings(),
        principals=StaticAgentPrincipalProvider({"researcher": make_principal("agent:researcher")}),
    )
    assert _strand_probe(scheduler._engine) == 0  # type: ignore[attr-defined]


def _request_json() -> str:
    return json.dumps(
        {
            "action": "write.external.email.send",
            "tier": 2,
            "principal_id": "user:ada",
            "payload": {"tool": "write.external.email.send"},
            "sla_s": 100.0,
            "expiry_s": 1000.0,
        }
    )


def test_expire_step_outbox_failure_strands_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """END-TO-END: a post-SAVEPOINT failure in expire_approval_step strands nothing."""
    url = _url(tmp_path, "durable.db")
    request_id = ad.create_approval_request_step(url, _request_json())
    assert ad.poll_approval_step(url, request_id) == "pending"

    def _append_then_boom(conn: sa.Connection, event: Any) -> int:
        real_append(conn, event)  # the outbox SAVEPOINT write succeeds...
        raise _Boom()  # ...then the leg fails, forcing the outer rollback.

    # ``expire_approval_step`` calls the module-global ``append``; patch it there.
    monkeypatch.setattr(ad, "append", _append_then_boom)

    with pytest.raises(_Boom):
        ad.expire_approval_step(url, request_id)

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            status = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).scalar_one()
            evidence_rows = conn.execute(
                sa.select(sa.func.count()).select_from(ne_approval_evidence)
            ).scalar_one()
        with engine.connect() as conn:
            approval_events = conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_outbox_events)
                .where(ne_outbox_events.c.event_type == "approval")
            ).scalar_one()
    finally:
        engine.dispose()

    # The CAS to 'expired' rolled back; nothing was stranded or left unaudited.
    assert status == "pending"
    assert approval_events == 0
    assert evidence_rows == 0


def test_plain_engine_reproduces_gap(tmp_path: Path) -> None:
    """RED-CONTROL in the runtime package: an un-recipe'd engine strands the row."""
    engine = sa.create_engine(_url(tmp_path, "plain.db"))
    assert _strand_probe(engine) == 1
