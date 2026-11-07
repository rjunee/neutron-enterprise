# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Strict M365 operational-envelope and connector fan-in controls."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import ne.connect.m365.contract as contract_module
from ne.connect.errors import GraphDocumentParseError
from ne.connect.m365 import run_m365_sync
from ne.connect.m365.acl import ACL_MAPPER_REVISION
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import m365_data_contract, m365_source_link_mapper_revision
from ne.connect.m365.operational_graph import (
    OperationalGraphEdge,
    OperationalGraphEnvelope,
    canonical_operational_graph_bytes,
    parse_operational_graph_body,
)
from ne.connect.state import ConnectorStateStore

from .conftest import WriterStack
from .m365_transport import RecordedBodyParser, build_m365_transport, m365_settings


def _envelope() -> OperationalGraphEnvelope:
    return OperationalGraphEnvelope(
        schema="meridian-operational-record/v1",
        schema_version="1.1.0",
        entity_class="Customer",
        record_id="mer-cus-001",
        fields={
            "id": "mer-cus-001",
            "customer_identifier": "mer-cus-001",
            "first_name": "Avery",
            "last_name": "Arbor",
            "county": "Columbia",
            "status": "active",
        },
        edges=(
            OperationalGraphEdge(
                edge_kind="HAS_AUTO_LOAN",
                target_class="AutoLoan",
                target_record_id="mer-loan-001",
            ),
        ),
    )


def _item(content: bytes) -> dict[str, object]:
    return {
        "id": "graph-item-1",
        "name": "graph-customer-mer-cus-001.negraph.json",
        "webUrl": "https://contoso.example/graph-customer-mer-cus-001.negraph.json",
        "lastModifiedDateTime": "2026-08-23T12:00:00Z",
        "size": len(content),
        "eTag": "etag-operational-1",
        "file": {"mimeType": "application/json"},
        "parentReference": {"path": "/drives/drive-1/root:/Meridian"},
    }


def test_canonical_envelope_flattens_only_connector_owned_namespaced_fields() -> None:
    body = canonical_operational_graph_bytes(_envelope())
    payload = parse_operational_graph_body(
        body,
        name="graph-customer-mer-cus-001.negraph.json",
        mime_type="application/json",
    )
    assert payload is not None
    assert payload["body_ingestion_status"] == "structured"
    assert payload["ne_meridian_operational_entity_class"] == "Customer"
    assert payload["ne_meridian_operational_field_customer_identifier"] == "mer-cus-001"
    assert payload["ne_meridian_operational_edge_has_auto_loan"] == ["mer-loan-001"]
    assert "body" not in payload
    assert "acl" not in payload


@pytest.mark.parametrize(
    ("name", "mime_type", "mutation"),
    [
        ("customer.negraph.txt", "application/json", None),
        ("customer.negraph.json", "text/plain", None),
        ("customer.negraph.json", "application/json", b"{}\n"),
        ("customer.negraph.json", "application/json", b'{"schema":"lookalike"}\n'),
    ],
)
def test_declared_operational_lookalikes_fail_closed(
    name: str, mime_type: str, mutation: bytes | None
) -> None:
    with pytest.raises(ValueError, match="operational"):
        parse_operational_graph_body(
            mutation if mutation is not None else canonical_operational_graph_bytes(_envelope()),
            name=name,
            mime_type=mime_type,
        )


def test_real_connector_record_path_emits_contract_valid_structured_record_without_docling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = canonical_operational_graph_bytes(_envelope())
    parser_constructed = False

    def unavailable_docling() -> object:
        nonlocal parser_constructed
        parser_constructed = True
        raise RuntimeError("installed runtime has no parser environment")

    monkeypatch.setattr(
        "ne.brain.ingest.docling_parser.DoclingParser",
        unavailable_docling,
    )
    connector = M365GraphConnector(m365_settings())
    connector._client.get_content = AsyncMock(return_value=body)  # type: ignore[method-assign]

    async def scenario() -> None:
        record = await connector._record_for(  # pyright: ignore[reportPrivateUsage]
            cast(Any, object()), "drive-1", _item(body), ("group-ops",)
        )
        assert record.source_id == "drive-1:graph-item-1"
        assert record.record_id == "graph-item-1"
        assert m365_data_contract(m365_settings()).validate(record.payload) == []

    asyncio.run(scenario())
    assert parser_constructed is False


def test_generic_supported_body_requires_docling_before_content_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ne.brain.ingest.docling_parser.DoclingParser",
        lambda: (_ for _ in ()).throw(RuntimeError("parser environment unavailable")),
    )
    connector = M365GraphConnector(m365_settings())
    connector._client.get_content = AsyncMock(return_value=b"never-read")  # type: ignore[method-assign]
    item = {
        **_item(b"never-read"),
        "name": "generic.md",
        "file": {"mimeType": "text/markdown"},
    }

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="parser environment unavailable"):
            await connector._record_for(  # pyright: ignore[reportPrivateUsage]
                cast(Any, object()), "drive-1", item, ("group-ops",)
            )

    asyncio.run(scenario())
    connector._client.get_content.assert_not_awaited()  # type: ignore[attr-defined]


def test_malformed_declared_record_aborts_connector_before_record_emission() -> None:
    body = b"{}\n"
    connector = M365GraphConnector(m365_settings())
    connector._client.get_content = AsyncMock(return_value=body)  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(GraphDocumentParseError, match="failed closed"):
            await connector._record_for(  # pyright: ignore[reportPrivateUsage]
                cast(Any, object()), "drive-1", _item(body), ("group-ops",)
            )

    asyncio.run(scenario())


def test_source_contract_revision_binds_operational_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = m365_settings()
    current = m365_source_link_mapper_revision(settings)
    monkeypatch.setattr(contract_module, "MERIDIAN_OPERATIONAL_SCHEMA_VERSION", "lookalike")
    assert m365_source_link_mapper_revision(settings) != current


def test_acl_semantics_change_invalidates_prior_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ACL-mapper revision bump must force a re-crawl, not leave items stale."""
    settings = m365_settings()
    current = m365_source_link_mapper_revision(settings)
    monkeypatch.setattr(contract_module, "ACL_MAPPER_REVISION", ACL_MAPPER_REVISION + 1)
    assert m365_source_link_mapper_revision(settings) != current


def test_malformed_declared_record_aborts_t1_without_delta_commit(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    del frozen_config
    state = ConnectorStateStore()

    async def scenario() -> None:
        with pytest.raises(GraphDocumentParseError, match="failed closed"):
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(
                    item_overrides={
                        "item-user": {
                            "name": "graph-customer-mer-cus-001.negraph.json",
                            "size": 3,
                            "file": {"mimeType": "application/json"},
                        }
                    },
                    content_bytes_overrides={"item-user": b"{}\n"},
                ),
                parser=RecordedBodyParser(),
            )
        assert state.load_state("m365-sharepoint") == {}

    asyncio.run(scenario())
