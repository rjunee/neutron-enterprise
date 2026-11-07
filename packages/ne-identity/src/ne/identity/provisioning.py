# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JIT provisioning and the frozen PrincipalResolver implementation.

On the first valid token for a subject, :class:`JitProvisioner` provisions a
:class:`ne.contracts.types.Principal`; on every subsequent token it UPSERTS,
recomputing groups and roles from the CURRENT token so that de-escalation
(a group removed at the IdP) propagates and roles never accrete.

Anti-escalation design (SPEC §5 NE-060):

* Platform roles derive ONLY from the config-driven group->role mapping
  (:mod:`ne.identity.roles`). Token ``roles`` / ``realm_access`` claims are
  never read.
* ``Principal.groups`` carries BOTH the sanitized IdP groups verbatim (NE-043
  source-ACL trimming matches these) AND the mapped platform roles namespaced
  as ``role:<name>``. The ``role:`` namespace is collision-proof because
  :func:`~ne.identity.roles.sanitize_idp_groups` drops any ``role:*`` token
  group.
* A ``claims.subject`` in the reserved ``acl:*`` / ``role:*`` namespace is
  REFUSED here as well (NE-FIX-IDENTITY P2-1 defense-in-depth) — the verifier
  already rejects such subjects, but a reserved id must never become a
  ``Principal.id`` (it would match every TENANT_ALL-stamped chunk). Refusal
  raises the opaque :class:`~ne.identity.errors.TokenVerificationError` so the
  middleware maps a direct-call escape to the oracle-free 401, never a 500.

Zero-mapped-groups observability (NE-BEW-GROUPS-ZEROGROUPS, SPEC §5): NE-BEW-
GROUPS made group emission depend on a manual Entra app-group-assignment step
that did not previously exist; a forgotten assignment produces a token with
NO overage indicator and NO groups, which resolves silently to default-roles-
only — an under-authorized user with no signal anywhere. This is OBSERVABLE,
not blocking (over-blocking a legitimately group-less user is its own
failure): when a login is BROKERED (a non-empty ``idp_claim`` value is
present) AND a role mapping is configured AND the roles actually granted by
:func:`~ne.identity.roles.map_groups_to_roles` are EXACTLY
``config.default_roles`` (no incoming group contributed anything beyond the
floor), :meth:`JitProvisioner.provision` still returns the principal (default
roles only) but additionally (a) logs a structured WARNING (idp alias +
counts only, never subject/group names), and (b) if an ``AuditSink`` is
wired, appends a queryable ``identity.brokered_login.zero_groups`` audit
event. The fire condition is DERIVED from ``map_groups_to_roles``'s actual
output rather than re-implementing its key lookup, so it cannot silently
desync from the authorization logic (a second hand-rolled "is this group a
mapping key" check disagreed with the real mapping output on inputs like
``mapping={"eng": ()}`` or a group mapped only to a role already in
``default_roles`` — both are still zero *effective* authorization and both
must fire). This also catches an all-unmapped-groups shape (e.g. a
stale/typo'd mapping key) and a group that maps to nothing new — all the
same silent under-authorization. A deployment with an empty mapping never
fires this on any login (opt-out for legitimately group-less deployments).
Audit emission NEVER blocks or fails the login — a sink outage is swallowed
with a logged exception, deliberately asymmetric with the agents-credential
C6 fail-closed rule: this event observes under-authorization on a user
login, it does not gate a capability grant.

Per-login dedup, not per-request (Argus review, NE-BEW-GROUPS-ZEROGROUPS
round 2): :meth:`OidcAuthMiddleware.__call__` calls ``provision()`` on EVERY
authenticated HTTP request, not just at login, so the raw fire condition
above would append an audit event AND log a WARNING on every single request
from an affected user for as long as their session lives — polluting the
tamper-evident, globally-serialized audit chain and adding head-lock
contention to the authn hot path, driven by exactly the misconfiguration
this signal targets. A bounded, TTL'd, per-subject memo
(``_claim_zero_groups_slot``) gates BOTH the audit append and the WARNING so
repeated requests within one dedup window fire at most once; the memo is
capped at ``zero_groups_dedup_max_entries`` (oldest evicted first) so it
cannot grow unbounded, and re-arms after ``zero_groups_dedup_ttl_seconds`` so
a persistently-broken session is still re-signalled periodically rather than
silenced forever.

:class:`JitPrincipalResolver` implements the frozen
:class:`ne.contracts.identity.PrincipalResolver`. ``resolve()`` NEVER creates a
principal — provisioning happens only from a verified token, so a miss raises
:class:`~ne.identity.errors.UnknownPrincipalError`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from ne.contracts.governance import AuditSink
from ne.contracts.identity import PrincipalResolver, TokenClaims
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.identity.config import RoleMappingConfig
from ne.identity.errors import TokenVerificationError, UnknownPrincipalError
from ne.identity.roles import (
    RESERVED_PREFIXES,
    ROLE_PREFIX,
    map_groups_to_roles,
    sanitize_idp_groups,
)
from ne.schema import AuditOutcome

logger = logging.getLogger(__name__)

# Claims copied (str-valued only) into Principal.claims. Principal.claims is a
# StrMap (str->str), so non-str claim values are dropped.
_CLAIM_WHITELIST = ("email", "preferred_username", "name")

# Audit action for a brokered login that resolves zero MAPPED platform groups
# (NE-BEW-GROUPS-ZEROGROUPS). Queryable via the audit API; survives log
# rotation, unlike the accompanying WARNING.
ZERO_GROUPS_AUDIT_ACTION = "identity.brokered_login.zero_groups"

# Zero-groups per-login dedup memo defaults (NE-BEW-GROUPS-ZEROGROUPS round 2,
# Argus P0): provision() runs on every authenticated request, not just at
# login, so emission must be bounded per subject. 5 minutes comfortably
# exceeds a single request-response cycle and typical short-poll intervals
# while still re-arming well within one access-token lifetime, so a session
# that stays broken keeps getting re-signalled rather than going silent for
# its whole lifetime. 4096 entries bounds worst-case memory to a small
# fixed size regardless of how many distinct subjects hit a misconfigured
# deployment.
_ZERO_GROUPS_DEDUP_TTL_SECONDS_DEFAULT = 300.0
_ZERO_GROUPS_DEDUP_MAX_ENTRIES_DEFAULT = 4096


class PrincipalStore(Protocol):
    """Durable store of provisioned principals.

    The production Postgres-backed store arrives with ne-permissions
    (NE-042/043); this seam is where it plugs in. Deliberately NOT added to
    ne-contracts — it is an internal ne-identity collaboration point.
    """

    async def get(self, subject: str) -> Principal | None: ...
    async def put(self, principal: Principal) -> None: ...


class InMemoryPrincipalStore:
    """A process-local principal store (dev/embedding/tests)."""

    def __init__(self) -> None:
        self._data: dict[str, Principal] = {}
        self._lock = asyncio.Lock()

    async def get(self, subject: str) -> Principal | None:
        async with self._lock:
            return self._data.get(subject)

    async def put(self, principal: Principal) -> None:
        async with self._lock:
            self._data[principal.id] = principal


class JitProvisioner:
    """Just-in-time principal provisioning from verified token claims."""

    def __init__(
        self,
        store: PrincipalStore,
        roles: RoleMappingConfig,
        groups_claim: str = "groups",
        principal_claims: tuple[str, ...] = _CLAIM_WHITELIST,
        idp_claim: str = "identity_provider",
        audit_sink: AuditSink | None = None,
        zero_groups_dedup_ttl_seconds: float = _ZERO_GROUPS_DEDUP_TTL_SECONDS_DEFAULT,
        zero_groups_dedup_max_entries: int = _ZERO_GROUPS_DEDUP_MAX_ENTRIES_DEFAULT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._roles = roles
        self._groups_claim = groups_claim
        self._principal_claims = principal_claims
        self._idp_claim = idp_claim
        self._audit_sink = audit_sink
        self._zero_groups_dedup_ttl = zero_groups_dedup_ttl_seconds
        self._zero_groups_dedup_max_entries = zero_groups_dedup_max_entries
        self._clock = clock
        # subject -> clock() value at last emission. An OrderedDict so the
        # bound below can evict the least-recently-touched entry first.
        self._zero_groups_last_fired: OrderedDict[str, float] = OrderedDict()
        self._zero_groups_lock = asyncio.Lock()

    async def provision(self, claims: TokenClaims) -> Principal:
        if claims.subject.startswith(RESERVED_PREFIXES):
            # Defense-in-depth: a reserved acl:*/role:* id must never become a
            # Principal.id. Opaque TokenVerificationError -> oracle-free 401.
            logger.warning("provisioning refused: reserved-namespace subject")
            raise TokenVerificationError("token verification failed")
        idp_groups = sanitize_idp_groups(claims.claims.get(self._groups_claim))
        roles = map_groups_to_roles(idp_groups, self._roles)
        groups = tuple(idp_groups) + tuple(f"{ROLE_PREFIX}{r}" for r in roles)

        principal_claims: dict[str, str] = {"iss": claims.issuer}
        for name in self._principal_claims:
            value = claims.claims.get(name)
            if isinstance(value, str):
                principal_claims[name] = value

        principal = Principal(id=claims.subject, groups=groups, claims=principal_claims)
        await self._store.put(principal)

        idp = claims.claims.get(self._idp_claim)
        # DERIVED from map_groups_to_roles' actual output (not a second,
        # hand-rolled "is this group a mapping key" lookup) — see the module
        # docstring for why a re-implementation silently desynced from the
        # real authorization logic on inputs like mapping={"g": ()}.
        zero_effective_authorization = set(roles) == set(self._roles.default_roles)
        # The dedup-slot claim is LAST and short-circuits: a login that does not
        # meet the fire condition must never consume a memo slot, or an ordinary
        # high-traffic user could evict the entry for a genuinely affected one.
        if (
            isinstance(idp, str)
            and idp
            and self._roles.mapping
            and zero_effective_authorization
            and await self._claim_zero_groups_slot(claims.subject)
        ):
            mapped_group_count = sum(1 for g in idp_groups if g in self._roles.mapping)
            await self._signal_zero_groups(
                claims=claims,
                idp=idp,
                idp_groups=idp_groups,
                mapped_group_count=mapped_group_count,
            )

        return principal

    async def _claim_zero_groups_slot(self, subject: str) -> bool:
        """Return True iff this call should emit the zero-groups signal now.

        Bounds emission to at most once per ``zero_groups_dedup_ttl_seconds``
        window per subject (see module docstring: "Per-login dedup, not
        per-request"). The memo is capped at
        ``zero_groups_dedup_max_entries``, evicting the least-recently-touched
        subject first, so it cannot grow without bound.
        """
        now = self._clock()
        async with self._zero_groups_lock:
            last = self._zero_groups_last_fired.get(subject)
            if last is not None and (now - last) < self._zero_groups_dedup_ttl:
                return False
            self._zero_groups_last_fired[subject] = now
            self._zero_groups_last_fired.move_to_end(subject)
            while len(self._zero_groups_last_fired) > self._zero_groups_dedup_max_entries:
                self._zero_groups_last_fired.popitem(last=False)
        return True

    async def _signal_zero_groups(
        self,
        *,
        claims: TokenClaims,
        idp: str,
        idp_groups: tuple[str, ...],
        mapped_group_count: int,
    ) -> None:
        """Make a brokered zero-mapped-groups login OBSERVABLE (never blocking).

        Logs a structured WARNING (idp alias + counts only — no subject, no
        group names, no claim material). Additionally appends a queryable
        ``ZERO_GROUPS_AUDIT_ACTION`` event through the frozen ``AuditSink``
        when one is wired; a sink outage is swallowed (logged) so the login
        is never blocked by signal-emission failure. Callers gate this via
        :meth:`_claim_zero_groups_slot` so it fires at most once per dedup
        window per subject.
        """
        logger.warning(
            "brokered login resolved zero mapped platform groups (idp=%s, idp_group_count=%d)",
            idp,
            len(idp_groups),
        )
        if self._audit_sink is None:
            return
        try:
            event = new_audit_event(
                action=ZERO_GROUPS_AUDIT_ACTION,
                resource=f"principal:{claims.subject}",
                outcome=AuditOutcome.success,
                actor=claims.subject,
                detail=(
                    f"brokered login via idp={idp} resolved zero mapped platform groups; "
                    "only default roles granted"
                ),
                provenance={
                    "idp": idp,
                    "idp_group_count": str(len(idp_groups)),
                    "mapped_group_count": str(mapped_group_count),
                },
            )
            await self._audit_sink.append(event)
        except Exception:
            logger.exception("zero-groups audit emit failed; login not blocked")


class JitPrincipalResolver:
    """Frozen PrincipalResolver: store lookup only, never creates principals."""

    def __init__(self, store: PrincipalStore) -> None:
        self._store = store

    async def resolve(self, subject: str) -> Principal:
        principal = await self._store.get(subject)
        if principal is None:
            raise UnknownPrincipalError(f"no provisioned principal for subject {subject!r}")
        return principal


# Static conformance to the frozen PrincipalResolver Protocol (pyright-checked).
def _check_principal_resolver(r: JitPrincipalResolver) -> PrincipalResolver:
    return r
