# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CLEAN2: the pysqlite dual-append recipe holds on FACTORY-built engines.

Proves the SPEC §11 gap is closed for every ne-flywheel *_from_settings factory
(their engines pass the SAVEPOINT strand probe) and, end-to-end, that a forced
chain-append failure during a REAL curation run on a FACTORY engine strands zero
proposals/outbox rows and does not advance the cursor. The strand probe is
duplicated locally (cross-package test imports are banned by the conftest policy).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.contracts import Principal
from ne.events.outbox import ne_outbox_events
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.curation import CurationAgent, ne_curation_cursor
from ne.flywheel.errors import CurationAuditError
from ne.flywheel.factory import (
    capture_from_settings,
    curation_agent_from_settings,
    review_service_from_settings,
)
from ne.flywheel.proposals import SqlAlchemyProposalStore, ne_fix_proposals
from ne.flywheel.signals import (
    SignalPayload,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
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


def _settings(tmp_path: Path) -> FlywheelSettings:
    return FlywheelSettings(signal_database_url=f"sqlite:///{tmp_path / 'fw.db'}")


@pytest.mark.parametrize("factory", ["capture", "curation", "review"])
def test_factory_engines_transactional(tmp_path: Path, factory: str) -> None:
    """Each *_from_settings factory hands back an engine that passes the strand probe."""
    settings = _settings(tmp_path)
    if factory == "capture":
        obj: object = capture_from_settings(settings)
    elif factory == "curation":
        obj = curation_agent_from_settings(settings)
    else:
        obj = review_service_from_settings(settings)
    engine = obj._engine  # type: ignore[attr-defined]  # private attr acceptable in tests
    assert _strand_probe(engine) == 0


def test_plain_engine_reproduces_gap(tmp_path: Path) -> None:
    """RED-CONTROL: an un-recipe'd engine on the same-style URL strands the row."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plain.db'}")
    assert _strand_probe(engine) == 1


def _provenance() -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id="user:ada@acme.com"),
        surface=SignalSurface.CONSOLE,
        session_id="sess-1",
        turn_id="turn-3",
        message_id="msg-9",
    )


def _seed(capture: CorrectionSignalCapture, payload: SignalPayload) -> None:
    asyncio.run(capture.capture(_provenance(), payload))


def test_curation_chain_failure_fully_rolls_back_on_factory_engine(
    tmp_path: Path,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END on a FACTORY engine: a chain-append failure strands nothing."""
    settings = _settings(tmp_path)
    agent = curation_agent_from_settings(settings)
    capture = capture_from_settings(settings)
    # Seed enough real signals (via the NE-064 capture path) to yield >=1 proposal.
    _seed(capture, ThumbsSignal(up=False, target_ref="doc-1"))
    _seed(capture, ThumbsSignal(up=False, target_ref="doc-1"))
    _seed(capture, ThumbsSignal(up=False, target_ref="doc-1"))

    def _raiser(self: CurationAgent, conn: sa.Connection, event: object) -> None:
        raise CurationAuditError("forced chain-append failure (NE-CLEAN2 end-to-end)")

    monkeypatch.setattr(CurationAgent, "_chain_append", _raiser)

    with pytest.raises(CurationAuditError):
        asyncio.run(agent.run_once())

    engine = agent._engine
    # Nothing stranded, nothing unaudited: no proposals, no outbox audit event,
    # and the curation cursor never advanced.
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 0
    with engine.connect() as conn:
        proposal_rows = conn.execute(
            sa.select(sa.func.count()).select_from(ne_fix_proposals)
        ).scalar_one()
        audit_outbox = conn.execute(
            sa.select(sa.func.count())
            .select_from(ne_outbox_events)
            .where(ne_outbox_events.c.event_type == "audit")
        ).scalar_one()
        cursor_rows = conn.execute(
            sa.select(sa.func.count()).select_from(ne_curation_cursor)
        ).scalar_one()
    assert proposal_rows == 0
    assert audit_outbox == 0
    assert cursor_rows == 0
