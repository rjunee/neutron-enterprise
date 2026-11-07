# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ODCS data contract for Slack message records (NE-045).

``slack_data_contract`` builds the :class:`~ne.connect.contracts.OdcsDataContract`
the SyncRunner validates records against and the AclMapper resolves ACLs under.

Permission model = ``SOURCE_ACL`` with an EMPTY ``default_acl``: this is the
fail-closed BACKSTOP. The connector itself quarantines any channel it cannot
resolve (so a resolvable channel's messages always arrive with a crawled
``AclEntry``), and the empty default means that if an unlabeled record ever
somehow reached the runner, the AclMapper raises ``AclResolutionError`` and
aborts the run LOUDLY rather than defaulting the item open.
``principal_namespace='slack'`` names raw Slack user ids (``U…``) plus the
reserved ``acl:tenant-all`` label.
"""

from __future__ import annotations

from ne.connect.contracts import (
    OdcsDataContract,
    PermissionMode,
    PermissionModelSpec,
)
from ne.connect.slack.config import SlackConnectorSettings

__all__ = ["slack_data_contract"]

#: Slack message record schema (v1 syncs text + metadata; files/attachments are
#: a documented deferral). Required keys are always present on a kept message.
_SCHEMA_DEF = {
    "type": "object",
    "required": ["ts", "channel", "text"],
    "properties": {
        "ts": {"type": "string"},
        "channel": {"type": "string"},
        "text": {"type": "string"},
        "user": {"type": "string"},
        "user_name": {"type": "string"},
        "thread_ts": {"type": "string"},
        "subtype": {"type": "string"},
        "edited_ts": {"type": "string"},
    },
}


def slack_data_contract(settings: SlackConnectorSettings) -> OdcsDataContract:
    """Return the ODCS contract for this connector's Slack message records."""
    return OdcsDataContract(
        source=settings.source_name,
        owner=settings.owner,
        schema_def=_SCHEMA_DEF,
        freshness_sla=settings.freshness_sla,
        pii=settings.pii,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL,
            default_acl=(),
            principal_namespace="slack",
        ),
    )
