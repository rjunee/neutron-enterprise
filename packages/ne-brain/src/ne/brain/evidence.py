# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Safe, package-local evidence descriptors for committed vector artifacts.

The frozen ``ne-contracts`` ChunkStore seam intentionally does not grow a
presentation concern.  Stores may implement :class:`VectorEvidenceSource`
structurally so the governed writer can bind a safe receipt to the same audit
event as a successful write.  The descriptor never contains a URL, collection,
table, vector, or credential-bearing value; its index reference is an opaque
SHA-256 fingerprint over adapter-owned identity material.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any, Protocol, runtime_checkable

from ne.contracts.types import Chunk

__all__ = [
    "VectorEvidenceDescriptor",
    "VectorEvidenceSource",
    "VectorEvidenceWriteOutcome",
    "VectorEvidenceWriteReceipt",
    "VectorEvidenceWriteSource",
    "VectorWriteSettlementHandle",
    "VectorWriteSettlementSource",
    "submitted_embedding_evidence",
    "submitted_embedding_receipt",
    "validate_vector_evidence_receipts",
    "validated_dense_embedding",
    "vector_evidence_descriptor",
]

_NUMERIC_PREVIEW_COMPONENTS = 8


@dataclass(frozen=True)
class VectorEvidenceDescriptor:
    """Safe facts about an acknowledged vector write.

    The static adapter descriptor leaves ``numeric_preview`` absent.  A real
    adapter may add the bounded prefix of the exact dense embedding submitted
    for one chunk, but must not return that enriched descriptor until its
    backend has acknowledged the corresponding upsert.
    """

    model_id: str
    dimensions: int
    index_fingerprint: str
    numeric_preview: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("vector evidence model_id must be non-empty")
        if isinstance(self.dimensions, bool) or self.dimensions < 1:
            raise ValueError("vector evidence dimensions must be positive")
        if len(self.index_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.index_fingerprint
        ):
            raise ValueError("vector evidence index_fingerprint must be lowercase SHA-256")
        if self.numeric_preview is not None:
            if not self.numeric_preview:
                raise ValueError("vector evidence numeric_preview must be non-empty when present")
            if len(self.numeric_preview) > min(self.dimensions, _NUMERIC_PREVIEW_COMPONENTS):
                raise ValueError("vector evidence numeric_preview exceeds the bounded prefix")
            for component in self.numeric_preview:
                if (
                    isinstance(component, bool)
                    or not isinstance(component, Real)
                    or not math.isfinite(float(component))
                ):
                    raise ValueError("vector evidence numeric_preview must contain finite numbers")

    @property
    def index_reference(self) -> str:
        """Return the deliberately opaque, non-routable index reference."""
        return f"sha256:{self.index_fingerprint}"


@dataclass(frozen=True)
class VectorEvidenceWriteReceipt:
    """Identity-bound evidence returned only after a backend acknowledges a write."""

    chunk_id: str
    descriptor: VectorEvidenceDescriptor

    def __post_init__(self) -> None:
        if not self.chunk_id.strip():
            raise ValueError("vector evidence receipt chunk_id must be non-empty")


class VectorWriteSettlementHandle:
    """Opaque, store-instance-bound authority to settle one acknowledged write.

    The handle deliberately carries no vectors or payloads. Adapter-owned state
    retains exact preimages until the writer partitions every acknowledged id
    into durable+audited or unaudited+restore. This is follow-up compensation
    within one live process, not crash-safe or cross-process atomicity.
    """

    __slots__ = ("__chunk_ids", "__consumed", "__owner", "__token")

    def __init__(self, *, owner: object, token: str, chunk_ids: Sequence[str]) -> None:
        ids = tuple(chunk_ids)
        if not token or len(ids) != len(set(ids)) or any(not value for value in ids):
            raise ValueError("invalid vector settlement handle")
        self.__owner = owner
        self.__token = token
        self.__chunk_ids = frozenset(ids)
        self.__consumed = False

    def _claim(
        self,
        *,
        owner: object,
        keep_chunk_ids: Sequence[str],
        restore_chunk_ids: Sequence[str],
    ) -> str:
        if owner is not self.__owner:
            raise ValueError("vector settlement handle belongs to another store")
        if self.__consumed:
            raise ValueError("vector settlement handle was already consumed")
        keep = tuple(keep_chunk_ids)
        restore = tuple(restore_chunk_ids)
        if len(keep) != len(set(keep)) or len(restore) != len(set(restore)):
            raise ValueError("vector settlement partitions contain duplicate chunk ids")
        if set(keep) & set(restore) or set(keep) | set(restore) != self.__chunk_ids:
            raise ValueError("vector settlement partitions must exactly cover the acknowledged ids")
        self.__consumed = True
        return self.__token

    def __repr__(self) -> str:
        return "VectorWriteSettlementHandle(<opaque>)"

    def __copy__(self) -> Any:
        raise TypeError("vector settlement handles cannot be copied")

    def __deepcopy__(self, memo: object) -> Any:
        raise TypeError("vector settlement handles cannot be copied")

    def __reduce__(self) -> Any:
        raise TypeError("vector settlement handles cannot be serialized")


@dataclass(frozen=True)
class VectorEvidenceWriteOutcome:
    """Receipts plus the one-shot authority that resolves their write."""

    receipts: tuple[VectorEvidenceWriteReceipt, ...]
    settlement: VectorWriteSettlementHandle


@runtime_checkable
class VectorEvidenceSource(Protocol):
    """Optional package-local capability implemented by real vector stores."""

    def vector_evidence_descriptor(self) -> VectorEvidenceDescriptor: ...


@runtime_checkable
class VectorEvidenceWriteSource(Protocol):
    """Optional package-local write seam returning identity-bound acknowledged evidence.

    This deliberately does not widen the frozen ``ne-contracts`` ``ChunkStore``
    Protocol. Implementations return exactly one receipt per distinct input
    chunk id, and return nothing when any backend write raises.
    """

    async def upsert_chunks_with_evidence(
        self, chunks: Sequence[Chunk]
    ) -> tuple[VectorEvidenceWriteReceipt, ...]: ...


@runtime_checkable
class VectorWriteSettlementSource(Protocol):
    """Optional atomicity seam for post-write audit settlement."""

    async def upsert_chunks_with_settlement(
        self, chunks: Sequence[Chunk]
    ) -> VectorEvidenceWriteOutcome: ...

    async def settle_vector_write(
        self,
        handle: VectorWriteSettlementHandle,
        *,
        keep_chunk_ids: Sequence[str],
        restore_chunk_ids: Sequence[str],
    ) -> None: ...


def validated_dense_embedding(values: Sequence[float], *, dimensions: int) -> tuple[float, ...]:
    """Return a finite exact-dimension float tuple or fail before an upsert."""
    if isinstance(dimensions, bool) or dimensions < 1:
        raise ValueError("dense embedding dimensions must be positive")
    if len(values) != dimensions:
        raise ValueError(
            f"dense embedding dimension mismatch: expected {dimensions}, received {len(values)}"
        )
    normalized: list[float] = []
    for component in values:
        if (
            isinstance(component, bool)
            or not isinstance(component, Real)
            or not math.isfinite(float(component))
        ):
            raise ValueError("dense embedding components must be finite numbers")
        normalized.append(float(component))
    return tuple(normalized)


def submitted_embedding_evidence(
    descriptor: VectorEvidenceDescriptor, dense: Sequence[float]
) -> VectorEvidenceDescriptor:
    """Add the bounded prefix of one validated dense embedding to ``descriptor``.

    This pure constructor may prepare an unpublished value before I/O. Adapters
    expose it only inside an identity-bound receipt returned after backend
    acknowledgement. It is not a claim about backend-specific normalization or
    stored byte representation.
    """
    values = validated_dense_embedding(dense, dimensions=descriptor.dimensions)
    return replace(descriptor, numeric_preview=values[:_NUMERIC_PREVIEW_COMPONENTS])


def submitted_embedding_receipt(
    chunk_id: str,
    descriptor: VectorEvidenceDescriptor,
    dense: Sequence[float],
) -> VectorEvidenceWriteReceipt:
    """Prepare an identity-bound receipt for later acknowledged return."""
    return VectorEvidenceWriteReceipt(
        chunk_id=chunk_id,
        descriptor=submitted_embedding_evidence(descriptor, dense),
    )


def validate_vector_evidence_receipts(
    receipts: Sequence[VectorEvidenceWriteReceipt],
    expected_chunk_ids: Sequence[str],
) -> tuple[VectorEvidenceWriteReceipt, ...]:
    """Require one unambiguous receipt for every expected chunk id.

    Ordering is deliberately not authority: callers correlate by ``chunk_id``.
    Duplicate input ids are refused because one backend row cannot truthfully
    acknowledge two distinct writes in the same call.
    """
    expected = tuple(expected_chunk_ids)
    expected_set = set(expected)
    if len(expected_set) != len(expected) or any(not chunk_id.strip() for chunk_id in expected):
        raise ValueError("vector evidence expected chunk ids must be distinct and non-empty")
    receipt_ids = tuple(receipt.chunk_id for receipt in receipts)
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValueError("vector evidence receipts contain duplicate chunk ids")
    if len(receipt_ids) != len(expected) or set(receipt_ids) != expected_set:
        raise ValueError("vector evidence receipts do not match the acknowledged chunk ids")
    return tuple(receipts)


def vector_evidence_descriptor(
    *,
    backend: str,
    logical_index: str,
    model_id: str,
    dimensions: int,
) -> VectorEvidenceDescriptor:
    """Build a safe descriptor without exposing ``logical_index`` to readers."""
    normalized_backend = backend.strip()
    normalized_index = logical_index.strip()
    normalized_model = model_id.strip()
    if not normalized_backend or not normalized_index or not normalized_model:
        raise ValueError("vector evidence identity values must be non-empty")
    if isinstance(dimensions, bool) or dimensions < 1:
        raise ValueError("vector evidence dimensions must be positive")
    material = json.dumps(
        {
            "backend": normalized_backend,
            "dimensions": dimensions,
            "logical_index": normalized_index,
            "model_id": normalized_model,
            "wire": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return VectorEvidenceDescriptor(
        model_id=normalized_model,
        dimensions=dimensions,
        index_fingerprint=hashlib.sha256(material).hexdigest(),
    )
