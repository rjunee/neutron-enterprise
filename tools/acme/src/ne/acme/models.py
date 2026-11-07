# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Sidecar Pydantic models for data the frozen ne-schema (NE-004) does not model.

ne-schema's ``OperationalEntity`` subclasses use ``extra="forbid"``, and the
schema itself is frozen (generated from LinkML, not hand-editable) — so the org
chart (departments/managers/groups), ACLs, and ground-truth facts that make the
Acme corpus's permission structure real live here as separate models, never as
extra fields bolted onto the ne-schema records.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Sidecar(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrgMember(_Sidecar):
    person_id: str
    department: str
    role: str
    manager_id: str | None  # None only for the CEO
    grade: Literal["exec", "manager", "ic"]


class Group(_Sidecar):
    name: str
    member_ids: list[str]


class OrgChart(_Sidecar):
    members: list[OrgMember]
    groups: list[Group]


class AclEntry(_Sidecar):
    doc_id: str
    visibility: Literal["public", "department", "exec_only", "named"]
    allowed_groups: list[str]
    allowed_people: list[str]


class Fact(_Sidecar):
    fact_id: str
    doc_id: str
    statement: str
    question: str
    answer: str


class DocGroundTruth(_Sidecar):
    doc_id: str
    visibility: Literal["public", "department", "exec_only", "named"]
    reader_ids: list[str]  # sorted, fully materialized
    facts: list[Fact]


class GroundTruth(_Sidecar):
    docs: list[DocGroundTruth]


class CorpusManifest(_Sidecar):
    corpus_version: str
    seed: int
    generator: str
    counts: dict[str, int]
    sha256: dict[str, str]
