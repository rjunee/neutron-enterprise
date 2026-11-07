# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/connect/quarantine (NE-CONNECT-CSV) — the ops-gated drill-down wire.

The bidirectional pair the access decision demands:

* an OPS principal sees the parked rows THEMSELVES — the raw validator reason
  (field + offending value), the record identity, the payload — because an
  inspectable quarantine is the lane's entire point;
* everyone else gets 403 + an audited refusal and NOT ONE BYTE of the parked
  content (sentinel sweep over the refusal response), because a parked write is
  customer data that failed validation.

Plus the honesty contract: no readable store ⇒ ``total: null`` (never a fake
zero/empty success), and long payload values are bounded with the truncation
flagged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.brain.write import QuarantinedWrite, SqliteWriteQuarantine

from .conftest import AppBuilder, get_state, run

#: Sentinels: a customer-ish field name + offending value that must NEVER cross
#: the wire to a non-ops principal.
_REASON = "$.contract_end: 'ASAP' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
_ACCOUNT = "Meridian Logistics Ltd"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> str:
    quarantine_path = f"{tmp_path}/quarantine.db"
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", f"{tmp_path}/state.db")
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", quarantine_path)
    return quarantine_path


def _seed_park(quarantine_path: str, *, payload_value: str = _ACCOUNT) -> None:
    SqliteWriteQuarantine(quarantine_path).record(
        QuarantinedWrite(
            item_id="a" * 64,
            reason=_REASON,
            payload={"renewal_id": "RNW-1007", "account": payload_value, "contract_end": "ASAP"},
            provenance="connector:csv:RNW-1007",
            shape="odcs:csv",
            created_at=datetime.now(UTC).isoformat(),
        )
    )


def test_ops_principal_reads_the_row_the_field_and_the_offending_value(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    quarantine_path = _configure(monkeypatch, tmp_path)
    _seed_park(quarantine_path)
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("op", groups=["ops"])
            )
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == 1
            [item] = body["items"]
            assert item["source"] == "csv"
            assert item["record_id"] == "RNW-1007"
            assert item["reason"] == _REASON  # the validator's own text, verbatim
            assert item["category"] == "schema-validation"
            assert item["payload"]["contract_end"] == "ASAP"
            assert item["payload"]["account"] == _ACCOUNT
            assert item["payload_truncated"] is False
            assert item["shape"] == "odcs:csv"

    run(_t())


def test_non_ops_principal_is_refused_audited_and_leaks_nothing(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    quarantine_path = _configure(monkeypatch, tmp_path)
    _seed_park(quarantine_path)
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("u", groups=["employees"])
            )
            assert r.status_code == 403
            # Sentinel sweep: no fragment of the parked content in the refusal.
            for sentinel in ("ASAP", "RNW-1007", _ACCOUNT, "contract_end"):
                assert sentinel not in r.text
        with state.audit_engine.connect() as conn:
            actions = [
                row[0]
                for row in conn.execute(
                    sa.select(ne_audit_log.c.action).where(
                        ne_audit_log.c.action == "api.connect.quarantine.unauthorized"
                    )
                )
            ]
        assert actions  # the refusal was audited

    run(_t())


def test_empty_ops_roles_fails_closed_for_everyone(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _seed_park(_configure(monkeypatch, tmp_path))
    h = make_app()  # ops_roles default () ⇒ nobody

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("op", groups=["ops"])
            )
            assert r.status_code == 403

    run(_t())


def test_no_readable_store_is_honestly_unknown_never_a_fake_empty(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # Paths configured but no file exists ⇒ total null, items empty — unknown,
    # not "quarantine is empty".
    _configure(monkeypatch, tmp_path)
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("op", groups=["ops"])
            )
            assert r.status_code == 200
            body = r.json()
            assert body["total"] is None
            assert body["items"] == []

    run(_t())


def test_long_payload_values_are_bounded_and_flagged(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    quarantine_path = _configure(monkeypatch, tmp_path)
    _seed_park(quarantine_path, payload_value="x" * 5000)
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("op", groups=["ops"])
            )
            assert r.status_code == 200
            [item] = r.json()["items"]
            assert len(item["payload"]["account"]) == 2000
            assert item["payload_truncated"] is True
            # Control: the short values ride unbounded.
            assert item["payload"]["contract_end"] == "ASAP"

    run(_t())


def test_nested_payload_strings_are_bounded_too(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The bound applies at EVERY depth, not just top level.

    CSV rows are flat, but the drill-down serves ALL parks — writer shape
    parks and CDC payloads nest — so a dict- or list-wrapped document body
    must be truncated and honestly flagged, exactly like a top-level string.
    """
    quarantine_path = _configure(monkeypatch, tmp_path)
    SqliteWriteQuarantine(quarantine_path).record(
        QuarantinedWrite(
            item_id="b" * 64,
            reason=_REASON,
            payload={
                "metadata": {"body": "y" * 5000, "kind": "note"},
                "attachments": [{"text": "z" * 5000}, "w" * 5000],
                "renewal_id": "RNW-1007",
            },
            provenance="connector:csv:RNW-1007",
            shape="odcs:csv",
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/connect/quarantine", headers=h.bearer("op", groups=["ops"])
            )
            assert r.status_code == 200
            [item] = r.json()["items"]
            assert len(item["payload"]["metadata"]["body"]) == 2000
            assert len(item["payload"]["attachments"][0]["text"]) == 2000
            assert len(item["payload"]["attachments"][1]) == 2000
            assert item["payload_truncated"] is True
            # Controls: short nested values ride whole, structure preserved.
            assert item["payload"]["metadata"]["kind"] == "note"
            assert item["payload"]["renewal_id"] == "RNW-1007"

    run(_t())
