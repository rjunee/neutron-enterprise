# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin httpx client for the OPA sidecar's v1 Data API.

This module is DELIBERATELY not a :class:`~ne.contracts.governance.PolicyDecisionPoint`.
:class:`OpaClient` speaks OPA's HTTP wire protocol and returns a private
:class:`_RawOpaResult` — it CANNOT mint a :class:`~ne.contracts.governance.PolicyDecision`
and is NOT exported from :mod:`ne.govern`. The only path to a ``PolicyDecision``
is :class:`ne.govern.pdp.GovernedPdp`, which couples every decision to an audit
append (C6): no ne-govern code can produce an authorization answer without the
sink. Undefined OPA results and every transport/shape failure fail CLOSED as a
:class:`~ne.govern.errors.PolicyBackendError`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from ne.govern.errors import PolicyBackendError

_LOG = logging.getLogger(__name__)

_VALID_EFFECTS = frozenset({"allow", "deny", "require-approval"})

#: Total POST attempts per :meth:`OpaClient.evaluate` when the request WAS
#: written to OPA and no answer came back — i.e. :class:`httpx.ReadTimeout`, and
#: ONLY that. The decision query is a read-only, idempotent policy evaluation
#: (measured ~1ms server-side), so re-asking cannot double-apply anything; every
#: measured production timeout was the CLIENT process stalling
#: (event-loop/GIL saturation in ne-api), not OPA, and an immediate re-ask
#: succeeded.
#:
#: :class:`httpx.TimeoutException` has THREE other subclasses in httpx 0.28.1
#: and none of them is re-asked, because none of them is that transient stall:
#:
#: * ``ConnectTimeout`` — a blackholed SYN (NetworkPolicy DROP, a wedged
#:   sidecar, iptables ``DROP`` rather than ``REJECT``). That is a total
#:   outage, exactly the condition this retry is scoped to exclude; re-asking
#:   only doubles time-to-refuse from one timeout budget to two.
#: * ``PoolTimeout`` — the connection pool was exhausted, so the request was
#:   NEVER SENT. Re-entering the same exhausted pool adds load during precisely
#:   the saturation that produced it.
#: * ``WriteTimeout`` — the request could not even be written out; the socket is
#:   already wedged, and this shape has not been observed in production here.
#:
#: An HTTP error, a connect refusal, an undefined result, and a deny are real
#: answers (or real refusals) and are likewise never re-asked. EVERY non-retried
#: shape, and an exhausted retry, still fails CLOSED as PolicyBackendError.
_TIMEOUT_ATTEMPTS = 2


@dataclass(frozen=True)
class _RawOpaResult:
    """The raw decision document OPA returned — NOT a ``PolicyDecision``.

    Intentionally a private, unexported dataclass so no caller can mistake it for
    the frozen contract type. ``GovernedPdp`` is the sole translator to a
    ``PolicyDecision``, and only after the audit append succeeds.
    """

    effect: str
    policy_version: str
    reason: str
    tier: int | None
    approval_window_seconds: float | None = None
    approval_window_declared: bool = False
    standing_approval_seconds: float | None = None
    obligations: dict[str, object] = field(default_factory=dict)


class OpaClient:
    """HTTP client for the OPA sidecar v1 Data API. NOT a PolicyDecisionPoint.

    Owns an :class:`httpx.AsyncClient` bound to ``base_url`` unless one is
    injected (the ne-brain ``TEIReranker`` ownership pattern); :meth:`aclose`
    closes only an owned client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        decision_path: str = "ne/govern/decision",
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._decision_path = decision_path.strip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def evaluate(self, input_doc: dict[str, object]) -> _RawOpaResult:
        """POST the input to OPA's data API and parse the decision document.

        Raises :class:`PolicyBackendError` (fail closed) on any transport error,
        a non-200 status, an OPA ``undefined`` result (no ``result`` key), or a
        decision document that violates the frozen contract. A
        :class:`httpx.ReadTimeout` — the request went out and no answer came
        back — is re-asked up to :data:`_TIMEOUT_ATTEMPTS` times total before
        failing closed; no other transport failure is re-asked (see
        :data:`_TIMEOUT_ATTEMPTS` for why each one is excluded).
        """
        path = f"/v1/data/{self._decision_path}"
        response: httpx.Response | None = None
        for attempt in range(1, _TIMEOUT_ATTEMPTS + 1):
            try:
                response = await self._client.post(path, json={"input": input_doc})
            except httpx.ReadTimeout as exc:
                # The request went out and no answer came back — the ONE
                # re-asked shape (see _TIMEOUT_ATTEMPTS). str(exc) is usually
                # empty for httpx timeouts, so name the class or the audit
                # reason says nothing.
                if attempt == _TIMEOUT_ATTEMPTS:
                    raise PolicyBackendError(
                        f"OPA request to {path} failed after {attempt} attempts: "
                        f"{type(exc).__name__}({exc})"
                    ) from exc
                # A swallowed attempt that then succeeds produces ONE clean
                # success row, and correctly so — a timed-out non-answer is not
                # a decision and does not belong in the ledger. That makes this
                # line the only record that the PDP failed to answer first time.
                # Without it, worsening saturation (every first attempt timing
                # out, every second succeeding) reads as 100% clean decisions at
                # 2x PDP load, with no leading indicator until it fails hard.
                _LOG.warning(
                    "OPA request to %s timed out on attempt %d/%d (%s); re-asking",
                    path,
                    attempt,
                    _TIMEOUT_ATTEMPTS,
                    type(exc).__name__,
                )
                continue
            except httpx.HTTPError as exc:
                raise PolicyBackendError(
                    f"OPA request to {path} failed: {type(exc).__name__}({exc})"
                ) from exc
            break
        if response is None:
            # Unreachable while _TIMEOUT_ATTEMPTS >= 1 (the loop breaks or
            # raises). NOT an assert: asserts vanish under PYTHONOPTIMIZE, and
            # the AttributeError that would follow is not a PolicyBackendError,
            # so GovernedPdp.decide would let it escape with NO audit event.
            raise PolicyBackendError(
                f"OPA request to {path} was never attempted (_TIMEOUT_ATTEMPTS={_TIMEOUT_ATTEMPTS})"
            )
        if response.status_code != 200:
            raise PolicyBackendError(
                f"OPA returned HTTP {response.status_code} for {path}: {response.text[:200]!r}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PolicyBackendError(f"OPA response for {path} was not JSON: {exc}") from exc
        if not isinstance(body, dict) or "result" not in body:
            # OPA omits "result" when the decision document is undefined — a
            # broken or missing bundle. Fail closed: never allow on undefined.
            raise PolicyBackendError(
                f"OPA result undefined for {path} (missing 'result' — bundle not loaded?)"
            )
        return _parse_result(body["result"])

    async def aclose(self) -> None:
        """Close the owned client. A no-op when the client was injected."""
        if self._owns_client:
            await self._client.aclose()


def _parse_result(result: object) -> _RawOpaResult:
    """Validate OPA's decision document against the frozen contract; fail loud."""
    if not isinstance(result, dict):
        raise PolicyBackendError(f"OPA decision is not an object: {result!r}")
    effect = result.get("effect")
    if not isinstance(effect, str) or effect not in _VALID_EFFECTS:
        raise PolicyBackendError(f"OPA decision effect not in {sorted(_VALID_EFFECTS)}: {effect!r}")
    policy_version = result.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        raise PolicyBackendError(f"OPA decision missing/empty policy_version: {policy_version!r}")
    reason = result.get("reason", "")
    if not isinstance(reason, str):
        raise PolicyBackendError(f"OPA decision reason is not a string: {reason!r}")
    tier = result.get("tier")
    if tier is not None and (not isinstance(tier, int) or isinstance(tier, bool)):
        raise PolicyBackendError(f"OPA decision tier is not an int|null: {tier!r}")
    obligations = result.get("obligations", {})
    if not isinstance(obligations, dict):
        raise PolicyBackendError(f"OPA decision obligations is not an object: {obligations!r}")
    # ADR-0030 (bundle 1.3.0): the per-action approval window rides FIRST-CLASS
    # on the decision — number on require-approval rules, null elsewhere, absent
    # from a pre-1.3.0 bundle (absent and null both mean "policy named no
    # window; the deployment default applies"). bool is rejected exactly like
    # tier's guard: isinstance(True, int) is True, and a True coerced to 1.0
    # would mint a one-second window.
    window = result.get("approval_window_seconds")
    if window is not None and (isinstance(window, bool) or not isinstance(window, int | float)):
        raise PolicyBackendError(
            f"OPA decision approval_window_seconds is not a number|null: {window!r}"
        )
    # Bundle 1.4.0: the window's PROVENANCE rides beside it — true only when the
    # deployment DECLARED the number (`approval_windows.actions/.tiers`), false
    # for `tiers.rego`'s built-in floor. Absent from a pre-1.4.0 bundle, which
    # reads as "not declared": the conservative answer, since the one consumer
    # (the runtime's park-time coherence check) refuses on true and merely warns
    # on false — so an old bundle degrades to `main`'s behaviour rather than to
    # refusing parks it cannot justify. Strictly boolean, never coerced: a
    # string "false" is truthy in Python and would invert the check.
    declared = result.get("approval_window_declared", False)
    if not isinstance(declared, bool):
        raise PolicyBackendError(
            f"OPA decision approval_window_declared is not a bool: {declared!r}"
        )
    # Bundle 1.5.0 (F1): the STANDING-approval lifetime rides beside the window
    # on the same require-approval rules — how long the decision outlives the
    # attempt it was asked for, once that attempt terminalizes
    # ``authorization_expired``. Same shape, same guards, same absent==null
    # reading; a pre-1.5.0 bundle simply omits the key and the runtime mints no
    # standing approval, which is the fail-safe direction.
    standing = result.get("standing_approval_seconds")
    if standing is not None and (
        isinstance(standing, bool) or not isinstance(standing, int | float)
    ):
        raise PolicyBackendError(
            f"OPA decision standing_approval_seconds is not a number|null: {standing!r}"
        )
    return _RawOpaResult(
        effect=effect,
        policy_version=policy_version,
        reason=reason,
        tier=tier,
        approval_window_seconds=float(window) if window is not None else None,
        # A declaration with no window is incoherent — nothing in the bundle can
        # produce it — so it is normalised away here rather than carried into a
        # refusal decision the window itself cannot justify.
        approval_window_declared=declared and window is not None,
        standing_approval_seconds=float(standing) if standing is not None else None,
        obligations=dict(obligations),
    )
