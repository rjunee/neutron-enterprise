# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceConnector — the frozen Connector Protocol over Bulk API 2.0 (NE-047).

Implements ``discover``/``backfill``/``incremental``/``permissions`` for a
config-driven set of SObjects: ONE ``ObjectPermissions`` REST-query crawl
resolves the OBJECT-LEVEL ACL for every configured object
(:mod:`ne.connect.salesforce.acl`), then each SURVIVING (non-quarantined)
object is extracted via a Bulk API 2.0 query job (create -> poll -> paginate
CSV results), with a ``SystemModstamp``-cursor WHERE clause added on an
incremental run.

ONE crawl per instance (instance == one sync run), the same crawl-once-cached
shape as :class:`ne.connect.sql.connector.SqlConnector` /
:class:`ne.connect.m365.connector.M365GraphConnector`: the crawl is lazy and
cached, ``permissions()`` (called first by the ``SyncRunner``) triggers it, and
``backfill``/``incremental`` replay the cache. ``incremental(state)`` honors a
DIFFERING cursor basis by re-crawling.

Fail-closed / run-abort table (the unit's security core):

* an SObject whose ``ObjectPermissions`` outcome is ``tainted`` OR carries ZERO
  principals is QUARANTINED — object-wide: no Bulk job is even CREATED for it,
  no ``Record``, no ``AclEntry``, counted and surfaced by ``sync.py`` as one
  ``connector.acl_quarantine`` denied audit event — never emitted
  world-readable. Quarantine granularity here is necessarily the OBJECT,
  because Salesforce object permissions are themselves object-level (v1;
  record-level sharing = v1.1 — see the package README).
* the ObjectPermissions crawl query ITSELF failing (a run-level fault, not a
  per-object one) ABORTS the whole run: no perms resolved, nothing written, no
  cursor commits for ANY object.
* a Bulk job entering ``Failed``/``Aborted`` state, or poll exhaustion, ABORTS
  the run (``SalesforceSyncError``, message carries the object name + state
  only — C12).
* a CSV row missing its configured id field is a CONFIG bug, not a
  data-quality one, and raises ``InvalidRecordError`` LOUD rather than
  silently skipping the row (mirrors the SQL connector's own discipline).

v1 limitations (documented, shared with the sibling connectors): the crawl is
memory-cached (bounded-corpus assumption); ``SystemModstamp`` cursors are
compared LEXICOGRAPHICALLY, which is correct ONLY because Salesforce emits a
uniform ISO-8601 ``...Z`` format in Bulk CSV output (a format change would
surface as a stale cursor, never data loss — the dedup ledger absorbs
re-reads); no relationship fields (dotted names) are supported; deletion
propagation is deferred (``IsDeleted`` is never queried).
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ne.connect.errors import InvalidRecordError, SalesforceSyncError
from ne.connect.salesforce.acl import AclOutcome, map_object_permissions
from ne.connect.salesforce.authority import (
    SalesforcePermissionReadEvidence,
    observed_salesforce_permissions,
)
from ne.connect.salesforce.client import Client, SalesforceClient, Transport
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["SalesforceConnector"]

_QUARANTINE_ID_CAP = 100
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


class SalesforceConnector:
    """Frozen ``Connector`` over a config-driven set of Salesforce SObjects."""

    def __init__(
        self,
        settings: SalesforceConnectorSettings,
        *,
        transport: Transport | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = SalesforceClient(settings, transport=transport)  # C2 fail-closed
        self._cursor_state: dict[str, str] = dict(cursor_state or {})

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self.pending_cursors: dict[str, str] = {}
        self.acl_quarantined = 0
        #: Always 0 in v1: the ObjectPermissions crawl is a single run-level
        #: query (its failure aborts the whole run rather than degrading a
        #: single object); kept for shape-parity with the sibling connectors'
        #: sync-outcome/audit-detail format.
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids: list[str] = []
        self.permission_evidence: dict[str, SalesforcePermissionReadEvidence] = {}

    async def acquire_organization_id(self) -> str:
        """Acquire provider-issued org identity before any content or ACL API call."""
        async with self._client.session() as client:
            return await self._client.authenticate(client)

    async def read_point_record(self, object_name: str, record_id: str) -> Record | None:
        """Re-read one admitted point for the bounded quiet-authority sweep."""
        cfg = next((item for item in self._settings.objects if item.name == object_name), None)
        if cfg is None or _RECORD_ID_RE.fullmatch(record_id) is None:
            raise SalesforceSyncError("Salesforce authority sweep received an invalid point")
        fields = tuple(
            dict.fromkeys(
                [cfg.id_field, cfg.modstamp_field, *cfg.content_fields, *cfg.metadata_fields]
            )
        )
        soql = (
            f"SELECT {', '.join(fields)} FROM {cfg.name} "
            f"WHERE {cfg.id_field} = '{record_id}' LIMIT 2"
        )
        async with self._client.session() as client:
            rows = await self._client.rest_query(client, soql)
        if not rows:
            return None
        if len(rows) != 1 or any(field not in rows[0] for field in fields):
            raise SalesforceSyncError(
                "Salesforce authority sweep returned an ambiguous or incomplete point"
            )
        payload = {field: "" if rows[0][field] is None else str(rows[0][field]) for field in fields}
        if payload[cfg.id_field] != record_id:
            raise SalesforceSyncError("Salesforce authority sweep returned another point")
        return Record(record_id=record_id, source_id=object_name, payload=payload)

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        """Config-driven — NO Salesforce I/O."""
        return [
            SourceObject(source_id=cfg.name, kind="sobject", name=cfg.name)
            for cfg in self._settings.objects
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix("modstamp:"): str(value)
            for key, value in state.items()
            if key.startswith("modstamp:")
        }

        async def _gen() -> AsyncIterator[Record]:
            if basis and basis != self._cursor_state:
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

    # -- crawl ---------------------------------------------------------------
    def _recrawl(self, basis: Mapping[str, str]) -> None:
        """Reset cached state and re-crawl from a different cursor basis."""
        self._cursor_state = dict(basis)
        self._crawled = False
        self._records = []
        self._acl = []
        self.pending_cursors = {}
        self.acl_quarantined = 0
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids = []
        self.permission_evidence = {}

    def _quarantine(self, object_name: str) -> None:
        """Fail-closed quarantine bookkeeping: count + (capped) capture the name."""
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(object_name)

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        settings = self._settings
        objects = settings.objects
        async with self._client.session() as client:
            # (1) ONE ObjectPermissions crawl for every configured object. Object
            # names are regex-validated at config-construction time
            # (`^[A-Za-z][A-Za-z0-9_]*$`), so building the IN-clause here is
            # injection-safe. ANY failure here ABORTS the run — fail-closed: no
            # perms resolved, nothing written, no cursor commits.
            names_clause = ", ".join(f"'{cfg.name}'" for cfg in objects)
            soql = (
                "SELECT SobjectType, PermissionsRead, Parent.Id, Parent.IsOwnedByProfile, "
                "Parent.ProfileId FROM ObjectPermissions WHERE PermissionsRead = true "
                f"AND SobjectType IN ({names_clause})"
            )
            rows = await self._client.rest_query(client, soql)

            outcomes = map_object_permissions(rows, [cfg.name for cfg in objects])

            for cfg in objects:
                outcome = outcomes[cfg.name]
                if outcome.tainted or not outcome.principals:
                    # Fail-closed: NO Bulk job is even created for a quarantined
                    # object — it is never extracted, never world-readable.
                    self._quarantine(cfg.name)
                    continue
                await self._crawl_object(client, cfg, outcome)
                self.permission_evidence[cfg.name] = observed_salesforce_permissions(
                    object_name=cfg.name,
                    principals=outcome.principals,
                    pages_read=self._client.last_rest_pages_read,
                    collection_complete=True,
                )
        self._crawled = True

    async def _crawl_object(
        self, client: Client, cfg: SalesforceObjectConfig, outcome: AclOutcome
    ) -> None:
        name = cfg.name
        fields = list(
            dict.fromkeys(
                [cfg.id_field, cfg.modstamp_field, *cfg.content_fields, *cfg.metadata_fields]
            )
        )
        cursor = self._cursor_state.get(name)
        soql = f"SELECT {', '.join(fields)} FROM {name}"
        if cursor:
            # A SOQL datetime literal is UNQUOTED — SystemModstamp comparisons
            # require the bare ISO-8601 value, not a quoted string.
            soql += f" WHERE {cfg.modstamp_field} > {cursor}"

        job_id = await self._client.create_query_job(client, soql)
        expected_records = await self._poll_job(client, name, job_id)

        max_modstamp: str | None = None
        locator: str | None = None
        seen_locators: set[str] = set()
        object_records: list[Record] = []
        page_count = 0
        while True:
            page_count += 1
            csv_text, locator = await self._client.get_query_results_page(client, job_id, locator)
            for row in csv.DictReader(io.StringIO(csv_text)):
                rid = row.get(cfg.id_field)
                if not rid:
                    raise InvalidRecordError(
                        f"Salesforce object {name!r} row is missing its id field {cfg.id_field!r}"
                    )
                object_records.append(Record(record_id=rid, source_id=name, payload=dict(row)))
                if len(object_records) > expected_records:
                    raise SalesforceSyncError(
                        f"Salesforce bulk job for object {name!r} returned more rows than declared"
                    )
                modstamp = row.get(cfg.modstamp_field)
                if modstamp and (max_modstamp is None or modstamp > max_modstamp):
                    max_modstamp = modstamp
            if locator is None:
                break
            if locator in seen_locators:
                raise SalesforceSyncError(
                    f"Salesforce bulk job for object {name!r} repeated a result locator"
                )
            if page_count >= self._settings.bulk_max_result_pages:
                raise SalesforceSyncError(
                    f"Salesforce bulk job for object {name!r} exceeded its result-page bound"
                )
            seen_locators.add(locator)

        if len(object_records) != expected_records:
            raise SalesforceSyncError(
                f"Salesforce bulk job for object {name!r} row count does not match its declaration"
            )

        self._records.extend(object_records)
        self._acl.append(AclEntry(resource_id=name, principals=outcome.principals))
        if max_modstamp is not None:
            self.pending_cursors[name] = max_modstamp

    async def _poll_job(self, client: Client, name: str, job_id: str) -> int:
        settings = self._settings
        for _ in range(settings.bulk_max_polls):
            job = await self._client.get_query_job(client, job_id)
            state = job.get("state")
            if state == "JobComplete":
                processed = job.get("numberRecordsProcessed")
                if (
                    not isinstance(processed, int)
                    or isinstance(processed, bool)
                    or processed < 0
                    or processed > settings.bulk_max_records_per_job
                ):
                    raise SalesforceSyncError(
                        f"Salesforce bulk job for object {name!r} has an invalid processed count"
                    )
                return processed
            if state in ("Failed", "Aborted"):
                raise SalesforceSyncError(
                    f"Salesforce bulk job for object {name!r} entered state {state!r}"
                )
            await asyncio.sleep(settings.bulk_poll_interval_seconds)
        raise SalesforceSyncError(
            f"Salesforce bulk job for object {name!r} did not complete after "
            f"{settings.bulk_max_polls} polls"
        )
