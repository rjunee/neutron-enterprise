# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce connector (NE-047): connected-app OAuth, Bulk API 2.0 backfill +
``SystemModstamp``-cursor incremental, fail-closed OBJECT-LEVEL permission mirroring.

A thin-httpx Salesforce REST/Bulk API 2.0 connector (NO ``simple-salesforce``)
that backfills and incrementally syncs configured SObjects, resolving each
object's ``ObjectPermissions`` rows into fail-closed
:class:`~ne.contracts.integration.AclEntry` rows feeding NE-042/043 trimming.
Importing this subpackage never drags in dlt, keeping the C1 telemetry
chokepoint untouched.

**Honest v1 limitation**: Salesforce ACL mirroring here is OBJECT-LEVEL ONLY —
profiles/permission-sets with object ``Read``. Salesforce's org-wide defaults,
role hierarchy, sharing rules, and manual/Apex shares (all RECORD-level) are
NOT mirrored in v1 (see :mod:`ne.connect.salesforce.acl` and the package
README). An SObject whose object permissions cannot be resolved (or whose
crawled data taints via a forged reserved-namespace id) is quarantined
object-wide — never extracted, never world-readable.
"""

from __future__ import annotations

from ne.connect.errors import SalesforceSyncError
from ne.connect.salesforce.acl import PERMSET_PREFIX, PROFILE_PREFIX, map_object_permissions
from ne.connect.salesforce.authority import SalesforceAuthorityAdmission, SalesforceAuthorityFactory
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.salesforce.connector import SalesforceConnector
from ne.connect.salesforce.contract import salesforce_data_contract
from ne.connect.salesforce.sync import SalesforceSyncOutcome, run_salesforce_sync
from ne.connect.salesforce.write import SalesforceWriteClient

__all__ = [
    "PERMSET_PREFIX",
    "PROFILE_PREFIX",
    "SalesforceAuthorityAdmission",
    "SalesforceAuthorityFactory",
    "SalesforceConnector",
    "SalesforceConnectorSettings",
    "SalesforceObjectConfig",
    "SalesforceSyncError",
    "SalesforceSyncOutcome",
    "SalesforceWriteClient",
    "map_object_permissions",
    "run_salesforce_sync",
    "salesforce_data_contract",
]
