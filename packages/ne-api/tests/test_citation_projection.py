# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed source-link projection for ACL-trimmed citations."""

from __future__ import annotations

import pytest

from ne.api.wire import citation_payloads
from ne.contracts.types import Chunk, Citation, RetrievalResult, ScoredChunk

_URL = "https://tenant.sharepoint.com/sites/ops/doc-1"


def _result(
    *,
    metadata: dict[str, object] | None = None,
    chunks: tuple[ScoredChunk, ...] | None = None,
) -> RetrievalResult:
    citation = Citation(
        chunk_id="chunk-1",
        document_id="doc-1",
        source_system="m365",
        source_ref="drive-1:item-1",
    )
    served = ScoredChunk(
        chunk=Chunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            text="served",
            metadata=metadata
            or {
                "connector": "m365",
                "source_system": "m365",
                "source_ref": "drive-1:item-1",
                "source_url": _URL,
            },
            acl=("ops",),
        ),
        score=1.0,
    )
    return RetrievalResult(chunks=chunks or (served,), citations=(citation,))


def test_exact_served_identity_projects_canonical_locator() -> None:
    assert citation_payloads(_result()) == [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "source_system": "m365",
            "source_ref": "drive-1:item-1",
            "source_url": _URL,
        }
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connector", "salesforce"),
        ("source_system", "salesforce"),
        ("source_ref", "drive-1:other-item"),
        ("source_url", None),
        ("source_url", "https://tenant.sharepoint.com/sites/ops/doc-1?token=secret"),
        ("source_url", "https://user:secret@tenant.sharepoint.com/sites/ops/doc-1"),
    ],
)
def test_untrusted_or_mismatched_metadata_omits_locator(field: str, value: object) -> None:
    metadata: dict[str, object] = {
        "connector": "m365",
        "source_system": "m365",
        "source_ref": "drive-1:item-1",
        "source_url": _URL,
    }
    metadata[field] = value

    assert citation_payloads(_result(metadata=metadata)) == [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "source_system": "m365",
            "source_ref": "drive-1:item-1",
        }
    ]


def test_unmatched_or_conflicting_served_chunks_omit_locator() -> None:
    mismatch = ScoredChunk(
        chunk=Chunk(
            chunk_id="different-chunk",
            document_id="doc-1",
            text="other",
            metadata={
                "connector": "m365",
                "source_system": "m365",
                "source_ref": "drive-1:item-1",
                "source_url": _URL,
            },
            acl=("ops",),
        ),
        score=1.0,
    )
    assert "source_url" not in citation_payloads(_result(chunks=(mismatch,)))[0]

    first = _result().chunks[0]
    conflict = first.model_copy(
        update={
            "chunk": first.chunk.model_copy(
                update={
                    "metadata": {
                        **first.chunk.metadata,
                        "source_url": "https://tenant.sharepoint.com/sites/ops/doc-2",
                    }
                }
            )
        }
    )
    assert "source_url" not in citation_payloads(_result(chunks=(first, conflict)))[0]
