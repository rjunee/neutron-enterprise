# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit coverage for the internal CRM clients (NE-S5-INTERNAL-CRM).

The e2e file (``test_s5_internal_crm_end_to_end``) proves the happy path
through the platform composition; this file pins the fail-closed semantics the
Salesforce clients had and the internal clients must keep: a miss is reported
(never raised), ambiguity refuses, an unaddressable contact refuses, and a
truncated scan refuses rather than resolving against a partial record set.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.internal_crm import (
    InternalCrmLookupClient,
    InternalCrmNoteClient,
    assert_crm_connector_is_unshared,
    ensure_internal_crm_schema,
    ne_internal_crm_notes,
)
from ne.contracts.types import Chunk

from .conftest import InMemoryFilteredChunkStore


def _contact(
    chunk_id: str,
    *,
    first: str = "Avery",
    last: str = "Arbor",
    email: str | None = "owner@example.com",
    record_id: str | None = "003AveryArbor00001",
    account_id: str | None = "001MeridianHh00001",
    connector: str = "salesforce",
) -> Chunk:
    blocks = [b for b in (first, last, email) if b]
    metadata: dict[str, Any] = {
        "connector": connector,
        "source_id": "Contact",
        "pii_class": "personal",
        "object": "Contact",
    }
    if record_id:
        metadata["record_id"] = record_id
    if account_id:
        metadata["AccountId"] = account_id
    return Chunk(
        chunk_id=chunk_id,
        document_id=chunk_id,
        text="\n\n".join(blocks),
        metadata=metadata,
        acl=("ops",),
    )


def _account(chunk_id: str, name: str, record_id: str, *, connector: str = "salesforce") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=chunk_id,
        text=name,
        metadata={
            "connector": connector,
            "source_id": "Account",
            "record_id": record_id,
            "pii_class": "personal",
            "object": "Account",
        },
        acl=("ops",),
    )


def _client(*chunks: Chunk, scan_limit: int = 1024) -> InternalCrmLookupClient:
    return InternalCrmLookupClient(InMemoryFilteredChunkStore(chunks), scan_limit=scan_limit)


def test_contact_lookup_serves_the_synced_mirror() -> None:
    client = _client(_contact("c1"), _account("a1", "Arbor Household", "001MeridianHh00001"))
    record = asyncio.run(client.find_contact_by_name("  Avery  Arbor "))
    assert record == {
        "record_id": "003AveryArbor00001",
        "email": "owner@example.com",
        "account_id": "001MeridianHh00001",
    }


def test_contact_lookup_miss_is_reported_not_raised() -> None:
    client = _client(_contact("c1"))
    assert asyncio.run(client.find_contact_by_name("Nobody Here")) is None


def test_contact_lookup_ambiguity_refuses() -> None:
    client = _client(_contact("c1"), _contact("c2", record_id="003AveryArbor00002"))
    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(client.find_contact_by_name("Avery Arbor"))


def test_contact_without_email_refuses_rather_than_inventing() -> None:
    client = _client(_contact("c1", email=None))
    with pytest.raises(ValueError, match="refusing to serve it"):
        asyncio.run(client.find_contact_by_name("Avery Arbor"))


def test_contact_without_account_refuses() -> None:
    client = _client(_contact("c1", account_id=None))
    with pytest.raises(ValueError, match="refusing to serve it"):
        asyncio.run(client.find_contact_by_name("Avery Arbor"))


def test_truncated_scan_refuses_to_resolve() -> None:
    chunks = [
        _contact(f"c{i}", first=f"F{i}", last=f"L{i}", record_id=f"003x{i:014d}") for i in range(3)
    ]
    client = _client(*chunks, scan_limit=3)
    with pytest.raises(ValueError, match="cap"):
        asyncio.run(client.find_contact_by_name("F0 L0"))


def test_empty_name_refused() -> None:
    client = _client(_contact("c1"))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(client.find_contact_by_name("   "))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(client.find_id_by_name("Account", ""))


def test_account_lookup_by_exact_name() -> None:
    client = _client(
        _account("a1", "Arbor Household", "001MeridianHh00001"),
        _account("a2", "Other Corp", "001Other0000000001"),
    )
    assert asyncio.run(client.find_id_by_name("Account", "arbor household")) == "001MeridianHh00001"
    assert asyncio.run(client.find_id_by_name("Account", "No Such Corp")) is None


def test_note_client_writes_a_durable_row_and_refuses_empty_fields(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path}/notes-{uuid.uuid4().hex}.db")
    try:
        ensure_internal_crm_schema(engine)
        client = InternalCrmNoteClient(engine)
        note_id = asyncio.run(
            client.create_record(
                "Note",
                {"ParentId": "001MeridianHh00001", "Title": "t", "Body": "b"},
            )
        )
        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_internal_crm_notes)).mappings().one()
        assert row["note_id"] == note_id
        assert row["parent_record_id"] == "001MeridianHh00001"

        with pytest.raises(ValueError, match="requires"):
            asyncio.run(client.create_record("Note", {"Title": "t", "Body": "b"}))
    finally:
        engine.dispose()


# -- F2: the scan is pinned to the SOURCE CONNECTOR, not just the sObject ------
#
# The metadata namespace is shared by every connector: the SQL sync stamps any
# configured ``metadata_columns`` verbatim (a source column literally named
# ``object`` lands as ``metadata.object``) and the m365 sync moves item metadata
# whole. Scanning on ``metadata.object`` ALONE therefore lets a row from ANOTHER
# source into a governed CRM read — poisoning the "recorded email" an outreach
# draft copies, or tripping the ambiguity refusal and denying S5 outright.


def test_scan_pins_both_the_connector_and_the_object() -> None:
    store = InMemoryFilteredChunkStore([_contact("c1")])
    client = InternalCrmLookupClient(store)
    asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert store.seen_filters == [
        {"metadata.connector": "salesforce", "metadata.object": "Contact"}
    ]


def test_a_same_shaped_contact_from_another_connector_is_not_served() -> None:
    """A SharePoint/SQL row wearing ``object: Contact`` must not resolve."""
    client = _client(
        _contact("c1"),
        _contact(
            "poisoned",
            email="attacker@evil.test",
            record_id="003Poisoned000001",
            connector="m365",
        ),
    )
    record = asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert record is not None
    assert record["email"] == "owner@example.com"
    assert record["record_id"] == "003AveryArbor00001"


def test_a_foreign_connector_contact_alone_is_a_miss_not_a_served_record() -> None:
    client = _client(_contact("poisoned", email="attacker@evil.test", connector="sql:crm_export"))
    assert asyncio.run(client.find_contact_by_name("Avery Arbor")) is None


def test_a_foreign_connector_account_is_not_served() -> None:
    client = _client(_account("a1", "Arbor Household", "001Poisoned0000001", connector="m365"))
    assert asyncio.run(client.find_id_by_name("Account", "Arbor Household")) is None


def test_the_connector_pin_is_configurable_and_never_empty() -> None:
    client = InternalCrmLookupClient(
        InMemoryFilteredChunkStore([_contact("c1", connector="sf-prod")]),
        connector="sf-prod",
    )
    record = asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert record is not None and record["email"] == "owner@example.com"

    with pytest.raises(ValueError, match="connector"):
        InternalCrmLookupClient(InMemoryFilteredChunkStore([]), connector="")


# -- The pin carries a source NAME, never a connector KIND ---------------------
#
# metadata["connector"] is the operator-chosen ``source_name``. Nothing in a
# synced document records which connector kind wrote it: every stamped key comes
# either from source_name or from the row payload. SqlConnectorSettings.source_name
# is freely configurable and ``object`` is an ALLOWED metadata_columns entry (it is
# NOT in sql/config.py's _RESERVED_METADATA_KEYS), so a SQL source configured with
# source_name="salesforce" carrying a column named ``object`` emits documents the
# scan cannot tell apart from real Salesforce records. Stamping an immutable
# connector-kind discriminator would only mark documents synced AFTER the change,
# and this corpus CANNOT be re-synced (the Salesforce REST quota is exhausted —
# the reason the internal path exists). So the name is enforced for UNIQUENESS at
# composition, and the residual limitation is documented and executable below.


def test_KNOWN_LIMITATION_an_equal_source_name_row_is_indistinguishable() -> None:
    """Executable proof of the residual gap the uniqueness guard fences off.

    A foreign row that copies the pinned source name EXACTLY is not excluded by
    the pin — the scan has nothing left to discriminate on. With the genuine
    record present this degrades to the ambiguity REFUSAL (a denial, never a
    silent swap)...
    """
    client = _client(
        _contact("c1"),
        _contact("sql-row", email="attacker@evil.test", record_id="003Poisoned000001"),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(client.find_contact_by_name("Avery Arbor"))


def test_KNOWN_LIMITATION_an_equal_source_name_row_alone_is_served() -> None:
    """...but ALONE it IS served. This is the gap; the guard below prevents it."""
    client = _client(_contact("sql-row", email="attacker@evil.test"))
    record = asyncio.run(client.find_contact_by_name("Avery Arbor"))
    assert record is not None
    assert record["email"] == "attacker@evil.test"


def test_a_second_connector_claiming_the_pinned_source_name_refuses_to_compose() -> None:
    with pytest.raises(ValueError, match="SAME source_name"):
        assert_crm_connector_is_unshared(
            "salesforce",
            configured_source_names={"salesforce": "salesforce", "sql": "salesforce"},
        )


def test_the_crm_alias_claiming_its_own_pinned_name_is_not_a_collision() -> None:
    assert_crm_connector_is_unshared(
        "salesforce", configured_source_names={"salesforce": "salesforce"}
    )


def test_distinct_source_names_compose_cleanly() -> None:
    assert_crm_connector_is_unshared(
        "salesforce",
        configured_source_names={"salesforce": "salesforce", "sql": "crm_export", "m365": "m365"},
    )


def test_the_collision_guard_follows_a_non_default_pin() -> None:
    """The guard checks the RESOLVED pin, not the literal string "salesforce"."""
    assert_crm_connector_is_unshared(
        "sf-prod", configured_source_names={"salesforce": "sf-prod", "sql": "salesforce"}
    )
    with pytest.raises(ValueError, match="SAME source_name"):
        assert_crm_connector_is_unshared(
            "sf-prod", configured_source_names={"salesforce": "sf-prod", "sql": "sf-prod"}
        )
