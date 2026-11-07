# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Resolve a CDC row's ACL principal-column value (NE-084) — pure, no I/O.

This is the SECURITY CORE of the CDC sink, a **byte-identical semantic copy** of
:mod:`ne.connect.sql.acl` (which itself mirrors :mod:`ne.connect.m365.acl`). A
Debezium change event carries NO native per-row ACL — the operator's
:class:`~ne.connect.cdc.config.CdcTableAclConfig` supplies either a static
principal tuple or names a ``principal_column`` whose per-row VALUE is resolved
here into a principal set.

Why a package-local copy rather than an import? Importing
``ne.connect.sql.acl`` executes ``ne/connect/sql/__init__.py``, which drags the
``dlt`` runtime into the process. The CDC sink is consumed by ne-api at runtime
and MUST stay ``dlt``-free (the import-hygiene guardian locks this), so this
module re-states the SQL connector's ACL core with identical semantics. A
parity test (:mod:`tests.test_cdc_acl_parity`) asserts byte-for-byte equal
output against ``ne.connect.sql.acl`` so the two can never drift.

Fail-closed, over EVERY branch:

* ``None`` / an empty-after-strip value resolves to zero principals (untainted
  — the caller quarantines on "no principals" regardless of taint);
* a non-``str`` item inside a list/tuple principal-column value TAINTS the
  outcome (the reason names only the offending type, never the value — no
  attacker-controlled data ever appears in a log/error);
* ANY surviving principal starting with a reserved namespace (``acl:`` /
  ``role:``) TAINTS the outcome: source DATA may never mint a reserved label
  (operator CONFIG may — see :mod:`ne.connect.cdc.config` for the
  ``acl:tenant-all`` carve-out). A source that forged ``acl:tenant-all`` in a
  row would over-grant to every store-known principal at trim time, so the row
  is quarantined rather than trusted.

The caller quarantines whenever the outcome is ``tainted`` OR resolves to zero
principals — there is no code path here that hands back an ACL the caller would
treat as "empty means public".
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TENANT_ALL", "RowAclOutcome", "resolve_row_principals"]

TENANT_ALL = "acl:tenant-all"

_RESERVED_PREFIXES = ("acl:", "role:")


@dataclass(frozen=True)
class RowAclOutcome:
    """The resolved principal set for one row's ACL column value."""

    principals: tuple[str, ...]
    tainted: bool
    reason: str


def _is_reserved(principal: str) -> bool:
    return principal.startswith(_RESERVED_PREFIXES)


def resolve_row_principals(value: object, *, separator: str | None = None) -> RowAclOutcome:
    """Resolve a raw ``principal_column`` cell value to a :class:`RowAclOutcome`.

    * ``None`` -> zero principals, untainted, reason ``"no principal value"``.
    * ``str`` -> ``[value]``, or ``value.split(separator)`` when ``separator`` is
      set; items are stripped and empties dropped.
    * ``list``/``tuple`` -> every item must be ``str``; any non-``str`` item
      taints the outcome (reason names the offending type only).
    * Any surviving item starting with a reserved namespace (``acl:``/``role:``)
      taints the outcome — a source may never mint a reserved label.

    Principals are order-preserving deduped (``dict.fromkeys``).
    """
    raw_items: list[str] = []
    tainted = False
    reason = ""

    if value is None:
        return RowAclOutcome(principals=(), tainted=False, reason="no principal value")

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return RowAclOutcome(principals=(), tainted=False, reason="empty principal value")
        parts = stripped.split(separator) if separator else [stripped]
        raw_items = [p.strip() for p in parts if p.strip()]
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str):
                tainted = True
                reason = f"non-string principal item of type {type(item).__name__!r}"
                continue
            item = item.strip()
            if item:
                raw_items.append(item)
    else:
        return RowAclOutcome(
            principals=(),
            tainted=True,
            reason=f"unsupported principal value type {type(value).__name__!r}",
        )

    principals: list[str] = []
    for item in raw_items:
        if _is_reserved(item):
            tainted = True
            reason = f"reserved-namespace principal {item!r} forged in source data"
            continue
        principals.append(item)

    deduped = tuple(dict.fromkeys(principals))
    if not reason:
        reason = "ok" if deduped else "no usable principals resolved"
    return RowAclOutcome(principals=deduped, tainted=tainted, reason=reason)
