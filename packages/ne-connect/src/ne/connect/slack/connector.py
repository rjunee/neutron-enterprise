# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackConnector — the frozen Connector Protocol over the Slack Web API (NE-045).

Implements ``discover`` / ``backfill`` / ``incremental`` / ``permissions`` for
Slack channel messages: a ``conversations.list`` enumeration, a per-channel
fail-closed ACL crawl through :mod:`ne.connect.slack.acl`, and a
``conversations.history``/``conversations.replies`` backfill with per-channel
``ts`` cursor capture for resumable incremental sync.

ONE crawl per instance (instance == one sync run): the crawl is lazy and
cached. The SyncRunner calls ``permissions()`` first, which triggers the crawl;
``backfill``/``incremental`` then replay the cached records. ``incremental(state)``
honors a DIFFERING cursor basis by re-crawling — so the conformance kit's
incremental-subset test exercises real cursor semantics while the SyncRunner
(which hands back the same ledger basis the crawl already used) reuses the
cache.

Fail-closed ACL (the unit's security core): quarantine (NO record, NO AclEntry,
counted and — by the sync helper — audited, never emitted world-readable)
covers a ``tainted``/zero-principal mapping outcome, an externally-shared
(Slack Connect) channel, an unresolvable channel type, AND a non-retryable
per-channel members-fetch failure. Server-side faults (all 5xx) still ABORT the
run so no cursor is committed on a transient/server fault — C7
quarantine-and-continue for per-item permanent failures, run-abort for
run-level faults.

Rate-limit exhaustion (NE-CLEAN3 item 2, superseding NE-CLEAN1's prior
run-abort policy): a per-channel Slack call (members-fetch or history/replies)
that exhausts the client's retry budget — HTTP 429, or the ``ok:false
error:'ratelimited'`` envelope at any HTTP status — raises
:class:`~ne.connect.errors.SlackRateLimitedError`. This is caught around the
per-channel crawl loop (NOT around ``conversations.list``/``users.list``,
which are run-level calls whose rate-limit exhaustion still aborts the whole
run) and treated as a per-channel RETRYABLE SKIP: any records/ACL entry
already produced for that channel this run are rolled back, its cursor is left
untouched, and the crawl continues with the next channel — it is counted in
``rate_limited``/``rate_limited_ids``, NEVER in ``acl_quarantined``/
``acl_quarantined_ids`` (a rate-limited channel is not a permanent ACL
failure; it is retried, in full, on the next run). A run with rate-limited
channels still commits the OTHER channels' cursors (durability contract,
:mod:`ne.connect.slack.sync`).

``users.list`` enrichment is NOT security-bearing: a degraded/failed fetch
drops ``user_name`` provenance but never quarantines a channel.

Content note: v1 syncs message TEXT + METADATA only. Files/attachments/canvases
are never stored; 1:1 DMs are excluded by default (no ``im`` scope in the
shipped manifest); Slack Connect ext-shared channels are quarantined — all
documented v1 scope cuts (see the README).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ne.connect.errors import SlackRateLimitedError, SlackSyncError
from ne.connect.slack.acl import map_channel_acl
from ne.connect.slack.client import Client, SlackClient, Transport
from ne.connect.slack.config import SlackConnectorSettings
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["SlackConnector"]

_QUARANTINE_ID_CAP = 100

#: Subtype messages that are channel-system noise, never content records.
_SKIP_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "bot_add",
        "bot_remove",
        "channel_archive",
        "channel_unarchive",
    }
)


def _is_member_list_type(channel: Mapping[str, object]) -> bool:
    return bool(channel.get("is_private") or channel.get("is_mpim") or channel.get("is_im"))


def _is_permanent_failure(exc: SlackSyncError) -> bool:
    """True for a per-item PERMANENT failure (a non-429 4xx, or a non-ratelimited
    ok:false 200).

    False for a 5xx fault (which re-raises out of ``_crawl_channel`` and aborts
    the run — a server-side fault, not a per-item ACL failure) or a Slack
    rate-limit exhaustion (``status is None`` guard failures also fall here as
    a defensive default, though the guard never raises mid-crawl). Rate-limit
    exhaustion (``SlackRateLimitedError`` — HTTP 429 exhaustion, or the
    ``ok:false error:'ratelimited'`` envelope some proxies emit on HTTP 200)
    must NEVER permanently ACL-quarantine a channel: this function returning
    False makes ``_crawl_channel`` re-raise it, and the caller
    (``_ensure_crawled``'s per-channel loop) catches it specifically and
    converts it into a per-channel retryable SKIP (NE-CLEAN3 item 2) — never
    an ``acl_quarantined`` entry.
    """
    if exc.error_code == "ratelimited":
        return False
    if exc.status is None:
        return False
    if exc.status == 200:
        return True
    return 400 <= exc.status < 500 and exc.status != 429


class SlackConnector:
    """Slack channel-message connector with a fail-closed per-channel ACL crawl."""

    def __init__(
        self,
        settings: SlackConnectorSettings,
        *,
        transport: Transport | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = SlackClient(settings, transport=transport)  # C2 fail-closed at construction
        self._cursor_state: dict[str, str] = dict(cursor_state or {})

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self._channels: tuple[dict[str, object], ...] = ()
        self.pending_cursors: dict[str, str] = {}
        self.acl_quarantined = 0
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids: list[str] = []
        self.rate_limited = 0
        self.rate_limited_ids: list[str] = []

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        await self._ensure_crawled()
        return [
            SourceObject(
                source_id=str(ch["id"]), kind="channel", name=str(ch.get("name") or ch["id"])
            )
            for ch in self._channels
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix("ts:"): str(value)
            for key, value in state.items()
            if key.startswith("ts:")
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
        self._channels = ()
        self.pending_cursors = {}
        self.acl_quarantined = 0
        self.acl_fetch_failed = 0
        self.acl_quarantined_ids = []
        self.rate_limited = 0
        self.rate_limited_ids = []

    def _quarantine(self, source_id: str) -> None:
        """Fail-closed quarantine bookkeeping: count + (capped) capture the id."""
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(source_id)

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        settings = self._settings
        async with self._client.session() as client:
            channels = await self._client.paginate(
                client,
                "conversations.list",
                {
                    "types": ",".join(settings.conversation_types),
                    "exclude_archived": "false" if settings.include_archived else "true",
                    "limit": str(settings.page_size),
                },
                key="channels",
            )
            if settings.channel_ids:
                allowed = frozenset(settings.channel_ids)
                channels = [ch for ch in channels if str(ch.get("id")) in allowed]
            self._channels = tuple(channels)

            users_map = await self._crawl_users(client)

            # NOTE: ``_crawl_channel`` only ever APPENDS to ``self._records`` /
            # ``self._acl`` (never mutates/removes) — the rollback below relies
            # on that invariant to truncate exactly this channel's partial
            # output via a length snapshot. If that ever changes, this
            # rollback silently breaks.
            for channel in self._channels:
                cid = str(channel["id"])
                records_mark = len(self._records)
                acl_mark = len(self._acl)
                try:
                    await self._crawl_channel(client, channel, users_map)
                except SlackRateLimitedError:
                    # Per-channel retryable skip (NE-CLEAN3 item 2): roll back any
                    # partial output from this channel, leave its cursor untouched
                    # (never advanced, never a stale acl_quarantine), and continue
                    # crawling the remaining channels — the run still completes.
                    del self._records[records_mark:]
                    del self._acl[acl_mark:]
                    self.pending_cursors.pop(cid, None)
                    self.rate_limited += 1
                    if len(self.rate_limited_ids) < _QUARANTINE_ID_CAP:
                        self.rate_limited_ids.append(cid)
                    continue
        self._crawled = True

    async def _crawl_users(self, client: Client) -> dict[str, str]:
        """Best-effort users.list provenance map — NEVER security-bearing."""
        settings = self._settings
        try:
            users = await self._client.paginate(
                client, "users.list", {"limit": str(settings.page_size)}, key="members"
            )
        except SlackSyncError as exc:
            if _is_permanent_failure(exc):
                return {}  # provenance enrichment only — degrade, never abort/quarantine
            raise
        return {str(u["id"]): str(u.get("real_name") or u.get("name") or "") for u in users}

    async def _crawl_channel(
        self, client: Client, channel: Mapping[str, object], users_map: Mapping[str, str]
    ) -> None:
        cid = str(channel["id"])
        settings = self._settings

        # -- ACL FIRST: fail-closed, before any message is ever emitted ------
        members: list[str] | None = None
        if _is_member_list_type(channel):
            try:
                members = await self._client.paginate(
                    client,
                    "conversations.members",
                    {"channel": cid, "limit": str(settings.page_size)},
                    key="members",
                )
            except SlackSyncError as exc:
                if _is_permanent_failure(exc):
                    self.acl_fetch_failed += 1
                    self._quarantine(cid)
                    return
                raise  # 429-exhaustion / 5xx: abort the run so no cursor commits

        outcome = map_channel_acl(channel, members)
        if outcome.tainted or not outcome.principals:
            self._quarantine(cid)
            return  # fail-closed: no record, no AclEntry, never world-readable
        self._acl.append(AclEntry(resource_id=cid, principals=outcome.principals))

        # -- HISTORY ----------------------------------------------------------
        params: dict[str, str] = {"channel": cid, "limit": str(settings.history_page_size)}
        cursor = self._cursor_state.get(cid)
        if cursor:
            params["oldest"] = cursor
            params["inclusive"] = "false"
        messages = await self._client.paginate(
            client, "conversations.history", params, key="messages"
        )

        max_ts: str | None = None
        for msg in messages:
            if msg.get("subtype") in _SKIP_SUBTYPES:
                continue
            self._emit(cid, msg, users_map)
            max_ts = _max_ts(max_ts, str(msg.get("ts") or ""))

            ts = str(msg.get("ts") or "")
            thread_ts = msg.get("thread_ts")
            is_thread_parent = int(msg.get("reply_count") or 0) > 0 and (
                thread_ts is None or str(thread_ts) == ts
            )
            if is_thread_parent:
                replies = await self._client.paginate(
                    client,
                    "conversations.replies",
                    {"channel": cid, "ts": ts, "limit": str(settings.history_page_size)},
                    key="messages",
                )
                for reply in replies:
                    reply_ts = str(reply.get("ts") or "")
                    if reply_ts == ts:
                        continue  # the parent is echoed first — dedupe it
                    self._emit(cid, reply, users_map)
                    max_ts = _max_ts(max_ts, reply_ts)

        if max_ts is not None:
            existing = self.pending_cursors.get(cid)
            if existing is None or max_ts >= existing:
                self.pending_cursors[cid] = max_ts

    def _emit(self, cid: str, msg: Mapping[str, object], users_map: Mapping[str, str]) -> None:
        self._records.append(
            Record(
                record_id=str(msg.get("ts") or ""),
                source_id=cid,
                payload=_payload(msg, cid, users_map),
            )
        )


def _max_ts(current: str | None, candidate: str) -> str:
    """Lexicographic max of two Slack ``ts`` strings (fixed-width, monotonic)."""
    if not candidate:
        return current if current is not None else ""
    if current is None or candidate > current:
        return candidate
    return current


def _payload(msg: Mapping[str, object], cid: str, users_map: Mapping[str, str]) -> dict[str, Any]:
    """Slack message payload — text + metadata only, never files/attachments/blobs."""
    payload: dict[str, Any] = {
        "ts": str(msg.get("ts") or ""),
        "channel": cid,
        "text": str(msg.get("text") or ""),
    }
    user = msg.get("user")
    if isinstance(user, str) and user:
        payload["user"] = user
        user_name = users_map.get(user)
        if user_name:
            payload["user_name"] = user_name
    thread_ts = msg.get("thread_ts")
    if thread_ts is not None and str(thread_ts) != payload["ts"]:
        payload["thread_ts"] = str(thread_ts)
    subtype = msg.get("subtype")
    if isinstance(subtype, str) and subtype:
        payload["subtype"] = subtype
    edited = msg.get("edited")
    if isinstance(edited, Mapping):
        edited_ts = edited.get("ts")
        if edited_ts is not None:
            payload["edited_ts"] = str(edited_ts)
    return payload
