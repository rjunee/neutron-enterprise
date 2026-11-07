# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Internal CRM clients over the platform's own stores (NE-S5-INTERNAL-CRM).

Owner decision 2026-08-28: the demo org's Salesforce REST quota is exhausted
(``REQUEST_LIMIT_EXCEEDED``, rolling 24h window), so the CRM legs of the S5
outreach ladder no longer make ANY live Salesforce call. The records the demo
needs are already HERE: the connector synced the org's Accounts/Contacts/Cases
into the platform chunk store before the cap, and every one of those documents
carries its Salesforce ``record_id``/``AccountId`` in stamped metadata
(``make_salesforce_record_mapper``). These clients serve the SAME narrow
Protocols the Salesforce clients served — the governed tools, their names,
their tiers, the PDP, the guardrails, and the approval gate are all unchanged;
only the client behind the seam stops crossing the network:

* :class:`InternalCrmLookupClient` satisfies BOTH
  ``ne.runtime.reference_actions.crm_lookup.CrmLookupClient`` (account id by
  exact name) and ``CrmContactLookupClient`` (contact by exact full name →
  record id, recorded email, parent account id), reading the already-synced
  connector documents out of the chunk store by stamped metadata.
* :class:`InternalCrmNoteClient` satisfies
  ``ne.runtime.reference_actions.crm_note.CrmNoteClient``, landing the note as
  a durable row in the platform's OWN runtime database
  (``ne_internal_crm_notes``) — a real write with a real generated id, never a
  recording stub.

Governance posture, stated plainly: the Salesforce lookup authenticated as the
ONE integration identity, so what it could read was never scoped to the calling
principal; serving the same fields from the synced mirror preserves exactly
that exposure. This is NOT a principal-scoped knowledge read under C7 —
SPEC NE-043 scopes that invariant to the ``KnowledgeRead``
retrieval surface ("MUST cover EVERY principal-scoped read path (both
``KnowledgeRead.search`` AND ``KnowledgeRead.retrieve`` fetch-by-id) ...
``ChunkStore.get`` is unscoped-by-design and stays raw"), and the NE-021
landmine it names is a principal-scoped method that IGNORES its principal.
``find_contact_by_name(self, name)`` never carried a principal at any layer, in
this client or in the Salesforce client it replaced: it is an
integration-identity system-of-record lookup, not a principal-scoped read that
dropped its principal. The un-trimmed exposure is a deliberate choice, pinned in
BOTH directions by ``packages/ne-brain/tests/test_scan_filtered_real_stores.py``
(omitting ACL terms does not trim; passing them does). Arbiter ruling
2026-08-29, on the owner's 2026-08-28 decision — adding ACL trimming here would
be the actual behaviour change, since it could refuse S5 lookups for principals
lacking the corpus's ``sf-profile:``/``sf-permset:`` labels.

FORWARD FENCE (recorded, deliberately NOT built — this box is single-user and no
such source exists today): that ruling rests on the v1 connector mirroring
OBJECT-level ACLs only, so within ``Contact`` every record carries the identical
ACL and no principal is entitled to contact A but not contact B. If a future
engagement ever syncs a source carrying RECORD-level ACLs into the chunk store
and points an internal-mirror lookup at it, the parity argument collapses and
that lookup MUST become principal-scoped.

The tool result shapes are byte-compatible with the Salesforce
clients' (a miss is reported, ambiguity refuses, an unaddressable contact
refuses) so no caller — model, workflow, or test — can tell the transport
changed except by the audit detail's ``source_system`` honestly saying
``internal``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa

from ne.contracts.types import Chunk

__all__ = [
    "CRM_SOURCE_ALIAS",
    "DEFAULT_CRM_CONNECTOR",
    "FilteredChunkScan",
    "InternalCrmLookupClient",
    "InternalCrmNoteClient",
    "assert_crm_connector_is_unshared",
    "ensure_internal_crm_schema",
    "internal_crm_metadata",
    "ne_internal_crm_notes",
]


@runtime_checkable
class FilteredChunkScan(Protocol):
    """Structural seam: a ChunkStore supporting a filter-only bounded scan.

    Deliberately NOT part of the frozen ne-contracts ``ChunkStore`` Protocol
    (the ``FilteredChunkFetch`` precedent in ``ne.brain.retriever``);
    ``QdrantChunkStore.scan_filtered`` and ``PgVectorChunkStore.scan_filtered``
    satisfy it.
    """

    async def scan_filtered(self, *, filters: Mapping[str, Any], limit: int) -> Sequence[Chunk]: ...


#: The connector-registry alias whose sync produced the CRM corpus.
CRM_SOURCE_ALIAS = "salesforce"

#: Fallback for the connector source name the synced CRM corpus carries in
#: ``metadata["connector"]`` — matches ``SalesforceConnectorSettings.source_name``'s
#: OWN default. The platform prefers the RESOLVED, config-hash-frozen connector
#: settings (``ne-connect-source-salesforce``); this constant is what remains when
#: the Salesforce sync is not a configured source, and being a code constant it
#: cannot be moved by configuration that never entered the frozen preimage.
#:
#: The scan pins this key because the metadata namespace is OPEN: the SQL
#: connector copies any configured ``metadata_columns`` verbatim (a source column
#: literally named ``object`` lands as ``metadata.object``) and the m365 sync moves
#: item metadata whole. Scanning on ``metadata.object`` ALONE would let another
#: source put a same-shaped row into a GOVERNED contact read.
DEFAULT_CRM_CONNECTOR = "salesforce"


def assert_crm_connector_is_unshared(
    connector: str, *, configured_source_names: Mapping[str, str]
) -> None:
    """Refuse to compose when another connector claims the CRM pin's source name.

    KNOWN LIMITATION this enforces around. ``metadata["connector"]`` is the
    connector's OPERATOR-CHOSEN ``source_name``, NOT its type. Nothing in a
    synced document records which connector KIND wrote it: every stamped key is
    derived either from ``source_name`` (``connector``, ``source_system``,
    ``provenance``, ``ne_source_snapshot_hash``) or from the row payload
    (``object``, ``record_id``, and — since it is not a reserved
    ``metadata_columns`` key on the SQL path and is validated nowhere in the
    write path — even ``source_url``). ``SqlConnectorSettings.source_name`` is
    freely configurable and ``object`` is an ALLOWED ``metadata_columns`` entry,
    so a SQL source configured with ``source_name="salesforce"`` carrying a row
    column named ``object`` emits documents the CRM scan CANNOT tell apart from
    real Salesforce records.

    Stamping an immutable connector-kind discriminator is the durable fix, but
    it would only mark documents synced AFTER the change — and this corpus
    cannot be re-synced (the demo org's Salesforce REST quota is exhausted; that
    is why the internal CRM path exists at all). So the identity is enforced for
    UNIQUENESS at composition instead: if any other configured connector claims
    the pinned source name, boot FAILS LOUD rather than serving a governed CRM
    read that cannot attribute its own records.
    """
    colliding = sorted(
        alias
        for alias, source_name in configured_source_names.items()
        if alias != CRM_SOURCE_ALIAS and source_name == connector
    )
    if colliding:
        raise ValueError(
            f"internal CRM lookups pin metadata.connector={connector!r}, but "
            f"connector source(s) {colliding} are configured with that SAME "
            "source_name. A synced document records only the source NAME, never "
            "which connector kind wrote it, so the governed CRM read could not "
            "tell their records apart; give them distinct source_name values."
        )


#: Hard ceiling on the filter-only scan, comfortably above the whole synced demo
#: corpus (~180 documents). Receiving exactly this many rows is unambiguous
#: evidence of truncation — the scan refuses loudly instead of resolving a lookup
#: against a partial record set.
_SCAN_LIMIT = 1024


def _blocks(chunk: Chunk) -> list[str]:
    """The mapper's config-ordered ``\\n\\n``-joined content fields, split back."""
    return [block for block in chunk.text.split("\n\n") if block.strip()]


def _is_email(block: str) -> bool:
    return "@" in block and " " not in block and "\n" not in block


class InternalCrmLookupClient:
    """Resolve CRM records from the already-synced connector documents.

    Matching mirrors the Salesforce clients' semantics: an exact-name match
    (case-insensitive, as SOQL string equality is), ``None`` on a miss, a
    raised error on MORE than one match (an ambiguous record is never silently
    resolved) and on a matched-but-unusable record (no id / no email / no
    parent account — fail closed, never invent a value).

    The scan is pinned to BOTH the source connector (:data:`DEFAULT_CRM_CONNECTOR`,
    stamped as ``metadata["connector"]``) and the sObject, because the metadata
    namespace is shared by every connector: ``metadata.object`` alone would let
    a same-shaped row from ANOTHER source be served as a CRM record.

    The synced document shape this reads is pinned by
    ``make_salesforce_record_mapper``: ``metadata["connector"]`` names the
    connector, ``metadata["object"]`` names the sObject,
    ``metadata["record_id"]`` its Salesforce id,
    ``metadata["AccountId"]`` a Contact's parent account, and ``text`` joins
    the configured content fields with ``\\n\\n`` — for the demo corpus
    ``FirstName\\n\\nLastName\\n\\nEmail[\\n\\nPhone]`` (Contact) and ``Name``
    (Account).
    """

    def __init__(
        self,
        store: FilteredChunkScan,
        *,
        connector: str = DEFAULT_CRM_CONNECTOR,
        scan_limit: int = _SCAN_LIMIT,
    ) -> None:
        self._store = store
        if not connector:
            raise ValueError("internal CRM lookups require a connector to pin the scan to")
        self._connector = connector
        self._scan_limit = scan_limit

    async def _scan(self, sobject: str) -> Sequence[Chunk]:
        chunks = await self._store.scan_filtered(
            filters={"metadata.connector": self._connector, "metadata.object": sobject},
            limit=self._scan_limit,
        )
        if len(chunks) >= self._scan_limit:
            # STATUS only — never a record value (C12).
            raise ValueError(
                f"internal CRM scan for {sobject} hit the {self._scan_limit}-row cap; "
                "refusing to resolve a lookup against a possibly-partial record set"
            )
        return chunks

    async def find_id_by_name(self, sobject: str, name: str) -> str | None:
        """The ``CrmLookupClient`` leg: one sObject record id by exact Name."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("refusing to look up an empty record name")
        wanted = cleaned.casefold()
        matches: list[Chunk] = []
        for chunk in await self._scan(sobject):
            blocks = _blocks(chunk)
            if blocks and blocks[0].strip().casefold() == wanted:
                matches.append(chunk)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"{sobject} name is ambiguous; refusing to pick one")
        record_id = str(matches[0].metadata.get("record_id") or "")
        if not record_id:
            raise ValueError(f"{sobject} record lacks a record_id; refusing to serve it")
        return record_id

    async def find_contact_by_name(self, name: str) -> Mapping[str, str] | None:
        """The ``CrmContactLookupClient`` leg: contact by exact full name.

        A Contact document's first two content blocks are FirstName and
        LastName (the mapper skips empty fields, and Salesforce requires
        LastName, so a one-block contact is matched against that single
        block); the email is the first block shaped like an address.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("refusing to look up an empty contact name")
        wanted = " ".join(cleaned.split()).casefold()
        matches: list[Chunk] = []
        for chunk in await self._scan("Contact"):
            blocks = _blocks(chunk)
            name_blocks = [block for block in blocks if not _is_email(block)][:2]
            candidate = " ".join(part.strip() for part in name_blocks).casefold()
            if candidate and candidate == wanted:
                matches.append(chunk)
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError("Contact name is ambiguous; refusing to pick one")
        chunk = matches[0]
        record_id = str(chunk.metadata.get("record_id") or "")
        account_id = str(chunk.metadata.get("AccountId") or "")
        email = next((block.strip() for block in _blocks(chunk) if _is_email(block)), "")
        if not record_id or not email or not account_id:
            # Same refusal (and near-verbatim message) as the Salesforce client:
            # an outreach flow must fail closed on an unaddressable or
            # unparented contact, not invent a value.
            raise ValueError(
                "Contact record lacks an Id, Email, or AccountId; refusing to serve it"
            )
        return {"record_id": record_id, "email": email, "account_id": account_id}


internal_crm_metadata = sa.MetaData()

#: The internal CRM note ledger — the durable landing table for
#: ``write.internal.crm.note``. Append-only by construction (no update/delete
#: path exists in this module); every row is also fully audited by the governed
#: executor that performed it, like any other action.
ne_internal_crm_notes = sa.Table(
    "ne_internal_crm_notes",
    internal_crm_metadata,
    sa.Column("note_id", sa.String(64), primary_key=True),
    sa.Column("sobject", sa.String(64), nullable=False),
    sa.Column("parent_record_id", sa.String(64), nullable=False),
    sa.Column("title", sa.String(255), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_internal_crm_schema(engine: sa.Engine) -> None:
    """Create the ``ne_internal_crm_notes`` table (idempotent)."""
    internal_crm_metadata.create_all(engine)


class InternalCrmNoteClient:
    """The ``CrmNoteClient`` seam over the platform's own runtime database.

    ``create_record`` lands ONE durable row and returns its server-generated
    id — a real internal write (queryable evidence, not a recorder). The field
    names mirror the Salesforce ``Note`` shape the reference action sends
    (``ParentId``/``Title``/``Body``) so the action definition needs no
    knowledge of which client is behind the seam.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        parent = str(fields.get("ParentId") or "")
        title = str(fields.get("Title") or "")
        body = str(fields.get("Body") or "")
        if not parent or not title or not body:
            raise ValueError("internal CRM note requires ParentId, Title, and Body")
        note_id = f"note-{uuid.uuid4().hex}"
        # Direct sync-engine write inside the async seam — the exact posture of
        # ``ne.connect.sql.write.SqlWriteClient.update_record`` (one bounded
        # single-row statement; not worth a thread hop).
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(ne_internal_crm_notes).values(
                    note_id=note_id,
                    sobject=sobject,
                    parent_record_id=parent,
                    title=title,
                    body=body,
                    created_at=datetime.now(UTC),
                )
            )
        return note_id
