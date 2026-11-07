# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Spend-cap guardrail (C12) — per-principal / per-department USD ceilings.

Checked BEFORE a model/tool call: if any applicable cap would be exceeded by the
call's estimated cost, the call is blocked. All arithmetic is
:class:`~decimal.Decimal` (parsed from strings) so the config hash stays
float-free and the comparison is exact. A hijacked agent cannot evade a cap by
omitting the estimate: when a cap applies and no parseable estimate is present,
the call is blocked (``spend.missing-estimate``).

A malformed or non-finite cap string is rejected at
:class:`~ne.govern.config.SpendCapSettings` construction (``GuardrailConfigError``,
naming the exact key) — never silently treated as "no cap"; ``_parse_cap`` raises
as a defense-in-depth backstop if one still reaches check time, which the engine
converts to a fail-closed, audited block.

The guardrail is a side-effect-free predicate — it READS recorded spend but never
records. The pessimistic pre-charge on the all-pass path lives in
:class:`~ne.govern.guardrails.engine.GuardrailEngine`.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.config import SpendCapSettings
from ne.govern.guardrails._common import failed, get_principal, passed


class SpendLedger(ABC):
    """Records cumulative spend per scope key (``principal:<id>`` / ``department:<n>``)."""

    @abstractmethod
    def recorded_spend(self, scope: str) -> Decimal:
        """Return the cumulative recorded spend for ``scope`` (0 if unseen)."""

    @abstractmethod
    def record(self, scope: str, amount: Decimal) -> None:
        """Add ``amount`` to the recorded spend for ``scope``."""

    @abstractmethod
    def charge(self, charges: Sequence[tuple[str, Decimal, Decimal]]) -> bool:
        """ATOMICALLY charge every ``(scope, amount, cap)`` triple iff each stays in cap.

        The authoritative, atomic reservation the engine's all-pass path calls
        (NE-070H): the check-limit-then-charge TOCTOU is closed by making the charge
        itself conditional. ALL-OR-NOTHING — if ANY scope would exceed its cap, NO
        scope is charged and ``False`` is returned (the caller audits + denies).
        Under K concurrent chargers a cap of C can never be overshot: the loser is
        the one whose conditional charge finds ``total + amount > cap``.
        """


class InMemorySpendLedger(SpendLedger):
    """A thread-safe in-memory :class:`SpendLedger` (process-local; test/dev default)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spend: dict[str, Decimal] = {}

    def recorded_spend(self, scope: str) -> Decimal:
        with self._lock:
            return self._spend.get(scope, Decimal(0))

    def record(self, scope: str, amount: Decimal) -> None:
        with self._lock:
            self._spend[scope] = self._spend.get(scope, Decimal(0)) + amount

    def charge(self, charges: Sequence[tuple[str, Decimal, Decimal]]) -> bool:
        # Atomic all-or-nothing under the one lock: check EVERY scope stays within
        # cap, then apply EVERY charge — so concurrent chargers cannot both pass the
        # check and overshoot (the TOCTOU the durable ledger closes on Postgres).
        with self._lock:
            for scope, amount, cap in charges:
                if self._spend.get(scope, Decimal(0)) + amount > cap:
                    return False
            for scope, amount, _cap in charges:
                self._spend[scope] = self._spend.get(scope, Decimal(0)) + amount
            return True


def _parse_cap(raw: str | None) -> Decimal | None:
    """Parse a cap string to Decimal. ``None`` means 'no cap for this key' (absent,
    legitimately uncapped) and returns ``None``. A malformed or non-finite cap
    string should never reach here — :class:`~ne.govern.config.SpendCapSettings`
    rejects both at construction (``GuardrailConfigError``, naming the bad key).
    This is defense-in-depth: if one still slips through (e.g. a settings dict
    mutated after construction), RAISE rather than silently treat the scope as
    uncapped. `GuardrailEngine.enforce` already converts ANY exception raised by
    a guardrail's `check()` into a denied, audited `guardrail.error` outcome
    (C12 fail closed) — no engine change is needed for this to fail closed.
    """
    if raw is None:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"malformed spend cap value {raw!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite spend cap value {raw!r} (NaN/Infinity is not a valid cap)")
    return parsed


def applicable_scopes(
    settings: SpendCapSettings, principal_id: str, department: str | None
) -> list[tuple[str, Decimal]]:
    """Resolve the ``(scope_key, cap)`` pairs that apply to this principal.

    Principal cap: an explicit ``principal_caps_usd`` entry, else the
    ``default_cap_usd`` fallback. Department cap: an explicit
    ``department_caps_usd`` entry for the principal's declared department.
    """
    scopes: list[tuple[str, Decimal]] = []
    principal_cap = _parse_cap(
        settings.principal_caps_usd.get(principal_id, settings.default_cap_usd)
    )
    if principal_cap is not None:
        scopes.append((f"principal:{principal_id}", principal_cap))
    if department is not None:
        dept_cap = _parse_cap(settings.department_caps_usd.get(department))
        if dept_cap is not None:
            scopes.append((f"department:{department}", dept_cap))
    return scopes


class SpendCapGuardrail:
    """Blocks a call that would breach an applicable per-principal/department cap."""

    def __init__(self, settings: SpendCapSettings, ledger: SpendLedger) -> None:
        self._settings = settings
        self._ledger = ledger

    def charge_plan(self, payload: Mapping[str, Any]) -> list[tuple[str, Decimal, Decimal]]:
        """Return the ``(scope, estimate, cap)`` triples the engine ATOMICALLY charges.

        Called only on the all-pass path, where the estimate is guaranteed
        parseable and non-negative (``check`` already verified it). Empty when no
        cap applies. The engine passes these straight to
        :meth:`SpendLedger.charge`, whose conditional charge is the atomic
        authoritative reservation (NE-070H): the cap is enforced by the charge
        itself, not a separate check, so concurrent chargers can't overshoot.
        """
        principal = get_principal(payload)
        if principal is None:
            return []
        estimate = _parse_estimate(payload.get("estimated_cost_usd"))
        if estimate is None:
            return []
        department = principal.claims.get("department")
        return [
            (scope, estimate, cap)
            for scope, cap in applicable_scopes(self._settings, principal.id, department)
        ]

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        principal = get_principal(payload)
        if principal is None:
            return failed("spend.missing-principal", "no principal in payload")

        department = principal.claims.get("department")
        scopes = applicable_scopes(self._settings, principal.id, department)
        if not scopes:
            return passed("spend.no-cap")

        estimate = _parse_estimate(payload.get("estimated_cost_usd"))
        if estimate is None:
            # An agent must not evade the cap by omitting the estimate.
            return failed("spend.missing-estimate", "cap applies but estimate missing/unparseable")
        if estimate.is_nan() or estimate < 0:
            return failed("spend.invalid-estimate", "estimate is NaN or negative")

        for scope, cap in scopes:
            if self._ledger.recorded_spend(scope) + estimate > cap:
                return failed("spend.cap-exceeded", f"scope {scope.split(':', 1)[0]} cap exceeded")
        return passed("spend.within-cap")


def _parse_estimate(raw: object) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, str):
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            return None
    if isinstance(raw, int):
        return Decimal(raw)
    # Floats are rejected as unparseable: monetary estimates must be exact
    # (string/Decimal), never a binary float.
    return None
