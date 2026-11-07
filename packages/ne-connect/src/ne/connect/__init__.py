# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Connector framework (NE-040): dlt-based, ODCS contracts, conformance kit.

Public surface for building source connectors. Connectors currently shipped:
Microsoft 365 / SharePoint (NE-044, :mod:`ne.connect.m365`), Slack (NE-045,
:mod:`ne.connect.slack`), SQL (NE-046, :mod:`ne.connect.sql`), Salesforce
(NE-047, :mod:`ne.connect.salesforce`), Jira (NE-093, :mod:`ne.connect.jira`),
and Zendesk (NE-093, :mod:`ne.connect.zendesk`). Importing ``ne.connect`` does
NOT drag in dlt: the dlt chokepoint lives in :mod:`ne.connect._dlt` and is
imported only by connectors that actually use dlt resources, keeping the
framework import cheap and the C1 telemetry-ordering explicit.

The NE-046 SQL connector (:mod:`ne.connect.sql`) DOES use dlt (via the ``_dlt``
seam) and is therefore deliberately NOT re-exported here — importing it would
drag ``dlt``/``sqlalchemy`` into every ``ne.connect`` import. Consumers of the
SQL connector import it explicitly: ``from ne.connect.sql import ...``. The Jira
and Zendesk connectors are likewise imported explicitly (``from ne.connect.jira
import ...`` / ``from ne.connect.zendesk import ...``) — Salesforce precedent, so
their thin-httpx clients are not pulled into every ``ne.connect`` import.
"""

from __future__ import annotations

from ne.connect.acl import AclMapper, build_acl_index
from ne.connect.bridge import (
    CONNECTOR_RECORD_SHAPE,
    SyncReport,
    SyncRunner,
    default_record_mapper,
)
from ne.connect.contracts import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
    PiiClass,
    m365_source_contract_identity,
)
from ne.connect.dlt_connector import DltResourceConnector, ResourceSpec
from ne.connect.endpoints import validate_source_url
from ne.connect.errors import (
    AclResolutionError,
    DeltaTokenExpiredError,
    GraphSyncError,
    InvalidRecordError,
    NeConnectError,
    SlackRateLimitedError,
    SlackSyncError,
    SqlSyncError,
)
from ne.connect.m365 import (
    PUBLIC_LINK,
    TENANT_ALL,
    M365ConnectorSettings,
    M365GraphConnector,
    M365SyncOutcome,
    m365_data_contract,
    run_m365_sync,
)
from ne.connect.slack import (
    SlackConnector,
    SlackConnectorSettings,
    SlackSyncOutcome,
    run_slack_sync,
    slack_data_contract,
)
from ne.connect.state import ConnectorStateStore

__version__ = "0.1.0"

__all__ = [
    "CONNECTOR_RECORD_SHAPE",
    "PUBLIC_LINK",
    "TENANT_ALL",
    "AclMapper",
    "AclResolutionError",
    "ConnectorStateStore",
    "DeltaTokenExpiredError",
    "DltResourceConnector",
    "GraphSyncError",
    "InvalidRecordError",
    "M365ConnectorSettings",
    "M365GraphConnector",
    "M365SyncOutcome",
    "NeConnectError",
    "OdcsDataContract",
    "PermissionMode",
    "PermissionModelSpec",
    "PiiClass",
    "ResourceSpec",
    "SlackConnector",
    "SlackConnectorSettings",
    "SlackRateLimitedError",
    "SlackSyncError",
    "SlackSyncOutcome",
    "SqlSyncError",
    "SyncReport",
    "SyncRunner",
    "__version__",
    "build_acl_index",
    "default_record_mapper",
    "m365_data_contract",
    "m365_source_contract_identity",
    "run_m365_sync",
    "run_slack_sync",
    "slack_data_contract",
    "validate_source_url",
]
