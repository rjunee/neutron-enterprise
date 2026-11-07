# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""create_validated_engine — dialect allowlist + C2 in-VPC host policy (NE-046).

Also the structural guardian: ``sa.create_engine`` / ``create_engine(`` must
appear ONLY in ``sql/engine.py`` under ``ne-connect`` ``src/`` (the sibling of
the M365 connector's validated-construction guardian).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path

import pytest

from ne.connect.errors import SqlSyncError
from ne.connect.sql.config import SqlConnectorSettings, SqlTableAclConfig, SqlTableConfig
from ne.connect.sql.engine import create_validated_engine, validate_connection_url

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "connect"

_TABLES = (
    SqlTableConfig(
        table="docs",
        id_column="id",
        content_columns=("title",),
        acl=SqlTableAclConfig(principals=("g-eng",)),
    ),
)


def _settings(connection_url: str, **overrides: object) -> SqlConnectorSettings:
    kwargs: dict[str, object] = {
        "connection_url": connection_url,
        "owner": "data-eng@acme.test",
        "tables": _TABLES,
    }
    kwargs.update(overrides)
    return SqlConnectorSettings(**kwargs)  # type: ignore[arg-type]


# -- pyodbc availability (an environment fact, NOT a repo property) -----------
#
# Some build hosts lack the unixODBC runtime (libodbc.so.2), so the pyodbc
# DBAPI cannot load there. Engine CONSTRUCTION for mssql imports that DBAPI;
# URL validation/normalisation does not. The construction test below is
# therefore conditionally skipped — never deleted (docs/testing/
# vacuous-guards.md) and never unconditionally skipped: on a host with the
# driver the coverage comes back automatically, and
# test_pyodbc_skip_condition_is_genuine keeps the condition honest.


#: The dynamic loader's "that shared library is not on this host" messages —
#: glibc first, macOS second.
_ABSENT_LIBRARY_MARKERS = ("cannot open shared object file", "library not loaded")

#: ...and the ONE library whose absence is an environment fact rather than a
#: repo failure. Both conditions are required, and each rejects a case the
#: other would wave through: an ABI break ("libodbc.so.2: undefined symbol:
#: SQLAllocHandle") names libodbc but is a BROKEN install, while a different
#: missing ``.so`` is a broken wheel that happens to fail at the loader. Only
#: "the unixODBC runtime is not installed here" skips; everything else
#: re-raises, so an unusable pyodbc fails CI instead of quietly deleting the
#: only engine-construction coverage.
_ODBC_RUNTIME = "libodbc"


def _pyodbc_import_error(
    importer: Callable[[str], object] = importlib.import_module,
) -> str | None:
    """Return a host-library ImportError if pyodbc cannot load, else None.

    A missing Python package is a repository dependency failure and must remain
    red. Only a present pyodbc wheel that cannot load its host library is an
    environmental skip — every other ImportError propagates, because "the wheel
    is broken" is a repo-visible failure wearing the same exception type.
    """
    try:
        importer("pyodbc")
    except ModuleNotFoundError:
        raise
    except ImportError as exc:
        message = str(exc)
        lowered = message.lower()
        absent = any(marker in lowered for marker in _ABSENT_LIBRARY_MARKERS)
        if not (absent and _ODBC_RUNTIME in lowered):
            raise
        return message
    return None


_PYODBC_IMPORT_ERROR = _pyodbc_import_error()


def _requires_pyodbc(import_error: str | None) -> pytest.MarkDecorator:
    """Build the conditional mark from an independently testable probe result."""
    return pytest.mark.skipif(
        import_error is not None,
        reason=(
            "pyodbc DBAPI not importable on this host — typically the unixODBC "
            "runtime (libodbc.so.2) is missing; install unixODBC to restore this "
            f"coverage. Underlying error: {import_error}"
        ),
    )


_REQUIRES_PYODBC = _requires_pyodbc(_PYODBC_IMPORT_ERROR)


# -- dialect allowlist --------------------------------------------------------


def test_mysql_dialect_rejected() -> None:
    settings = _settings("mysql+pymysql://u:hunter2@10.1.2.3/db")
    with pytest.raises(SqlSyncError, match="mysql"):
        create_validated_engine(settings)


def test_postgresql_normalized_to_psycopg() -> None:
    settings = _settings("postgresql://u:hunter2@10.1.2.3/db")
    engine = create_validated_engine(settings)
    assert engine.url.drivername == "postgresql+psycopg"


def test_postgresql_psycopg_accepted_as_is() -> None:
    settings = _settings("postgresql+psycopg://u:hunter2@10.1.2.3/db")
    engine = create_validated_engine(settings)
    assert engine.url.drivername == "postgresql+psycopg"


def test_mssql_pyodbc_accepted_as_is() -> None:
    # Explicit mssql+pyodbc exercises allowlist acceptance without importing
    # the DBAPI. The bare-mssql sibling below exercises OUR normalisation.
    settings = _settings("mssql+pyodbc://user:hunter2@10.0.0.5/db")
    assert validate_connection_url(settings).drivername == "mssql+pyodbc"


def test_mssql_bare_normalized_to_pyodbc() -> None:
    settings = _settings("mssql://user:hunter2@10.0.0.5/db")
    assert validate_connection_url(settings).drivername == "mssql+pyodbc"


@_REQUIRES_PYODBC
def test_mssql_engine_constructible_when_driver_installed() -> None:
    # The driver half of the old two-in-one test: proves the C4-cleared
    # pyodbc dep actually installs and create_engine accepts the normalized
    # URL. Runs wherever libodbc.so.2 exists.
    settings = _settings("mssql://user:hunter2@10.0.0.5/db")
    engine = create_validated_engine(settings)
    assert engine.url.drivername == "mssql+pyodbc"


def test_pyodbc_skip_condition_is_genuine() -> None:
    # T3 guard: the skip must be CONDITIONAL. An importable pyodbc must yield
    # no skip; a libodbc failure must yield one; and that exact conditional
    # mark must decorate the engine-construction test.
    assert _pyodbc_import_error(lambda _name: object()) is None

    def _missing_pyodbc(_name: str) -> object:
        raise ModuleNotFoundError("No module named 'pyodbc'", name="pyodbc")

    with pytest.raises(ModuleNotFoundError, match="pyodbc"):
        _pyodbc_import_error(_missing_pyodbc)

    def _missing_libodbc(_name: str) -> object:
        raise ImportError("libodbc.so.2: cannot open shared object file: No such file or directory")

    error = _pyodbc_import_error(_missing_libodbc)
    assert error is not None
    assert "libodbc.so.2" in error

    # A pyodbc that IMPORTS but is broken (ABI mismatch) is a repo failure, not
    # an environment fact: it must propagate, or a wheel that can never build an
    # engine would skip the only test that would have noticed.
    def _broken_pyodbc(_name: str) -> object:
        raise ImportError("libodbc.so.2: undefined symbol: SQLAllocHandle")

    with pytest.raises(ImportError, match="undefined symbol"):
        _pyodbc_import_error(_broken_pyodbc)

    # ...and a wheel missing some OTHER shared library is equally a broken
    # install: the loader phrasing matches, the library is not unixODBC, so it
    # must propagate rather than suppress the construction test.
    def _missing_other_library(_name: str) -> object:
        raise ImportError("libcrypto.so.3: cannot open shared object file: No such file")

    with pytest.raises(ImportError, match="libcrypto"):
        _pyodbc_import_error(_missing_other_library)
    assert _requires_pyodbc(None).mark.args[0] is False
    assert _requires_pyodbc(error).mark.args[0] is True
    assert "libodbc.so.2" in _requires_pyodbc(error).mark.kwargs["reason"]
    # pytestmark is attached by the decorator at import time, so pyright cannot
    # see it on the FunctionType. Read it reflectively and default to an EMPTY list:
    # a test that lost its mark yields [] and this assertion still fails, which is
    # the whole point of the guard.
    marks = getattr(test_mssql_engine_constructible_when_driver_installed, "pytestmark", [])
    assert _REQUIRES_PYODBC.mark in marks


def test_sqlite_rejected_by_default() -> None:
    settings = _settings("sqlite:///whatever.db")
    with pytest.raises(SqlSyncError, match="sqlite"):
        create_validated_engine(settings)


def test_sqlite_accepted_when_allow_sqlite(tmp_path: Path) -> None:
    settings = _settings(f"sqlite:///{tmp_path / 'f.db'}", allow_sqlite=True)
    engine = create_validated_engine(settings)
    assert engine.url.drivername == "sqlite"


# -- C2 in-VPC host policy -----------------------------------------------------


def test_metadata_host_rejected() -> None:
    settings = _settings("postgresql://u:hunter2@169.254.169.254/db")
    with pytest.raises(SqlSyncError):
        create_validated_engine(settings)


def test_packed_ipv4_host_rejected() -> None:
    settings = _settings("postgresql://u:hunter2@2852039166/db")
    with pytest.raises(SqlSyncError):
        create_validated_engine(settings)


def test_external_host_rejected() -> None:
    settings = _settings("postgresql://u:hunter2@db.evil.example.com/db")
    with pytest.raises(SqlSyncError):
        create_validated_engine(settings)


def test_external_host_accepted_when_allowlisted() -> None:
    settings = _settings(
        "postgresql://u:hunter2@db.evil.example.com/db",
        allowed_hosts=("db.evil.example.com",),
    )
    engine = create_validated_engine(settings)
    assert engine.url.host == "db.evil.example.com"


@pytest.mark.parametrize("host", ["10.1.2.3", "localhost", "db.internal"])
def test_in_vpc_hosts_accepted(host: str) -> None:
    settings = _settings(f"postgresql://u:hunter2@{host}/db")
    engine = create_validated_engine(settings)
    assert engine.url.host == host


# -- C12: no secret echo -------------------------------------------------------


@pytest.mark.parametrize(
    "connection_url",
    [
        "postgresql://u:hunter2@169.254.169.254/db",
        "postgresql://u:hunter2@2852039166/db",
        "postgresql://u:hunter2@db.evil.example.com/db",
        "mysql+pymysql://u:hunter2@10.1.2.3/db",
        "sqlite:///wherever.db",
    ],
)
def test_every_rejection_omits_password_and_full_url(connection_url: str) -> None:
    settings = _settings(connection_url)
    with pytest.raises(SqlSyncError) as excinfo:
        create_validated_engine(settings)
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert connection_url not in message


def test_unparsable_url_rejected_without_echo() -> None:
    settings = _settings("not a url at all ::: %%%")
    with pytest.raises(SqlSyncError) as excinfo:
        create_validated_engine(settings)
    assert "not a url at all" not in str(excinfo.value)


# -- structural guardian: create_engine confined to sql/engine.py -------------


def test_create_engine_confined_to_sql_engine_module() -> None:
    engine_module = _SRC / "sql" / "engine.py"
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path == engine_module:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "create_engine(" in line:
                offenders.append(f"{path}: {line.strip()}")
    assert not offenders, f"create_engine( used outside sql/engine.py: {offenders}"
