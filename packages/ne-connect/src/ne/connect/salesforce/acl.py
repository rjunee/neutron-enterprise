# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map Salesforce ObjectPermissions rows to frozen ACL principals (NE-047) — pure, no I/O.

This is the SECURITY CORE of the connector: it turns a crawled
``ObjectPermissions`` result set into the OBJECT-LEVEL principal set stamped
onto every record's :class:`~ne.contracts.integration.AclEntry` for that
SObject.

**Salesforce ACL mirroring is OBJECT-LEVEL ONLY in v1.** Salesforce's real
sharing model composes organization-wide defaults (OWD), the role hierarchy,
profile/permission-set object- and field-level permissions, and manual/Apex
sharing rules into a per-RECORD visibility decision. None of the record-level
machinery (OWD, role hierarchy, sharing rules, manual shares) is mirrored here
— only "can a profile/permission-set read this SObject at all" is. Concretely:
a record that a user cannot see in Salesforce because of a restrictive
org-wide default or a role-hierarchy boundary MAY still be retrievable through
this connector if their profile or an assigned permission set grants object
Read on that SObject. Record-level sharing is deferred to v1.1.

Operators MUST scope :attr:`~ne.connect.salesforce.config.SalesforceConnectorSettings.objects`
accordingly — sync only SObjects whose intended access boundary genuinely IS
"can this profile/permission-set read the object at all" (see the package
README for guidance). The mapper is fail-closed by construction: an SObject
whose permissions cannot be resolved (zero surviving grants, or a forged
reserved-namespace id) is quarantined by the caller, never treated as
world-readable.

Fail-closed, over EVERY branch:

* only rows with a truthy ``PermissionsRead`` are ever consulted — every other
  row is silently ignored (it grants nothing);
* a profile-owned grant (``Parent.IsOwnedByProfile`` truthy) maps to
  ``sf-profile:<Parent.ProfileId>``; a standalone permission-set grant maps to
  ``sf-permset:<Parent.Id>``;
* a missing/empty/non-mapping ``Parent``, or a ``Parent`` missing the id field
  this branch needs, is DROPPED WITH REASON — it never silently widens or
  narrows the outcome;
* a RAW source-provided id (the un-prefixed ``Parent.ProfileId``/``Parent.Id``)
  that already starts with a reserved namespace (``acl:``/``role:`` — NE-CKD2)
  TAINTS the whole object's outcome: a source that forged ``acl:tenant-all`` as
  a "profile id" would, if trusted, over-grant to every store-known principal
  at trim time. The caller quarantines a tainted object rather than admitting
  the forged label;
* every name in ``object_names`` gets an entry, even one with zero surviving
  rows — the caller quarantines any object whose outcome is tainted OR carries
  zero principals.

Unlike ``ne.connect.slack.acl`` / ``ne.connect.m365.acl``, this mapper emits
NO reserved labels (no analog of Slack's ``TENANT_ALL``) — Salesforce grants
are always attributable to a specific profile or permission set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["PERMSET_PREFIX", "PROFILE_PREFIX", "AclOutcome", "map_object_permissions"]

PROFILE_PREFIX = "sf-profile:"
PERMSET_PREFIX = "sf-permset:"

_RESERVED_PREFIXES = ("acl:", "role:")


@dataclass(frozen=True)
class AclOutcome:
    """The resolved OBJECT-LEVEL ACL for one Salesforce SObject."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(raw_id: str) -> bool:
    return raw_id.startswith(_RESERVED_PREFIXES)


def map_object_permissions(
    rows: Sequence[Mapping[str, object]], object_names: Sequence[str]
) -> dict[str, AclOutcome]:
    """Map a crawled ``ObjectPermissions`` result set to per-object :class:`AclOutcome`.

    ``object_names`` seeds every key of the returned mapping (even one with no
    surviving rows), so the caller can always look up every configured object's
    outcome regardless of what the query returned.
    """
    principals: dict[str, list[str]] = {name: [] for name in object_names}
    dropped: dict[str, list[str]] = {name: [] for name in object_names}
    tainted: dict[str, bool] = dict.fromkeys(object_names, False)

    for row in rows:
        if not row.get("PermissionsRead"):
            continue  # grants nothing — silently ignored, never a "drop".

        obj = row.get("SobjectType")
        if not isinstance(obj, str) or obj not in principals:
            continue  # not one of the objects we asked about — defensive, ignore.

        parent = row.get("Parent")
        if not isinstance(parent, Mapping) or not parent:
            dropped[obj].append("ObjectPermissions row has a missing or empty Parent")
            continue

        is_profile = bool(parent.get("IsOwnedByProfile"))
        raw_id = parent.get("ProfileId") if is_profile else parent.get("Id")
        if not isinstance(raw_id, str) or not raw_id:
            dropped[obj].append(
                f"Parent is missing its id field ({'ProfileId' if is_profile else 'Id'})"
            )
            continue

        if _is_reserved(raw_id):
            # NE-CKD2: a source-forged reserved-namespace id (e.g. a "ProfileId"
            # literally equal to "acl:tenant-all") is NEVER admitted as a
            # principal — it taints the whole object so the caller quarantines
            # it, rather than risk over-granting at trim time.
            tainted[obj] = True
            continue

        label = f"{PROFILE_PREFIX}{raw_id}" if is_profile else f"{PERMSET_PREFIX}{raw_id}"
        principals[obj].append(label)

    return {
        name: AclOutcome(
            principals=tuple(dict.fromkeys(principals[name])),  # order-preserving dedupe
            dropped=tuple(dropped[name]),
            tainted=tainted[name],
        )
        for name in object_names
    }
