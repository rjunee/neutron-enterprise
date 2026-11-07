# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A failing sync must STATE ITS CAUSE — and still leak nothing (C12).

The demo box spent real time behind ``fixture: ERROR OperationalError``: the
class alone covers connection-refused, too-many-clients, deadlock,
lock/statement timeout and sqlite readonly/locked, and it is frequently a
RECLASSIFICATION of something more specific underneath. These tests pin both
halves of that pair, with REAL driver exceptions (a real sqlite readonly write,
a real UNIQUE violation through a real SQLAlchemy engine) rather than stand-ins:

* a failed sync states its cause — code, origin class, redacted message;
* a successful (or merely non-driver) failure says nothing new — the audit
  detail for a plain application error is byte-identical to what it always was;
* nothing new that leaks — no bound parameters, no DSN password, no token, and
  for Postgres no ``DETAIL:`` row values.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ne.connect.errors import AclResolutionError
from ne.contracts.failure import (
    describe_failure,
    failure_code,
    failure_detail,
    failure_origin,
    redact_secrets,
)


def _readonly_sqlite_error(tmp_path: Path) -> sqlite3.OperationalError:
    """A REAL ``sqlite3.OperationalError`` from a genuinely read-only database.

    This is the exact class the connector state/quarantine lanes raise when the
    container uid cannot write the bind-mounted sqlite file — and sqlite reports
    it as ``OperationalError``, not ``IntegrityError``, which is precisely the
    ambiguity the class-only line could not resolve.
    """
    db = tmp_path / "ro.db"
    with sqlite3.connect(db) as seed:
        seed.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.execute("INSERT INTO t (id) VALUES ('x')")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        conn.close()
    raise AssertionError("a read-only sqlite database accepted a write")


def _sqlalchemy_unique_violation(tmp_path: Path) -> IntegrityError:
    """A REAL duplicate key, raised through a REAL SQLAlchemy engine.

    The inserted value is deliberately secret-shaped so the C12 assertions below
    are testing a projection that HAS the payload available and declines it,
    not one that never saw it.
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'dup.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE t (id TEXT PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO t (id) VALUES (:v)"), {"v": "hunter2-swordfish"})
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO t (id) VALUES (:v)"), {"v": "hunter2-swordfish"})
    except IntegrityError as exc:
        return exc
    finally:
        engine.dispose()
    raise AssertionError("a duplicate primary key was accepted")


class _FakeDiagnostic:
    """The psycopg ``Diagnostic`` FIELD SHAPE, so field selection is pinnable offline.

    A real ``Diagnostic`` can only be produced by a live libpq result, so the
    Postgres acceptance for this lives in the env-gated live-Postgres proof. What
    this pins is the projection's CHOICE of fields: identifiers yes, ``DETAIL``
    (which carries the offending row's VALUES) no.
    """

    schema_name = "public"
    table_name = "keycloak_group"
    column_name = None
    constraint_name = "sibling_names"
    message_primary = 'duplicate key value violates unique constraint "sibling_names"'
    message_detail = "Key (realm_id, parent_group, name)=(ne-pilot, top, servicing) already exists."
    message_hint = "consider a different name"


class _FakePgError(Exception):
    sqlstate = "23505"
    diag = _FakeDiagnostic()

    def __str__(self) -> str:  # libpq renders DETAIL into the rendered message
        return f"{_FakeDiagnostic.message_primary}\nDETAIL:  {_FakeDiagnostic.message_detail}"


# --- the pair: a failed sync states its cause -------------------------------


def test_a_failed_sync_states_its_cause(tmp_path: Path) -> None:
    """The line names the code, the origin class and the message — not just a class."""
    exc = _readonly_sqlite_error(tmp_path)
    line = describe_failure(exc)[0]

    assert "OperationalError" in line
    # The code is the whole point: SQLITE_READONLY is a diagnosis, the class is not.
    assert failure_code(exc) == "SQLITE_READONLY"
    assert "code=SQLITE_READONLY" in line
    assert "readonly database" in line


def test_the_reclassification_is_named_not_hidden(tmp_path: Path) -> None:
    """SQLAlchemy re-raises the DBAPI fault as its own class; the origin survives.

    This is the exact shape that made ``OperationalError`` unreadable on the box:
    the class that reaches the CLI belongs to the WRAPPER, and the class that
    knows what happened is underneath it.
    """
    exc = _sqlalchemy_unique_violation(tmp_path)

    assert type(exc).__name__ == "IntegrityError"
    origin = failure_origin(exc)
    assert origin == "sqlite3.IntegrityError", origin
    assert failure_code(exc) == "SQLITE_CONSTRAINT_PRIMARYKEY"

    line = describe_failure(exc)[0]
    assert "via=sqlite3.IntegrityError" in line
    assert "code=SQLITE_CONSTRAINT_PRIMARYKEY" in line
    assert "UNIQUE constraint failed" in line


def test_the_failing_statement_is_carried_without_its_parameters(tmp_path: Path) -> None:
    """SQL TEXT is code we wrote; the bound values are source data.

    ``str()`` on a SQLAlchemy ``StatementError`` appends ``[parameters: ...]``.
    The projection must reach the statement WITHOUT ever going through that
    rendering — so the secret-shaped bound value must be absent everywhere.
    """
    exc = _sqlalchemy_unique_violation(tmp_path)
    lines = describe_failure(exc)

    statements = [line for line in lines if line.startswith("statement: ")]
    assert statements, lines
    assert "INSERT INTO t" in statements[0]
    assert "hunter2-swordfish" not in "\n".join(lines)
    # Control: the payload really IS reachable from this exception — the
    # projection declined it rather than never having had it.
    assert "hunter2-swordfish" in str(exc)


def test_postgres_identifiers_are_projected_but_detail_row_values_are_not() -> None:
    """Constraint/relation NAMES surface; the offending key's VALUES never do."""
    exc = _FakePgError()
    line = describe_failure(exc)[0]

    assert "code=23505" in line
    assert "constraint=sibling_names" in line
    assert "relation=keycloak_group" in line
    assert "schema=public" in line
    assert 'duplicate key value violates unique constraint "sibling_names"' in line
    # DETAIL carries the row. It must not be in the line, even though str(exc) has it.
    assert "ne-pilot" not in line
    assert "already exists" not in line
    assert "ne-pilot" in str(exc)


# --- the pair: a successful sync says nothing new ---------------------------


@pytest.mark.parametrize(
    "exc",
    [
        AclResolutionError("acl resolved empty"),
        ConnectionError("connection reset"),
        ValueError("bad shape"),
    ],
)
def test_a_non_driver_failure_detail_is_byte_identical_to_before(exc: Exception) -> None:
    """No driver ⇒ no code ⇒ the ledger detail is exactly what it always was.

    The counterpart of "state the cause": the projection must not rewrite every
    existing audit detail in the ledger, only the ones it can actually explain.
    """
    assert failure_detail("backfill", exc) == f"mode=backfill error_class={type(exc).__name__}"
    assert failure_code(exc) is None
    assert failure_origin(exc) is None


def test_a_driver_failure_detail_gains_its_code_and_origin(tmp_path: Path) -> None:
    exc = _sqlalchemy_unique_violation(tmp_path)
    detail = failure_detail("incremental", exc)

    assert detail == (
        "mode=incremental error_class=IntegrityError "
        "error_code=SQLITE_CONSTRAINT_PRIMARYKEY error_origin=sqlite3.IntegrityError"
    )
    # The ledger gets CODES AND CLASS NAMES ONLY — never the driver's prose.
    assert "UNIQUE constraint failed" not in detail
    assert "hunter2-swordfish" not in detail


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (
            "connection to server failed: postgresql://ne:s3cr3t-pw@postgres:5432/neutron",
            "s3cr3t-pw",
        ),
        ("FATAL: password authentication failed, password=hunter2 for user ne", "hunter2"),
        ("request rejected: token: 'ghp_deadbeefcafe' expired", "ghp_deadbeefcafe"),
        ("authorization=Bearer abcdef123456 was refused", "abcdef123456"),
    ],
)
def test_credentials_never_survive_redaction(raw: str, secret: str) -> None:
    redacted = redact_secrets(raw)
    assert secret not in redacted
    assert "[redacted]" in redacted


def test_redaction_keeps_the_diagnosis_it_is_protecting() -> None:
    """Redaction that ate the message would defeat the whole change."""
    redacted = redact_secrets(
        "connection to server at postgres:5432 failed: "
        "postgresql://ne:s3cr3t@postgres:5432/neutron: too many clients already"
    )
    assert "too many clients already" in redacted
    assert "s3cr3t" not in redacted


def test_a_message_carrying_no_secret_is_left_intact() -> None:
    assert redact_secrets("deadlock detected") == "deadlock detected"


def test_the_chain_walk_terminates_on_a_self_referential_cause() -> None:
    """A wrapper whose cause loops back must not spin the projection."""
    outer = RuntimeError("outer")
    inner = RuntimeError("inner")
    outer.__cause__ = inner
    inner.__cause__ = outer

    assert failure_origin(outer) == "RuntimeError"
    assert describe_failure(outer)[0].startswith("RuntimeError: outer")


def test_describe_failure_is_never_empty() -> None:
    class _Mute(Exception):
        def __str__(self) -> str:
            return ""

    lines = describe_failure(_Mute())
    assert lines and lines[0] == "_Mute"


def test_no_secret_key_shape_is_silently_dropped_from_the_redactor() -> None:
    """Guardian: every declared secret key must actually be redacted.

    Deleting a key from the list would silently widen what a driver message may
    print; this iterates the declared list rather than trusting the four spot
    checks above.
    """
    from ne.contracts.failure import _SECRET_KEYS

    for key in _SECRET_KEYS:
        assert "leaked-value" not in redact_secrets(f"{key}=leaked-value")
        assert "leaked-value" not in redact_secrets(f"{key}: leaked-value")


def _projection_source() -> str:
    import ne.contracts.failure as module

    return Path(module.__file__ or "").read_text(encoding="utf-8")


def test_the_projection_never_reads_a_detail_or_hint_field() -> None:
    """Structural guardian: ``message_detail``/``message_hint`` carry row values.

    Reading either would make every duplicate-key line a data leak, and the leak
    would be invisible in tests that happen not to exercise Postgres.
    """
    source = _projection_source()
    for forbidden in ("message_detail", "message_hint"):
        assert f'"{forbidden}"' not in source
        assert f"getattr(diag, {forbidden!r}" not in source


def _pg_diag_fields(exc: Any) -> Any:
    return getattr(exc, "diag", None)


def test_a_missing_diagnostic_is_not_a_crash() -> None:
    """Not every driver has ``diag``; the projection degrades, never raises."""
    assert _pg_diag_fields(ValueError("x")) is None
    assert describe_failure(ValueError("x"))[0] == "ValueError: x"
