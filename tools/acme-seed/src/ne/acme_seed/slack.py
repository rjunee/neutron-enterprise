# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack seeder (NE-102) — 3 channels + fact-bearing messages, ACL = membership.

:class:`SeedSlackClient` SUBCLASSES the connector's
:class:`~ne.connect.slack.client.SlackClient`, adding a WRITE call with its OWN
method allowlist (``conversations.create`` / ``.invite`` / ``.archive`` /
``chat.postMessage`` / ``chat.delete`` / ``auth.test``) POSTed as JSON — the
shipped read-only client's ``_ALLOWED_METHODS`` is untouched, so the product's
Slack egress stays read-only. Reads (``conversations.list`` / ``.history``) ride
the inherited, already-allowlisted ``call``.

Channel model (ACL = channel membership, not message author, so bot-authored
fact messages are fine): ``#acme-general`` (public, all 4 personas),
``#eng-private`` (private, P2 only), ``#legal-confidential`` (private, P4 only).
The dept-sales doc + the named-P3 doc are posted NOWHERE — there is no sales
channel and posting them elsewhere would leak the fact across the ACL boundary
(recorded as a warning). Message idempotency gates on ``[acme:<fact_id>]``
markers already present in channel history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.acme_seed.manifest import SeedManifest
from ne.acme_seed.personas import PERSONAS, PrincipalsMap
from ne.acme_seed.slice import SliceDoc, SliceSelection
from ne.connect.errors import SlackSyncError
from ne.connect.slack.client import Client, SlackClient, Transport
from ne.connect.slack.config import SlackConnectorSettings

__all__ = ["CHANNELS", "SeedSlackClient", "SlackSeeder"]

CH_GENERAL = "acme-general"
CH_ENG = "eng-private"
CH_LEGAL = "legal-confidential"

#: (channel name, is_private, personas invited).
CHANNELS: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    (CH_GENERAL, False, ("per-0000", "per-0006", "per-0016", "per-0038")),
    (CH_ENG, True, ("per-0006",)),
    (CH_LEGAL, True, ("per-0038",)),
)

_FACTS_PER_DOC = 2
_INVITE_OK_ERRORS = frozenset({"already_in_channel"})
_DELETE_OK_ERRORS = frozenset({"message_not_found"})
_ARCHIVE_OK_ERRORS = frozenset({"already_archived"})


class SeedSlackClient(SlackClient):
    """Slack client + a JSON WRITE call gated by its OWN seed-method allowlist."""

    _SEED_METHODS = frozenset(
        {
            "auth.test",
            "conversations.create",
            "conversations.invite",
            "conversations.archive",
            "chat.postMessage",
            "chat.delete",
        }
    )
    _WRITE_RETRYABLE = frozenset({429, 503, 504})

    async def call_write(
        self, client: Client, method: str, json_body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST a Slack write ``method`` as JSON; retry / ok:false aware (mirror of call)."""
        if method not in self._SEED_METHODS:
            raise SlackSyncError(f"refusing to call disallowed Slack seed method {method!r}")
        settings = self._settings
        max_attempts = settings.max_attempts
        cap = settings.retry_after_cap_seconds
        token = settings.bot_token.get_secret_value()
        for attempt in range(1, max_attempts + 1):
            await self._general_throttle.acquire()
            resp = await client.post(
                f"{self._base}/{method}",
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            status = resp.status_code
            if status in self._WRITE_RETRYABLE:
                if attempt == max_attempts:
                    raise SlackSyncError(
                        f"Slack API {method} returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await self._general_throttle.wait_retry(
                    attempt, resp.headers.get("Retry-After"), cap
                )
                continue
            if status >= 400:
                raise SlackSyncError(f"Slack API {method} returned HTTP {status}", status=status)
            payload = resp.json()
            if payload.get("ok") is not True:
                code = str(payload.get("error") or "unknown_error")
                if code == "ratelimited" and attempt < max_attempts:
                    await self._general_throttle.wait_retry(
                        attempt, resp.headers.get("Retry-After"), cap
                    )
                    continue
                raise SlackSyncError(
                    f"Slack API {method} returned error {code!r}", status=200, error_code=code
                )
            return payload
        raise SlackSyncError("exhausted Slack retries")  # pragma: no cover


class SlackSeeder:
    """Create the 3 channels, invite personas, and post fact-bearing messages."""

    def __init__(
        self,
        settings: SlackConnectorSettings,
        principals: PrincipalsMap,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._principals = principals
        self._client = SeedSlackClient(settings, transport=transport)

    def _placement(self, doc: SliceDoc) -> str | None:
        """Which channel a doc's facts go to (None == deliberately posted nowhere)."""
        if doc.visibility == "public":
            return CH_GENERAL
        if doc.visibility == "exec_only":
            return CH_LEGAL
        if doc.visibility == "department":
            if "dept-engineering" in doc.allowed_groups:
                return CH_ENG
            if "dept-legal" in doc.allowed_groups:
                return CH_LEGAL
            return None  # dept-sales: no sales channel
        if doc.visibility == "named":
            personas = [p for p in doc.allowed_people if p in PERSONAS]
            if "per-0006" in personas:
                return CH_ENG
            if "per-0038" in personas:
                return CH_LEGAL
            return None  # named-P3 (sales): posting anywhere would leak the fact
        return None

    async def _ensure_channel(self, http: Client, name: str, is_private: bool) -> str:
        """Create a channel or, if the name is taken, resolve the existing id."""
        try:
            payload = await self._client.call_write(
                http, "conversations.create", {"name": name, "is_private": is_private}
            )
            return str(payload["channel"]["id"])
        except SlackSyncError as exc:
            if exc.error_code != "name_taken":
                raise
        channels = await self._client.paginate(
            http,
            "conversations.list",
            {"types": "public_channel,private_channel", "limit": "1000"},
            key="channels",
        )
        for channel in channels:
            if isinstance(channel, dict) and channel.get("name") == name:
                return str(channel["id"])
        raise SlackSyncError(f"channel {name!r} reported name_taken but was not found in the list")

    async def _existing_markers(self, http: Client, channel_id: str) -> set[str]:
        """The ``[acme:<fact_id>]`` markers already present in a channel's history."""
        messages = await self._client.paginate(
            http,
            "conversations.history",
            {"channel": channel_id, "limit": "200"},
            key="messages",
        )
        markers: set[str] = set()
        for message in messages:
            if isinstance(message, dict):
                markers |= _extract_markers(str(message.get("text", "")))
        return markers

    async def push(self, selection: SliceSelection) -> SeedManifest:
        manifest = SeedManifest(
            tenant="slack",
            seeded_at=datetime.now(UTC).isoformat(),
            corpus_version=selection.corpus_version,
            personas={p: self._principals.slack_user_id(p) for p in PERSONAS},
        )
        async with self._client.session() as http:
            channel_ids: dict[str, str] = {}
            # (1) channels + (2) invites.
            for name, is_private, personas in CHANNELS:
                cid = await self._ensure_channel(http, name, is_private)
                channel_ids[name] = cid
                manifest.channels[name] = cid
                user_ids: list[str] = []
                for persona in personas:
                    uid = self._principals.slack_user_id(persona)
                    if uid is None:
                        manifest.warnings.append(
                            f"no Slack user for persona {persona} "
                            f"({PERSONAS.get(persona, '?')}) — not invited to #{name}"
                        )
                    else:
                        user_ids.append(uid)
                if user_ids:
                    try:
                        await self._client.call_write(
                            http,
                            "conversations.invite",
                            {"channel": cid, "users": ",".join(user_ids)},
                        )
                    except SlackSyncError as exc:
                        if exc.error_code not in _INVITE_OK_ERRORS:
                            raise

            # (3) fact-bearing messages, gated on existing history markers.
            markers_cache: dict[str, set[str]] = {}
            for doc in selection.docs:
                channel_name = self._placement(doc)
                if channel_name is None:
                    manifest.warnings.append(
                        f"doc {doc.doc_id} ({doc.visibility}) posted to NO Slack channel "
                        "(no matching channel; avoids crossing the ACL boundary)"
                    )
                    continue
                cid = channel_ids[channel_name]
                if cid not in markers_cache:
                    markers_cache[cid] = await self._existing_markers(http, cid)
                markers = markers_cache[cid]
                for fact in doc.facts[:_FACTS_PER_DOC]:
                    if fact.fact_id in markers:
                        continue
                    posted = await self._client.call_write(
                        http,
                        "chat.postMessage",
                        {"channel": cid, "text": f"{fact.statement} [acme:{fact.fact_id}]"},
                    )
                    manifest.messages[fact.fact_id] = {
                        "channel": cid,
                        "ts": str(posted["ts"]),
                    }
                    markers.add(fact.fact_id)
        return manifest

    async def unseed(self, manifest_dir: Path) -> None:
        """Delete recorded bot messages, then archive channels (the free-tier asymmetry)."""
        manifest = SeedManifest.load(manifest_dir, "slack")
        async with self._client.session() as http:
            for entry in manifest.messages.values():
                try:
                    await self._client.call_write(
                        http,
                        "chat.delete",
                        {"channel": entry["channel"], "ts": entry["ts"]},
                    )
                except SlackSyncError as exc:
                    if exc.error_code not in _DELETE_OK_ERRORS:
                        raise
            for cid in manifest.channels.values():
                try:
                    await self._client.call_write(http, "conversations.archive", {"channel": cid})
                except SlackSyncError as exc:
                    if exc.error_code not in _ARCHIVE_OK_ERRORS:
                        raise
        SeedManifest.delete(manifest_dir, "slack")


def _extract_markers(text: str) -> set[str]:
    markers: set[str] = set()
    start = 0
    token = "[acme:"
    while True:
        i = text.find(token, start)
        if i == -1:
            break
        j = text.find("]", i)
        if j == -1:
            break
        markers.add(text[i + len(token) : j])
        start = j + 1
    return markers
