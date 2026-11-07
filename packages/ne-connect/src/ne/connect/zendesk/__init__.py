# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Zendesk connector (NE-093): Basic-auth REST, cursor-based incremental exports,
fail-closed org/group/agent/user + Help Center user-segment permission mirroring.

A thin-httpx Zendesk connector (NO ``zenpy``, NO dlt import — the build-vs-adopt
call in the package NOTICE) that backfills and incrementally syncs tickets (with
comments) and Help Center articles, resolving each ticket/article/comment into
fail-closed :class:`~ne.contracts.integration.AclEntry` rows feeding NE-042/043
trimming. Importing this subpackage never drags in dlt, keeping the C1 telemetry
chokepoint untouched.

**Honest v1 limitation**: Zendesk ACL mirroring mirrors requester/assignee/group/
org ticket visibility (``zd-user:`` / ``zd-agent:`` / ``zd-group:`` / ``zd-org:``)
and Help Center user segments (``zd-segment:`` / ``acl:tenant-all`` for a null
segment). A PRIVATE comment is narrowed to the STAFF SUBSET; agent ticket-access
policies, custom roles, and brand restrictions are NOT mirrored; a principal-less
ticket, an empty-staff-subset private comment, and a draft article are quarantined
— never world-readable. Imported explicitly (``from ne.connect.zendesk import ...``)
— NOT re-exported from ``ne.connect`` (Salesforce precedent).
"""

from __future__ import annotations

from ne.connect.errors import ZendeskSyncError
from ne.connect.zendesk.acl import (
    AGENT_PREFIX,
    GROUP_PREFIX,
    ORG_PREFIX,
    SEGMENT_PREFIX,
    TENANT_ALL,
    USER_PREFIX,
    map_article_acl,
    map_comment_acl,
    map_ticket_acl,
)
from ne.connect.zendesk.config import ZendeskConnectorSettings
from ne.connect.zendesk.connector import ZendeskConnector
from ne.connect.zendesk.contract import zendesk_data_contract
from ne.connect.zendesk.sync import (
    ZendeskSyncOutcome,
    make_zendesk_record_mapper,
    run_zendesk_sync,
)

__all__ = [
    "AGENT_PREFIX",
    "GROUP_PREFIX",
    "ORG_PREFIX",
    "SEGMENT_PREFIX",
    "TENANT_ALL",
    "USER_PREFIX",
    "ZendeskConnector",
    "ZendeskConnectorSettings",
    "ZendeskSyncError",
    "ZendeskSyncOutcome",
    "make_zendesk_record_mapper",
    "map_article_acl",
    "map_comment_acl",
    "map_ticket_acl",
    "run_zendesk_sync",
    "zendesk_data_contract",
]
