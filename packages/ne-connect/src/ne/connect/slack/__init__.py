# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack connector (NE-045): customer-internal manifest app, backfill+incremental,
fail-closed channel ACL.

A thin-httpx Slack Web API connector (NO ``slack_sdk``) that backfills and
incrementally syncs channel messages, resolving each channel's
membership/type into fail-closed :class:`~ne.contracts.integration.AclEntry`
rows feeding NE-042/043 trimming. Ships as a customer-internal app manifest
(``manifest.json``) — the post-2026-03 Slack rate-limit regime exempts
customer-internal apps from the aggressive non-Marketplace-distributed caps.
Importing this subpackage does NOT import dlt, keeping the C1 telemetry
chokepoint untouched.
"""

from __future__ import annotations

from ne.connect.errors import SlackRateLimitedError, SlackSyncError
from ne.connect.slack.acl import TENANT_ALL
from ne.connect.slack.config import SlackConnectorSettings
from ne.connect.slack.connector import SlackConnector
from ne.connect.slack.contract import slack_data_contract
from ne.connect.slack.manifest import load_app_manifest
from ne.connect.slack.sync import SlackSyncOutcome, run_slack_sync

__all__ = [
    "TENANT_ALL",
    "SlackConnector",
    "SlackConnectorSettings",
    "SlackRateLimitedError",
    "SlackSyncError",
    "SlackSyncOutcome",
    "load_app_manifest",
    "run_slack_sync",
    "slack_data_contract",
]
