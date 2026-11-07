# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FixturePrincipalSource — a zero-network recorded-fixture ``PrincipalSource``.

Proves recorded-fixture CI mode: replays a committed JSON (or in-memory) list of
snapshots with ZERO network I/O, so NE-043 / NE-044 suites are CI-testable
without a live Entra tenant. Registered under ``ne.plugins.principal_source`` as
``fixture``.

Fixture shape::

    {
      "name": "fixture",
      "snapshots": [
        {
          "is_full": true,
          "principals": [
            {"principal_id": "u-ada", "kind": "user", "display_name": "Ada"},
            {"principal_id": "g-eng", "kind": "group"}
          ],
          "edges": [{"member_id": "u-ada", "group_id": "g-eng"}],
          "replaced_groups": ["g-eng"],
          "replaced_members": [],
          "removed_principal_ids": [],
          "state": {}
        }
      ]
    }

Successive ``fetch()`` calls replay successive snapshots; the last snapshot
repeats once the list is exhausted.

SANITIZATION IS THE RECORDER'S JOB — committed fixtures MUST carry innocuous fake
data only, never real credentials or PII (plan §1.4 secrets hygiene).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ne.permissions.models import (
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)

__all__ = ["FixturePrincipalSource"]


class FixturePrincipalSource:
    """Replay recorded principal snapshots as a frozen ``PrincipalSource``."""

    def __init__(self, name: str, snapshots: Sequence[Mapping[str, Any]]) -> None:
        self._name = name
        self._snapshots = [self._parse(s) for s in snapshots]
        self._cursor = 0

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> FixturePrincipalSource:
        return cls(data.get("name", "fixture"), list(data.get("snapshots", [])))

    @classmethod
    def from_path(cls, path: str | Path) -> FixturePrincipalSource:
        return cls.from_data(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _parse(raw: Mapping[str, Any]) -> PrincipalSnapshot:
        return PrincipalSnapshot(
            principals=tuple(
                SyncedPrincipal(
                    principal_id=p["principal_id"],
                    kind=PrincipalKind(p["kind"]),
                    display_name=p.get("display_name", ""),
                )
                for p in raw.get("principals", [])
            ),
            edges=tuple(
                MembershipEdge(member_id=e["member_id"], group_id=e["group_id"])
                for e in raw.get("edges", [])
            ),
            replaced_groups=tuple(raw.get("replaced_groups", ())),
            replaced_members=tuple(raw.get("replaced_members", ())),
            removed_principal_ids=tuple(raw.get("removed_principal_ids", ())),
            is_full=bool(raw.get("is_full", False)),
            state=dict(raw.get("state", {})),
        )

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        if not self._snapshots:
            return PrincipalSnapshot(state=dict(state))
        index = min(self._cursor, len(self._snapshots) - 1)
        self._cursor += 1
        return self._snapshots[index]
