# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Microsoft Teams knowledge connector (NE-080): Graph app-only channel-message
delta sync, fail-closed team/channel ACL mirroring.

A thin-httpx Graph connector (NO msgraph-sdk / botbuilder — the NE-042/044
precedent) that delta-syncs channel messages (and replies) for the explicitly
scoped teams and resolves each channel's membership type into fail-closed
:class:`~ne.contracts.integration.AclEntry` rows feeding NE-042/043 trimming.
Importing this subpackage never drags in dlt (the teams modules never touch it),
keeping the C1 telemetry chokepoint untouched.

**Honest v1 limitation**: Teams ACL mirroring maps a STANDARD channel to its
owning team (``teams-team:<team>``) and a PRIVATE channel to a distinct
per-channel label (``teams-channel:<team>/<channel>``). SHARED channels
(cross-tenant membership) and any unknown ``membershipType`` are QUARANTINED
(fail-closed, never over-returned); 1:1 / group chats are out of v1 scope
(channel messages only). Membership-edge seeding (who belongs to
``teams-team:X`` / ``teams-channel:X/Y``) is principal-store-side (NE-042), like
the Zendesk connector. Imported explicitly
(``from ne.connect.teams import ...``) — NOT re-exported from ``ne.connect``
(the Salesforce/Zendesk precedent).
"""

from __future__ import annotations

from ne.connect.errors import DeltaTokenExpiredError, GraphSyncError, InvalidRecordError
from ne.connect.teams.acl import (
    CHANNEL_PREFIX,
    TEAM_PREFIX,
    AclOutcome,
    map_channel_acl,
)
from ne.connect.teams.config import TeamsConnectorSettings
from ne.connect.teams.connector import TeamsConnector
from ne.connect.teams.contract import teams_data_contract
from ne.connect.teams.sync import TeamsSyncOutcome, run_teams_sync

__all__ = [
    "CHANNEL_PREFIX",
    "TEAM_PREFIX",
    "AclOutcome",
    "DeltaTokenExpiredError",
    "GraphSyncError",
    "InvalidRecordError",
    "TeamsConnector",
    "TeamsConnectorSettings",
    "TeamsSyncOutcome",
    "map_channel_acl",
    "run_teams_sync",
    "teams_data_contract",
]
