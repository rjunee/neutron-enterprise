# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fakes for the ne-backup unit suite.

The service ports (postgres/qdrant/volumes/container-runner) are faked with a
single shared ordered event log so ordering assertions (safety-before-destructive)
and failure injection are precise. The SecretStore is NOT faked past the seam:
``make_bundle`` builds a REAL :class:`ne.secrets.FileSecretStore` (plain codec) so
key resolution exercises the real store (C12 proof requires this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pytest

from ne.backup.errors import NeBackupError
from ne.backup.runner import RunResult
from ne.backup.runtime import Services

_DESTRUCTIVE_PG = {"restore_globals", "restore_database"}
_DESTRUCTIVE_QDRANT = {"upload_snapshot"}
_DESTRUCTIVE_VOL = {"reset", "extract"}


@dataclass
class Event:
    name: str
    arg: object = None
    destructive: bool = False


class FakePostgres:
    def __init__(
        self,
        events: list[Event],
        databases: list[str],
        table_counts: dict[str, int] | None = None,
        fail: frozenset[str] = frozenset(),
    ) -> None:
        self.events = events
        self._databases = list(databases)
        self._table_counts = table_counts or {d: 1 for d in databases}
        self.fail = set(fail)
        # Staging paths handed to dump/restore — used by the P2-8 off-dest assertions.
        self.dump_dests: list[Path] = []
        self.restore_srcs: list[Path] = []

    def _record(self, name: str, arg: object = None) -> None:
        self.events.append(Event(f"pg.{name}", arg, name in _DESTRUCTIVE_PG))
        if name in self.fail:
            raise NeBackupError(f"injected postgres failure: {name}")

    def list_databases(self) -> list[str]:
        self._record("list_databases")
        return list(self._databases)

    def dump_globals(self, dest: Path) -> None:
        self._record("dump_globals")
        self.dump_dests.append(dest)
        dest.write_bytes(b"-- fake pg globals\n")

    def dump_database(self, db: str, dest: Path) -> None:
        self._record("dump_database", db)
        self.dump_dests.append(dest)
        dest.write_bytes(f"-- fake dump of {db}\n".encode())

    def restore_globals(self, src: Path) -> None:
        self._record("restore_globals")
        self.restore_srcs.append(src)

    def restore_database(self, db: str, src: Path) -> None:
        self._record("restore_database", db)
        self.restore_srcs.append(src)

    def scalar(self, db: str, sql: str) -> str:
        self._record("scalar", (db, sql))
        return "1"

    def user_table_count(self, db: str) -> int:
        self._record("user_table_count", db)
        return self._table_counts.get(db, 0)


class FakeQdrant:
    def __init__(
        self,
        events: list[Event],
        collections: dict[str, int] | None = None,
        fail: frozenset[str] = frozenset(),
    ) -> None:
        self.events = events
        self._collections = dict(collections or {})
        self.fail = set(fail)

    def _record(self, name: str, arg: object = None) -> None:
        self.events.append(Event(f"qdrant.{name}", arg, name in _DESTRUCTIVE_QDRANT))
        if name in self.fail:
            raise NeBackupError(f"injected qdrant failure: {name}")

    def list_collections(self) -> list[str]:
        self._record("list_collections")
        return sorted(self._collections)

    def points_count(self, name: str) -> int:
        self._record("points_count", name)
        return self._collections[name]

    def create_snapshot(self, name: str) -> str:
        self._record("create_snapshot", name)
        return "snap-1"

    def download_snapshot(self, name: str, snapshot: str, dest: Path) -> None:
        self._record("download_snapshot", name)
        dest.write_bytes(f"qdrant-snapshot:{name}".encode())

    def delete_snapshot(self, name: str, snapshot: str) -> None:
        self._record("delete_snapshot", name)

    def upload_snapshot(self, name: str, src: Path) -> None:
        self._record("upload_snapshot", name)


class FakeVolumes:
    def __init__(self, events: list[Event], fail: frozenset[str] = frozenset()) -> None:
        self.events = events
        self.fail = set(fail)

    def _record(self, name: str, key: str) -> None:
        self.events.append(Event(f"vol.{name}", key, name in _DESTRUCTIVE_VOL))
        if name in self.fail:
            raise NeBackupError(f"injected volume failure: {name}")

    def archive(self, key: str, dest_tar: Path) -> None:
        self._record("archive", key)
        dest_tar.write_bytes(f"volume-tar:{key}".encode())

    def reset(self, key: str) -> None:
        self._record("reset", key)

    def extract(self, key: str, src_tar: Path) -> None:
        self._record("extract", key)


class RecordingRunner:
    """Fake container-control runner (stop/start only in these tests)."""

    def __init__(self, events: list[Event]) -> None:
        self.events = events

    def run(self, args: list[str], *, stdin: bytes | None = None, check: bool = True) -> RunResult:
        destructive = bool(args) and args[0] == "stop"
        self.events.append(Event(f"docker.{args[0]}", args[1:], destructive))
        return RunResult(0, b"", b"")

    def run_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        self.events.append(Event(f"docker.stream.{args[0]}", args[1:]))
        return RunResult(0, b"", b"")


@dataclass
class Harness:
    events: list[Event] = field(default_factory=list)
    postgres: FakePostgres | None = None
    qdrant: FakeQdrant | None = None
    volumes: FakeVolumes | None = None
    runner: RecordingRunner | None = None

    def services(self) -> Services:
        assert self.postgres and self.volumes and self.runner
        return Services(
            postgres=self.postgres,
            volumes=self.volumes,
            runner=self.runner,
            qdrant=self.qdrant,
            echo=lambda msg: self.events.append(Event("echo", msg)),
        )

    def destructive_events(self) -> list[Event]:
        return [e for e in self.events if e.destructive]

    def echoes(self) -> list[str]:
        return [str(e.arg) for e in self.events if e.name == "echo"]


def make_harness(
    *,
    databases: list[str] | None = None,
    collections: dict[str, int] | None = None,
    with_qdrant: bool = True,
    pg_fail: frozenset[str] = frozenset(),
    qdrant_fail: frozenset[str] = frozenset(),
    vol_fail: frozenset[str] = frozenset(),
) -> Harness:
    events: list[Event] = []
    databases = ["neutron"] if databases is None else databases
    harness = Harness(events=events)
    harness.postgres = FakePostgres(events, databases, fail=pg_fail)
    harness.volumes = FakeVolumes(events, fail=vol_fail)
    harness.runner = RecordingRunner(events)
    if with_qdrant:
        harness.qdrant = FakeQdrant(
            events, collections if collections is not None else {"drill": 5}, fail=qdrant_fail
        )
    return harness


def make_bundle(root: Path) -> Path:
    """Build a real customer bundle with a plain-codec FileSecretStore."""
    bundle = root / "bundle"
    (bundle / "config").mkdir(parents=True)
    secrets_file = bundle / "secrets.ne.json"
    toml = f'store = "sops-file"\n[file]\npath = "{secrets_file.as_posix()}"\nmode = "plain"\n'
    (bundle / "config" / "ne-secrets.toml").write_text(toml, encoding="utf-8")
    (bundle / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    # A runtime/ dir carrying a fake resolved secret — must be EXCLUDED from backups.
    (bundle / "runtime").mkdir()
    (bundle / "runtime" / "pilot.env").write_text(
        "NE_PILOT_POSTGRES_PASSWORD=super-secret-value\n", encoding="utf-8"
    )
    (bundle / "config" / "ne-runtime.toml").write_text("# runtime\n", encoding="utf-8")
    return bundle


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return make_bundle(tmp_path)


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d
