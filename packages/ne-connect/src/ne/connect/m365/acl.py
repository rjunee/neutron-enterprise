# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Map Graph permission payloads to frozen ACL principals (NE-044) — pure, no I/O.

This is the SECURITY CORE of the connector: it turns a driveItem's
``/permissions`` collection into the principal set stamped onto the chunk's
:class:`~ne.contracts.integration.AclEntry`, mapped to the label namespace
NE-042/043 consume (raw Entra object ids, plus the reserved ``acl:tenant-all`` /
``acl:public-link`` labels for org-wide / anonymous-link grants).

Fail-closed, over EVERY branch:

* an identity Graph gives no usable AAD id (a bare siteGroup, a UPN-valued
  ``membership`` claim, an application / device identity) is DROPPED with a
  reason — it does NOT silently widen or narrow the grant. A ``siteGroup`` /
  ``siteUser`` whose ``loginName`` is a claims-encoded Entra object id IS
  resolved to that id (see :data:`_CLAIMS_AAD_PREFIXES`), but ONLY on a full
  prefix match with a syntactically valid GUID as the ENTIRE value;
* a SOURCE-provided principal id in a reserved namespace (``acl:`` / ``role:`` —
  NE-CKD2) marks the whole outcome ``tainted``: a source that forges
  ``acl:tenant-all`` would over-grant to every store-known principal at trim
  time, so the item is quarantined rather than trusted;
* :data:`TENANT_ALL` / :data:`PUBLIC_LINK` are emitted ONLY by this mapper for
  genuine ``organization`` / ``anonymous`` link grants — never lifted from a
  source id (that path taints).

The caller QUARANTINES an item whenever the outcome is ``tainted`` OR resolves to
zero principals (never default-open). ``roles`` are intentionally NOT inspected:
any surviving grant implies read visibility here (the store's trimming is the
authority on what a principal may then retrieve).

The local constants are byte-identical to ``ne.permissions.models`` (a dev-dep
test locks the equality) — the same package-local pattern ``ne.brain.trimming``
uses, avoiding a runtime ne-connect -> ne-permissions edge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["ACL_MAPPER_REVISION", "PUBLIC_LINK", "TENANT_ALL", "AclOutcome", "map_permissions"]

#: The revision of this module's ACL mapping SEMANTICS — the rules by which a
#: driveItem's ``/permissions`` collection becomes a principal set. Bumping it is
#: how an ACL-semantics change INVALIDATES prior mappings: the value is hashed
#: into ``m365_source_link_mapper_revision``, so a new value changes the source
#: contract identity and forces a full re-crawl. Without that, a semantics fix
#: cannot reach driveItems the delta cursor considers unchanged, and items
#: quarantined by the old rules stay quarantined forever.
#:
#: ``2``: claims-encoded Entra principals are resolved. Revision ``1`` is the
#: shipped pre-fix behaviour, which dropped a ``siteGroup``/``siteUser`` whose
#: ``loginName`` was a claims-encoded Entra object id as "no usable AAD id" and
#: so quarantined every item granted only through one.
ACL_MAPPER_REVISION = 2

TENANT_ALL = "acl:tenant-all"
PUBLIC_LINK = "acl:public-link"

_RESERVED_PREFIXES = ("acl:", "role:")
_EVERYONE_CLAIMS = ("spo-grid-all-users", "c:0(.s|true", "everyone except external")

# SharePoint claims-encoded login names whose value segment IS an Entra object id.
# Both are documented SPO encodings: ``c:0t.c|tenant|<guid>`` is the tenant claim
# provider's form for an Entra security group or user, and
# ``c:0o.c|federateddirectoryclaimprovider|<guid>`` is the federated-directory
# claim provider's form for a Microsoft 365 (unified) group.
#
# DELIBERATELY ABSENT — these are documented too, and are NOT object ids:
# * ``i:0#.f|membership|<upn>`` — the Entra USER form carries a UPN/email, not a
#   guid, so it cannot be resolved to an object id here and must stay dropped;
# * ``c:0-.f|rolemanager|spo-grid-all-users/<tenant>`` and ``c:0(.s|true`` — the
#   everyone-claims, handled above and unchanged;
# * the M365 group OWNERS variant, which suffixes the guid (``<guid>_o``).
#   Resolving it to the bare guid would grant the group's MEMBERS an access the
#   tenant gave only its OWNERS, so the full-value GUID match below rejects it.
_CLAIMS_AAD_PREFIXES = (
    "c:0t.c|tenant|",
    "c:0o.c|federateddirectoryclaimprovider|",
)

# A syntactically valid GUID, and nothing else. Intentionally UNANCHORED so the
# single call site's ``fullmatch`` is the only thing enforcing "the WHOLE value
# is a guid" — a relaxation there is a one-edit mutation the tests must catch.
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class AclOutcome:
    """The resolved ACL for one driveItem's permission collection."""

    principals: tuple[str, ...]
    dropped: tuple[str, ...]
    tainted: bool


def _is_reserved(principal_id: str) -> bool:
    return principal_id.startswith(_RESERVED_PREFIXES)


def _claims_aad_value(login: str) -> str | None:
    """The value segment of a RECOGNISED claims-encoded AAD login, else ``None``.

    Prefix match ONLY — this says "the login is one of the encodings whose value
    segment is supposed to be an object id", NOT that the value is well-formed.
    The caller is what validates it. Splitting the two keeps the unrecognised
    -prefix guard and the strict-GUID guard independently observable.
    """
    for prefix in _CLAIMS_AAD_PREFIXES:
        if login.startswith(prefix):
            return login[len(prefix) :]
    return None


def _resolve_identity_set(
    identity_set: Mapping[str, object],
    principals: list[str],
    dropped: list[str],
) -> bool:
    """Resolve one SharePointIdentitySet; append principals/drops. Return tainted."""
    tainted = False
    for kind in ("group", "user"):
        ident = identity_set.get(kind)
        if isinstance(ident, Mapping):
            raw = ident.get("id")
            if isinstance(raw, str) and raw:
                if _is_reserved(raw):
                    tainted = True  # source forged a reserved-namespace id
                else:
                    principals.append(raw)
    for kind in ("siteGroup", "siteUser"):
        ident = identity_set.get(kind)
        if isinstance(ident, Mapping):
            login = str(ident.get("loginName") or "").lower()
            if any(claim in login for claim in _EVERYONE_CLAIMS):
                principals.append(TENANT_ALL)
                continue
            value = _claims_aad_value(login)
            if value is not None and _is_reserved(value):
                tainted = True  # source forged a reserved-namespace id
            elif value is not None and _GUID.fullmatch(value):
                principals.append(value)
            else:
                # Unrecognised encoding, or a recognised one whose value is not
                # a whole GUID. Fail closed: never guess, never take a substring.
                dropped.append(
                    f"{kind} {ident.get('loginName')!r}: no resolvable AAD object id in login name"
                )
    for kind in ("application", "device"):
        if isinstance(identity_set.get(kind), Mapping):
            dropped.append(f"{kind} identity is not an ACL principal")
    return tainted


def map_permissions(perms: Sequence[Mapping[str, object]]) -> AclOutcome:
    """Map a driveItem ``/permissions`` collection to an :class:`AclOutcome`."""
    principals: list[str] = []
    dropped: list[str] = []
    tainted = False

    for perm in perms:
        link = perm.get("link")
        if isinstance(link, Mapping):
            scope = str(link.get("scope") or "")
            if scope == "anonymous":
                principals.append(PUBLIC_LINK)
            elif scope == "organization":
                principals.append(TENANT_ALL)
            elif scope == "users":
                for iset in _identity_lists(perm):
                    tainted = _resolve_identity_set(iset, principals, dropped) or tainted
            elif scope == "existingAccess":
                dropped.append("existingAccess link grants nothing new")
            else:
                dropped.append(f"unhandled link scope {scope!r}")
            continue
        # Non-link grant: walk grantedToV2 (single) + grantedToIdentitiesV2 (list).
        for iset in _identity_lists(perm):
            tainted = _resolve_identity_set(iset, principals, dropped) or tainted

    # Order-preserving dedupe (matches build_acl_index style).
    deduped = tuple(dict.fromkeys(principals))
    return AclOutcome(principals=deduped, dropped=tuple(dropped), tainted=tainted)


def _identity_lists(perm: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The identity sets a permission grants to: grantedToV2 + grantedToIdentitiesV2."""
    sets: list[Mapping[str, object]] = []
    single = perm.get("grantedToV2")
    if isinstance(single, Mapping):
        sets.append(single)
    many = perm.get("grantedToIdentitiesV2")
    if isinstance(many, Sequence) and not isinstance(many, (str, bytes)):
        sets.extend(iset for iset in many if isinstance(iset, Mapping))
    return sets
