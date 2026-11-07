# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365 / SharePoint connector: Graph delta, ACL crawl, Docling file bodies.

A thin-httpx Graph connector (NO msgraph-sdk — NE-042 precedent) that delta-syncs
driveItem metadata plus supported file bodies and resolves permissions into fail-closed
:class:`~ne.contracts.integration.AclEntry` rows feeding NE-042/043 trimming.
Importing this subpackage does NOT import dlt (the m365 modules never touch it),
keeping the C1 telemetry chokepoint untouched.
"""

from __future__ import annotations

from ne.connect.errors import DeltaTokenExpiredError, GraphSyncError
from ne.connect.m365.acl import PUBLIC_LINK, TENANT_ALL
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.contract import (
    M365_SOURCE_KIND,
    m365_admitted_target_digest,
    m365_contract_identity,
    m365_contract_sla_seconds,
    m365_data_contract,
    m365_sweep_plan,
)
from ne.connect.m365.sync import M365SyncOutcome, run_m365_sync
from ne.connect.m365.write import GraphWriteClient

__all__ = [
    "M365_SOURCE_KIND",
    "PUBLIC_LINK",
    "TENANT_ALL",
    "DeltaTokenExpiredError",
    "GraphSyncError",
    "GraphWriteClient",
    "M365ConnectorSettings",
    "M365GraphConnector",
    "M365SyncOutcome",
    "m365_admitted_target_digest",
    "m365_contract_identity",
    "m365_contract_sla_seconds",
    "m365_data_contract",
    "m365_sweep_plan",
    "run_m365_sync",
]
