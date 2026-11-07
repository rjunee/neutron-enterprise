# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed ACL resolution for connector records (NE-040, C7/C12).

:class:`AclMapper` maps a source :class:`~ne.contracts.integration.Record` to the
ACL labels stamped onto its stored chunk. The invariant that matters:
:meth:`AclMapper.resolve` can NEVER return an empty ACL. An empty result would
mean an unlabeled record entering the brain — the NE-CKC2 fail-open failure. A
single exit-point guard raises :class:`AclResolutionError` instead; there is no
code path returning ``()``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ne.connect.contracts import PermissionMode, PermissionModelSpec
from ne.connect.errors import AclResolutionError
from ne.contracts.integration import AclEntry, Record


def build_acl_index(entries: Iterable[AclEntry]) -> dict[str, tuple[str, ...]]:
    """Build a ``resource_id -> principals`` lookup, UNIONing duplicate resources.

    A paginated permission crawl legitimately emits MULTIPLE ``AclEntry`` rows for
    the same ``resource_id`` (one per page of grants). Last-write-wins would keep
    only the final page and DROP every principal named on earlier pages — an
    over-restriction bug where users silently lose access (NE-040 ACL P2). Each
    resource's principals are therefore accumulated with an order-preserving
    UNION (``dict.fromkeys`` dedupe), so the index carries every granted principal
    across all pages.
    """
    index: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        index[entry.resource_id] = tuple(
            dict.fromkeys((*index.get(entry.resource_id, ()), *entry.principals))
        )
    return index


class AclMapper:
    """Resolve a record's ACL under a :class:`PermissionModelSpec`, fail-closed."""

    def __init__(self, spec: PermissionModelSpec) -> None:
        self._spec = spec

    def resolve(self, record: Record, index: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
        """Return the non-empty ACL for ``record``, or raise ``AclResolutionError``.

        * ``public-within-tenant`` -> ``spec.default_acl`` (non-empty by
          construction — the spec validator forbids an empty default here).
        * ``source-acl`` -> the source's principals for ``record.source_id``,
          falling back to ``spec.default_acl`` when the source has none.

        The final single-exit guard enforces the never-empty invariant: an empty
        result is a fail-closed error, never a silent unlabeled write.
        """
        if self._spec.mode is PermissionMode.PUBLIC_WITHIN_TENANT:
            resolved = self._spec.default_acl
        else:  # SOURCE_ACL
            principals = index.get(record.source_id)
            resolved = principals if principals else self._spec.default_acl

        if not resolved:
            raise AclResolutionError(
                f"no ACL could be resolved for record {record.record_id!r} from source "
                f"{record.source_id!r} (source-acl mode, no source principals and no "
                "default_acl) — the record is fail-closed and must NOT be written"
            )
        return resolved
