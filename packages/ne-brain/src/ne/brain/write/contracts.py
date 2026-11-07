# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Value types for the governed KnowledgeWrite path (NE-023).

The write path stamps every stored chunk with a first-class, queryable
:class:`ProvenanceStamp` (who/what/when/source + a provenance CLASS), validates
every write against a JSON-Schema :class:`ShapeContract` BEFORE it can enter the
store (T1 "shapes as contract"), and carries an ACL onto curated writes exactly
as the NE-026 ``IngestSource.acl`` pattern does.

Reserved chunk-metadata key
---------------------------
The stamp is written under the reserved ``metadata['provenance']`` key. This is
the NEW reserved chunk-metadata key introduced by NE-023, sitting alongside the
NE-026 spine's reserved ``metadata['source']`` key. Like ``'source'`` it is
written WHOLESALE (never deep-merged) so a re-stamp never leaves stale leftover
sub-keys, and it is addressable by the store's verbatim dotted filter grammar as
``'metadata.provenance.provenance_class'`` — the C7 audit/replay hook ("why does
the brain believe X").

Contract-freeze note: :class:`CuratedKnowledgeItem` SUBCLASSES the frozen
``ne.contracts.knowledge.KnowledgeItem`` (additive consumer-side composition, NOT
a contract edit — ne-contracts stays byte-identical).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.brain.parsed_evidence import ParsedDocumentEvidenceDescriptor
from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.knowledge import KnowledgeItem
from ne.contracts.types import Chunk

#: The reserved chunk-metadata key the provenance stamp is written under. Mirrors
#: the NE-026 spine's reserved ``'source'`` key (see :mod:`ne.brain.ingest.spine`).
PROVENANCE_METADATA_KEY = "provenance"


class _Frozen(BaseModel):
    """Base for immutable write-path value objects."""

    model_config = ConfigDict(frozen=True)


class ProvenanceClass(StrEnum):
    """How a stored piece of knowledge came to be believed.

    ``LEARNED_BY_AGENT`` is the class the NE-037 agent-memory promotion path
    stamps when it writes a learned fact back into the brain.
    """

    INGESTED = "ingested"
    CURATED = "curated"
    LEARNED_BY_AGENT = "learned-by-agent"


class ProvenanceStamp(_Frozen):
    """First-class, queryable provenance on every governed write.

    Serialized under ``chunk.metadata['provenance']``; ``provenance_class`` is
    retrievable via the store filter ``'metadata.provenance.provenance_class'``.
    """

    provenance_class: ProvenanceClass
    source: str
    """The ``KnowledgeItem.provenance`` string this write originated from."""
    actor: str
    written_at: str
    """UTC ISO-8601 timestamp the write was stamped."""
    write_id: str
    """A fresh uuid4 hex identifying this individual write."""
    shape: str
    """``"<name>@<version>"`` of the shape contract that admitted the write."""


class ShapeContract(_Frozen):
    """A named+versioned JSON-Schema (Draft 2020-12) shape a write must satisfy.

    An INVALID schema fails LOUD at construction (``ValueError`` wrapping the
    jsonschema ``SchemaError``) — a malformed contract can never silently admit
    or reject writes at runtime.
    """

    name: str
    version: str
    json_schema: AnyMap = FrozenDict()

    @model_validator(mode="after")
    def _check_schema(self) -> ShapeContract:
        try:
            jsonschema.Draft202012Validator.check_schema(_to_plain(self.json_schema))
        except SchemaError as exc:
            raise ValueError(
                f"invalid JSON-Schema for shape contract {self.name!r}@{self.version}: "
                f"{exc.message}"
            ) from exc
        return self


class CuratedKnowledgeItem(KnowledgeItem):
    """A ``KnowledgeItem`` carrying governance attributes as TYPED fields.

    Governance attributes are typed fields ONLY — there are no reserved
    input-metadata keys. A plain ``KnowledgeItem`` submitted to the writer takes
    the writer's defaults (``acl=()``, the writer's default provenance class, and
    the writer's default shape).
    """

    acl: tuple[str, ...] = ()
    """ACL labels stamped verbatim onto the stored ``Chunk.acl`` (the NE-026
    ``IngestSource.acl`` pattern; trimming enforced later by NE-043)."""
    provenance_class: ProvenanceClass = ProvenanceClass.CURATED
    shape: str | None = None
    """Overrides the writer's ``default_shape`` for this item when set."""
    parsed_source_evidence: ParsedDocumentEvidenceDescriptor | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    """Producer-owned, in-memory evidence; never serialized or persisted raw."""


def _to_plain(value: Any) -> Any:
    """Recursively convert a frozen/JSON-shaped value into plain ``dict``/``list``.

    ``jsonschema``'s validators type-check ``"object"`` via ``isinstance(x, dict)``
    and a ``FrozenDict`` is a ``Mapping`` but not a ``dict``; this normalizes both
    schemas and instance payloads so validation sees genuine ``dict``/``list``.
    """
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_plain(v) for v in value]
    return value


def stamped_metadata(metadata: Mapping[str, Any], stamp: ProvenanceStamp) -> dict[str, Any]:
    """Return ``metadata`` with the reserved ``'provenance'`` key set WHOLESALE.

    Never deep-merges — exactly the NE-026 ``_merged_metadata`` reserved-key
    discipline for its ``'source'`` key. The stamp is serialized to a plain JSON
    dict so it round-trips through the store payload and stays filter-addressable.
    """
    merged = dict(metadata)
    merged[PROVENANCE_METADATA_KEY] = stamp.model_dump(mode="json")
    return merged


def provenance_from_chunk(chunk: Chunk) -> ProvenanceStamp | None:
    """Read the provenance stamp back off a stored chunk (``None`` if unstamped)."""
    raw = chunk.metadata.get(PROVENANCE_METADATA_KEY)
    if raw is None:
        return None
    return ProvenanceStamp.model_validate(_to_plain(raw))


class KnowledgeWriteError(Exception):
    """Base for all package-local KnowledgeWrite errors.

    The frozen ``KnowledgeWrite.curate``/``quarantine`` signatures return
    ``None``, so error semantics live in these package-local exceptions.
    """


class ShapeValidationError(KnowledgeWriteError):
    """A write failed its shape contract (raised by ``curate``; ``upsert`` quarantines)."""

    def __init__(self, item_id: str, shape: str, messages: tuple[str, ...]) -> None:
        self.item_id = item_id
        self.shape = shape
        self.messages = messages
        super().__init__(f"{item_id} failed shape {shape}: {'; '.join(messages)}")


class KnowledgeItemNotFoundError(KnowledgeWriteError):
    """A curate/quarantine target does not exist in the store."""


class InvalidPatchError(KnowledgeWriteError):
    """A curate patch used a disallowed key (or the reserved provenance key)."""


class UnknownShapeError(KnowledgeWriteError):
    """The shape contract named by a stored chunk is no longer registered."""


class SinkRequiredError(KnowledgeWriteError):
    """KnowledgeWriter was constructed without a required audit_sink/violation_sink.

    A governed write with no audit/violation transport is fail-open (Checkpoint-D
    boundary-audit #5); construct with real sinks.
    """


class KnowledgeAuditError(KnowledgeWriteError):
    """The audit append for a governed write FAILED after the store mutation.

    Raised (NE-CKD6) when a governed write's per-item audit append fails AFTER
    the underlying store write has been committed. To keep the governed write
    ATOMIC (store+audit together or neither) under a single audit-store fault,
    the writer has UNWOUND the not-yet-audited store writes via the governed
    park-before-delete discipline (full payload parked in the quarantine lane,
    then deleted from the store), so no durable+retrievable chunk exists without
    a matching audit record under a single fault.

    * ``unwound`` — item ids whose store writes were rolled back (parked+deleted).
    * ``audited`` — item ids that remain durable WITH their audit records
      (per-item store+audit atomicity: an item whose success-audit already
      durably appended is complete and is NOT unwound).

    A retry after sink recovery heals idempotently (the same ids re-upsert and
    re-audit; the parked rows clear on the durable re-write).
    """

    def __init__(
        self,
        message: str,
        *,
        action: str,
        unwound: tuple[str, ...] = (),
        audited: tuple[str, ...] = (),
    ) -> None:
        self.action = action
        self.unwound = unwound
        self.audited = audited
        super().__init__(message)


__all__ = [
    "PROVENANCE_METADATA_KEY",
    "CuratedKnowledgeItem",
    "InvalidPatchError",
    "KnowledgeAuditError",
    "KnowledgeItemNotFoundError",
    "KnowledgeWriteError",
    "ProvenanceClass",
    "ProvenanceStamp",
    "ShapeContract",
    "ShapeValidationError",
    "SinkRequiredError",
    "UnknownShapeError",
    "provenance_from_chunk",
    "stamped_metadata",
]
