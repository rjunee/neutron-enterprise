# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for Teams connector records (NE-080).

``teams_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: the fail-closed
BACKSTOP. The connector quarantines any channel it cannot resolve a non-empty,
untainted ACL for, and the empty default means an unlabeled record reaching the
runner raises ``AclResolutionError`` and aborts the run LOUDLY rather than
defaulting open. ``principal_namespace='teams'`` names the NE-042 principal store
that resolves the ``teams-team:`` / ``teams-channel:`` labels.
"""

from __future__ import annotations

from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec
from ne.connect.teams.config import TeamsConnectorSettings

__all__ = ["teams_data_contract"]

_SCHEMA_DEF: dict[str, object] = {"type": "object"}


def teams_data_contract(settings: TeamsConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's records."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_SCHEMA_DEF,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace="teams",
        ),
    )
