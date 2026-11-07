# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The seed-manifest round-trips through disk with every section preserved."""

from __future__ import annotations

from pathlib import Path

from ne.acme_seed.manifest import SeedManifest


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = SeedManifest(
        tenant="slack",
        seeded_at="2026-07-19T00:00:00+00:00",
        corpus_version="acme-v1",
        personas={"per-0006": "U0006", "per-0000": None},
        docs={"doc-0011": "item-0011"},
        records={"acct-0000": "live-acct-0000"},
        channels={"acme-general": "C-acme-general"},
        messages={"doc-0011-f0": {"channel": "C-acme-general", "ts": "ts-1"}},
        permsets={"Acme_Case_Reader": "0PS-1"},
        grants={"doc-0011": ["g-exec-id"]},
        warnings=["a note"],
    )
    path = manifest.save(tmp_path)
    assert path == tmp_path / "slack.json"

    loaded = SeedManifest.load(tmp_path, "slack")
    assert loaded == manifest
    assert loaded.docs == {"doc-0011": "item-0011"}
    assert loaded.records == {"acct-0000": "live-acct-0000"}
    assert loaded.personas == {"per-0006": "U0006", "per-0000": None}
    assert loaded.channels == {"acme-general": "C-acme-general"}
    assert loaded.messages == {"doc-0011-f0": {"channel": "C-acme-general", "ts": "ts-1"}}
    assert loaded.warnings == ["a note"]

    SeedManifest.delete(tmp_path, "slack")
    assert not path.exists()
