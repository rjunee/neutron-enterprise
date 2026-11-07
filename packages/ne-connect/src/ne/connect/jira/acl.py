# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map a Jira project permission scheme to frozen ACL principals (NE-093) — pure, no I/O.

This is the SECURITY CORE of the connector: it turns a crawled project
permission scheme into the PROJECT-LEVEL principal set stamped onto every
record's :class:`~ne.contracts.integration.AclEntry` for that project.

**Jira ACL mirroring is PROJECT-BROWSE-LEVEL ONLY in v1.** Only the
``BROWSE_PROJECTS`` permission's grant holders are mirrored, into
``jira-group:`` / ``jira-role:<projectId>:<roleId>`` / ``jira-user:`` labels.
Concretely:

* **issue security levels** compose a per-issue visibility narrower than the
  project browse grant — they are NOT resolved here; the connector QUARANTINES
  any issue carrying a non-null ``fields.security`` per-item rather than
  admitting it under the project ACL;
* **visibility-restricted comments** (a ``visibility`` block, or
  ``jsdPublic == False``) are likewise QUARANTINED per-item by the connector,
  never admitted under the project ACL;
* **project-role actor membership** (which users/groups a ``projectRole`` grant
  actually expands to) is the PRINCIPAL STORE's job (NE-042), not this mapper's
  — we emit the ``jira-role:`` label and let the store resolve members.

Fail-closed, over EVERY branch:

* ONLY rows whose ``permission == 'BROWSE_PROJECTS'`` are consulted — every
  other permission row is silently ignored (it grants no browse access);
* a ``group`` holder maps to ``jira-group:<groupId>`` (fallback ``parameter``);
  a ``projectRole`` holder to ``jira-role:<projectId>:<roleId>`` (fallback
  ``parameter``); a ``user`` holder to ``jira-user:<accountId>`` (fallback
  ``parameter``); an ``applicationRole`` / ``anyone`` holder to
  :data:`TENANT_ALL` (any-logged-in-user / public — within a single-tenant
  deployment tenant-all is the widest mirror);
* a missing/non-mapping holder, or a holder missing the id field its branch
  needs, is DROPPED WITH REASON — it never silently widens or narrows;
* a RAW source-provided id already starting with a reserved namespace
  (``acl:``/``role:`` — NE-CKD2) TAINTS the whole outcome: a source that forged
  ``acl:tenant-all`` as a "groupId" would, if trusted, over-grant to every
  store-known principal at trim time. The caller quarantines a tainted project.

:data:`TENANT_ALL` is emitted ONLY by this mapper for an ``applicationRole`` /
``anyone`` grant — never lifted from a source id (that path taints). The
constant is byte-identical to ``ne.permissions.models`` (a dev-dep test locks
the equality), avoiding a runtime ne-connect -> ne-permissions edge — the same
package-local pattern ``ne.connect.slack.acl`` uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "GROUP_PREFIX",
    "PROJECT_PREFIX",
    "ROLE_PREFIX",
    "TENANT_ALL",
    "USER_PREFIX",
    "AclOutcome",
    "map_permission_scheme",
]

GROUP_PREFIX = "jira-group:"
ROLE_PREFIX = "jira-role:"
USER_PREFIX = "jira-user:"
PROJECT_PREFIX = "jira-project:"
TENANT_ALL = "acl:tenant-all"

_RESERVED_PREFIXES = ("acl:", "role:")
_BROWSE = "BROWSE_PROJECTS"


@dataclass(frozen=True)
class AclOutcome:
    """The resolved PROJECT-LEVEL ACL for one Jira project."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(raw_id: str) -> bool:
    return raw_id.startswith(_RESERVED_PREFIXES)


def _holder_id(holder: Mapping[str, object], nested: str, key: str) -> str | None:
    """Return the nested id (``holder[nested][key]``) or the flat ``parameter`` fallback.

    Ids are coerced to ``str`` — a Jira project-role id arrives as an integer,
    an accountId/groupId as a string; both become their string form.
    """
    inner = holder.get(nested)
    if isinstance(inner, Mapping):
        value = inner.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    parameter = holder.get("parameter")
    if isinstance(parameter, str) and parameter:
        return parameter
    return None


def map_permission_scheme(scheme: Mapping[str, object], project_id: str) -> AclOutcome:
    """Map a Jira project permission scheme to a project-level :class:`AclOutcome`.

    ``project_id`` is the project's numeric id, woven into every
    ``jira-role:<projectId>:<roleId>`` label so a role grant is scoped to this
    project (a role id is only meaningful relative to its project).
    """
    principals: list[str] = []
    dropped: list[str] = []
    tainted = False

    raw_rows = scheme.get("permissions")
    rows = raw_rows if isinstance(raw_rows, (list, tuple)) else ()

    for row in rows:
        if not isinstance(row, Mapping):
            dropped.append("permission row is not a mapping")
            continue
        if row.get("permission") != _BROWSE:
            continue  # not a browse grant — silently ignored.

        holder = row.get("holder")
        if not isinstance(holder, Mapping) or not holder:
            dropped.append("BROWSE_PROJECTS row has a missing or empty holder")
            continue

        holder_type = holder.get("type")
        if holder_type == "group":
            raw_id = _holder_id(holder, "group", "groupId")
            prefix = GROUP_PREFIX
        elif holder_type == "projectRole":
            role_id = _holder_id(holder, "projectRole", "id")
            if role_id is None:
                dropped.append("projectRole holder is missing its role id")
                continue
            if _is_reserved(role_id):
                tainted = True
                continue
            principals.append(f"{ROLE_PREFIX}{project_id}:{role_id}")
            continue
        elif holder_type == "user":
            raw_id = _holder_id(holder, "user", "accountId")
            prefix = USER_PREFIX
        elif holder_type in ("applicationRole", "anyone"):
            # any-logged-in-user / public -> the widest single-tenant mirror.
            # TENANT_ALL is minted ONLY here, never lifted from a source id.
            principals.append(TENANT_ALL)
            continue
        else:
            dropped.append(f"unsupported holder type {holder_type!r}")
            continue

        if raw_id is None:
            dropped.append(f"{holder_type} holder is missing its id")
            continue
        if _is_reserved(raw_id):
            # NE-CKD2: a source-forged reserved-namespace id is NEVER admitted;
            # it taints the whole project so the caller quarantines it.
            tainted = True
            continue
        principals.append(f"{prefix}{raw_id}")

    return AclOutcome(
        principals=tuple(dict.fromkeys(principals)),  # order-preserving dedupe
        dropped=tuple(dropped),
        tainted=tainted,
    )
