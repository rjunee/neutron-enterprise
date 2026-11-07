# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-tenant seed manifests, stored outside the repository."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ne.connect.m365.operational_graph import (
    OperationalGraphItemBinding,
    manifest_record_key,
    validate_binding_acls,
    validate_binding_set,
)

Tenant = Literal["m365", "salesforce"]
SeedStage = Literal["initial", "operational_graph", "supersession"]


def default_manifest_dir() -> Path:
    return Path.home() / ".ne" / "meridian-seed"


class StageHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: SeedStage
    completed_at: str


class M365SeedTarget(BaseModel):
    """Frozen, credential-free binding of the exact proven M365 seed target.

    Saved only after the live Graph objects have been resolved and created by
    this tool, so a later run can compare every statically knowable member
    before authentication and re-prove the drive/folder identity read-only
    before any mutation. It intentionally carries NO secrets, tokens,
    certificate paths, headers, or ambient filesystem identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: Literal["m365"]
    corpus_version: str
    graph_base_url: str
    tenant_id: str
    site_id: str
    drive_id: str
    folder_item_id: str
    generation_id: str

    @field_validator(
        "corpus_version", "graph_base_url", "tenant_id", "site_id", "drive_id", "folder_item_id"
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or value != value.strip() or any(not char.isprintable() for char in value):
            raise ValueError("target members must be non-empty, trimmed, control-char-free ids")
        return value

    @field_validator("generation_id")
    @classmethod
    def _generation_format(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", value):
            raise ValueError(
                "generation id must be 1-128 ASCII chars, starting with a letter or digit"
            )
        return value


class SeedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: Tenant
    seeded_at: str
    corpus_version: str
    personas: dict[str, str | None] = Field(default_factory=dict)
    docs: dict[str, str] = Field(default_factory=dict)
    document_paths: dict[str, str] = Field(default_factory=dict)
    records: dict[str, str] = Field(default_factory=dict)
    grants: dict[str, list[str]] = Field(default_factory=dict)
    permsets: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    stage_history: list[StageHistoryEntry] = Field(default_factory=list)
    m365_target: M365SeedTarget | None = None
    m365_groups: dict[str, str] = Field(default_factory=dict)
    operational_graph: tuple[OperationalGraphItemBinding, ...] = ()

    @model_validator(mode="after")
    def _operational_graph_receipt(self) -> SeedManifest:
        completed = tuple(entry.stage for entry in self.stage_history)
        if not self.operational_graph:
            if "operational_graph" in completed:
                raise ValueError("operational_graph stage is missing its exact item receipts")
            return self
        validate_binding_set(self.operational_graph)
        if set(self.m365_groups) != {"ops", "approver", "frontline"}:
            raise ValueError("operational graph receipts require the exact M365 group bindings")
        validate_binding_acls(
            self.operational_graph,
            ops_group=self.m365_groups["ops"],
            approver_group=self.m365_groups["approver"],
            frontline_group=self.m365_groups["frontline"],
        )
        if "operational_graph" not in completed:
            raise ValueError(
                "operational graph receipts require a completed operational_graph stage"
            )
        for binding in self.operational_graph:
            key = manifest_record_key(binding.entity_class, binding.record_id)
            if self.records.get(key) != binding.item_id:
                raise ValueError(f"operational graph record {key!r} has no exact live item id")
            if self.document_paths.get(key) != binding.path:
                raise ValueError(f"operational graph record {key!r} has no exact source path")
            if tuple(self.grants.get(key, ())) != binding.acl:
                raise ValueError(f"operational graph record {key!r} has no exact ACL grant receipt")
        return self

    def save(self, manifest_dir: Path) -> Path:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_dir / f"{self.tenant}.json"
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=manifest_dir,
                prefix=f".{self.tenant}.",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path

    @classmethod
    def load(cls, manifest_dir: Path, tenant: Tenant) -> SeedManifest:
        return cls.model_validate_json((manifest_dir / f"{tenant}.json").read_bytes())

    @classmethod
    def delete(cls, manifest_dir: Path, tenant: Tenant) -> None:
        (manifest_dir / f"{tenant}.json").unlink(missing_ok=True)
