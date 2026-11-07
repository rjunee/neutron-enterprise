# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Skill Forge contract surface (ADR-0011, additive in 1.8.0).

The frozen, substrate-neutral types every Skill Forge unit builds on:

- :class:`SkillPack` — the portable skill artifact (a lossless superset of the
  cross-agent SKILL.md standard). Procedural-ONLY in v1: there is deliberately
  no executable-artifact field, so skill firing stays inside the C6 governed-
  tool chokepoint.
- :class:`WorkflowSnapshot` / :class:`WorkflowStep` — a completed-work record.
  A step carries an action NAME + outcome only, NEVER argument values (C12
  redaction discipline; the absence of an ``args`` field is the structural
  guarantee).
- :class:`SkillInvocation` — one skill firing, for usage telemetry.
- :class:`AuthoringStandard` — the typed house authoring standard passed to
  ``SkillSubstrate.author``.
- Two canonical content hashes, both computed fields over canonical JSON:
  :attr:`SkillPack.content_hash` (``acl_labels`` INCLUDED — version identity +
  hash-pinned serve) and :attr:`SkillPack.routing_hash` (``acl_labels``
  EXCLUDED — the eval gate, so relabel-at-apply does not invalidate every
  eval). Both are frozen now because retrofitting either after 1.8.0 ships
  would break every stored hash (SPEC section 5 D-12).
- :class:`SnapshotPage` / :class:`InvocationPage` — self-documenting cursor-pull
  return types with an explicit ``exhausted`` end-of-stream flag.
- :class:`SkillSubstrate` — the Protocol everything about skills that differs
  per agent backend implements. The ``ne.plugins.skill_substrate`` entry-point
  slot (named normatively in ADR-0011) is where bindings register.

acl_labels is the shared ne-permissions ACL vocabulary (design section 7.7 —
ONE ACL), NOT a bespoke scope map; it is assigned by the approver at the gate,
never model-supplied.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from ne.contracts.frozen import AnyMap, FrozenDict


class SkillSource(StrEnum):
    """Where a skill pack came from — its provenance channel, not its ACL."""

    BUNDLED = "bundled"
    ENGAGEMENT = "engagement"
    FORGED = "forged"
    LEARNED = "learned"
    MANUAL = "manual"


class AuthoringStandard(BaseModel):
    """The house authoring standard passed to :meth:`SkillSubstrate.author`.

    A typed parameter (not a bare ``AnyMap``) so every binding codes against a
    known-shape contract. The ``extra`` bag preserves additive evolution: a new
    standard knob can ride inside it without a Protocol-breaking signature
    change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description_max_chars: int = Field(default=500, ge=1)
    required_sections: tuple[str, ...] = (
        "When to Use",
        "Procedure",
        "Pitfalls",
        "Verification",
    )
    body_max_chars: int = Field(default=20_000, ge=1)
    extra: AnyMap = FrozenDict()


class WorkflowStep(BaseModel):
    """One observed action in completed work.

    ``action`` is the tool/action NAME only and ``outcome`` a scrubbed result —
    NEVER argument values (C12 redaction discipline; mirrors the middleware
    audit-detail rule). The ABSENCE of an argument field is the structural
    guarantee: there is no ``args`` field, ever. ``detail_ref`` is an opaque
    reference back into substrate-side records.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    outcome: str
    detail_ref: str = ""


class WorkflowSnapshot(BaseModel):
    """A substrate-neutral record of completed multi-step work.

    ``intent`` is a scrubbed one-line statement of what was asked. Datetimes are
    validated timezone-aware, and ``finished_at`` may not precede
    ``started_at``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    substrate_id: str
    intent: str
    steps: tuple[WorkflowStep, ...]
    succeeded: bool
    principal_id: str
    session_id: str
    department: str | None = None
    started_at: datetime
    finished_at: datetime

    @field_validator("started_at", "finished_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("started_at/finished_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _non_negative_duration(self) -> WorkflowSnapshot:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class SkillInvocation(BaseModel):
    """One skill firing, for the registry's usage fold.

    ``run_succeeded`` is ``None`` when the outcome is unknown / not yet
    attributable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    version: int = Field(ge=1)
    principal_id: str
    session_id: str
    fired_at: datetime
    run_succeeded: bool | None = None

    @field_validator("fired_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("fired_at must be timezone-aware")
        return value


class SkillPack(BaseModel):
    """The portable skill artifact — a lossless superset of SKILL.md.

    Procedural-ONLY in v1: there is deliberately NO executable-artifact field
    (no ``scripts``/code). Adding one later would be reviewed as a
    redaction/governance regression, because it would move skill firing outside
    the C6 governed-tool chokepoint.

    Field hash-classification (frozen by the tripwire test + ADR-0011):

    - ORDER-SIGNIFICANT, hashed as-is: ``name``, ``version``, ``description``,
      ``triggers`` (order is meaningful — the eval targets), ``body``,
      ``source``, ``category``.
    - SET-SEMANTIC, normalized to ``sorted(set(...))`` before hashing:
      ``tags``, ``requires_tools``, ``platforms`` (and ``acl_labels``, which is
      hashed into ``content_hash`` only).
    - EXCLUDED from both hashes: ``provenance`` (metadata, not served/routed
      content) and the two computed hashes themselves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: int = Field(ge=1)
    description: str = Field(max_length=500)
    triggers: tuple[str, ...] = Field(min_length=1)
    body: str
    source: SkillSource
    category: str = ""
    tags: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    acl_labels: tuple[str, ...] = ()
    """Shared ne-permissions ACL labels (design section 7.7) — NOT a scope map.
    ``()`` means GLOBAL (visible to all). The SAME vocabulary PrincipalExpander
    matches chunks against; assigned by the approver at the gate, never
    model-supplied."""
    provenance: AnyMap = FrozenDict()
    """Hash-EXCLUDED metadata. Well-known keys: ``snapshot_id``, ``session_id``,
    ``principal_id``, ``config_hash``, ``review_id`` (plus dates). Additive by
    construction — new keys ride inside without a contract revision."""

    @field_validator("tags", "requires_tools", "platforms", "acl_labels")
    @classmethod
    def _normalize_set_semantic(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Canonicalize set-semantic fields to sorted, de-duplicated order.

        ``triggers`` is deliberately EXCLUDED: its order is meaningful, so it is
        never sorted (design section 1.7 overrides any list that treated
        triggers as set-semantic).
        """
        return tuple(sorted(set(value)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Full content hash (``acl_labels`` INCLUDED) — version identity +
        hash-pinned serve. Re-labelling a pack is a content change."""
        return skill_content_hash(self)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def routing_hash(self) -> str:
        """Label-EXCLUDED hash — the eval gate. Relabel-at-apply keeps every
        eval valid because ``acl_labels`` never enter this hash."""
        return skill_routing_hash(self)


#: Fields hashed in declared (order-significant) form.
_HASH_ORDERED = frozenset(
    {"name", "version", "description", "triggers", "body", "source", "category"}
)
#: Set-semantic fields, normalized to sorted+deduped form before hashing.
_HASH_SORTED_SET = frozenset({"tags", "requires_tools", "platforms"})
#: Hashed into ``content_hash`` only (EXCLUDED from ``routing_hash``).
_HASH_ACL = frozenset({"acl_labels"})
#: Excluded from BOTH hashes.
_HASH_EXCLUDED = frozenset({"provenance"})


def _hash_payload(pack: SkillPack, *, include_acl: bool) -> dict[str, object]:
    """Build the canonical hash payload from a (pre-normalized) pack.

    Set-semantic fields are already ``sorted(set(...))`` by the field validator,
    so they are read as-is. ``triggers`` order is preserved. ``provenance`` and
    the computed hashes are excluded. ``acl_labels`` is present for the content
    hash only — the routing hash OMITS the key entirely.
    """
    payload: dict[str, object] = {
        "name": pack.name,
        "version": pack.version,
        "description": pack.description,
        "triggers": list(pack.triggers),
        "body": pack.body,
        "source": pack.source.value,
        "category": pack.category,
        "tags": list(pack.tags),
        "requires_tools": list(pack.requires_tools),
        "platforms": list(pack.platforms),
    }
    if include_acl:
        payload["acl_labels"] = list(pack.acl_labels)
    return payload


def _canonical_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def skill_content_hash(pack: SkillPack) -> str:
    """sha256 hex over canonical pack bytes WITH ``acl_labels``.

    Version identity + hash-pinned serve bind to this hash. ``provenance`` and
    the computed hashes are excluded; set-semantic fields are canonically
    sorted; ``triggers`` order is preserved.
    """
    return _canonical_sha256(_hash_payload(pack, include_acl=True))


def skill_routing_hash(pack: SkillPack) -> str:
    """sha256 hex over canonical pack bytes WITHOUT ``acl_labels``.

    The eval gate binds to this hash so relabel-at-apply does not invalidate
    every stored eval. Identical to :func:`skill_content_hash` except the
    ``acl_labels`` key is omitted entirely.
    """
    return _canonical_sha256(_hash_payload(pack, include_acl=False))


class SnapshotPage(NamedTuple):
    """A cursor-pull page of :class:`WorkflowSnapshot` records.

    ``cursor`` is an OPAQUE, substrate-defined string resume token (NOT the
    flywheel integer ``after_id`` — this is a genuinely opaque string cursor).
    ``exhausted`` is ``True`` when no further items are currently available; the
    ``cursor`` is stable on an empty/exhausted page.
    """

    items: Sequence[WorkflowSnapshot]
    cursor: str
    exhausted: bool


class InvocationPage(NamedTuple):
    """A cursor-pull page of :class:`SkillInvocation` records.

    ``cursor`` is an OPAQUE, substrate-defined string resume token (NOT the
    flywheel integer ``after_id``). ``exhausted`` is ``True`` when no further
    items are currently available; the ``cursor`` is stable on an
    empty/exhausted page.
    """

    items: Sequence[SkillInvocation]
    cursor: str
    exhausted: bool


class SkillSubstrate(Protocol):
    """Everything about skills that differs per agent backend.

    Cursor-pull throughout: the core's scheduled jobs drive ALL I/O; a substrate
    never calls into the core. Bindings register under the
    ``ne.plugins.skill_substrate`` entry-point slot (ADR-0011).
    """

    @property
    def substrate_id(self) -> str: ...

    async def snapshots(self, *, cursor: str | None = None, limit: int = 50) -> SnapshotPage:
        """Completed multi-step work since ``cursor``. HOW completed work is
        observed is the substrate's business (audit-derived here; transcript-
        derived on Claude Code)."""
        ...

    async def author(
        self,
        snapshot: WorkflowSnapshot,
        *,
        standard: AuthoringStandard,
        existing: SkillPack | None = None,
    ) -> SkillPack:
        """LLM-author (never template) a DRAFT pack from a snapshot.

        ``existing`` set ⇒ author an UPDATE with the same ``name`` and
        ``version`` + 1 (the near-dup path). The output is a DRAFT: empty
        ``provenance`` and ``acl_labels=()``; the core alone validates it
        against the house standard and decides whether it is ever proposed.
        """
        ...

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None:
        """Make the given ACTIVE SET discoverable on one discovery surface.

        The packs arrive ALREADY ACL-filtered for that surface's principal by
        the core — the substrate PROJECTS them and makes NO authorization
        decision. ``surface_key`` is a session/principal for an interactive
        agent, or a shared surface for cron.
        """
        ...

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None:
        """Remove retired/deactivated skills from the discovery surface.

        Kept as its own method (rather than folded into a set-replace
        ``materialize``) so a targeted retirement/security-pull removes a skill
        without recomputing every surface's active set (ADR-0011).
        """
        ...

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500
    ) -> InvocationPage:
        """Firings since ``cursor``, for the registry's usage fold."""
        ...
