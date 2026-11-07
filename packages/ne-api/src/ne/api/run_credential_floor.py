# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""How much credential life a Run must still hold to be ADMITTED at all.

THE DEFECT THIS CLOSES. Run admission freezes the submitting bearer's ``exp``
into ``ne_agent_run_authenticated_contexts`` (``ne.runtime.run_store``), and the
worker re-checks that FROZEN value against wall clock before every protected
disclosure (``ProtectedSourceResultGuard.authorization``). A Run admitted with N
seconds of credential left therefore survives only if it finishes inside N.
Both admission sites checked ``expires_at <= now`` -- ZERO margin -- so a Run
with three seconds of credential was admitted, ran for two minutes, and died
mid-flight with an ``AuthorizationExpiredError`` that named the run, not the
cause. Measured on the live stack across 40 runs: 5 died that way (12.5%), and
the separation was perfect -- every completed run beat its frozen ``exp``, every
failed one did not.

WHY A FLOOR AT ADMISSION IS THE RIGHT SHAPE, AND WHAT IT IS NOT. It is not a
fix for the mid-flight guard: that guard is correct and stays exactly as it is,
because a credential really can expire under a Run and a disclosure really must
stop. What the floor removes is the PROVABLY DOOMED admission -- the Run whose
credential cannot outlast the wall clock the runner will hold it to even if
execution begins instantly. Those runs are refused up front, with a refusal that
names the remedy, instead of being accepted and killed two minutes later.

⛔ IT DOES NOT ELIMINATE ``AuthorizationExpiredError``. A slow queue, a parked
approval or a clock moving under a long attempt can still exhaust a credential
that cleared this floor. The floor moves the common failure earlier and makes it
explicable; it does not make the class impossible, and nothing here should be
read as claiming it does.

THE NUMBER IS DERIVED, NOT PICKED. The floor for a Run is the wall clock its own
agent's Runs are ENFORCED at -- ``max_steps * _SECONDS_PER_STEP`` from
``ne.api.agent_execution_caps``, the point ``ne.runtime.runner`` hard-cancels the
attempt -- plus one dispatch allowance. That is deliberately the repo's granted
PERMISSION rather than an observed sample, following the same doctrine as
``test_approval_window_is_honourable``: a measurement of one run on one box
bounds nothing, whereas a ceiling a counter is actually compared against does.
Per-agent rather than global matters: a one-step deterministic profile is
enforced at 10 s and has no business being refused because a 40-step analyst
needs 400 s.

⚠ THE ONE FREE PARAMETER IS THE DISPATCH ALLOWANCE, and it is called out rather
than buried. Nothing in this repo bounds the interval between admission and the
start of execution -- the outbox insert, the DBOS enqueue and the worker pickup
have no declared deadline between them -- so the allowance is a deployment
judgement, not a derivation. 20 s is one SSE keep-alive period
(``ApiSettings.sse_keepalive_s`` is 15 s) plus slack, and it is small next to the
400 s it sits on for the largest agent in the registry. It is also exactly the
residual that makes the deployment bound below reproduce the 420 s the console
has already been shipping, so this change EXPLAINS the deployed number instead
of replacing it with a differently-arbitrary one.

⭐ WHY THE CONSOLE'S CONSTANT IS NOT THE SOURCE OF TRUTH, AND HOW THEY CANNOT
DRIFT. ``RUN_CREDENTIAL_MIN_REMAINING_SECONDS`` in
``packages/ne-console/src/api/client.ts`` is a browser-side FAIL-OPEN RENEW
TRIGGER: it returns early when no renew hook is installed, swallows a failed
renew, and binds the console alone -- a scripted client or a customer API
integration never passes through it. It cannot be the authority for a server
refusal, and TypeScript cannot see an agent's budgets anyway. So Python owns the
derivation and the console's number is pinned AGAINST it: the console trigger
must be at least :data:`RUN_CREDENTIAL_FLOOR_BOUND_SECONDS`, or a token the
console declines to renew would be one the server refuses -- an unrecoverable
console. ``ne-api/tests/test_run_credential_floor.py`` reads the TypeScript
constant out of its source (the idiom already used by
``ne-identity/tests/test_realm_config.py`` and
``test_approval_window_is_honourable``) and reds on either side moving alone.
"""

from __future__ import annotations

from ne.api.agent_execution_caps import enforced_wall_clock_seconds
from ne.api.agent_manifest import AgentBudgets

__all__ = [
    "RUN_CREDENTIAL_DISPATCH_ALLOWANCE_SECONDS",
    "RUN_CREDENTIAL_FLOOR_BOUND_SECONDS",
    "credential_too_short_detail",
    "run_credential_floor_seconds",
]

#: Seconds allowed between a Run's admission and the first instant of its
#: execution. See the module docstring: this is the one number here that is
#: declared rather than derived.
RUN_CREDENTIAL_DISPATCH_ALLOWANCE_SECONDS = 20

#: The floor used by an admission that cannot name the budgets it will run
#: under -- today the pre-010G legacy chat turn, whose ``agent_name`` is a chat
#: adapter rather than a registry agent with a manifest. It is the bound over
#: the deployed registry, so it is never LESS restrictive than the per-agent
#: floor would have been, which is the fail-closed direction. Pinned against
#: ``deploy/agents/registry.toml`` and against the console trigger by
#: ``ne-api/tests/test_run_credential_floor.py``.
RUN_CREDENTIAL_FLOOR_BOUND_SECONDS = 420


def run_credential_floor_seconds(budgets: AgentBudgets) -> int:
    """Seconds of credential a Run under ``budgets`` must still hold to admit.

    The enforced attempt wall clock plus the dispatch allowance. Returned as a
    whole number of seconds because the frozen ``expires_at`` it is compared
    against is a whole-second UNIX timestamp; rounding to anything finer would
    be precision the comparison cannot carry.
    """

    return int(enforced_wall_clock_seconds(budgets)) + RUN_CREDENTIAL_DISPATCH_ALLOWANCE_SECONDS


def credential_too_short_detail(*, remaining_seconds: int, required_seconds: int) -> str:
    """The refusal text: what happened, why, and what the caller should do.

    ⛔ Never includes any part of the credential itself. ``remaining_seconds`` is
    derived from the ``exp`` the caller already holds in its own token, so
    returning it discloses nothing the caller did not send, and withholding it
    would leave the refusal unactionable -- the caller could not tell a token it
    should renew from one the server will never accept.
    """

    return (
        "run credential expires too soon to admit this run: "
        f"{remaining_seconds}s remaining, {required_seconds}s required "
        "(the run's enforced wall clock plus dispatch allowance). "
        "The run would be terminated mid-flight when the credential expires. "
        "Renew the access token and resubmit."
    )
