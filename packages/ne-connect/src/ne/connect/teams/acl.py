# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map a Teams channel's membershipType to frozen ACL principals (NE-080) — pure.

This is the SECURITY CORE of the connector: it turns a channel's
``membershipType`` into the principal set stamped onto every message/reply
record for that channel.

Decided rules:

* ``membershipType == 'standard'`` -> the owning-team label
  ``teams-team:<team_id>`` (a standard channel is visible to every team member);
* ``membershipType == 'private'`` -> the distinct per-channel label
  ``teams-channel:<team_id>/<channel_id>`` (only the private channel's roster);
* ``membershipType == 'shared'`` -> QUARANTINE (empty principals + a dropped
  reason, ``tainted=False``): cross-tenant shared-channel membership is out of v1
  scope — the Slack-Connect exclusion precedent;
* ANY OTHER / MISSING ``membershipType`` -> QUARANTINE (fail-closed): an
  unresolvable visibility is never over-returned.

Fail-closed forged-label guard (NE-CKD2): a raw ``team_id`` / ``channel_id``
starting with a reserved principal namespace (``acl:`` / ``role:``) TAINTS the
outcome so the caller quarantines rather than admitting a smuggled label. This
mapper NEVER mints ``acl:tenant-all`` — a Teams channel is never tenant-wide.

Membership-EDGE seeding (which principals belong to ``teams-team:X`` /
``teams-channel:X/Y``) is principal-store-side (NE-042), exactly like
``ne.connect.zendesk.acl``'s documented note — the connector only stamps the
LABEL a chunk requires; the store resolves who holds it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CHANNEL_PREFIX",
    "TEAM_PREFIX",
    "AclOutcome",
    "map_channel_acl",
]

TEAM_PREFIX = "teams-team:"
CHANNEL_PREFIX = "teams-channel:"

_RESERVED_PREFIXES = ("acl:", "role:")


@dataclass(frozen=True)
class AclOutcome:
    """The resolved ACL for one Teams channel."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(raw_id: str) -> bool:
    return raw_id.startswith(_RESERVED_PREFIXES)


def map_channel_acl(team_id: str, channel: Mapping[str, object]) -> AclOutcome:
    """Map one Teams channel's ``membershipType`` to an :class:`AclOutcome`.

    The caller quarantines when the outcome is ``tainted`` OR carries zero
    principals (an unresolvable/shared channel is never over-returned, and its
    messages are never fetched).
    """
    channel_id = str(channel.get("id") or "")
    # Forged-label guard (NE-CKD2): a raw team/channel id impersonating a reserved
    # principal namespace taints the outcome so the caller quarantines.
    if _is_reserved(team_id) or _is_reserved(channel_id):
        return AclOutcome((), ("reserved-prefix id (forged-label guard, NE-CKD2)",), True)

    membership = channel.get("membershipType")
    if membership == "standard":
        return AclOutcome((f"{TEAM_PREFIX}{team_id}",), (), False)
    if membership == "private":
        return AclOutcome((f"{CHANNEL_PREFIX}{team_id}/{channel_id}",), (), False)
    if membership == "shared":
        return AclOutcome(
            (), ("shared channel (cross-tenant membership) is out of v1 scope",), False
        )
    return AclOutcome((), (f"unsupported membershipType {membership!r} (fail-closed)",), False)
