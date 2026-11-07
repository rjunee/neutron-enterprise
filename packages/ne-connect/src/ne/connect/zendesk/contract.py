# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for Zendesk connector records (NE-093).

``zendesk_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: the fail-closed
BACKSTOP. The connector quarantines any ticket/article/comment it cannot resolve
a non-empty, untainted ACL for, and the empty default means an unlabeled record
reaching the runner raises ``AclResolutionError`` and aborts the run LOUDLY
rather than defaulting open. ``principal_namespace='zendesk'`` names the NE-042
principal store that resolves the ``zd-org:`` / ``zd-group:`` / ``zd-agent:`` /
``zd-user:`` / ``zd-segment:`` labels.
"""

from __future__ import annotations

from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec
from ne.connect.zendesk.config import ZendeskConnectorSettings

__all__ = ["zendesk_data_contract"]

_SCHEMA_DEF: dict[str, object] = {"type": "object"}


def zendesk_data_contract(settings: ZendeskConnectorSettings) -> OdcsDataContract:
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
            principal_namespace="zendesk",
        ),
    )
