# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsConnector — the frozen Connector Protocol over Microsoft Graph (NE-080).

Implements ``discover`` / ``backfill`` / ``incremental`` / ``permissions`` for
the channel-messages stream: per scoped team, a channels enumeration and a
per-channel messages ``delta`` crawl, with a fail-closed
:mod:`ne.connect.teams.acl` mapping deciding whether a channel's messages are
ingested (with a non-empty ACL) or QUARANTINED. Per-channel ``deltaLink`` capture
drives resumable incremental sync.

ONE crawl per instance (crawl-once-cached, the sibling-connector shape):
``permissions()`` triggers the crawl, ``backfill`` / ``incremental`` replay the
cache, and ``incremental(state)`` honors a DIFFERING ``cursor:``-prefixed delta
basis by re-crawling.

Fail-closed / run-abort table (the unit's security core):

* a channel whose ``map_channel_acl`` outcome is ``tainted`` (forged-prefix id)
  OR carries ZERO principals (shared / unknown ``membershipType``) is
  QUARANTINED — its messages are NEVER fetched, counted, never over-returned;
* a message OR reply with ``deletedDateTime`` set, or
  ``messageType != 'message'`` (system events), is SKIPPED;
* a message (or reply) missing its id raises ``InvalidRecordError`` LOUD;
* an expired persisted deltaLink (Graph HTTP 410 → ``DeltaTokenExpiredError``)
  re-baselines that channel with a full delta crawl (the Graph-documented
  resync, m365 parity) instead of aborting the run;
* an HTTP fault / retry exhaustion ABORTS the run (``GraphSyncError``) — no
  cursor commits.

v1 limitations (documented, see README + :mod:`ne.connect.teams.acl`): channel
messages only (1:1 / group chats not crawled); shared channels quarantined;
membership-edge seeding is NE-042-side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ne.connect.errors import DeltaTokenExpiredError, InvalidRecordError
from ne.connect.teams.acl import map_channel_acl
from ne.connect.teams.config import TeamsConnectorSettings
from ne.connect.teams.graph import Client, TeamsGraphClient, Transport
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["TeamsConnector"]

_QUARANTINE_ID_CAP = 100


class TeamsConnector:
    """Frozen ``Connector`` over the Teams channel-messages stream."""

    def __init__(
        self,
        settings: TeamsConnectorSettings,
        *,
        transport: Transport | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = TeamsGraphClient(settings, transport=transport)  # C2 fail-closed
        self._cursor_state: dict[str, str] = dict(cursor_state or {})

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self.pending_cursors: dict[str, str] = {}
        self.acl_quarantined = 0
        self.acl_quarantined_ids: list[str] = []

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        """Config-driven — NO Teams I/O."""
        return [
            SourceObject(source_id=f"team/{team_id}", kind="team", name=team_id)
            for team_id in self._settings.team_ids
        ]

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

    def _quarantine_object(self, object_id: str) -> None:
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(object_id)

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        async with self._client.session() as client:
            for team_id in self._settings.team_ids:
                await self._crawl_team(client, team_id)
        self._crawled = True

    async def _crawl_team(self, client: Client, team_id: str) -> None:
        for channel in await self._client.list_channels(client, team_id):
            raw_cid = channel.get("id")
            channel_id = str(raw_cid) if raw_cid not in (None, "") else ""
            resource = f"team/{team_id}/channel/{channel_id}"
            outcome = map_channel_acl(team_id, channel)
            if outcome.tainted or not outcome.principals:
                # Fail-closed: quarantine the channel; its messages are NEVER fetched.
                self._quarantine_object(resource)
                continue
            self._acl.append(AclEntry(resource_id=resource, principals=outcome.principals))
            await self._crawl_channel_messages(client, team_id, channel_id, resource)

    async def _crawl_channel_messages(
        self, client: Client, team_id: str, channel_id: str, resource: str
    ) -> None:
        cursor = self._cursor_state.get(f"delta:{team_id}/{channel_id}")
        try:
            messages, delta_link = await self._client.channel_messages_delta(
                client, team_id, channel_id, cursor
            )
        except DeltaTokenExpiredError:
            # Graph-documented resync: the stored deltaLink is stale (HTTP 410) →
            # re-baseline this channel with a full delta crawl (m365 parity).
            messages, delta_link = await self._client.channel_messages_delta(
                client, team_id, channel_id, None
            )
        for message in messages:
            await self._process_message(client, team_id, channel_id, resource, message)
        if delta_link is not None:
            self.pending_cursors[f"delta:{team_id}/{channel_id}"] = delta_link

    async def _process_message(
        self,
        client: Client,
        team_id: str,
        channel_id: str,
        resource: str,
        message: Mapping[str, Any],
    ) -> None:
        if message.get("deletedDateTime"):
            return  # deletion propagation deferred (sibling parity).
        if message.get("messageType") != "message":
            return  # system events / other message types are not content records.
        raw_id = message.get("id")
        if raw_id in (None, ""):
            raise InvalidRecordError("Teams channel message is missing its id")
        message_id = str(raw_id)
        self._records.append(
            Record(
                record_id=message_id,
                source_id=resource,
                payload=_message_payload(message, team_id, channel_id),
            )
        )
        if self._settings.include_replies and message.get("replyCount"):
            await self._process_replies(client, team_id, channel_id, resource, message_id)

    async def _process_replies(
        self, client: Client, team_id: str, channel_id: str, resource: str, message_id: str
    ) -> None:
        for reply in await self._client.message_replies(client, team_id, channel_id, message_id):
            if reply.get("deletedDateTime"):
                continue  # deletion propagation deferred (sibling parity).
            if reply.get("messageType") != "message":
                continue  # system events / other reply types are not content records.
            raw_rid = reply.get("id")
            if raw_rid in (None, ""):
                raise InvalidRecordError(f"Teams reply on message {message_id!r} is missing its id")
            payload = _message_payload(reply, team_id, channel_id)
            payload["reply_to_id"] = message_id
            self._records.append(
                Record(
                    record_id=f"reply-{raw_rid}",
                    source_id=resource,
                    payload=payload,
                )
            )


def _from_fields(message: Mapping[str, Any]) -> tuple[Any, Any]:
    frm = message.get("from")
    user = frm.get("user") if isinstance(frm, Mapping) else None
    if isinstance(user, Mapping):
        return user.get("id"), user.get("displayName")
    return None, None


def _message_payload(message: Mapping[str, Any], team_id: str, channel_id: str) -> dict[str, Any]:
    body = message.get("body")
    content = body.get("content") if isinstance(body, Mapping) else None
    content_type = body.get("contentType") if isinstance(body, Mapping) else None
    from_user_id, from_display_name = _from_fields(message)
    return {
        "id": str(message.get("id")),
        "team_id": team_id,
        "channel_id": channel_id,
        "from_user_id": from_user_id,
        "from_display_name": from_display_name,
        "subject": message.get("subject"),
        "body": content,
        "content_type": content_type,
        "created_at": message.get("createdDateTime"),
        "last_modified_at": message.get("lastModifiedDateTime"),
    }
