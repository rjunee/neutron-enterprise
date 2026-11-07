# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for Jira connector records (NE-093).

``jira_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: this is the
fail-closed BACKSTOP. The connector itself quarantines any project it cannot
resolve a non-empty, untainted PROJECT-LEVEL ACL for (so a record that does
reach the runner always carries a crawled ``AclEntry``), and the empty default
means an unlabeled record reaching the runner raises ``AclResolutionError`` and
aborts the run LOUDLY rather than defaulting open.

``principal_namespace='jira'`` names which principal store resolves the
``jira-group:`` / ``jira-role:<projectId>:<roleId>`` / ``jira-user:`` /
``jira-project:<projectId>`` labels the mapper emits — the NE-042 principal store.
"""

from __future__ import annotations

from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec
from ne.connect.jira.config import JiraConnectorSettings

__all__ = ["jira_data_contract"]

#: The record payload is the connector's flattened field/metadata shape;
#: per-record integrity is the connector's job, so the schema is permissive
#: (mirroring the Salesforce/SQL contracts).
_SCHEMA_DEF: dict[str, object] = {"type": "object"}


def jira_data_contract(settings: JiraConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's configured projects."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_SCHEMA_DEF,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace="jira",
        ),
    )
