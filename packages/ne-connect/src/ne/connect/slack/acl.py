# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map Slack channel/member payloads to frozen ACL principals (NE-045) — pure, no I/O.

This is the SECURITY CORE of the connector: it turns a channel's type flags
and (when applicable) its member list into the principal set stamped onto the
chunk's :class:`~ne.contracts.integration.AclEntry`.

Fail-closed, over EVERY branch:

* an externally-shared channel (Slack Connect: ``is_ext_shared`` / ``is_shared``
  / ``is_pending_ext_shared``) is OUT OF v1 SCOPE — its membership spans
  organizations this deployment cannot enumerate, so it is never mapped, only
  flagged for the caller to quarantine;
* a genuine PUBLIC channel (``is_channel`` and not ``is_private``) maps to
  :data:`TENANT_ALL` — the reserved workspace-wide label. Members are NOT
  consulted for a public channel (the whole point of the label);
* a MEMBER-LIST channel (private channel / mpim / im) maps to the deduped
  member-id tuple. A SOURCE-provided member id in a reserved namespace
  (``acl:`` / ``role:`` — NE-CKD2) taints the whole outcome: a source that
  forges ``acl:tenant-all`` as a "member" would over-grant to every
  store-known principal at trim time, so the channel is quarantined rather
  than trusted. An empty (or unfetched — ``None``) member list resolves to
  empty principals, which the caller also quarantines;
* a channel whose type cannot be determined from ANY flag is quarantined.

:data:`TENANT_ALL` is emitted ONLY by this mapper for a genuine public channel —
never lifted from a source id (that path taints). The constant is byte-identical
to ``ne.permissions.models`` (a dev-dep test locks the equality), avoiding a
runtime ne-connect -> ne-permissions edge — the same package-local pattern
``ne.connect.m365.acl`` uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["TENANT_ALL", "AclOutcome", "map_channel_acl"]

TENANT_ALL = "acl:tenant-all"

_RESERVED_PREFIXES = ("acl:", "role:")


@dataclass(frozen=True)
class AclOutcome:
    """The resolved ACL for one Slack channel."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(principal_id: str) -> bool:
    return principal_id.startswith(_RESERVED_PREFIXES)


def map_channel_acl(channel: Mapping[str, object], members: Sequence[str] | None) -> AclOutcome:
    """Map one Slack channel's type/membership to an :class:`AclOutcome`.

    ``members`` is ``None`` when the member-list has not been (or could not be)
    fetched; the caller quarantines on both a ``None`` and an empty member list.
    """
    ext_shared = (
        channel.get("is_ext_shared")
        or channel.get("is_shared")
        or channel.get("is_pending_ext_shared")
    )
    if ext_shared:
        return AclOutcome(
            (), ("externally shared channel (Slack Connect) is out of v1 scope",), False
        )

    is_public = bool(channel.get("is_channel")) and not channel.get("is_private")
    if is_public:
        return AclOutcome((TENANT_ALL,), (), False)

    is_member_list = bool(
        channel.get("is_private") or channel.get("is_mpim") or channel.get("is_im")
    )
    if is_member_list:
        if not members:
            return AclOutcome((), (), False)
        principals: list[str] = []
        tainted = False
        for member_id in members:
            if _is_reserved(member_id):
                tainted = True  # source forged a reserved-namespace id; NOT admitted
            else:
                principals.append(member_id)
        # Order-preserving dedupe. The caller quarantines whenever tainted is
        # True OR principals is empty, regardless of what survives here.
        return AclOutcome(tuple(dict.fromkeys(principals)), (), tainted)

    return AclOutcome((), ("channel type unresolvable",), False)
