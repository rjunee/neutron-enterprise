# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Owner-only fsynced hash-chain journal for lifecycle state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class JournalRefused(ValueError):
    """The journal is malformed or its chain is not intact."""


@dataclass(frozen=True)
class JournalRecord:
    sequence: int
    previous_hash: str
    payload: dict[str, Any]
    record_hash: str


def _canonical(sequence: int, previous_hash: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {"payload": payload, "previous_hash": previous_hash, "sequence": sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class StateJournal:
    def __init__(self, path: Path, *, owner_uid: int = 0) -> None:
        if not path.is_absolute():
            raise JournalRefused("journal path must be absolute")
        self.path = path
        self.owner_uid = owner_uid
        self._records = self._load()

    def _load(self) -> list[JournalRecord]:
        if not self.path.exists():
            return []
        info = self.path.lstat()
        if (
            self.path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.owner_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or self.path.resolve(strict=True) != self.path
        ):
            raise JournalRefused("journal ownership or mode is unsafe")
        records: list[JournalRecord] = []
        previous = "0" * 64
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines, start=1):
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {
                    "sequence",
                    "previous_hash",
                    "payload",
                    "record_hash",
                }:
                    raise JournalRefused("journal row has an invalid shape")
                if row["sequence"] != index or row["previous_hash"] != previous:
                    raise JournalRefused("journal sequence or chain is invalid")
                if not isinstance(row["payload"], dict):
                    raise JournalRefused("journal payload is invalid")
                expected = hashlib.sha256(_canonical(index, previous, row["payload"])).hexdigest()
                if row["record_hash"] != expected:
                    raise JournalRefused("journal hash is invalid")
                records.append(JournalRecord(index, previous, row["payload"], expected))
                previous = expected
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise JournalRefused("journal cannot be verified") from exc
        return records

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    def append(self, payload: dict[str, Any]) -> JournalRecord:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.lstat()
        if (
            self.path.parent.is_symlink()
            or self.path.parent.resolve(strict=True) != self.path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.owner_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise JournalRefused("journal directory ownership or mode is unsafe")
        sequence = len(self._records) + 1
        previous = self._records[-1].record_hash if self._records else "0" * 64
        record_hash = hashlib.sha256(_canonical(sequence, previous, payload)).hexdigest()
        row = {
            "sequence": sequence,
            "previous_hash": previous,
            "payload": payload,
            "record_hash": record_hash,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise JournalRefused("journal changed to an unsafe file")
            payload_bytes = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            remaining = memoryview(payload_bytes)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise JournalRefused("journal write did not make progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        record = JournalRecord(sequence, previous, payload, record_hash)
        self._records.append(record)
        return record


__all__ = ["JournalRecord", "JournalRefused", "StateJournal"]
