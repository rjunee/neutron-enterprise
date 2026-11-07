# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ZendeskConnector — the frozen Connector Protocol over Zendesk REST (NE-093).

Implements ``discover``/``backfill``/``incremental``/``permissions`` for the
tickets and Help Center articles streams. Tickets are pulled via the cursor-based
incremental export (``after_cursor`` / ``end_of_stream``); articles via the Help
Center incremental export (``end_time``). Each surviving ticket/article resolves
its own fail-closed :class:`~ne.contracts.integration.AclEntry`
(:mod:`ne.connect.zendesk.acl`); a private ticket comment is stamped with the
STAFF SUBSET of the ticket ACL under a distinct ``ticket/{id}/internal`` resource.

ONE crawl per instance (crawl-once-cached, the sibling-connector shape):
``permissions()`` triggers the crawl, ``backfill``/``incremental`` replay the
cache, and ``incremental(state)`` honors a DIFFERING cursor basis by re-crawling.

Fail-closed / run-abort table (the unit's security core):

* a ticket whose ``map_ticket_acl`` outcome is ``tainted`` OR carries ZERO
  principals is QUARANTINED — the ticket AND its comments (never fetched),
  counted, never world-readable;
* a ``deleted``-status ticket is SKIPPED (deletion propagation deferred, sibling
  parity);
* a PRIVATE comment whose staff subset is EMPTY is QUARANTINED per-item;
* a DRAFT article, or an article whose segment outcome is tainted/zero, is
  QUARANTINED;
* an HTTP fault / retry exhaustion ABORTS the run (``ZendeskSyncError``) — no
  cursor commits; a ticket/article/comment missing its id raises
  ``InvalidRecordError`` LOUD.

v1 limitations (documented, see README + :mod:`ne.connect.zendesk.acl`): one
``AclEntry`` per ticket (memory-bounded-corpus assumption, sibling parity);
agent ticket-access policies / custom roles / brand restrictions not mirrored;
collaborators/followers a documented cut.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ne.connect.errors import InvalidRecordError
from ne.connect.zendesk.acl import AclOutcome, map_article_acl, map_comment_acl, map_ticket_acl
from ne.connect.zendesk.client import Client, Transport, ZendeskClient
from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["ZendeskConnector"]

_QUARANTINE_ID_CAP = 100


class ZendeskConnector:
    """Frozen ``Connector`` over the Zendesk tickets + Help Center articles streams."""

    def __init__(
        self,
        settings: ZendeskConnectorSettings,
        *,
        transport: Transport | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = ZendeskClient(settings, transport=transport)  # C2 fail-closed
        self._cursor_state: dict[str, str] = dict(cursor_state or {})

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self.pending_cursors: dict[str, str] = {}
        self.acl_quarantined = 0
        self.acl_quarantined_ids: list[str] = []
        self.items_quarantined = 0
        self.items_quarantined_ids: list[str] = []

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        """Config-driven — NO Zendesk I/O."""
        objects: list[SourceObject] = []
        if self._settings.sync_tickets:
            objects.append(SourceObject(source_id="tickets", kind="stream", name="tickets"))
        if self._settings.sync_articles:
            objects.append(SourceObject(source_id="articles", kind="stream", name="articles"))
        return objects

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix("cursor:"): str(value)
            for key, value in state.items()
            if key.startswith("cursor:")
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
        self._cursor_state = dict(basis)
        self._crawled = False
        self._records = []
        self._acl = []
        self.pending_cursors = {}
        self.acl_quarantined = 0
        self.acl_quarantined_ids = []
        self.items_quarantined = 0
        self.items_quarantined_ids = []

    def _quarantine_object(self, object_id: str) -> None:
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(object_id)

    def _quarantine_item(self, item_id: str) -> None:
        self.items_quarantined += 1
        if len(self.items_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.items_quarantined_ids.append(item_id)

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        async with self._client.session() as client:
            if self._settings.sync_tickets:
                await self._crawl_tickets(client)
            if self._settings.sync_articles:
                await self._crawl_articles(client)
        self._crawled = True

    async def _crawl_tickets(self, client: Client) -> None:
        after_cursor: str | None = self._cursor_state.get("tickets")
        last_cursor = after_cursor
        while True:
            if after_cursor is None:
                tickets, next_cursor, end = await self._client.incremental_tickets(
                    client, start_time=self._settings.tickets_start_time, cursor=None
                )
            else:
                tickets, next_cursor, end = await self._client.incremental_tickets(
                    client, start_time=None, cursor=after_cursor
                )
            for ticket in tickets:
                await self._process_ticket(client, ticket)
            if next_cursor is not None:
                last_cursor = next_cursor
            if end or next_cursor is None:
                break
            after_cursor = next_cursor
        if last_cursor is not None:
            self.pending_cursors["tickets"] = last_cursor

    async def _process_ticket(self, client: Client, ticket: Mapping[str, Any]) -> None:
        if ticket.get("status") == "deleted":
            return  # deletion propagation deferred (sibling parity).
        raw_id = ticket.get("id")
        if raw_id in (None, ""):
            raise InvalidRecordError("Zendesk ticket is missing its id")
        ticket_id = str(raw_id)
        outcome = map_ticket_acl(ticket)
        if outcome.tainted or not outcome.principals:
            # Quarantine the ticket AND its comments (never fetched).
            self._quarantine_object(f"ticket/{ticket_id}")
            return

        resource = f"ticket/{ticket_id}"
        self._acl.append(AclEntry(resource_id=resource, principals=outcome.principals))
        self._records.append(
            Record(
                record_id=ticket_id,
                source_id=resource,
                payload={
                    "id": ticket_id,
                    "subject": ticket.get("subject"),
                    "description": ticket.get("description"),
                    "status": ticket.get("status"),
                    "priority": ticket.get("priority"),
                    "created_at": ticket.get("created_at"),
                    "updated_at": ticket.get("updated_at"),
                    "group_id": ticket.get("group_id"),
                    "organization_id": ticket.get("organization_id"),
                },
            )
        )

        if self._settings.include_ticket_comments:
            await self._process_comments(client, int(raw_id), ticket_id, outcome)

    async def _process_comments(
        self, client: Client, ticket_numeric_id: int, ticket_id: str, ticket_outcome: AclOutcome
    ) -> None:
        internal_emitted = False
        for comment in await self._client.ticket_comments(client, ticket_numeric_id):
            raw_cid = comment.get("id")
            if raw_cid in (None, ""):
                raise InvalidRecordError(
                    f"Zendesk comment on ticket {ticket_id!r} is missing its id"
                )
            cid = str(raw_cid)
            public = comment.get("public") is not False  # default public
            comment_outcome = map_comment_acl(ticket_outcome, public)
            if not comment_outcome.principals:
                # Empty staff subset on a private comment -> per-item quarantine.
                self._quarantine_item(f"comment/{cid}")
                continue
            source_id = f"ticket/{ticket_id}" if public else f"ticket/{ticket_id}/internal"
            if not public and not internal_emitted:
                self._acl.append(
                    AclEntry(resource_id=source_id, principals=comment_outcome.principals)
                )
                internal_emitted = True
            self._records.append(
                Record(
                    record_id=f"comment-{cid}",
                    source_id=source_id,
                    payload={
                        "id": cid,
                        "ticket_id": ticket_id,
                        "public": public,
                        "author_id": comment.get("author_id"),
                        "body": comment.get("body"),
                        "created_at": comment.get("created_at"),
                    },
                )
            )

    async def _crawl_articles(self, client: Client) -> None:
        stored = self._cursor_state.get("articles")
        start = int(stored) if stored is not None else self._settings.articles_start_time
        final_end = start
        while True:
            articles, end_time, has_next = await self._client.incremental_articles(client, start)
            for article in articles:
                self._process_article(article)
            if end_time is not None:
                final_end = end_time
            if not articles or not has_next or end_time is None or end_time <= start:
                break
            start = end_time
        self.pending_cursors["articles"] = str(final_end)

    def _process_article(self, article: Mapping[str, Any]) -> None:
        raw_id = article.get("id")
        if raw_id in (None, ""):
            raise InvalidRecordError("Zendesk article is missing its id")
        article_id = str(raw_id)
        outcome = map_article_acl(article)
        if outcome.tainted or not outcome.principals:
            self._quarantine_object(f"article/{article_id}")
            return
        resource = f"article/{article_id}"
        self._acl.append(AclEntry(resource_id=resource, principals=outcome.principals))
        self._records.append(
            Record(
                record_id=article_id,
                source_id=resource,
                payload={
                    "id": article_id,
                    "title": article.get("title"),
                    "body": article.get("body"),
                    "locale": article.get("locale"),
                    "section_id": article.get("section_id"),
                    "updated_at": article.get("updated_at"),
                },
            )
        )
