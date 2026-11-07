# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AgentCredentialService — issue / rotate / revoke scoped per-agent credentials.

Each agent/department session gets its OWN least-privilege service credential
(NOT a shared platform god-credential), so a compromised agent is bounded to its
scope. The secret MATERIAL lives ONLY in the frozen SecretStore (NE-007, C1) at a
KV-v2 versioned path — one path per agent, one version per credential. The
service holds only metadata (:class:`AgentCredential`), a per-agent rotation pool
(:mod:`ne.identity.agents.pool`), and the frozen ``AuditSink``.

Governance coupling (C6, fail closed):

* every ``issue`` / ``rotate`` / ``revoke`` emits a config_hash-stamped
  ``AuditEvent`` through the frozen ``AuditSink``;
* an ``issue`` whose audit emit fails — event CONSTRUCTION (e.g.
  ``ConfigHashNotFrozenError``) or sink APPEND — is UNWOUND: the just-written
  secret version is destroyed and the credential deregistered, so an unaudited
  credential can never be used;
* a ``rotate`` whose audit emit fails — event CONSTRUCTION (e.g.
  ``ConfigHashNotFrozenError``) or sink APPEND — unwinds the NEW version only (the
  old credential reverts to ACTIVE);
* a ``revoke`` is immediate: the credential is marked REVOKED and its pool entry
  parked BEFORE the audit append, so revocation stands even if the audit fails
  (``CredentialAuditError`` is still raised).

Redaction (C1): audit detail/provenance carry agent keys, counts, states, and a
reason CLASS — never token values or raw scope args beyond tool-name lists. The
only secret-returning API is :meth:`resolve_secret`, which wraps the material in a
``SecretStr``.

New pool selections are ACTIVE-only: :meth:`select_credential` draws exclusively
from ACTIVE credentials (via an ACTIVE-only ``eligible`` predicate), so a rotated-
out GRACE credential — possibly broader-scoped after a narrowing rotation — is
never handed to a NEW dispatch; GRACE serves in-flight holders exclusively
(``validate``/``resolve_secret`` by id), and no ACTIVE credential fails closed.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from ne.contracts.governance import AuditSink
from ne.contracts.secrets import SecretStore
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.identity.agents.binding import bind_agent_principal
from ne.identity.agents.config import AgentCredentialConfig
from ne.identity.agents.model import (
    AgentCredential,
    AgentRef,
    CredentialScope,
    CredentialState,
)
from ne.identity.agents.pool import (
    CredentialPoolState,
    PoolEntry,
    has_usable,
    park,
    report_failure,
    report_success,
    select_entry,
)
from ne.identity.errors import (
    CredentialAlreadyProvisionedError,
    CredentialAuditError,
    CredentialExpiredError,
    CredentialNotFoundError,
    CredentialRevokedError,
)
from ne.schema import AuditOutcome

_PROVENANCE = {"source": "ne-identity", "component": "agents"}
_LIVE = (CredentialState.ACTIVE, CredentialState.GRACE)
_DEAD = (CredentialState.REVOKED, CredentialState.EXPIRED)

_STATE_RANK = {CredentialState.ACTIVE: 0, CredentialState.GRACE: 1}


class AgentCredentialService:
    """Issues, rotates, revokes, and validates scoped per-agent credentials."""

    def __init__(
        self,
        store: SecretStore,
        sink: AuditSink,
        config: AgentCredentialConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if store is None:
            raise TypeError("AgentCredentialService requires a non-None SecretStore (C1)")
        if sink is None:
            raise TypeError("AgentCredentialService requires a non-None AuditSink (C6)")
        self._store = store
        self._sink = sink
        self._config = config if config is not None else AgentCredentialConfig()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._credentials: dict[str, AgentCredential] = {}
        self._pools: dict[str, CredentialPoolState] = {}

    def __repr__(self) -> str:
        return (
            f"AgentCredentialService(credentials={len(self._credentials)}, "
            f"pools={len(self._pools)})"
        )

    # --- clock helpers -----------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _epoch(self) -> float:
        return self._now().timestamp()

    # --- path -------------------------------------------------------------

    def _path(self, agent: AgentRef) -> str:
        return f"{self._config.secret_prefix}/{agent.key}"

    # --- lazy state transitions -------------------------------------------

    def _refresh(self, credential_id: str) -> AgentCredential:
        """Apply the lazy GRACE->EXPIRED transition and return the current metadata."""
        cred = self._credentials[credential_id]
        if (
            cred.state is CredentialState.GRACE
            and cred.grace_until is not None
            and self._now() >= cred.grace_until
        ):
            cred = cred.model_copy(update={"state": CredentialState.EXPIRED})
            self._credentials[credential_id] = cred
            self._park_entry(cred.agent, credential_id)
        return cred

    def _live_for(self, agent: AgentRef) -> list[AgentCredential]:
        live: list[AgentCredential] = []
        for cid, cred in list(self._credentials.items()):
            if cred.agent == agent:
                refreshed = self._refresh(cid)
                if refreshed.state in _LIVE:
                    live.append(refreshed)
        return live

    def _active_for(self, agent: AgentRef) -> AgentCredential | None:
        for cred in self._live_for(agent):
            if cred.state is CredentialState.ACTIVE:
                return cred
        return None

    # --- pool bookkeeping -------------------------------------------------

    def _pool(self, agent: AgentRef) -> CredentialPoolState:
        pool = self._pools.get(agent.key)
        if pool is None:
            pool = CredentialPoolState(entries=[], strategy=self._config.strategy)
            self._pools[agent.key] = pool
        return pool

    def _add_entry(self, agent: AgentRef, credential_id: str) -> None:
        self._pool(agent).entries.append(PoolEntry(credential_id=credential_id))

    def _remove_entry(self, agent: AgentRef, credential_id: str) -> None:
        pool = self._pools.get(agent.key)
        if pool is not None:
            pool.entries = [e for e in pool.entries if e.credential_id != credential_id]

    def _park_entry(self, agent: AgentRef, credential_id: str) -> None:
        pool = self._pools.get(agent.key)
        if pool is not None:
            park(pool, credential_id, self._epoch())

    def _sync_pool_liveness(self, agent: AgentRef) -> None:
        """Park dead entries and order ACTIVE entries ahead of GRACE ones.

        The ACTIVE-before-GRACE sort is now ordering hygiene only — selection
        eligibility is enforced by the ACTIVE-only predicate in
        :meth:`select_credential`, not by this ordering.
        """
        pool = self._pools.get(agent.key)
        if pool is None:
            return
        for entry in list(pool.entries):
            cred = self._credentials.get(entry.credential_id)
            if cred is None:
                continue
            refreshed = self._refresh(entry.credential_id)
            if refreshed.state in _DEAD:
                self._park_entry(agent, entry.credential_id)
        pool.entries.sort(key=lambda e: _STATE_RANK.get(self._state_of(e.credential_id), 2))

    def _state_of(self, credential_id: str) -> CredentialState:
        cred = self._credentials.get(credential_id)
        return cred.state if cred is not None else CredentialState.EXPIRED

    # --- audit ------------------------------------------------------------

    async def _emit(
        self,
        *,
        action: str,
        credential: AgentCredential,
        actor: str,
        reason: str | None = None,
    ) -> None:
        # Event CONSTRUCTION stays OUTSIDE the try so ConfigHashNotFrozenError
        # propagates rather than being swallowed into CredentialAuditError (plan
        # §1.4 / SPEC §4). detail carries NAMES / COUNTS / STATES / a reason class
        # only — never token values.
        detail: dict[str, object] = {
            "agent": credential.agent.key,
            "secret_version": credential.secret_version,
            "scope_tools_count": len(credential.scope.tools),
            "scope_sources_count": len(credential.scope.source_systems),
            "state": credential.state.value,
        }
        if reason:
            detail["reason"] = reason
        event = new_audit_event(
            action=action,
            resource=f"agent-credential:{credential.credential_id}",
            outcome=AuditOutcome.success,
            actor=actor,
            detail=json.dumps(detail),
            provenance=dict(_PROVENANCE),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise CredentialAuditError(
                f"audit append failed for {action!r} on agent-credential:{credential.credential_id}"
            ) from exc

    # --- issue ------------------------------------------------------------

    async def issue(
        self, agent: AgentRef, scope: CredentialScope, *, actor: str
    ) -> AgentCredential:
        """Issue a fresh scoped credential for ``agent`` (secret -> SecretStore)."""
        if self._live_for(agent):
            raise CredentialAlreadyProvisionedError(
                f"a live credential already exists for agent {agent.key!r}; "
                "rotate or revoke before re-issuing"
            )
        token = secrets.token_urlsafe(self._config.secret_bytes)
        path = self._path(agent)
        now = self._now()
        data = {
            "secret": token,
            "scope_tools": ",".join(sorted(scope.tools)),
            "scope_sources": ",".join(sorted(scope.source_systems)),
            "issued_at": now.isoformat(),
        }
        written = await self._store.write(path, data)
        version = written.version
        credential = AgentCredential(
            credential_id=f"{agent.key}/v{version}",
            agent=agent,
            scope=scope,
            secret_path=path,
            secret_version=version,
            issued_at=now,
            state=CredentialState.ACTIVE,
        )
        self._credentials[credential.credential_id] = credential
        self._add_entry(agent, credential.credential_id)
        try:
            await self._emit(action="agent.credential.issue", credential=credential, actor=actor)
        except Exception:
            # Fail closed: an unaudited credential must never be usable. ANY
            # audit-path failure — event construction (e.g. ConfigHashNotFrozenError)
            # or sink append (CredentialAuditError) — destroys the secret version
            # (best effort) and deregisters before re-raising.
            with contextlib.suppress(Exception):
                await self._store.destroy(path, [version])
            self._credentials.pop(credential.credential_id, None)
            self._remove_entry(agent, credential.credential_id)
            raise
        return credential

    # --- rotate -----------------------------------------------------------

    async def rotate(
        self,
        agent: AgentRef,
        *,
        actor: str,
        scope: CredentialScope | None = None,
    ) -> AgentCredential:
        """Rotate ``agent``'s active credential; the old one lingers in GRACE."""
        old = self._active_for(agent)
        if old is None:
            raise CredentialNotFoundError(f"no active credential to rotate for agent {agent.key!r}")
        new_scope = scope if scope is not None else old.scope
        token = secrets.token_urlsafe(self._config.secret_bytes)
        path = self._path(agent)
        now = self._now()
        data = {
            "secret": token,
            "scope_tools": ",".join(sorted(new_scope.tools)),
            "scope_sources": ",".join(sorted(new_scope.source_systems)),
            "issued_at": now.isoformat(),
        }
        written = await self._store.write(path, data)
        version = written.version
        new_cred = AgentCredential(
            credential_id=f"{agent.key}/v{version}",
            agent=agent,
            scope=new_scope,
            secret_path=path,
            secret_version=version,
            issued_at=now,
            state=CredentialState.ACTIVE,
        )
        graced = old.model_copy(
            update={
                "state": CredentialState.GRACE,
                "grace_until": now + timedelta(seconds=self._config.grace_seconds),
            }
        )
        self._credentials[new_cred.credential_id] = new_cred
        self._credentials[old.credential_id] = graced
        self._add_entry(agent, new_cred.credential_id)
        try:
            await self._emit(action="agent.credential.rotate", credential=new_cred, actor=actor)
        except Exception:
            # Unwind the NEW version only on ANY audit-path failure; the old
            # credential reverts to ACTIVE.
            with contextlib.suppress(Exception):
                await self._store.destroy(path, [version])
            self._credentials.pop(new_cred.credential_id, None)
            self._remove_entry(agent, new_cred.credential_id)
            self._credentials[old.credential_id] = old
            raise
        return new_cred

    # --- revoke -----------------------------------------------------------

    async def revoke(self, credential_id: str, *, actor: str, reason: str = "") -> None:
        """Revoke a credential immediately (fail closed, even if the audit fails)."""
        cred = self._credentials.get(credential_id)
        if cred is None:
            raise CredentialNotFoundError(f"no credential registered under {credential_id!r}")
        # Revocation is immediate and stands even if the audit append fails:
        # mark REVOKED + park the pool entry FIRST, then soft-delete the secret.
        revoked = cred.model_copy(update={"state": CredentialState.REVOKED})
        self._credentials[credential_id] = revoked
        self._park_entry(cred.agent, credential_id)
        with contextlib.suppress(Exception):
            await self._store.delete(cred.secret_path, versions=[cred.secret_version])
        await self._emit(
            action="agent.credential.revoke",
            credential=revoked,
            actor=actor,
            reason=reason or None,
        )

    async def revoke_agent(self, agent: AgentRef, *, actor: str) -> None:
        """Revoke every live (ACTIVE or GRACE) credential of ``agent``.

        Fail closed in bulk (C6): an audit failure on ONE credential must not
        leave the agent's other live credentials usable. Each ``revoke`` marks
        its credential REVOKED and parks its pool entry BEFORE the audit append,
        so every live credential is revoked here regardless of audit outcome;
        the FIRST audit failure, if any, is re-raised only after the whole
        sweep completes.
        """
        first_error: Exception | None = None
        for cred in self._live_for(agent):
            try:
                await self.revoke(cred.credential_id, actor=actor, reason="revoke_agent")
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    # --- validation / resolution ------------------------------------------

    def validate(self, credential_id: str) -> AgentCredential:
        """Return the live credential, or fail closed (SYNC — guardrail-safe)."""
        if credential_id not in self._credentials:
            raise CredentialNotFoundError(f"no credential registered under {credential_id!r}")
        cred = self._refresh(credential_id)
        if cred.state is CredentialState.REVOKED:
            raise CredentialRevokedError(f"credential {credential_id!r} is revoked")
        if cred.state is CredentialState.EXPIRED:
            raise CredentialExpiredError(f"credential {credential_id!r} is expired")
        return cred

    def credential_state(self, credential_id: str) -> CredentialState | None:
        """Return the current (lazily-transitioned) state, or None if unknown."""
        if credential_id not in self._credentials:
            return None
        return self._refresh(credential_id).state

    async def resolve_secret(self, credential_id: str) -> SecretStr:
        """Return the secret material wrapped in a ``SecretStr`` (validate first)."""
        cred = self.validate(credential_id)
        version = await self._store.read(cred.secret_path, version=cred.secret_version)
        return SecretStr(version.data["secret"])

    # --- rotation schedule seam (NE-034 cron wires this; wiring out of scope) --

    def rotation_due(self, agent: AgentRef) -> bool:
        """True iff ``agent``'s active credential is past its rotation interval."""
        active = self._active_for(agent)
        if active is None:
            return False
        due_at = active.issued_at.timestamp() + self._config.rotate_after_seconds
        return due_at <= self._epoch()

    def rotation_due_agents(self) -> tuple[AgentRef, ...]:
        """Every agent whose active credential is due for rotation (dedup, stable)."""
        seen: list[AgentRef] = []
        for cred in list(self._credentials.values()):
            if cred.agent not in seen and self.rotation_due(cred.agent):
                seen.append(cred.agent)
        return tuple(seen)

    # --- pool selection ---------------------------------------------------

    def _selectable(self, credential_id: str) -> bool:
        """New selections draw ONLY from ACTIVE credentials.

        GRACE is reserved for in-flight holders (``validate()`` by id), never
        handed to a new dispatch.
        """
        return self._state_of(credential_id) is CredentialState.ACTIVE

    def select_credential(self, agent: AgentRef) -> AgentCredential:
        """Pick a usable ACTIVE credential for a NEW dispatch, or fail closed.

        Selection is ACTIVE-only: a rotated-out GRACE credential is never handed
        to a new dispatch (it may carry the old, broader scope). GRACE credentials
        remain valid only for sessions that already hold them.
        """
        self._sync_pool_liveness(agent)
        pool = self._pools.get(agent.key)
        now = self._epoch()
        msg = (
            f"no ACTIVE credential to select for agent {agent.key!r} "
            "(GRACE is never handed to a new selection; fail closed)"
        )
        if pool is None or not has_usable(pool, now, eligible=self._selectable):
            raise CredentialNotFoundError(msg)
        entry = select_entry(pool, now, eligible=self._selectable)
        if entry is None:
            raise CredentialNotFoundError(msg)
        return self._credentials[entry.credential_id]

    def report_success(self, credential_id: str) -> None:
        """Reset the failure counter + clear cooldown for a credential's pool entry."""
        cred = self._credentials.get(credential_id)
        if cred is None:
            return
        pool = self._pools.get(cred.agent.key)
        if pool is not None:
            report_success(pool, credential_id)

    def report_failure(
        self,
        credential_id: str,
        status: int,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Record a per-failure-class cooldown for a credential's pool entry."""
        cred = self._credentials.get(credential_id)
        if cred is None:
            return
        pool = self._pools.get(cred.agent.key)
        if pool is not None:
            report_failure(
                pool,
                self._config,
                credential_id,
                status,
                self._epoch(),
                retry_after_seconds,
            )

    # --- principal binding ------------------------------------------------

    def bind_principal(
        self, credential_id: str, *, on_behalf_of: Principal | None = None
    ) -> Principal:
        """Mint an agent principal from a LIVE credential (revoked/expired -> raises)."""
        cred = self.validate(credential_id)
        return bind_agent_principal(cred, on_behalf_of=on_behalf_of)
