# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""All-artifact ACL decisions, persona traps, and year/version near-duplicates."""

from __future__ import annotations

from difflib import SequenceMatcher
from zipfile import ZipFile

from ne.meridian.models import PERSONA_IDS, MeridianCorpus

from .conftest import FIXTURES


def _docx_body(version: str) -> str:
    path = FIXTURES / "assets" / f"debit-card-dispute-handling-sop-{version}.docx"
    with ZipFile(path) as archive:
        return archive.read("word/document.xml").decode()


def test_every_artifact_has_complete_nonoverlapping_acl(corpus: MeridianCorpus) -> None:
    for artifact in corpus.artifacts:
        allowed = set(artifact.acl.allowed_personas)
        denied = set(artifact.acl.denied_personas)
        assert allowed
        assert not allowed & denied
        assert allowed | denied == set(PERSONA_IDS)


def test_every_persona_pair_has_bidirectional_allow_deny_evidence(corpus: MeridianCorpus) -> None:
    artifacts = corpus.artifacts
    for left in PERSONA_IDS:
        for right in PERSONA_IDS:
            if left == right:
                continue
            assert any(
                left in artifact.acl.allowed_personas and right in artifact.acl.denied_personas
                for artifact in artifacts
            ), f"missing allow({left})/deny({right}) artifact"


def test_versioned_families_have_one_current_and_are_near_duplicates(
    corpus: MeridianCorpus,
) -> None:
    families: dict[str, list[object]] = {}
    for artifact in corpus.artifacts:
        families.setdefault(artifact.family, []).append(artifact)
    versioned = {name: rows for name, rows in families.items() if len(rows) > 1}
    assert set(versioned) == {
        "debit-card-dispute-sop",
        "collections-policy",
        "reg-e-operations-bulletin",
    }
    for family, rows in versioned.items():
        assert sum(row.current for row in rows) == 1, family  # type: ignore[attr-defined]

    assert SequenceMatcher(None, _docx_body("v3"), _docx_body("v4")).ratio() > 0.75
    for stem in ("consumer-auto-collections-policy", "reg-e-operations-bulletin"):
        older = (FIXTURES / "assets" / f"{stem}-2025.md").read_text()
        newer = (FIXTURES / "assets" / f"{stem}-2026.md").read_text()
        assert SequenceMatcher(None, older, newer).ratio() > 0.70


def test_sharepoint_stage_partition_and_same_path_supersession(corpus: MeridianCorpus) -> None:
    initial = [row for row in corpus.artifacts if row.seed_stage == "initial"]
    supersession = [row for row in corpus.artifacts if row.seed_stage == "supersession"]
    assert len(initial) == 11
    assert [row.artifact_id for row in supersession] == ["art-dispute-sop-v4"]
    v3 = next(row for row in initial if row.artifact_id == "art-dispute-sop-v3")
    assert v3.sharepoint_path == supersession[0].sharepoint_path
    assert v3.filename != supersession[0].filename
