# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365GraphConnector — the frozen Connector Protocol over Microsoft Graph (NE-044).

Implements ``discover`` / ``backfill`` / ``incremental`` / ``permissions`` for
SharePoint/OneDrive driveItems: a driveItem ``delta`` crawl per scoped drive, a
per-item ACL crawl through the fail-closed :mod:`ne.connect.m365.acl` mapper, and
per-drive ``deltaLink`` capture for resumable incremental sync.

ONE crawl per instance (instance == one sync run): the crawl is lazy and cached.
The SyncRunner calls ``permissions()`` first, which triggers the crawl; ``backfill``
/ ``incremental`` then replay the cached records. ``incremental(state)`` honors a
DIFFERING delta basis by re-crawling — so the conformance kit's incremental-subset
test exercises real delta semantics while the SyncRunner (which hands back the
same ledger basis the crawl already used) reuses the cache.

Fail-closed ACL (the unit's security core): quarantine (NO record, NO AclEntry,
counted and — by the sync helper — audited, never emitted world-readable) covers
BOTH a ``tainted``/zero-principal mapping outcome AND an item-scoped permanent
permissions-fetch failure: exact HTTP 403/404, or HTTP 410 on the permissions URL
(which means the item is gone). C7 quarantine-and-continue: one bad item can no
longer wedge the whole crawl — the deltaLink still commits. HTTP 401, every other
4xx including 429 exhaustion, and server-side faults (all 5xx), by contrast, still
ABORT the run so no deltaLink is committed on an auth/transient/server fault.

For Docling-supported file types, the connector fetches bytes through a locally
derived canonical Graph ``/content`` endpoint, parses them through the existing
``ne-brain`` DocumentParser seam, and emits the textual body with provenance
metadata. ``@microsoft.graph.downloadUrl`` (a short-lived credential) is never
read, followed, stored, or exposed. Unsupported files remain explicit
``body_ingestion_status='unsupported'`` metadata-only records. Any supported
file fetch/size/redirect/parse failure aborts the crawl, so its deltaLink cannot
advance behind a false metadata-only success.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ne.brain.ingest.contracts import DocumentParser, ParserEnvironmentError
from ne.brain.parsed_evidence import (
    ParsedDocumentEvidenceDescriptor,
    _M365ParsedEvidenceAuthority,
)
from ne.connect.authority_admission import (
    M365DeletionObservation,
    PermissionReadEvidence,
    observed_permissions,
)
from ne.connect.endpoints import AirGapViolationError
from ne.connect.errors import (
    DeltaTokenExpiredError,
    GraphContentError,
    GraphDocumentConversionError,
    GraphDocumentParseError,
    GraphSyncError,
    GraphUnauthorizedError,
)
from ne.connect.m365.acl import map_permissions
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.content import M365BodyParser, supported_format
from ne.connect.m365.graph import Client, GraphClient, GraphPage, Transport
from ne.connect.m365.operational_graph import parse_operational_graph_body
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["M365GraphConnector", "ResolvedPermissions"]


@dataclass(frozen=True)
class ResolvedPermissions:
    """One driveItem's complete permission collection, mapped and evidenced."""

    principals: tuple[str, ...]
    tainted: bool
    evidence: PermissionReadEvidence


_QUARANTINE_ID_CAP = 100
_GRAPH_DOC_TYPE = "other"


class M365GraphConnector(_M365ParsedEvidenceAuthority):
    """Graph driveItem connector with a fail-closed per-item ACL crawl."""

    def __init__(
        self,
        settings: M365ConnectorSettings,
        *,
        transport: Transport | None = None,
        delta_state: Mapping[str, str] | None = None,
        parser: DocumentParser | None = None,
        before_site_discovery: Callable[[str], Awaitable[None]] | None = None,
        before_drive_io: Callable[[tuple[tuple[str, str], ...]], Awaitable[None]] | None = None,
    ) -> None:
        # The two hooks are the connector's claim-before-I/O seam. They are
        # awaited at the exact points a hierarchical claim must already be held:
        # before a site's /drives discovery, and after discovery but before ANY
        # delta, item or permission request. The connector calls them; it never
        # learns what they do and can never advance authority itself.
        self._before_site_discovery = before_site_discovery
        self._before_drive_io = before_drive_io
        self._settings = settings
        self._client = GraphClient(settings, transport=transport)  # C2 fail-closed at construction
        self._graph = self._client.graph_base
        self._delta_state: dict[str, str] = dict(delta_state or {})
        self._body_parser = M365BodyParser(parser)
        evidence_issue, self.__parsed_evidence_claim = self._compose_parsed_evidence_channel()
        self._body_parser._bind_evidence(
            evidence_sink=self._record_parsed_evidence,
            evidence_issue=evidence_issue,
        )

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self._parsed_evidence: dict[tuple[str, str], deque[ParsedDocumentEvidenceDescriptor]] = {}
        self._drives: tuple[str, ...] = ()
        self._drives_resolved = False
        #: Every discovered drive with the site it was reached through (``""``
        #: for a directly configured drive). Kept as PAIRS, not a flat set, so a
        #: drive reached by both routes is visible instead of deduplicated away.
        self._discovered_drives: tuple[tuple[str, str], ...] = ()
        self.pending_delta_links: dict[str, str] = {}
        #: Every deletion this crawl OBSERVED, each carrying the exact drive,
        #: item and source revision it was observed at. A bare count cannot
        #: authorize a tombstone — it names no identity — so the observation, not
        #: the number, is what this connector preserves.
        self.deleted_observations: list[M365DeletionObservation] = []
        self.acl_quarantined = 0
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids: list[str] = []
        #: The completed permission read behind each emitted record, keyed by
        #: ``<drive>:<item>``. The bridge needs it to admit, and it exists only
        #: for items whose collection was actually read to completion.
        self.permission_evidence: dict[str, PermissionReadEvidence] = {}

    def graph_session(self) -> AbstractAsyncContextManager[Client]:
        """The SAME guarded Graph session the crawl uses.

        Exposed so the bounded permission sweep re-reads through this connector's
        own C2-guarded client rather than opening a second, unguarded path to
        Graph.
        """
        return self._client.session()

    @property
    def deleted_count(self) -> int:
        """The number of observed deletions, derived from the observations.

        Derived rather than incremented so the count can never disagree with the
        identity-bearing list an authority tombstone is actually built from.
        """
        return len(self.deleted_observations)

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        await self._ensure_crawled()
        return [
            SourceObject(source_id=drive_id, kind="drive", name=f"drive:{drive_id}")
            for drive_id in self._drives
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix("delta:"): str(value)
            for key, value in state.items()
            if key.startswith("delta:")
        }

        async def _gen() -> AsyncIterator[Record]:
            if basis and basis != self._delta_state:
                self._recrawl(basis)
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def permissions(self) -> AsyncIterator[AclEntry]:
        async def _gen() -> AsyncIterator[AclEntry]:
            await self._ensure_crawled()
            for entry in self._acl:
                yield entry

        return _gen()

    @property
    def discovered_drives(self) -> tuple[tuple[str, str], ...]:
        """The ``(drive_id, site_id)`` pairs this crawl actually discovered."""
        return self._discovered_drives

    async def resolve_drive_ids(self) -> tuple[str, ...]:
        """Resolve only the exact configured and site-discovered drive scope.

        Explicit-only scopes need no provider call. Site scopes perform only the
        bounded drive discovery; delta, permission, item, and content reads stay
        deferred until the graph host accepts the exact resolved mapping.
        """
        if self._drives_resolved:
            return self._drives
        if not self._settings.site_ids:
            self._set_resolved_drives(
                tuple((drive_id, "") for drive_id in self._settings.drive_ids)
            )
            return self._drives
        async with self._client.session() as client:
            await self._resolve_drives(client)
        return self._drives

    # -- crawl ---------------------------------------------------------------
    def _recrawl(self, basis: Mapping[str, str]) -> None:
        """Reset cached state and re-crawl from a different delta basis."""
        self._delta_state = dict(basis)
        self._crawled = False
        self._records = []
        self._acl = []
        self._parsed_evidence = {}
        if not self._drives_resolved:
            self._discovered_drives = ()
        # CLEARED, never rebound: the bridge holds this exact mapping, so a
        # re-crawl that replaced the object would leave the bridge reading an
        # orphaned dict and silently admitting nothing.
        self.permission_evidence.clear()
        self.pending_delta_links = {}
        self.deleted_observations = []
        self.acl_quarantined = 0
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids = []

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        async with self._client.session() as client:
            await self._resolve_drives(client)
            if self._before_drive_io is not None:
                await self._before_drive_io(self._discovered_drives)
            for drive_id in self._drives:
                await self._crawl_drive(client, drive_id)
        self._crawled = True

    async def _resolve_drives(self, client: Client) -> None:
        if self._drives_resolved:
            return
        discovered: list[tuple[str, str]] = [
            (drive_id, "") for drive_id in self._settings.drive_ids
        ]
        for site_id in self._settings.site_ids:
            if self._before_site_discovery is not None:
                await self._before_site_discovery(site_id)
            site_drives, _ = await self._client.get_paginated(
                client, f"{self._graph}/sites/{site_id}/drives"
            )
            discovered.extend((str(d["id"]), site_id) for d in site_drives if d.get("id"))
        self._set_resolved_drives(tuple(discovered))

    def _set_resolved_drives(self, discovered: tuple[tuple[str, str], ...]) -> None:
        self._discovered_drives = tuple(dict.fromkeys(discovered))
        self._drives = tuple(dict.fromkeys(drive_id for drive_id, _ in discovered))
        self._drives_resolved = True

    async def _crawl_drive(self, client: Client, drive_id: str) -> None:
        full_url = f"{self._graph}/drives/{drive_id}/root/delta"
        start_url = self._delta_state.get(drive_id) or full_url
        try:
            items, delta_link = await self._client.get_paginated(client, start_url)
        except DeltaTokenExpiredError:
            # Graph-documented resync: stored token stale → full delta for this drive.
            items, delta_link = await self._client.get_paginated(client, full_url)
        if delta_link:
            self.pending_delta_links[drive_id] = delta_link
        for item in items:
            await self._ingest_item(client, drive_id, item)

    def _quarantine(self, source_id: str) -> None:
        """Fail-closed quarantine bookkeeping: count + (capped) capture the id."""
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(source_id)

    async def _ingest_item(self, client: Client, drive_id: str, item: Mapping[str, Any]) -> None:
        if "root" in item or "folder" in item:
            return  # the drive root and folder facets are not content records
        if "deleted" in item:
            self.deleted_observations.append(_deletion_observation(drive_id, item))
            return
        item_id = str(item["id"])
        source_id = f"{drive_id}:{item_id}"

        try:
            resolved = await self.read_permissions(client, drive_id, item_id)
        except DeltaTokenExpiredError:
            # 410 on a permissions URL means the ITEM is gone, not a delta-basis
            # expiry — quarantine, never world-readable.
            self.acl_fetch_failed += 1
            self._quarantine(source_id)
            return
        except GraphSyncError as exc:
            # Quarantine-and-continue ONLY for a per-item PERMANENT client error
            # (403/404). A 401 is a run-level authentication failure, never an
            # item-local permission result; status None (guard/transport), every
            # other 4xx including 429, and ALL 5xx likewise re-raise so the run
            # aborts and the deltaLink does NOT commit — load-bearing durability.
            if isinstance(exc, GraphUnauthorizedError):
                raise
            if exc.status in {403, 404}:
                self.acl_fetch_failed += 1
                self._quarantine(source_id)
                return
            raise
        if resolved.tainted or not resolved.principals:
            self._quarantine(source_id)
            return  # fail-closed: no record, no AclEntry, never world-readable
        outcome = resolved
        self.permission_evidence[source_id] = resolved.evidence

        record = await self._record_for(client, drive_id, item, outcome.principals)
        self._records.append(record)
        self._acl.append(AclEntry(resource_id=source_id, principals=outcome.principals))

    async def _record_for(
        self,
        client: Client,
        drive_id: str,
        item: Mapping[str, Any],
        principals: tuple[str, ...],
    ) -> Record:
        """Build the exact ``Record`` for one driveItem, body included.

        Shared by the delta crawl and the bounded sweep so a re-read produces
        byte-identical canonical content for unchanged source state — which is
        precisely what lets the immutable artifact be re-claimed rather than
        duplicated.
        """
        item_id = str(item["id"])
        source_id = f"{drive_id}:{item_id}"
        payload = _payload(item)
        file_facet = item.get("file")
        mime_type = file_facet.get("mimeType") if isinstance(file_facet, Mapping) else None
        structured_declared = isinstance(item.get("name"), str) and ".negraph." in str(item["name"])
        source_format = supported_format(item)
        declared_size = item.get("size")
        if (structured_declared or source_format is not None) and (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size < 0
            or declared_size > self._settings.max_content_bytes
        ):
            raise GraphContentError(
                "refusing M365 file body with missing, invalid, or oversized Graph metadata"
            )
        content_url = (
            f"{self._graph}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}/content"
        )
        if structured_declared:
            content = await self._client.get_content(
                client, content_url, max_bytes=self._settings.max_content_bytes
            )
            try:
                structured = parse_operational_graph_body(
                    content,
                    name=item.get("name"),
                    mime_type=mime_type,
                )
            except ValueError:
                raise GraphDocumentParseError(
                    "M365 operational record envelope failed closed validation"
                ) from None
            if structured is None:  # defensive: ``structured_declared`` is the same grammar
                raise GraphDocumentParseError("M365 operational record declaration was not parsed")
            payload.update(structured)
            return Record(record_id=item_id, payload=payload, source_id=source_id)

        if source_format is None:
            payload["body_ingestion_status"] = "unsupported"
        else:
            # Resolve the generic-document parser BEFORE downloading source
            # bytes. Operational envelopes returned above never need this
            # sidecar and remain deployable in the installed runtime image.
            self._body_parser.require_parser()
            content = await self._client.get_content(
                client, content_url, max_bytes=self._settings.max_content_bytes
            )
            try:
                payload.update(
                    await self._body_parser.parse(
                        content,
                        document_id=source_id,
                        record_id=item_id,
                        source_format=source_format,
                        acl=principals,
                    )
                )
            except (ParserEnvironmentError, AirGapViolationError):
                raise
            except GraphDocumentParseError:
                raise
            except Exception:
                # Do not include parser stderr, temp paths, source metadata, or a
                # credential-bearing redirect in the public sync error. Aborting
                # here prevents both Record emission and deltaLink commit.
                raise GraphDocumentConversionError("M365 document conversion failed") from None

        return Record(record_id=item_id, payload=payload, source_id=source_id)

    # -- permission reads (the sweep and the crawl share this exact path) ----

    async def read_permission_page(
        self, client: Client, drive_id: str, item_id: str, *, page_url: str | None = None
    ) -> GraphPage:
        """Read ONE guarded page of a driveItem's permission collection.

        The bounded sweep drives this itself so it can persist the exact next
        page between batches; the crawl loops over it. Both therefore traverse
        the same C2-guarded client and the same page parse.
        """
        url = page_url or (
            f"{self._graph}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(item_id, safe='')}/permissions"
        )
        return await self._client.get_page(client, url)

    async def read_permissions(
        self,
        client: Client,
        drive_id: str,
        item_id: str,
        *,
        page_url: str | None = None,
        principals: Sequence[str] = (),
    ) -> ResolvedPermissions:
        """Read a permission collection to COMPLETION and evidence it.

        ``page_url``/``principals`` resume a collection a previous process left
        part-read. The returned evidence exists only because every page was
        actually fetched: a run that stops early raises out of here and mints
        nothing.
        """
        collected: list[str] = list(principals)
        tainted = False
        pages = 0
        next_url = page_url
        while True:
            page = await self.read_permission_page(client, drive_id, item_id, page_url=next_url)
            pages += 1
            outcome = map_permissions(list(page.items))
            tainted = tainted or outcome.tainted
            collected.extend(outcome.principals)
            next_url = page.next_link
            if next_url is None:
                break
        deduped = tuple(dict.fromkeys(collected))
        return ResolvedPermissions(
            principals=deduped,
            tainted=tainted,
            evidence=observed_permissions(
                drive_id=drive_id,
                item_id=item_id,
                principals=deduped,
                pages_read=pages,
                collection_complete=True,
            ),
        )

    async def fetch_point_record(
        self, client: Client, drive_id: str, item_id: str, *, acl: tuple[str, ...]
    ) -> Record | None:
        """Re-fetch one admitted point through the NORMAL guarded source path.

        The sweep needs the exact canonical bytes to re-claim or re-verify the
        immutable artifact, and there is deliberately no artifact-byte export
        seam to read them back from: the bytes come from the source, through the
        same metadata/body path the crawl uses, so a re-read cannot disagree with
        a crawl of the same item.
        """
        url = f"{self._graph}/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"
        try:
            item = await self._client.get_json(client, url)
        except GraphSyncError as exc:
            if isinstance(exc, GraphUnauthorizedError):
                raise
            if exc.status in {403, 404}:
                return None
            raise
        except DeltaTokenExpiredError:
            return None
        return await self._record_for(client, drive_id, item, acl)

    def take_parsed_evidence(
        self,
        source_id: str,
        record_id: str,
        payload: Mapping[str, Any],
        acl: tuple[str, ...],
        item_id: str,
    ) -> ParsedDocumentEvidenceDescriptor | None:
        """Consume the parser-instance carrier for one streamed record occurrence."""
        key = (source_id, record_id)
        values = self._parsed_evidence.get(key)
        if not values:
            return None
        value = values.popleft()
        if not values:
            del self._parsed_evidence[key]
        return self.__parsed_evidence_claim(
            value,
            item_id=item_id,
            source_id=source_id,
            record_id=record_id,
            acl=acl,
            payload=payload,
        )

    def _record_parsed_evidence(
        self,
        source_id: str,
        record_id: str,
        descriptor: ParsedDocumentEvidenceDescriptor,
    ) -> None:
        """Receive a descriptor only from this connector's parser instance."""
        self._parsed_evidence.setdefault((source_id, record_id), deque()).append(descriptor)


def _deletion_observation(drive_id: str, item: Mapping[str, Any]) -> M365DeletionObservation:
    """Preserve one Graph delta deletion as an exact, identity-bearing observation.

    The revision is the item's own ``eTag`` when Graph supplies one, and
    otherwise a digest of the exact deletion payload under its drive and item
    id. Neither form is a wall clock, an attempt id or a counter: a tombstone
    must name the revision it was observed at, and "the crawl saw N deletions"
    names nothing that can be tombstoned.
    """
    item_id = str(item["id"])
    etag = item.get("eTag")
    if isinstance(etag, str) and etag:
        revision = etag
    else:
        canonical = json.dumps(
            {"drive_id": drive_id, "item": _to_plain_json(item), "item_id": item_id},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        revision = "deleted-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return M365DeletionObservation(drive_id=drive_id, item_id=item_id, source_revision=revision)


def _to_plain_json(value: Any) -> Any:
    """Recursively convert a Graph payload to plain JSON-shaped values."""
    if isinstance(value, Mapping):
        return {str(key): _to_plain_json(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_json(member) for member in value]
    return value


def _payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Safe driveItem metadata — NEVER the credential-bearing downloadUrl."""
    payload: dict[str, Any] = {
        "id": item["id"],
        "name": item.get("name"),
        "web_url": item.get("webUrl"),
        "last_modified": item.get("lastModifiedDateTime"),
        # Graph does not own the bounded operational-schema classification.
        # A provider payload lookalike can therefore never select a different
        # Tier-2 Document type.
        "graph_doc_type": _GRAPH_DOC_TYPE,
    }
    file_facet = item.get("file")
    parent = item.get("parentReference")
    optional = {
        "created": item.get("createdDateTime"),
        "size": item.get("size"),
        "mime_type": file_facet.get("mimeType") if isinstance(file_facet, Mapping) else None,
        "path": parent.get("path") if isinstance(parent, Mapping) else None,
        "etag": item.get("eTag"),
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload
