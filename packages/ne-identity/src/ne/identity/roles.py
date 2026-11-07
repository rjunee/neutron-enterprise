# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""IdP-group sanitization and config-driven group -> platform-role mapping.

This module is the JIT anti-escalation core. Two invariants hold here:

* :func:`sanitize_idp_groups` DROPS any incoming group whose name starts with a
  RESERVED prefix (``role:`` OR ``acl:``) — an IdP (or a forged token claim) can
  therefore never smuggle a platform role (``role:*``) NOR one of the reserved
  ne-permissions ACL labels (``acl:tenant-all`` / ``acl:public-link``) in via the
  groups claim. The latter would otherwise bypass the ne-permissions ``is_known``
  fail-closed gate and grant tenant-wide retrieval (Checkpoint-D finding #2, C7).
* :func:`map_groups_to_roles` grants a role ONLY when the group is an explicit
  key in the config mapping; unmapped groups contribute nothing, and token
  claims such as ``roles`` / ``realm_access`` are never consulted anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence

from ne.identity.config import RoleMappingConfig

# Namespace applied to mapped platform roles when they are folded into
# ``Principal.groups``. Collision-proof because sanitize_idp_groups() strips any
# incoming group that already starts with this prefix.
ROLE_PREFIX = "role:"

# Reserved ACL-label namespace owned by ne-permissions (TENANT_ALL =
# "acl:tenant-all", PUBLIC_LINK = "acl:public-link" in ne.permissions.models).
# ne-identity must NOT import ne-permissions (dependency direction is
# contracts-only), so the prefix string is mirrored here — keep the two in sync.
ACL_PREFIX = "acl:"

# Every namespace a raw IdP group is forbidden from occupying. str.startswith
# accepts this tuple directly, so sanitize_idp_groups needs no loop.
RESERVED_PREFIXES: tuple[str, ...] = (ROLE_PREFIX, ACL_PREFIX)


def sanitize_idp_groups(raw: object) -> tuple[str, ...]:
    """Normalize a raw groups-claim value into safe, deduped, sorted group names.

    * Non-``str`` items are dropped.
    * A single leading ``/`` is stripped (Keycloak full-path form).
    * Any group starting with a :data:`RESERVED_PREFIXES` namespace is DROPPED —
      ``role:*`` blocks JIT platform-role self-assignment, and ``acl:*`` blocks
      smuggling the reserved ne-permissions ACL labels (``acl:tenant-all`` /
      ``acl:public-link``) that would otherwise bypass its ``is_known``
      fail-closed gate and widen retrieval (Checkpoint-D finding #2, C7).
    * The result is deduplicated and sorted for determinism.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    cleaned: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item[1:] if item.startswith("/") else item
        if not name:
            continue
        if name.startswith(RESERVED_PREFIXES):
            continue
        cleaned.add(name)
    return tuple(sorted(cleaned))


def map_groups_to_roles(groups: Sequence[str], config: RoleMappingConfig) -> tuple[str, ...]:
    """Map sanitized IdP groups to platform roles via the config mapping only.

    Returns the deduped, sorted union of ``config.mapping[g]`` for every group
    that is an explicit key, plus ``config.default_roles``. Unmapped groups
    contribute nothing.
    """
    roles: set[str] = set(config.default_roles)
    for group in groups:
        mapped = config.mapping.get(group)
        if mapped is not None:
            roles.update(mapped)
    return tuple(sorted(roles))
