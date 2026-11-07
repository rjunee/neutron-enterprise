# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JiraConnector — the frozen Connector Protocol over Jira Cloud REST v3 / Agile 1.0 (NE-093).

Implements ``discover``/``backfill``/``incremental``/``permissions`` for a
config-driven set of projects. For each configured project ONE permission-scheme
crawl resolves the PROJECT-LEVEL ACL (:mod:`ne.connect.jira.acl`); each SURVIVING
(non-quarantined) project then has its issues (enhanced JQL search), comments,
and sprints (Agile board -> sprint) extracted, with an ``updated >=`` JQL cursor
added on an incremental run.

ONE crawl per site (site == one sync run), the crawl-once-cached shape shared
with the Salesforce/M365/SQL connectors: the crawl is lazy and cached,
``permissions()`` (called first by the ``SyncRunner``) triggers it, and
``backfill``/``incremental`` replay the cache. ``incremental(state)`` honors a
DIFFERING cursor basis by re-crawling.

Fail-closed / run-abort table (the unit's security core):

* a project whose permission-scheme outcome is ``tainted`` OR carries ZERO
  grant-derived principals is QUARANTINED — project-wide: NO issue/comment/sprint
  fetch, no ``Record``, no ``AclEntry`` — counted and surfaced by ``sync.py`` as
  one ``connector.acl_quarantine`` denied audit event, never world-readable;
* an issue carrying a non-null ``fields.security`` (an issue-security level,
  unresolvable in v1) is QUARANTINED PER-ITEM — never inherits the project ACL;
* a comment with a ``visibility`` restriction OR ``jsdPublic == False`` is
  QUARANTINED PER-ITEM (restricted-comment principals unresolvable in v1);
* the ``jira-project:<projectId>`` label is appended to a surviving project's
  principal tuple AFTER the quarantine decision, so it can never mask an
  unresolvable scheme;
* a permission-scheme / search / comment / board / sprint fetch failing, or
  retry exhaustion, ABORTS the whole run (``JiraSyncError``) — nothing committed;
* an issue/comment payload missing its id raises ``InvalidRecordError`` LOUD
  (Salesforce/SQL discipline).

v1 limitations (documented, see the package README + :mod:`ne.connect.jira.acl`):
project-browse-level ACL mirroring only (issue security levels and restricted
comments are quarantined per-item, not resolved); role-actor membership
resolution is the principal store's job; sprints have no ``updated`` cursor on
the Agile API and are re-fetched every run (unchanged sprints are dedup-skipped);
deletion propagation is deferred.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ne.connect.errors import InvalidRecordError, JiraSyncError
from ne.connect.jira.acl import PROJECT_PREFIX, map_permission_scheme
from ne.connect.jira.client import Client, JiraClient, Transport
from ne.connect.jira.config import JiraConnectorSettings, JiraProjectConfig
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["JiraConnector"]

_QUARANTINE_ID_CAP = 100
_CURSOR_SHAPE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def _updated_to_cursor(value: object) -> str | None:
    """Parse a Jira ``fields.updated`` timestamp to a ``'%Y-%m-%d %H:%M'`` UTC minute.

    JQL comparison has minute granularity, so the cursor is minute-truncated.
    Boundary re-reads (a ``>=`` window) are absorbed by the dedup ledger. A value
    that cannot be parsed yields ``None`` (no cursor advance rather than a crash —
    a stale cursor never loses data, the ledger absorbs the re-read).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M")


class JiraConnector:
    """Frozen ``Connector`` over a config-driven set of Jira projects."""

    def __init__(
        self,
        settings: JiraConnectorSettings,
        *,
        transport: Transport | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._client = JiraClient(settings, transport=transport)  # C2 fail-closed
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
        """Config-driven — NO Jira I/O."""
        return [
            SourceObject(source_id=f"project/{cfg.key}", kind="project", name=cfg.key)
            for cfg in self._settings.projects
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            await self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix("updated:"): str(value)
            for key, value in state.items()
            if key.startswith("updated:")
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

    def _quarantine_project(self, key: str) -> None:
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(key)

    def _quarantine_item(self, item_id: str) -> None:
        self.items_quarantined += 1
        if len(self.items_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.items_quarantined_ids.append(item_id)

    async def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        async with self._client.session() as client:
            for cfg in self._settings.projects:
                await self._crawl_project(client, cfg)
        self._crawled = True

    async def _crawl_project(self, client: Client, cfg: JiraProjectConfig) -> None:
        key = cfg.key
        project = await self._client.project(client, key)
        project_id = project.get("id")
        if project_id in (None, ""):
            raise InvalidRecordError(f"Jira project {key!r} response is missing its id")
        project_id = str(project_id)

        scheme = await self._client.project_permission_scheme(client, key)
        outcome = map_permission_scheme(scheme, project_id)
        if outcome.tainted or not outcome.principals:
            # Fail-closed: NO issue/comment/sprint fetch for a quarantined project.
            self._quarantine_project(key)
            return

        # jira-project:<id> appended AFTER the quarantine decision (spec-named).
        principals = (*outcome.principals, f"{PROJECT_PREFIX}{project_id}")
        for source_id in (
            f"project/{key}",
            f"issues/{key}",
            f"comments/{key}",
            f"sprints/{key}",
        ):
            self._acl.append(AclEntry(resource_id=source_id, principals=principals))

        # (d) project metadata record.
        self._records.append(
            Record(
                record_id=project_id,
                source_id=f"project/{key}",
                payload={
                    "key": key,
                    "id": project_id,
                    "name": project.get("name"),
                    "description": project.get("description"),
                },
            )
        )

        surviving_issue_keys = await self._crawl_issues(client, cfg, project_id)
        if cfg.include_comments:
            await self._crawl_comments(client, key, surviving_issue_keys)
        if cfg.include_sprints:
            await self._crawl_sprints(client, key)

    async def _crawl_issues(
        self, client: Client, cfg: JiraProjectConfig, project_id: str
    ) -> list[str]:
        key = cfg.key
        cursor = self._cursor_state.get(key)
        if cursor:
            _require_cursor_shape(cursor)
            jql = f'project = {key} AND updated >= "{cursor}" ORDER BY updated ASC'
        else:
            jql = f"project = {key} ORDER BY updated ASC"

        issues = await self._client.search_jql(client, jql, list(self._settings.issue_fields))
        surviving: list[str] = []
        max_updated: str | None = None
        for issue in issues:
            issue_key = issue.get("key")
            if not issue_key or not isinstance(issue_key, str):
                raise InvalidRecordError(f"Jira issue in project {key!r} is missing its key")
            fields = issue.get("fields")
            fields = fields if isinstance(fields, Mapping) else {}
            if fields.get("security") is not None:
                # Issue-security level unresolvable in v1 -> per-item quarantine.
                self._quarantine_item(issue_key)
                continue
            payload: dict[str, Any] = {
                **dict(fields),
                "key": issue_key,
                "id": str(issue.get("id", "")),
                "project_key": key,
            }
            self._records.append(
                Record(record_id=issue_key, source_id=f"issues/{key}", payload=payload)
            )
            surviving.append(issue_key)
            raw_updated = fields.get("updated")
            if isinstance(raw_updated, str) and (max_updated is None or raw_updated > max_updated):
                max_updated = raw_updated

        if max_updated is not None:
            cursor_value = _updated_to_cursor(max_updated)
            if cursor_value is not None:
                self.pending_cursors[key] = cursor_value
        return surviving

    async def _crawl_comments(self, client: Client, key: str, issue_keys: list[str]) -> None:
        for issue_key in issue_keys:
            for comment in await self._client.issue_comments(client, issue_key):
                cid = comment.get("id")
                if cid in (None, ""):
                    raise InvalidRecordError(
                        f"Jira comment on issue {issue_key!r} is missing its id"
                    )
                cid = str(cid)
                if comment.get("visibility") is not None or comment.get("jsdPublic") is False:
                    # Restricted-comment principals unresolvable in v1 -> quarantine.
                    self._quarantine_item(f"comment/{cid}")
                    continue
                self._records.append(
                    Record(
                        record_id=cid,
                        source_id=f"comments/{key}",
                        payload={
                            "id": cid,
                            "issue_key": issue_key,
                            "project_key": key,
                            "body": comment.get("body"),
                            "author": comment.get("author"),
                            "created": comment.get("created"),
                            "updated": comment.get("updated"),
                        },
                    )
                )

    async def _crawl_sprints(self, client: Client, key: str) -> None:
        seen: set[str] = set()
        for board in await self._client.boards(client, key):
            board_id = board.get("id")
            if not isinstance(board_id, int):
                continue
            for sprint in await self._client.sprints(client, board_id):
                sid = sprint.get("id")
                if sid in (None, ""):
                    raise InvalidRecordError(
                        f"Jira sprint on board {board_id} in project {key!r} is missing its id"
                    )
                sid = str(sid)
                if sid in seen:
                    continue  # a sprint may appear on multiple boards — dedupe.
                seen.add(sid)
                self._records.append(
                    Record(
                        record_id=sid,
                        source_id=f"sprints/{key}",
                        payload={
                            "id": sid,
                            "project_key": key,
                            "name": sprint.get("name"),
                            "goal": sprint.get("goal"),
                            "state": sprint.get("state"),
                            "startDate": sprint.get("startDate"),
                            "endDate": sprint.get("endDate"),
                            "boardId": board_id,
                        },
                    )
                )


def _require_cursor_shape(cursor: str) -> None:
    """Reject any cursor not shaped ``YYYY-MM-DD HH:MM`` before JQL interpolation."""
    if not _CURSOR_SHAPE_RE.match(cursor):
        raise JiraSyncError("refusing to interpolate a malformed Jira updated cursor into JQL")
