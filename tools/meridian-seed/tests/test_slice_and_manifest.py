# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed slice mapping, stage partition, hash failure, and manifest round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.meridian_seed.errors import SeedError
from ne.meridian_seed.manifest import SeedManifest, StageHistoryEntry
from ne.meridian_seed.slice import slice_corpus


def test_slice_has_exact_stage_partition_and_salesforce_mapping() -> None:
    selection = slice_corpus()
    assert len(selection.documents) == 12
    assert len(selection.documents_for_stage("initial")) == 11
    assert [row.artifact_id for row in selection.documents_for_stage("supersession")] == [
        "art-dispute-sop-v4"
    ]
    v3 = next(row for row in selection.documents if row.artifact_id == "art-dispute-sop-v3")
    v4 = next(row for row in selection.documents if row.artifact_id == "art-dispute-sop-v4")
    assert v3.sharepoint_path == v4.sharepoint_path
    assert len(selection.accounts) == 10
    assert len(selection.contacts) == 15
    assert len(selection.cases) == 19
    assert {row.case_type for row in selection.cases} == {
        "Servicing",
        "Complaint",
        "Reg E Dispute",
    }


def test_slice_fails_closed_on_hash_drift(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "meridian" / "fixtures" / "meridian-v1"
    # Copying is a test-local filesystem setup, not fixture generation.
    import shutil

    shutil.copytree(source, tmp_path / "fixture")
    (tmp_path / "fixture" / "assets" / "fee-schedule-2026.json").write_bytes(b"tampered")
    with pytest.raises(SeedError, match="hash mismatch"):
        slice_corpus(tmp_path / "fixture")


def test_manifest_round_trip_preserves_real_stage_history(tmp_path: Path) -> None:
    manifest = SeedManifest(
        tenant="m365",
        seeded_at="2026-08-18T10:00:00+00:00",
        corpus_version="meridian-v1",
        personas={"ops": "ops@example.invalid"},
        docs={"art-dispute-sop-v3": "item-sop"},
        document_paths={"art-dispute-sop-v3": "Debit Card Dispute Handling SOP.docx"},
        stage_history=[
            StageHistoryEntry(stage="initial", completed_at="2026-08-18T10:01:00+00:00")
        ],
    )
    path = manifest.save(tmp_path)
    assert SeedManifest.load(tmp_path, "m365") == manifest
    SeedManifest.delete(tmp_path, "m365")
    assert not path.exists()
