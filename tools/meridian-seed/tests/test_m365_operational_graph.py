# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact 45-item Meridian operational M365 seed-stage controls."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from ne.connect.errors import GraphSyncError
from ne.connect.m365.operational_graph import (
    MERIDIAN_FRONTLINE_WITHHELD_CLASSES,
    OperationalGraphEnvelope,
    manifest_record_key,
    validate_binding_acls,
    validate_binding_set,
)
from ne.meridian_seed.m365 import M365Seeder
from ne.meridian_seed.personas import PrincipalsMap
from ne.meridian_seed.slice import slice_corpus

from .conftest import build_m365_transport, m365_settings


def _seed_initial(principals: PrincipalsMap):
    return asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(),
        ).push(
            slice_corpus(),
            stage="initial",
            generation_id="generation-structured-1",
        )
    )


def test_slice_produces_exact_canonical_45_items_and_40_edges() -> None:
    selection = slice_corpus()
    assert len(selection.operational_graph) == 45
    assert {
        entity_class: sum(
            document.entity_class == entity_class for document in selection.operational_graph
        )
        for entity_class in ("Customer", "AutoLoan", "PaymentSchedule", "ServicingNote")
    } == {"Customer": 15, "AutoLoan": 10, "PaymentSchedule": 10, "ServicingNote": 10}
    assert sum(len(document.edges) for document in selection.operational_graph) == 40
    for document in selection.operational_graph:
        assert document.filename.endswith(".negraph.json")
        assert document.media_type == "application/json"
        envelope = OperationalGraphEnvelope.model_validate_json(document.content)
        assert envelope.record_id == document.record_id
        assert envelope.entity_class == document.entity_class
    withheld = [
        document
        for document in selection.operational_graph
        if document.entity_class in MERIDIAN_FRONTLINE_WITHHELD_CLASSES
    ]
    assert len(withheld) == 30
    assert all(document.acl.allowed_personas == ["ops", "approver"] for document in withheld)
    assert all(document.acl.denied_personas == ["frontline"] for document in withheld)
    customers = [
        document for document in selection.operational_graph if document.entity_class == "Customer"
    ]
    assert all(document.acl.denied_personas == [] for document in customers)


def test_operational_stage_uploads_exact_live_items_acls_and_manifest_bindings(
    principals: PrincipalsMap,
) -> None:
    initial = _seed_initial(principals)
    recorder: list[httpx.Request] = []
    manifest = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(recorder=recorder, folder_exists=True),
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=initial,
            generation_id="generation-structured-1",
        )
    )
    validate_binding_set(manifest.operational_graph)
    assert len(manifest.operational_graph) == 45
    assert sum(len(binding.edges) for binding in manifest.operational_graph) == 40
    assert [entry.stage for entry in manifest.stage_history] == ["initial", "operational_graph"]
    assert sum(request.method == "PUT" for request in recorder) == 45
    assert sum(request.url.path.endswith("/invite") for request in recorder) == 45
    for binding in manifest.operational_graph:
        key = manifest_record_key(binding.entity_class, binding.record_id)
        assert manifest.records[key] == binding.item_id
        assert manifest.document_paths[key] == binding.path
        assert manifest.grants[key] == list(binding.acl)
        assert binding.item_id == "item-" + re.sub(r"[^a-z0-9]+", "-", binding.path.lower()).strip(
            "-"
        )
        if binding.entity_class in MERIDIAN_FRONTLINE_WITHHELD_CLASSES:
            assert binding.acl == ("group-ops", "group-approver")
        else:
            assert binding.acl == ("group-ops", "group-approver", "group-frontline")


def test_completed_operational_stage_rerun_is_zero_request_noop(
    principals: PrincipalsMap,
) -> None:
    initial = _seed_initial(principals)
    manifest = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(folder_exists=True),
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=initial,
            generation_id="generation-structured-1",
        )
    )
    recorder: list[httpx.Request] = []
    rerun = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(recorder=recorder, folder_exists=True),
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=manifest,
            generation_id="generation-structured-1",
        )
    )
    assert rerun == manifest
    assert recorder == []


def test_operational_failure_never_mutates_prior_manifest(
    principals: PrincipalsMap,
) -> None:
    initial = _seed_initial(principals)
    before = initial.model_dump_json()
    with pytest.raises(GraphSyncError):
        asyncio.run(
            M365Seeder(
                m365_settings(),
                principals,
                transport=build_m365_transport(folder_exists=True, fail_put_number=7),
            ).push(
                slice_corpus(),
                stage="operational_graph",
                prior_manifest=initial,
                generation_id="generation-structured-1",
            )
        )
    assert initial.model_dump_json() == before
    assert initial.operational_graph == ()


def test_manifest_missing_one_live_item_fails_closed(principals: PrincipalsMap) -> None:
    initial = _seed_initial(principals)
    manifest = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(folder_exists=True),
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=initial,
            generation_id="generation-structured-1",
        )
    )
    payload = manifest.model_dump(mode="json")
    first = manifest.operational_graph[0]
    payload["records"].pop(manifest_record_key(first.entity_class, first.record_id))
    with pytest.raises(ValueError, match="exact live item id"):
        type(manifest).model_validate_json(json.dumps(payload))


def test_binding_path_and_note_acl_are_exact(principals: PrincipalsMap) -> None:
    initial = _seed_initial(principals)
    manifest = asyncio.run(
        M365Seeder(
            m365_settings(),
            principals,
            transport=build_m365_transport(folder_exists=True),
        ).push(
            slice_corpus(),
            stage="operational_graph",
            prior_manifest=initial,
            generation_id="generation-structured-1",
        )
    )
    first = manifest.operational_graph[0]
    with pytest.raises(ValueError, match=r"canonical \.negraph\.json"):
        validate_binding_set(
            (
                first.model_copy(update={"path": "unsupported.txt"}),
                *manifest.operational_graph[1:],
            )
        )
    for entity_class in sorted(MERIDIAN_FRONTLINE_WITHHELD_CLASSES):
        withheld_index = next(
            index
            for index, binding in enumerate(manifest.operational_graph)
            if binding.entity_class == entity_class
        )
        withheld = manifest.operational_graph[withheld_index]
        widened = (
            *manifest.operational_graph[:withheld_index],
            withheld.model_copy(update={"acl": (*withheld.acl, "group-frontline")}),
            *manifest.operational_graph[withheld_index + 1 :],
        )
        with pytest.raises(ValueError, match=f"{entity_class} ACL"):
            validate_binding_acls(
                widened,
                ops_group="group-ops",
                approver_group="group-approver",
                frontline_group="group-frontline",
            )
    duplicate_item = (
        manifest.operational_graph[0],
        manifest.operational_graph[1].model_copy(
            update={"item_id": manifest.operational_graph[0].item_id}
        ),
        *manifest.operational_graph[2:],
    )
    with pytest.raises(ValueError, match="duplicate record/item identity"):
        validate_binding_set(duplicate_item)
