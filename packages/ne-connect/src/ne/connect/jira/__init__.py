# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Jira Cloud connector (NE-093): Basic-auth REST v3 + Agile 1.0, enhanced-JQL
incremental, fail-closed PROJECT-BROWSE-LEVEL permission mirroring.

A thin-httpx Jira connector (NO ``atlassian-python-api``, NO dlt import — the
build-vs-adopt call in the package NOTICE) that backfills and incrementally syncs
configured projects' issues, comments, and sprints, resolving each project's
permission scheme into fail-closed :class:`~ne.contracts.integration.AclEntry`
rows feeding NE-042/043 trimming. Importing this subpackage never drags in dlt,
keeping the C1 telemetry chokepoint untouched.

**Honest v1 limitation**: Jira ACL mirroring here is PROJECT-BROWSE-LEVEL ONLY —
``BROWSE_PROJECTS`` grants mapped to ``jira-group:`` / ``jira-role:`` /
``jira-user:`` labels plus a per-project ``jira-project:<id>`` label. Jira's
issue-security levels and visibility-restricted comments (both narrower than the
project browse grant) are QUARANTINED per-item rather than resolved; role-actor
membership resolution is the principal store's job (NE-042). A project whose
permission scheme cannot be resolved (zero browse grants, or a forged
reserved-namespace id) is quarantined project-wide — never world-readable. This
subpackage is imported explicitly (``from ne.connect.jira import ...``) — it is
NOT re-exported from ``ne.connect`` (Salesforce precedent).
"""

from __future__ import annotations

from ne.connect.errors import JiraSyncError
from ne.connect.jira.acl import (
    GROUP_PREFIX,
    PROJECT_PREFIX,
    ROLE_PREFIX,
    TENANT_ALL,
    USER_PREFIX,
    map_permission_scheme,
)
from ne.connect.jira.adf import adf_to_text
from ne.connect.jira.config import JiraConnectorSettings, JiraProjectConfig
from ne.connect.jira.connector import JiraConnector
from ne.connect.jira.contract import jira_data_contract
from ne.connect.jira.sync import JiraSyncOutcome, make_jira_record_mapper, run_jira_sync

__all__ = [
    "GROUP_PREFIX",
    "PROJECT_PREFIX",
    "ROLE_PREFIX",
    "TENANT_ALL",
    "USER_PREFIX",
    "JiraConnector",
    "JiraConnectorSettings",
    "JiraProjectConfig",
    "JiraSyncError",
    "JiraSyncOutcome",
    "adf_to_text",
    "jira_data_contract",
    "make_jira_record_mapper",
    "map_permission_scheme",
    "run_jira_sync",
]
