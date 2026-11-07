# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The per-tenant seed-manifest (NE-102) — corpus-id -> live-id + persona -> principal.

Every tenant push writes a :class:`SeedManifest` recording exactly what NE-075 /
NE-077 need to assert permission traps against the LIVE tenant ids: which corpus
doc/record became which live id, and which persona maps to which real principal
(for Salesforce the persona maps to a permission-set NAME, since the trap is
object-level). The manifest is written OUTSIDE the repo by default
(``~/.ne/acme-seed/{tenant}.json``) and, by construction, has NO secret-typed
fields — it carries live IDs only, never tokens or key material (C12).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["SeedManifest", "default_manifest_dir"]

Tenant = Literal["m365", "salesforce", "slack"]


def default_manifest_dir() -> Path:
    """Where manifests are written by default — OUTSIDE the repo, never committed."""
    return Path.home() / ".ne" / "acme-seed"


class SeedManifest(BaseModel):
    """What was seeded into one tenant, for the NE-075/NE-077 LIVE assertions."""

    model_config = ConfigDict(extra="forbid")

    tenant: Tenant
    seeded_at: str
    corpus_version: str
    #: persona person-id -> live principal id (Entra user / Slack user / permset
    #: NAME for Salesforce); ``None`` when this tenant has no principal for it.
    personas: dict[str, str | None] = {}
    #: doc_id -> live id (SharePoint driveItem id).
    docs: dict[str, str] = {}
    #: corpus record id (acct-/per-/tkt-) -> live Salesforce record id.
    records: dict[str, str] = {}
    #: channel name -> live channel id (Slack).
    channels: dict[str, str] = {}
    #: fact_id -> {"channel": id, "ts": ts} (Slack messages carrying facts).
    messages: dict[str, dict[str, str]] = {}
    #: permission-set name -> live id (Salesforce).
    permsets: dict[str, str] = {}
    #: doc_id -> the live principal ids granted (M365 invite recipients).
    grants: dict[str, list[str]] = {}
    #: non-fatal, owner-completable notes (missing Slack user, sales/named skips).
    warnings: list[str] = []

    def path_in(self, manifest_dir: Path) -> Path:
        return manifest_dir / f"{self.tenant}.json"

    def save(self, manifest_dir: Path) -> Path:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = self.path_in(manifest_dir)
        path.write_text(json.dumps(self.model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        return path

    @classmethod
    def load(cls, manifest_dir: Path, tenant: Tenant) -> SeedManifest:
        path = manifest_dir / f"{tenant}.json"
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def delete(cls, manifest_dir: Path, tenant: Tenant) -> None:
        (manifest_dir / f"{tenant}.json").unlink(missing_ok=True)
