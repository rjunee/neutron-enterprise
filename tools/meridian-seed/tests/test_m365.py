# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SharePoint binary/MIME/ACL fidelity plus the ownership-first refusal lattice.

Each control proves refusal ORDER as well as outcome: static target drift fails
with an empty recorder (before the token POST), live-identity drift fails with
zero PUT/post-children/invite/DELETE calls, and successes bind the exact
resolved ids into ``manifest.m365_target``.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ne.connect.errors import GraphSyncError
from ne.connect.m365.config import M365ConnectorSettings
from ne.meridian_seed import cli
from ne.meridian_seed.errors import SeedPreflightError, SeedSafetyError
from ne.meridian_seed.m365 import M365CheckResult, M365Seeder, SeedGraphClient
from ne.meridian_seed.manifest import SeedManifest
from ne.meridian_seed.personas import PrincipalsMap
from ne.meridian_seed.slice import slice_corpus

from .conftest import (
    GRAPH,
    M365_SITE,
    M365_TENANT,
    M365_TOKEN_URL,
    build_m365_transport,
    m365_settings,
)

_GRAPH_BASE = GRAPH
_GRAPH_PATH = urllib.parse.urlparse(GRAPH).path.rstrip("/")
_DRIVE_ID = "drive-meridian"
_FOLDER_ITEM_ID = "folder-meridian"
_SOP_ITEM_ID = "item-dispute-sop"
_SOP_PATH = "Debit Card Dispute Handling SOP.docx"

_GROUP_IDS = {
    "ops": "group-ops",
    "approver": "group-approver",
    "frontline": "group-frontline",
}


def _settings(**overrides: object) -> M365ConnectorSettings:
    return m365_settings(**overrides)


def _seeder(principals: PrincipalsMap, recorder: list[httpx.Request]) -> M365Seeder:
    return M365Seeder(_settings(), principals, transport=build_m365_transport(recorder=recorder))


def _puts(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in recorder if request.method == "PUT"]


def _deletes(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in recorder if request.method == "DELETE"]


def _mutations(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [
        request
        for request in recorder
        if request.method in {"PUT", "DELETE"}
        or (request.method == "POST" and str(request.url) != M365_TOKEN_URL)
    ]


def _decoded_path(request: httpx.Request) -> str:
    return urllib.parse.unquote(request.url.path)


def _live_item_id(sharepoint_path: str) -> str:
    if sharepoint_path == _SOP_PATH:
        return _SOP_ITEM_ID
    return "item-" + re.sub(r"[^a-z0-9]+", "-", sharepoint_path.lower()).strip("-")


def _legacy_manifest() -> SeedManifest:
    return SeedManifest(
        tenant="m365",
        seeded_at=datetime.now(UTC).isoformat(),
        corpus_version=slice_corpus().corpus_version,
    )


def _initial_manifest(principals: PrincipalsMap) -> SeedManifest:
    seeder = _seeder(principals, [])
    return asyncio.run(seeder.push(slice_corpus(), stage="initial", generation_id="gen-1"))


def _completed_manifest(principals: PrincipalsMap) -> SeedManifest:
    seeder = _seeder(principals, [])

    async def run() -> SeedManifest:
        initial = await seeder.push(slice_corpus(), stage="initial", generation_id="gen-1")
        return await seeder.push(
            slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
        )

    return asyncio.run(run())


def _saved_bound_manifest(manifest_dir: Path, principals: PrincipalsMap) -> SeedManifest:
    manifest = _initial_manifest(principals)
    manifest.save(manifest_dir)
    return manifest


def _retarget(manifest: SeedManifest, **members: str) -> SeedManifest:
    target = manifest.m365_target
    assert target is not None
    retargeted = manifest.model_copy(deep=True)
    retargeted.m365_target = target.model_copy(update=members)
    return retargeted


def _changed_graph_base(manifest: SeedManifest) -> SeedManifest:
    return _retarget(manifest, graph_base_url="https://graph.microsoft.us/v1.0")


def _changed_tenant(manifest: SeedManifest) -> SeedManifest:
    return _retarget(manifest, tenant_id="99999999-9999-9999-9999-999999999999")


def _changed_site(manifest: SeedManifest) -> SeedManifest:
    return _retarget(manifest, site_id="site-lookalike")


def _changed_corpus(manifest: SeedManifest) -> SeedManifest:
    return manifest.model_copy(update={"corpus_version": "meridian-v0"})


def test_initial_binds_target_identity_and_exact_document_records(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    selection = slice_corpus()
    seeder = _seeder(principals, recorder)
    manifest = asyncio.run(seeder.push(selection, stage="initial", generation_id="gen-1"))
    target = manifest.m365_target
    assert target is not None
    assert target.graph_base_url == _GRAPH_BASE
    assert target.tenant_id == M365_TENANT
    assert target.site_id == M365_SITE
    assert target.drive_id == _DRIVE_ID
    assert target.folder_item_id == _FOLDER_ITEM_ID
    assert target.generation_id == "gen-1"
    assert target.tenant == "m365"
    assert target.corpus_version == selection.corpus_version
    documents = {row.artifact_id: row for row in selection.documents_for_stage("initial")}
    assert manifest.document_paths == {
        artifact_id: row.sharepoint_path for artifact_id, row in documents.items()
    }
    assert manifest.docs == {
        artifact_id: _live_item_id(row.sharepoint_path) for artifact_id, row in documents.items()
    }
    assert manifest.stage_history[0].stage == "initial"


def test_initial_uploads_exact_binary_bytes_mime_and_acl(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    selection = slice_corpus()
    seeder = _seeder(principals, recorder)
    manifest = asyncio.run(seeder.push(selection, stage="initial", generation_id="gen-1"))
    documents = {row.sharepoint_path: row for row in selection.documents_for_stage("initial")}
    assert len(_puts(recorder)) == 11
    for request in _puts(recorder):
        path = _decoded_path(request).split("/root:/Meridian/", 1)[1].removesuffix(":/content")
        assert request.content == documents[path].content
        assert request.headers["Content-Type"] == documents[path].media_type
    assert manifest.stage_history[0].stage == "initial"
    assert len(manifest.docs) == 11

    invites = {
        _decoded_path(request).split("/items/", 1)[1].split("/invite", 1)[0]: json.loads(
            request.content
        )
        for request in recorder
        if request.method == "POST" and _decoded_path(request).endswith("/invite")
    }
    for document in selection.documents_for_stage("initial"):
        live_id = manifest.docs[document.artifact_id]
        recipients = {row["objectId"] for row in invites[live_id]["recipients"]}
        expected = {_GROUP_IDS[persona] for persona in document.acl.allowed_personas}
        denied = {_GROUP_IDS[persona] for persona in document.acl.denied_personas}
        assert recipients == expected == set(manifest.grants[document.artifact_id])
        assert not recipients & denied


def test_created_folder_response_is_validated_and_persisted(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    seeder = _seeder(principals, recorder)
    manifest = asyncio.run(seeder.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    creates = [
        request
        for request in recorder
        if request.method == "POST"
        and _decoded_path(request) == f"{_GRAPH_PATH}/drives/{_DRIVE_ID}/root/children"
    ]
    assert len(creates) == 1
    assert json.loads(creates[0].content) == {
        "name": "Meridian",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    assert manifest.m365_target is not None
    assert manifest.m365_target.folder_item_id == _FOLDER_ITEM_ID

    malformed: list[httpx.Request] = []
    guarded = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=malformed, folder_get_malformed=True),
    )
    with pytest.raises(GraphSyncError, match="invalid JSON object"):
        asyncio.run(guarded.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    assert _mutations(malformed) == []


@pytest.mark.parametrize(
    "transport",
    [
        build_m365_transport(folder_create_name="Meridian-lookalike"),
        build_m365_transport(folder_create_has_folder_facet=False),
    ],
)
def test_initial_refuses_a_non_folder_or_wrong_name_create_response_before_upload(
    principals: PrincipalsMap, transport: httpx.MockTransport
) -> None:
    with pytest.raises(SeedPreflightError, match="folder creation returned a malformed"):
        asyncio.run(
            M365Seeder(_settings(), principals, transport=transport).push(
                slice_corpus(), stage="initial", generation_id="gen-1"
            )
        )


def test_initial_refuses_a_wrong_name_upload_response_before_acl_or_manifest_record(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="name does not equal"):
        asyncio.run(
            M365Seeder(
                _settings(),
                principals,
                transport=build_m365_transport(recorder=recorder, put_name_changed=True),
            ).push(slice_corpus(), stage="initial", generation_id="gen-1")
        )
    assert len(_puts(recorder)) == 1
    assert not any(_decoded_path(request).endswith("/invite") for request in recorder)


def test_upload_redirect_is_not_mistaken_for_graph_success(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    with pytest.raises(GraphSyncError, match="upload returned HTTP redirect 307"):
        asyncio.run(
            M365Seeder(
                _settings(),
                principals,
                transport=build_m365_transport(recorder=recorder, put_redirect=True),
            ).push(slice_corpus(), stage="initial", generation_id="gen-1")
        )
    assert len(_puts(recorder)) == 1
    assert not any(_decoded_path(request).endswith("/invite") for request in recorder)


def test_legacy_manifest_refuses_push_before_any_network_call(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="predates target binding"):
        asyncio.run(
            _seeder(principals, recorder).push(
                slice_corpus(),
                stage="supersession",
                prior_manifest=_legacy_manifest(),
                generation_id="gen-1",
            )
        )
    assert recorder == []


@pytest.mark.parametrize(
    ("drift", "fragment"),
    [
        (_changed_graph_base, "Graph cloud base URL"),
        (_changed_tenant, "Entra tenant id"),
        (_changed_site, "SharePoint site id"),
        (_changed_corpus, "does not belong to this Meridian M365 corpus"),
    ],
)
def test_changed_cloud_tenant_site_or_corpus_refuses_with_zero_calls(
    principals: PrincipalsMap,
    drift: Callable[[SeedManifest], SeedManifest],
    fragment: str,
) -> None:
    prior = drift(_initial_manifest(principals))
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match=fragment):
        asyncio.run(
            _seeder(principals, recorder).push(
                slice_corpus(), stage="supersession", prior_manifest=prior, generation_id="gen-1"
            )
        )
    assert recorder == []


def test_changed_resolved_drive_refuses_supersession_after_read_only_preflight(
    principals: PrincipalsMap,
) -> None:
    initial = _initial_manifest(principals)
    recorder: list[httpx.Request] = []
    relocated = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=recorder, drive_id="drive-other"),
    )
    with pytest.raises(SeedPreflightError, match="resolved default drive does not match"):
        asyncio.run(
            relocated.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert _mutations(recorder) == []
    assert [request.method for request in recorder] == ["POST", "GET"]


def test_malformed_resolved_drive_id_refuses_before_any_mutation(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    seeder = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=recorder, drive_id="../drive-lookalike"),
    )
    with pytest.raises(SeedPreflightError, match="malformed Graph item id"):
        asyncio.run(seeder.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    assert _mutations(recorder) == []


def test_unowned_meridian_folder_or_create_conflict_refuses_before_first_put(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    stranger = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=recorder, folder_present_unowned=True),
    )
    with pytest.raises(SeedPreflightError, match="already exists on the resolved drive"):
        asyncio.run(stranger.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    assert _puts(recorder) == []
    assert _mutations(recorder) == []

    conflict: list[httpx.Request] = []
    conflicting = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=conflict, folder_conflict=True),
    )
    with pytest.raises(SeedPreflightError, match="create race"):
        asyncio.run(conflicting.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    assert _puts(conflict) == []


def test_supersession_refuses_before_initial_with_zero_network_calls(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="requires a completed initial"):
        asyncio.run(
            _seeder(principals, recorder).push(
                slice_corpus(), stage="supersession", generation_id="gen-1"
            )
        )
    assert recorder == []


def test_supersession_revalidates_the_exact_owned_folder_before_sop_or_put(
    principals: PrincipalsMap,
) -> None:
    initial = _initial_manifest(principals)
    recorder: list[httpx.Request] = []
    failing = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(
            recorder=recorder,
            folder_exists=True,
            folder_live_id="folder-lookalike",
        ),
    )
    with pytest.raises(SeedPreflightError, match="folder id does not equal"):
        asyncio.run(
            failing.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert _mutations(recorder) == []
    assert not any("Dispute%20Handling" in str(request.url) for request in recorder)


@pytest.mark.parametrize(
    ("flag", "error_type", "fragment"),
    [
        ("sop_missing", SeedPreflightError, "missing from the live folder"),
        ("sop_id_changed", SeedPreflightError, "does not equal the manifest-owned v3 item id"),
        ("sop_malformed", GraphSyncError, "invalid JSON object"),
    ],
)
def test_supersession_refuses_live_sop_drift_before_any_put(
    principals: PrincipalsMap,
    flag: str,
    error_type: type[Exception],
    fragment: str,
) -> None:
    initial = _initial_manifest(principals)
    recorder: list[httpx.Request] = []
    if flag == "sop_missing":
        transport = build_m365_transport(recorder=recorder, folder_exists=True, sop_missing=True)
    elif flag == "sop_id_changed":
        transport = build_m365_transport(recorder=recorder, folder_exists=True, sop_id_changed=True)
    else:
        transport = build_m365_transport(recorder=recorder, folder_exists=True, sop_malformed=True)
    failing = M365Seeder(_settings(), principals, transport=transport)
    with pytest.raises(error_type, match=fragment):
        # sop_malformed surfaces as GraphSyncError from the client's JSON-object
        # guard, so refusal-before-PUT rests on the zero-PUT census below.
        asyncio.run(
            failing.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert _puts(recorder) == []


def test_supersession_refuses_a_live_sop_name_lookalike_before_put(
    principals: PrincipalsMap,
) -> None:
    initial = _initial_manifest(principals)
    recorder: list[httpx.Request] = []
    failing = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(
            recorder=recorder,
            folder_exists=True,
            sop_name_changed=True,
        ),
    )
    with pytest.raises(SeedPreflightError, match="name does not equal"):
        asyncio.run(
            failing.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert _puts(recorder) == []


def test_v4_put_replaces_exact_v3_path_and_history_uses_run_time(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    seeder = _seeder(principals, recorder)

    async def run() -> tuple[SeedManifest, SeedManifest]:
        initial = await seeder.push(slice_corpus(), stage="initial", generation_id="gen-1")
        superseded = await seeder.push(
            slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
        )
        return initial, superseded

    initial, superseded = asyncio.run(run())
    assert [entry.stage for entry in superseded.stage_history] == [
        "initial",
        "supersession",
    ]
    assert all(
        datetime.fromisoformat(entry.completed_at).year >= 2026
        for entry in superseded.stage_history
    )
    v3_path = initial.document_paths["art-dispute-sop-v3"]
    v4_path = superseded.document_paths["art-dispute-sop-v4"]
    assert v3_path == v4_path == _SOP_PATH
    assert initial.docs["art-dispute-sop-v3"] == superseded.docs["art-dispute-sop-v4"]
    sop_puts = [request for request in _puts(recorder) if "Dispute%20Handling" in str(request.url)]
    assert len(sop_puts) == 2
    assert _decoded_path(sop_puts[0]) == _decoded_path(sop_puts[1])
    assert (
        _decoded_path(sop_puts[1])
        == f"{_GRAPH_PATH}/drives/{_DRIVE_ID}/root:/Meridian/{_SOP_PATH}:/content"
    )
    assert sop_puts[0].content != sop_puts[1].content


def test_completed_stage_reruns_are_zero_request_noops(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    seeder = _seeder(principals, recorder)
    initial = asyncio.run(seeder.push(slice_corpus(), stage="initial", generation_id="gen-1"))
    after_initial = len(recorder)
    rerun = asyncio.run(
        seeder.push(slice_corpus(), stage="initial", prior_manifest=initial, generation_id="gen-1")
    )
    assert rerun == initial
    assert len(recorder) == after_initial
    completed = asyncio.run(
        seeder.push(
            slice_corpus(), stage="supersession", prior_manifest=rerun, generation_id="gen-1"
        )
    )
    after_supersession = len(recorder)
    again = asyncio.run(
        seeder.push(
            slice_corpus(), stage="supersession", prior_manifest=completed, generation_id="gen-1"
        )
    )
    assert again == completed
    assert len(recorder) == after_supersession
    assert [row.stage for row in again.stage_history] == ["initial", "supersession"]


@pytest.mark.parametrize("tamper", ["missing_sop", "unsafe_drive_id"])
def test_tampered_completed_manifest_never_qualifies_as_a_zero_call_success(
    principals: PrincipalsMap, tamper: str
) -> None:
    initial = _initial_manifest(principals)
    if tamper == "missing_sop":
        initial.docs.pop("art-dispute-sop-v3")
    else:
        assert initial.m365_target is not None
        initial.m365_target = initial.m365_target.model_copy(update={"drive_id": "../lookalike"})
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError):
        asyncio.run(
            _seeder(principals, recorder).push(
                slice_corpus(), stage="initial", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert recorder == []


def test_repeated_initial_is_idempotent_and_never_duplicates_history(
    principals: PrincipalsMap,
) -> None:
    recorder: list[httpx.Request] = []
    seeder = _seeder(principals, recorder)

    async def run():
        first = await seeder.push(slice_corpus(), stage="initial", generation_id="gen-1")
        second = await seeder.push(
            slice_corpus(), stage="initial", prior_manifest=first, generation_id="gen-1"
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.docs == second.docs
    assert [row.stage for row in second.stage_history] == ["initial"]
    assert len(_puts(recorder)) == 11


def test_repeated_supersession_is_a_zero_call_noop(principals: PrincipalsMap) -> None:
    completed = _completed_manifest(principals)
    recorder: list[httpx.Request] = []
    seeder = _seeder(principals, recorder)
    repeated = asyncio.run(
        seeder.push(
            slice_corpus(), stage="supersession", prior_manifest=completed, generation_id="gen-1"
        )
    )
    assert repeated == completed
    assert recorder == []


def test_completed_supersession_cannot_be_downgraded_and_makes_no_new_call(
    principals: PrincipalsMap,
) -> None:
    completed = _completed_manifest(principals)
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="downgrade"):
        asyncio.run(
            _seeder(principals, recorder).push(
                slice_corpus(), stage="initial", prior_manifest=completed, generation_id="gen-1"
            )
        )
    assert recorder == []


def test_failed_supersession_does_not_mutate_completed_initial_manifest(
    principals: PrincipalsMap,
) -> None:
    initial = _initial_manifest(principals)
    before = initial.model_copy(deep=True)
    failing_recorder: list[httpx.Request] = []
    failing = M365Seeder(
        _settings(max_attempts=1),
        principals,
        transport=build_m365_transport(
            recorder=failing_recorder, folder_exists=True, fail_put_number=1
        ),
    )
    with pytest.raises(GraphSyncError, match="HTTP 500"):
        asyncio.run(
            failing.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert initial == before
    assert [row.stage for row in initial.stage_history] == ["initial"]


def test_unseed_deletes_only_the_manifest_owned_folder_item(
    tmp_path: Path,
    principals: PrincipalsMap,
) -> None:
    manifest_dir = tmp_path / "state"
    manifest = _saved_bound_manifest(manifest_dir, principals)
    assert manifest.m365_target is not None
    assert manifest.m365_target.folder_item_id == _FOLDER_ITEM_ID
    recorder: list[httpx.Request] = []
    unseeder = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=recorder, folder_exists=True),
    )
    asyncio.run(unseeder.unseed(manifest_dir, generation_id="gen-1"))
    deletes = _deletes(recorder)
    assert len(deletes) == 1
    assert _decoded_path(deletes[0]) == f"{_GRAPH_PATH}/drives/{_DRIVE_ID}/items/{_FOLDER_ITEM_ID}"
    assert _mutations(recorder) == deletes
    assert not (manifest_dir / "m365.json").exists()


def test_unseed_redirect_does_not_erase_the_ownership_manifest(
    tmp_path: Path,
    principals: PrincipalsMap,
) -> None:
    manifest_dir = tmp_path / "state"
    _saved_bound_manifest(manifest_dir, principals)
    recorder: list[httpx.Request] = []
    with pytest.raises(GraphSyncError, match="delete returned HTTP redirect 307"):
        asyncio.run(
            M365Seeder(
                _settings(),
                principals,
                transport=build_m365_transport(
                    recorder=recorder, folder_exists=True, delete_redirect=True
                ),
            ).unseed(manifest_dir, generation_id="gen-1")
        )
    assert len(_deletes(recorder)) == 1
    assert (manifest_dir / "m365.json").is_file()


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        ("legacy_manifest", "predates target binding"),
        ("wrong_generation", "generation id does not match"),
        ("missing_manifest_file", "no saved M365 manifest exists"),
        ("absent_live_folder", "folder is absent"),
        ("stranger_folder_id", "does not equal the manifest-owned"),
    ],
)
def test_unseed_refusals_issue_zero_delete_requests(
    tmp_path: Path,
    principals: PrincipalsMap,
    case: str,
    fragment: str,
) -> None:
    manifest_dir = tmp_path / "state"
    generation = "gen-1"
    if case == "legacy_manifest":
        _legacy_manifest().save(manifest_dir)
    elif case != "missing_manifest_file":
        _saved_bound_manifest(manifest_dir, principals)
    if case == "wrong_generation":
        generation = "gen-2"
    recorder: list[httpx.Request] = []
    if case == "stranger_folder_id":
        transport = build_m365_transport(recorder=recorder, folder_present_unowned=True)
    else:
        transport = build_m365_transport(recorder=recorder)
    unseeder = M365Seeder(_settings(), principals, transport=transport)
    with pytest.raises(SeedPreflightError, match=fragment):
        asyncio.run(unseeder.unseed(manifest_dir, generation_id=generation))
    assert _deletes(recorder) == []
    if case in {"legacy_manifest", "wrong_generation", "missing_manifest_file"}:
        assert recorder == []
    else:
        assert _mutations(recorder) == []
    if case != "missing_manifest_file":
        assert (manifest_dir / "m365.json").exists()


@pytest.mark.parametrize("drift", ["tenant", "corpus"])
def test_unseed_refuses_outer_manifest_target_binding_drift_before_network(
    tmp_path: Path,
    principals: PrincipalsMap,
    drift: str,
) -> None:
    manifest_dir = tmp_path / "state"
    manifest = _saved_bound_manifest(manifest_dir, principals)
    if drift == "tenant":
        manifest.tenant = "salesforce"
    else:
        manifest.corpus_version = "meridian-v0"
    (manifest_dir / "m365.json").write_text(
        json.dumps(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError, match="manifest target binding"):
        asyncio.run(
            M365Seeder(
                _settings(), principals, transport=build_m365_transport(recorder=recorder)
            ).unseed(manifest_dir, generation_id="gen-1")
        )
    assert recorder == []
    assert (manifest_dir / "m365.json").is_file()


def test_invalid_manifest_error_is_class_only_and_does_not_echo_input(
    tmp_path: Path, principals: PrincipalsMap
) -> None:
    secret = "not-for-error-output"
    (tmp_path / "m365.json").write_text(
        json.dumps({"tenant": "m365", "unexpected_secret": secret}), encoding="utf-8"
    )
    with pytest.raises(SeedPreflightError) as raised:
        cli._prior_m365_manifest(tmp_path)
    assert secret not in str(raised.value)
    recorder: list[httpx.Request] = []
    with pytest.raises(SeedPreflightError) as unseed_raised:
        asyncio.run(
            M365Seeder(
                _settings(), principals, transport=build_m365_transport(recorder=recorder)
            ).unseed(tmp_path, generation_id="gen-1")
        )
    assert secret not in str(unseed_raised.value)
    assert recorder == []


@pytest.mark.parametrize(
    "argv",
    [
        [
            "push",
            "--tenant",
            "m365",
            "--stage",
            "initial",
            "--confirm",
            "MERIDIAN-DEMO-TENANT",
        ],
        ["check", "--tenant", "m365"],
        [
            "unseed",
            "--tenant",
            "m365",
            "--confirm",
            "MERIDIAN-DEMO-TENANT",
        ],
    ],
)
def test_cli_requires_generation_before_m365_configuration_or_provider_io(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 1
    assert "--generation-id is required" in capsys.readouterr().err


def test_check_verdicts_are_truthful_and_issues_only_reads(
    principals: PrincipalsMap,
) -> None:
    selection = slice_corpus()
    completed = _completed_manifest(principals)
    drifted_corpus = completed.model_copy(update={"corpus_version": "meridian-v0"})
    tampered = completed.model_copy(deep=True)
    assert completed.m365_target is not None
    tampered.m365_target = completed.m365_target.model_copy(
        update={"tenant_id": "99999999-9999-9999-9999-999999999999"}
    )

    sealed: list[httpx.Request] = []
    safe_new: list[httpx.Request] = []
    blocked: list[httpx.Request] = []
    other_drive: list[httpx.Request] = []
    owned: list[httpx.Request] = []

    def check(
        prior: SeedManifest | None,
        transport: httpx.MockTransport,
        generation: str = "gen-1",
    ) -> M365CheckResult:
        checker = M365Seeder(_settings(), principals, transport=transport)
        return asyncio.run(checker.check(selection, prior_manifest=prior, generation_id=generation))

    assert check(_legacy_manifest(), build_m365_transport(recorder=sealed)).status == (
        "legacy_refused"
    )
    assert check(drifted_corpus, build_m365_transport(recorder=sealed)).status == "mismatched"
    assert check(tampered, build_m365_transport(recorder=sealed)).status == "mismatched"
    assert check(completed, build_m365_transport(recorder=sealed), "gen-2").status == "mismatched"
    assert sealed == []
    assert check(None, build_m365_transport(recorder=safe_new)).status == "safe_new"
    assert (
        check(None, build_m365_transport(recorder=blocked, folder_present_unowned=True)).status
        == "blocked_unowned_folder"
    )
    assert (
        check(completed, build_m365_transport(recorder=other_drive, drive_id="drive-other")).status
        == "drive_mismatch"
    )
    verdict = check(completed, build_m365_transport(recorder=owned, folder_exists=True))
    assert verdict.status == "owned"
    assert verdict.completed_stages == ("initial", "supersession")
    sop_lookalike: list[httpx.Request] = []
    lookalike = check(
        completed,
        build_m365_transport(recorder=sop_lookalike, folder_exists=True, sop_name_changed=True),
    )
    assert lookalike.status == "mismatched"
    for recorder in (safe_new, blocked, other_drive, owned):
        assert _mutations(recorder) == []
    assert _mutations(sop_lookalike) == []


def test_graph_hostile_url_is_rejected_before_token_call() -> None:
    recorder: list[httpx.Request] = []
    client = SeedGraphClient(_settings(), transport=build_m365_transport(recorder=recorder))

    async def run() -> None:
        async with client.session() as http:
            await client.put_bytes(
                http,
                "https://evil.example.invalid/file:/content",
                b"secret",
                "application/octet-stream",
            )

    with pytest.raises(GraphSyncError, match="outside the configured Graph cloud"):
        asyncio.run(run())
    assert recorder == []


def test_m365_fails_closed_in_air_gap(
    monkeypatch: pytest.MonkeyPatch, principals: PrincipalsMap
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(RuntimeError, match="AIR_GAP"):
        M365Seeder(_settings(), principals, transport=build_m365_transport())


def test_m365_requires_one_unambiguous_site(principals: PrincipalsMap) -> None:
    with pytest.raises(SeedSafetyError, match="exactly one"):
        M365Seeder(
            _settings(site_ids=(M365_SITE, "site-lookalike")),
            principals,
            transport=build_m365_transport(),
        )


def test_failed_push_preserves_prior_manifest_object_and_saved_bytes(
    tmp_path: Path,
    principals: PrincipalsMap,
) -> None:
    manifest_dir = tmp_path / "state"
    initial = _saved_bound_manifest(manifest_dir, principals)
    saved_bytes = (manifest_dir / "m365.json").read_bytes()
    before = initial.model_copy(deep=True)
    recorder: list[httpx.Request] = []
    failing = M365Seeder(
        _settings(),
        principals,
        transport=build_m365_transport(recorder=recorder, folder_exists=True, sop_id_changed=True),
    )
    with pytest.raises(SeedPreflightError, match="does not equal the manifest-owned v3 item id"):
        asyncio.run(
            failing.push(
                slice_corpus(), stage="supersession", prior_manifest=initial, generation_id="gen-1"
            )
        )
    assert initial == before
    assert [row.stage for row in initial.stage_history] == ["initial"]
    assert (manifest_dir / "m365.json").read_bytes() == saved_bytes
    assert [path.name for path in manifest_dir.iterdir()] == ["m365.json"]
