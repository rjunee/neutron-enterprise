# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped correlation id threaded AS DATA (NE-166, design doc §10.1).

The run-trace seam itself (:func:`current_run_trace_id` / :func:`run_trace_scope`)
now LIVES IN :mod:`ne.events.correlation` and is RE-EXPORTED here UNCHANGED.
``ne-runtime`` sits ABOVE ``ne-govern`` in the dependency graph, so ``ne.govern``'s
``pdp.decide`` audit writer could not import this module without a circular
package dependency — which is why every ``pdp.decide`` event carried a null
``trace_id`` (NE-DEMO-PDP-TRACE). The ONE ContextVar therefore lives in
``ne-events``, which BOTH packages depend on. There is exactly one ContextVar
object, so a value set through either import path is visible through the other,
and this module remains the documented import site for runtime-layer callers.
See :mod:`ne.events.correlation` for the full NE-166 semantics — mint-at-run-root,
inherit-when-nested, opaque 32-hex, no PII, and ABSENCE IS HONEST (a writer that
reads ``None`` stamps ``None``; no writer may invent an id to fill the gap).

The action idempotency scope (NE-ACTION-DETERMINISTIC-REQID) is a second, narrower
contextvar seam that carries the DBOS-stable turn identity for DERIVED write-dedup
keys (NE-050 :mod:`~ne.runtime.action_tools`): it is an OPAQUE caller-supplied token
(the worker's ``turn_id``), never task text, prompt content, or any other PII.

The run principal scope (NE-COST-PRINCIPAL) is a THIRD, distinct contextvar seam
that carries the calling principal's opaque identifier from a governed run root
(``DepartmentAgentSession.run``, ``run_isolated_agent``) down to
:class:`~ne.runtime.model.GatewayModel`, which stamps it onto
``CompletionRequest.principal_id`` so the gateway can attribute the resulting
``CostEvent`` to the actual calling principal instead of its own service
identity. It follows :func:`action_scope`'s semantics (ALWAYS sets — no minting,
innermost wins, reset on exit) rather than :func:`run_trace_scope`'s
mint-or-inherit semantics: a principal is never invented, only ever the VERIFIED
argument a run root already holds. It is a distinct seam from the middleware
``current_tool_principal`` carrier (:mod:`ne.runtime.middleware`) — that carrier's
read surface is deliberately narrow (skill bindings only) and is NOT widened by
this addition.

The requested-model scope (BD-15) is a FOURTH carrier. A governed run root sets
the exact boot-registry alias (or explicitly sets ``None``) for the whole run;
:class:`~ne.runtime.model.GatewayModel` reads it for every provider request,
including retries and post-tool calls. Unlike :func:`run_trace_scope`, it NEVER
inherits implicitly: entering a run with ``None`` deliberately clears any outer
choice so one request cannot bleed its model selection into another.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ne.contracts.types import Lane
from ne.events.correlation import current_run_trace_id as current_run_trace_id
from ne.events.correlation import run_trace_scope as run_trace_scope

__all__ = [
    "ConversationAttemptBinding",
    "action_scope",
    "approval_window_scope",
    "conversation_attempt_scope",
    "current_action_scope",
    "current_approval_window_declared",
    "current_approval_window_s",
    "current_conversation_attempt",
    "current_run_principal_id",
    "current_run_requested_model",
    "current_run_requested_model_lane",
    "current_run_trace_id",
    "current_standing_lifetime_s",
    "run_principal_scope",
    "run_requested_model_scope",
    "run_trace_scope",
    "standing_lifetime_scope",
]

_action_scope: ContextVar[str | None] = ContextVar("ne_action_scope", default=None)
#: The POLICY-resolved approval window for the action being executed, in seconds.
#: Ambient rather than a parameter because ``ApprovalRequest`` is frozen at
#: contracts 1.10.0 and widening ``gate.submit``/``request_approval`` breaks every
#: protocol-typed gate stand-in — including ones monkeypatched onto a REAL
#: DurableApprovalGate, which the isinstance guard cannot protect. ``submit()``
#: already reads two ContextVars from this module; this is the third.
_approval_window_s: ContextVar[float | None] = ContextVar("ne_approval_window_s", default=None)
#: Did the DEPLOYMENT declare that window, or is it a floor the product ships?
#: Bound by the SAME scope, because provenance without its window is meaningless
#: and a window without its provenance is what made the guard refuse parks that
#: succeed on ``main``. Default ``False``: unbound means nothing was declared.
_approval_window_declared: ContextVar[bool] = ContextVar(
    "ne_approval_window_declared", default=False
)

#: The POLICY-resolved STANDING-APPROVAL lifetime for the action being executed,
#: in seconds — how long an approver's decision outlives the attempt it was
#: asked for, once that attempt terminalizes ``authorization_expired``. A
#: DIFFERENT clock from ``_approval_window_s`` (which bounds how long the human
#: has to answer) and carried ambiently for exactly the same reason: the park
#: site is reached through frozen signatures that must not grow a fourth
#: keyword.
_standing_lifetime_s: ContextVar[float | None] = ContextVar("ne_standing_lifetime_s", default=None)
_run_principal_id: ContextVar[str | None] = ContextVar("ne_run_principal_id", default=None)
_run_model_selection: ContextVar[tuple[str | None, Lane | None]] = ContextVar(
    "ne_run_model_selection", default=(None, None)
)


@dataclass(frozen=True)
class ConversationAttemptBinding:
    """Opaque exact ownership generation for one principal Conversation turn."""

    conversation_id: str
    owner_principal_id: str
    department: str
    agent_name: str
    attachment_id: str
    attempt_id: str
    trace_id: str


_conversation_attempt: ContextVar[ConversationAttemptBinding | None] = ContextVar(
    "ne_conversation_attempt", default=None
)


def current_action_scope() -> str | None:
    """The ambient action idempotency scope, or ``None`` outside any scope."""
    return _action_scope.get()


def current_conversation_attempt() -> ConversationAttemptBinding | None:
    """Return the exact active Conversation attempt, if this is a chat turn."""
    return _conversation_attempt.get()


def current_approval_window_s() -> float | None:
    """The POLICY-resolved approval window in seconds, or ``None`` if unset.

    ``None`` means policy said nothing and the deployment default applies — it is
    NEVER read as "expire immediately". Owner requirement, 2026-08-30: approval
    time is a property of the ACTION, declared beside its tier, never a side
    effect of how long a login happens to last.
    """
    return _approval_window_s.get()


def current_approval_window_declared() -> bool:
    """Did the deployment DECLARE the ambient window, or is it a shipped floor?

    ``True`` only when the bound window came from
    ``data.ne.config.approval_windows.actions[<action>]`` or ``.tiers[<tier>]``
    (``PolicyDecision.approval_window_declared``, bundle 1.4.0). ``False`` for
    ``tiers.rego``'s built-in floor, for the ``cfg.expiry_s`` fallback that
    applies when policy named no window at all, and outside any scope.

    Read at exactly one site — ``_require_canonical_run_approval``, which hands
    it to ``_require_honourable_approval_window``. It decides whether an
    unkeepable window is a REFUSAL or a WARNING, and that is the whole reason it
    exists: refusing a floor punishes a decision nobody took.
    """
    return _approval_window_declared.get()


@contextmanager
def approval_window_scope(
    window_s: float | None, *, declared: bool = False
) -> Iterator[float | None]:
    """Bind the policy-resolved approval window, and where it came from.

    Non-positive and non-finite values are normalised to ``None`` HERE as well as
    at the gate: a zero window would expire an approval the instant it is
    created, which is the exact failure this machinery exists to remove, so it
    must not be reachable from either end.

    ``declared`` is normalised WITH the window: a window that normalises away
    cannot have been declared, because there is then no promise to have made.
    The default is ``False``, so any caller that does not pass it — every test
    that enters this scope by hand — binds a FLOOR, which is the side that
    parks. The one production caller (``ne.runtime.middleware``'s executor)
    passes ``decision.approval_window_declared``, so it binds a DECLARED window
    whenever policy declared one; the default exists for the hand-entered
    scopes, not for it.
    """
    normalised = (
        float(window_s)
        if window_s is not None and math.isfinite(window_s) and window_s > 0
        else None
    )
    token = _approval_window_s.set(normalised)
    declared_token = _approval_window_declared.set(bool(declared) and normalised is not None)
    try:
        yield normalised
    finally:
        _approval_window_declared.reset(declared_token)
        _approval_window_s.reset(token)


def current_standing_lifetime_s() -> float | None:
    """The POLICY-resolved standing-approval lifetime in seconds, or ``None``.

    ``None`` means the policy named no standing lifetime, and the ONLY reading
    the runtime gives that is "mint NO standing approval". It is never read as
    "standing forever" — an unbounded carryover of a human decision is the one
    thing this value must not be able to express by omission.
    """
    return _standing_lifetime_s.get()


@contextmanager
def standing_lifetime_scope(lifetime_s: float | None) -> Iterator[float | None]:
    """Bind the policy-resolved standing-approval lifetime for one governed call.

    Non-positive and non-finite values normalise to ``None`` HERE as well as at
    the park site, exactly like :func:`approval_window_scope`: a zero lifetime
    would lapse a decision the instant it was made, so it must not be reachable
    from either end.
    """
    normalised = (
        float(lifetime_s)
        if lifetime_s is not None and math.isfinite(lifetime_s) and lifetime_s > 0
        else None
    )
    token = _standing_lifetime_s.set(normalised)
    try:
        yield normalised
    finally:
        _standing_lifetime_s.reset(token)


@contextmanager
def conversation_attempt_scope(
    binding: ConversationAttemptBinding,
) -> Iterator[ConversationAttemptBinding]:
    """Bind one verified claim epoch for model/tool/approval execution."""
    token = _conversation_attempt.set(binding)
    try:
        yield binding
    finally:
        _conversation_attempt.reset(token)


@contextmanager
def action_scope(scope: str) -> Iterator[str]:
    """Establish the action idempotency scope for the block (ALWAYS sets — no minting).

    Unlike :func:`run_trace_scope`, this never mints a value on the caller's behalf:
    ``scope`` is always the caller-supplied token (the worker's stable ``turn_id``).
    Nested scopes: the INNERMOST call wins for the duration of its block, and the
    prior ambient value (possibly ``None``) is restored on exit — a sub-agent
    dispatched inside a turn simply inherits the ambient scope by not entering a new
    one.
    """
    token = _action_scope.set(scope)
    try:
        yield scope
    finally:
        _action_scope.reset(token)


def current_run_principal_id() -> str | None:
    """The ambient run-scoped calling-principal id, or ``None`` outside any
    run-principal scope (a principal-less/background call)."""
    return _run_principal_id.get()


@contextmanager
def run_principal_scope(principal_id: str) -> Iterator[str]:
    """Establish the run-scoped calling-principal id for the block (ALWAYS sets —
    no minting, mirrors :func:`action_scope`'s semantics, NOT
    :func:`run_trace_scope`'s mint-or-inherit semantics).

    ``principal_id`` must be the VERIFIED ``Principal.id`` a run root already
    holds — never derived from request/message data, never guessed. Set ONLY at
    the governed run roots (``DepartmentAgentSession.run``,
    ``run_isolated_agent``); read by :class:`~ne.runtime.model.GatewayModel` to
    stamp ``CompletionRequest.principal_id``, which the gateway uses to
    attribute the resulting ``CostEvent`` to the calling principal instead of
    its own service identity (NE-COST-PRINCIPAL).

    Nested scopes: the INNERMOST call wins for the duration of its block, and
    the prior ambient value (possibly ``None``) is restored on exit. This is
    NOT the middleware ``current_tool_principal`` carrier — that carrier's read
    surface is deliberately narrower (skill bindings only) and is not widened
    by this seam.
    """
    token = _run_principal_id.set(principal_id)
    try:
        yield principal_id
    finally:
        _run_principal_id.reset(token)


def current_run_requested_model() -> str | None:
    """The exact model alias selected for this run, or ``None`` when the run
    uses its governed task route."""
    return _run_model_selection.get()[0]


def current_run_requested_model_lane() -> Lane | None:
    """The server-resolved lane paired with the selected model, if supplied."""
    return _run_model_selection.get()[1]


@contextmanager
def run_requested_model_scope(
    requested_model: str | None, *, lane: Lane | None = None
) -> Iterator[str | None]:
    """Establish one run's model choice, explicitly including ``None``.

    Every invocation sets an atomic ``(alias, lane)`` pair and resets it on exit.
    In particular, ``None`` does NOT inherit an outer choice: a new run with no
    explicit selection must use its governed route rather than whichever model an
    enclosing run chose. ``lane`` is server-resolved registry data, never inferred
    from the alias; supplying it without an explicit alias is refused so a caller
    cannot override a deployment lane without making a governed model choice.
    Nested explicit scopes restore the prior pair after exit, and ``ContextVar``
    keeps concurrent runs isolated on the same event loop.
    """
    if requested_model is None and lane is not None:
        raise ValueError("a requested model lane requires an explicit requested model")
    token = _run_model_selection.set((requested_model, lane))
    try:
        yield requested_model
    finally:
        _run_model_selection.reset(token)
