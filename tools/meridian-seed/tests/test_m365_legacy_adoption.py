# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-only, exact-target controls for adopting the legacy M365 manifest."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime

import httpx
import pytest

from ne.meridian_seed.errors import SeedPreflightError
from ne.meridian_seed.m365 import M365Seeder
from ne.meridian_seed.manifest import SeedManifest, StageHistoryEntry
from ne.meridian_seed.personas import PrincipalsMap
from ne.meridian_seed.slice import slice_corpus

from .conftest import build_m365_transport, m365_settings


def _item_id(path: str) -> str:
    if path == "Debit Card Dispute Handling SOP.docx":
        return "item-dispute-sop"
    return "item-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def _legacy(principals: PrincipalsMap) -> tuple[SeedManifest, list[dict[str, object]]]:
    selection = slice_corpus()
    documents = selection.documents_for_stage("initial") + selection.documents_for_stage(
        "supersession"
    )
    manifest = SeedManifest(
        tenant="m365",
        seeded_at=datetime.now(UTC).isoformat(),
        corpus_version=selection.corpus_version,
        personas={
            persona: principals.m365_user_id(persona)
            for persona in ("ops", "approver", "frontline")
        },
        stage_history=[
            StageHistoryEntry(stage="initial", completed_at="2026-08-01T00:00:00+00:00"),
            StageHistoryEntry(stage="supersession", completed_at="2026-08-02T00:00:00+00:00"),
        ],
    )
    for document in documents:
        manifest.docs[document.artifact_id] = _item_id(document.sharepoint_path)
        manifest.document_paths[document.artifact_id] = document.sharepoint_path
        manifest.grants[document.artifact_id] = [
            principals.m365_group_id(persona) for persona in document.acl.allowed_personas
        ]
    children = [
        {"id": item_id, "name": path, "file": {}}
        for path, item_id in sorted(
            {
                path: manifest.docs[artifact] for artifact, path in manifest.document_paths.items()
            }.items()
        )
    ]
    return manifest, children


def test_adoption_proves_exact_legacy_target_and_replay_is_zero_io(
    principals: PrincipalsMap,
) -> None:
    legacy, children = _legacy(principals)
    recorder: list[httpx.Request] = []
    adopted = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(
                recorder=recorder, folder_exists=True, folder_children=children
            ),
        ).adopt_legacy(slice_corpus(), prior_manifest=legacy, generation_id="legacy-gen-1")
    )
    assert adopted.m365_target is not None
    assert adopted.m365_target.generation_id == "legacy-gen-1"
    assert adopted.stage_history == legacy.stage_history
    assert adopted.warnings == ["legacy-m365-target-adopted"]
    assert len(adopted.docs) == 12
    sealed: list[httpx.Request] = []
    replay = asyncio.run(
        M365Seeder(
            m365_settings(), principals, transport=build_m365_transport(recorder=sealed)
        ).adopt_legacy(slice_corpus(), prior_manifest=adopted, generation_id="legacy-gen-1")
    )
    assert replay == adopted
    assert sealed == []


@pytest.mark.parametrize("case", ["missing", "surplus", "mismatch", "partial"])
def test_adoption_refuses_non_exact_folder(principals: PrincipalsMap, case: str) -> None:
    legacy, children = _legacy(principals)
    if case == "missing":
        children.pop()
    elif case == "surplus":
        children.append({"id": "surplus", "name": "surplus.txt", "file": {}})
    elif case == "mismatch":
        children[0]["id"] = "lookalike"
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(
        recorder=recorder,
        folder_exists=True,
        folder_children=children,
        children_next_link=case == "partial",
    )
    with pytest.raises(SeedPreflightError):
        asyncio.run(
            M365Seeder(m365_settings(), principals, transport=transport).adopt_legacy(
                slice_corpus(), prior_manifest=legacy, generation_id="legacy-gen-1"
            )
        )
    assert all(request.method == "GET" or "oauth2" in str(request.url) for request in recorder)


def test_adoption_refuses_malformed_manifest_before_io(principals: PrincipalsMap) -> None:
    legacy, _ = _legacy(principals)
    legacy.docs.pop(next(iter(legacy.docs)))
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="exactly all 12"):
        asyncio.run(
            M365Seeder(
                m365_settings(), principals, transport=build_m365_transport(recorder=recorder)
            ).adopt_legacy(slice_corpus(), prior_manifest=legacy, generation_id="legacy-gen-1")
        )
    assert recorder == []


def test_adopted_history_allows_existing_operational_graph_stage(
    principals: PrincipalsMap,
) -> None:
    legacy, children = _legacy(principals)
    adopted = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(folder_exists=True, folder_children=children),
        ).adopt_legacy(slice_corpus(), prior_manifest=legacy, generation_id="legacy-gen-1")
    )
    completed = asyncio.run(
        M365Seeder(
            m365_settings(), principals, transport=build_m365_transport(folder_exists=True)
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=adopted,
            generation_id="legacy-gen-1",
        )
    )
    assert [entry.stage for entry in completed.stage_history] == [
        "initial",
        "supersession",
        "operational_graph",
    ]
    assert len(completed.operational_graph) == 45
    assert sum(len(binding.edges) for binding in completed.operational_graph) == 40


def test_atomic_manifest_replace_failure_preserves_original(
    principals: PrincipalsMap, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, _ = _legacy(principals)
    path = legacy.save(tmp_path)
    original = path.read_bytes()
    changed = legacy.model_copy(update={"warnings": ["replacement"]})

    def fail_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        raise OSError("forced replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="forced replace failure"):
        changed.save(tmp_path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def _adopted_operational(principals: PrincipalsMap) -> SeedManifest:
    legacy, children = _legacy(principals)
    seeder = M365Seeder(
        m365_settings(),
        principals,
        transport=build_m365_transport(folder_exists=True, folder_children=children),
    )

    async def run() -> SeedManifest:
        adopted = await seeder.adopt_legacy(
            slice_corpus(), prior_manifest=legacy, generation_id="legacy-gen-1"
        )
        return await seeder.push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=adopted,
            generation_id="legacy-gen-1",
        )

    return asyncio.run(run())


def test_check_accepts_exact_live_shaped_adopted_operational_manifest(
    principals: PrincipalsMap,
) -> None:
    manifest = _adopted_operational(principals)
    recorder: list[httpx.Request] = []
    result = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(recorder=recorder, folder_exists=True),
        ).check(slice_corpus(), prior_manifest=manifest, generation_id="legacy-gen-1")
    )
    assert result.status == "owned"
    assert result.completed_stages == ("initial", "supersession", "operational_graph")
    assert all(request.method == "GET" or "oauth2" in str(request.url) for request in recorder)


@pytest.mark.parametrize("tamper", ["marker", "missing", "surplus", "binding"])
def test_check_rejects_non_exact_adopted_operational_manifest_before_io(
    principals: PrincipalsMap, tamper: str
) -> None:
    manifest = _adopted_operational(principals).model_copy(deep=True)
    if tamper == "marker":
        manifest.warnings.clear()
    elif tamper == "missing":
        manifest.operational_graph = manifest.operational_graph[:-1]
    elif tamper == "surplus":
        manifest.operational_graph += (manifest.operational_graph[0],)
    else:
        first = manifest.operational_graph[0]
        manifest.operational_graph = (
            first.model_copy(update={"path": "lookalike.negraph.json"}),
            *manifest.operational_graph[1:],
        )
    recorder: list[httpx.Request] = []
    result = asyncio.run(
        M365Seeder(
            m365_settings(), principals, transport=build_m365_transport(recorder=recorder)
        ).check(slice_corpus(), prior_manifest=manifest, generation_id="legacy-gen-1")
    )
    assert result.status == "mismatched"
    assert recorder == []


@pytest.mark.parametrize(
    "stages",
    [
        ("supersession", "initial", "operational_graph"),
        ("initial", "supersession", "supersession", "operational_graph"),
        ("supersession", "operational_graph"),
    ],
)
def test_check_rejects_reordered_duplicate_or_missing_adopted_stage_before_io(
    principals: PrincipalsMap, stages: tuple[str, ...]
) -> None:
    manifest = _adopted_operational(principals).model_copy(deep=True)
    manifest.stage_history = [
        StageHistoryEntry.model_construct(stage=stage, completed_at=f"2026-08-0{index}T00:00:00Z")
        for index, stage in enumerate(stages, start=1)
    ]
    recorder: list[httpx.Request] = []
    result = asyncio.run(
        M365Seeder(
            m365_settings(), principals, transport=build_m365_transport(recorder=recorder)
        ).check(slice_corpus(), prior_manifest=manifest, generation_id="legacy-gen-1")
    )
    assert result.status == "mismatched"
    assert recorder == []
