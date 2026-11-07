# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Brain API tests (spec §1.3) — validation, trimming, backend-failure mapping."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ne.contracts.types import Chunk, ScoredChunk, SparseVector

from .conftest import AppBuilder, admit_current_source, chunk, get_state, run


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department="engineering")


def test_search_top_k_bounds(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            for bad in (0, 51):
                r = await client.post(
                    "/api/v1/brain/search", json={"query": "q", "top_k": bad}, headers=_auth(h)
                )
                assert r.status_code == 422, bad

    run(_t())


def test_search_empty_query(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "   ", "top_k": 5}, headers=_auth(h)
            )
            assert r.status_code == 422

    run(_t())


def test_retrieve_list_bounds(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r0 = await client.post(
                "/api/v1/brain/retrieve", json={"chunk_ids": []}, headers=_auth(h)
            )
            assert r0.status_code == 422
            r101 = await client.post(
                "/api/v1/brain/retrieve",
                json={"chunk_ids": [f"c{i}" for i in range(101)]},
                headers=_auth(h),
            )
            assert r101.status_code == 422

    run(_t())


def test_retrieve_trims_invisible_chunk_silently(make_app: AppBuilder) -> None:
    h = make_app(chunks=[chunk("secret", acl=["sales"], text="hush")])

    async def _t() -> None:
        async with h.client() as client:
            # An eng principal retrieving a sales-only chunk: absent, not an error.
            r = await client.post(
                "/api/v1/brain/retrieve", json={"chunk_ids": ["secret"]}, headers=_auth(h)
            )
            assert r.status_code == 200
            assert r.json()["chunks"] == []

    run(_t())


def test_current_source_filter_requires_a_stamp_or_explicit_source_free_status(
    make_app: AppBuilder,
) -> None:
    unstamped = chunk("unstamped", acl=["eng"], text="private").model_copy(
        update={"metadata": {"source_system": "wiki", "source_ref": "ref-unstamped"}}
    )
    source_free = chunk("source-free", acl=["eng"], text="definition").model_copy(
        update={"metadata": {"source_status": "no_source"}}
    )
    h = make_app(chunks=[unstamped, source_free])

    async def _t() -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/brain/retrieve",
                json={"chunk_ids": ["unstamped", "source-free"]},
                headers=_auth(h),
            )
            assert response.status_code == 200
            assert [row["chunk"]["chunk_id"] for row in response.json()["chunks"]] == [
                "source-free"
            ]
            assert "private" not in response.text

    run(_t())


class _FailingQueryStore:
    """A ChunkStore whose query/get_filtered raise a backend connection error."""

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None: ...

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return []

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        raise httpx.ConnectError("connect to http://qdrant.internal:6333 failed")

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        raise httpx.ConnectError("connect to http://qdrant.internal:6333 failed")

    async def delete(self, chunk_ids: Sequence[str]) -> None: ...


def test_backend_failure_maps_to_502_class_name_only(make_app: AppBuilder) -> None:
    h = make_app(chunk_store=_FailingQueryStore())

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "q", "top_k": 5}, headers=_auth(h)
            )
            assert r.status_code == 502
            detail = r.json()["detail"]
            assert detail == "ConnectError"  # class name only
            assert "http" not in detail and "qdrant" not in detail

    run(_t())


def test_citations_omit_source_url_without_exact_connector_enrichment(
    make_app: AppBuilder,
) -> None:
    h = make_app(chunks=[chunk("c1", acl=["eng"], text="alpha")])

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "alpha", "top_k": 5}, headers=_auth(h)
            )
            assert r.status_code == 200
            for cit in r.json()["citations"]:
                assert "source_url" not in cit

    run(_t())


def test_search_and_retrieve_serve_exact_connector_source_url(make_app: AppBuilder) -> None:
    source_url = "https://tenant.sharepoint.com/sites/ops/doc-1"
    served = Chunk(
        chunk_id="c1",
        document_id="doc-c1",
        text="alpha",
        metadata={
            "connector": "m365",
            "source_system": "m365",
            "source_ref": "drive-1:item-1",
            "source_url": source_url,
        },
        acl=("eng",),
    )
    h = make_app(chunks=[served])
    _receipt, stamp = admit_current_source(
        get_state(h.app).source_authority_reader,
        key="brain:m365:drive-1:item-1",
        labels=("eng",),
        content=served.text.encode(),
    )
    served = served.model_copy(
        update={"metadata": {**served.metadata, "ne_source_authority_stamp": stamp.model_dump()}}
    )
    run(h.chunk_store.upsert_chunks((served,)))

    async def _t() -> None:
        async with h.client() as client:
            search = await client.post(
                "/api/v1/brain/search", json={"query": "alpha", "top_k": 5}, headers=_auth(h)
            )
            retrieve = await client.post(
                "/api/v1/brain/retrieve", json={"chunk_ids": ["c1"]}, headers=_auth(h)
            )
            assert search.status_code == retrieve.status_code == 200
            assert (
                search.json()["citations"]
                == retrieve.json()["citations"]
                == [
                    {
                        "chunk_id": "c1",
                        "document_id": "doc-c1",
                        "source_system": "m365",
                        "source_ref": "drive-1:item-1",
                        "source_url": source_url,
                    }
                ]
            )

    run(_t())


def test_retrieve_route_emits_read_audit_event(make_app: AppBuilder) -> None:
    # NE-164: an eng principal retrieves a mix of visible + sales-only chunks;
    # the composed app's ONE platform audit sink records exactly one
    # 'knowledge.retrieve' read-path event, visible via the ops audit surface.
    h = make_app(
        ops_roles=("ops",),
        chunks=[
            chunk("visible", acl=["eng"], text="alpha"),
            chunk("secret", acl=["sales"], text="hush"),
        ],
    )

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/retrieve",
                json={"chunk_ids": ["visible", "secret"]},
                headers=_auth(h),
            )
            assert r.status_code == 200
            assert [c["chunk"]["chunk_id"] for c in r.json()["chunks"]] == ["visible"]

            ops_headers = h.bearer("user:ops1", groups=["ops"])
            audit_resp = await client.get(
                "/api/v1/audit/events?actor=user:ada", headers=ops_headers
            )
            assert audit_resp.status_code == 200
            body = audit_resp.json()
            events = [e for e in body["events"] if e["action"] == "knowledge.retrieve"]
            assert len(events) == 1
            event = events[0]
            assert event["actor"] == "user:ada"
            assert event["resource"] == "knowledge://chunks"
            assert event["payload"]["detail"] == "candidates=2 returned=1 acl_trim_delta=1"
            # bare HTTP call outside any run scope -> no ambient trace_id.
            assert event.get("trace_id") is None

            event_text = json.dumps(event)
            assert "sales" not in event_text
            assert "visible" not in event_text
            assert "secret" not in event_text

    run(_t())
