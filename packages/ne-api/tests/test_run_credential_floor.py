# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The server-side remaining-lifetime floor at Run admission.

WHAT THIS PINS, AND WHY EACH GUARD CARRIES A MUTATION TRIPLE. A test that passes
for every value of the thing it protects proves nothing, and a floor is
especially easy to test vacuously: assert that a run with an hour of credential
is admitted and the assertion stays green whether the floor is 420, 0, or gone.
So every guard below is exercised three ways -- `control:` the guard intact,
`mutant:` the protected value moved, `red:` the assertion that must fail on the
mutant -- and the mutants move the FLOOR itself, not the credential.

⛔ THE DISCLOSURE GUARD IS NOT WHAT THIS FIXES. `ProtectedSourceResultGuard.
authorization` refusing at `now >= context.expires_at` is correct and is the
last line of defence; `test_the_disclosure_guard_was_not_weakened` exists so a
future attempt to make this symptom go away by loosening THAT comparison reds
here instead of shipping.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from ne.api.agent_execution_caps import enforced_agentic_caps, enforced_wall_clock_seconds
from ne.api.agent_manifest import AgentBudgets
from ne.api.run_credential_floor import (
    RUN_CREDENTIAL_DISPATCH_ALLOWANCE_SECONDS,
    RUN_CREDENTIAL_FLOOR_BOUND_SECONDS,
    run_credential_floor_seconds,
)
from ne.api.run_submission import RunCredentialTooShort, RunSubmissionConflict
from ne.contracts.identity import AuthenticatedRequestContext

from .test_run_submission import (
    AGENT_ID,
    AGENT_VERSION,
    CAPABILITY_HASH,
    CONFIG_HASH,
    OWNER,
    _service,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSOLE_CLIENT = REPO_ROOT / "packages" / "ne-console" / "src" / "api" / "client.ts"
AGENT_REGISTRY = REPO_ROOT / "deploy" / "agents" / "registry.toml"
AGENT_CAPS = REPO_ROOT / "packages" / "ne-api" / "src" / "ne" / "api" / "agent_execution_caps.py"
SOURCE_RESULTS = (
    REPO_ROOT / "packages" / "ne-runtime" / "src" / "ne" / "runtime" / "source_results.py"
)
DEV_REALM = REPO_ROOT / "deploy" / "compose" / "dev" / "keycloak" / "ne-dev-realm.json"


def _budgets(max_steps: int) -> AgentBudgets:
    """A real manifest budget set: only ``max_steps`` reaches the derivation.

    The REAL model, not a stand-in, so `max_steps`'s own validated range
    (`ge=1, le=64`) is what bounds the parametrization below -- a stand-in would
    have let this file assert a floor for a budget the manifest cannot express.
    """

    return AgentBudgets(
        max_steps=max_steps,
        max_tool_calls=8,
        max_input_tokens=4096,
        max_output_tokens=1024,
    )


def _context(*, remaining_s: int) -> AuthenticatedRequestContext:
    """An owner context with exactly ``remaining_s`` of credential left."""

    return AuthenticatedRequestContext(
        principal_id=OWNER.id,
        authorization_groups=tuple(sorted(set(OWNER.groups))),
        department="operations",
        issuer="https://issuer.example.test",
        expires_at=int(time.time()) + remaining_s,
    )


def _start(service: Any, *, remaining_s: int, key: str) -> Any:
    return asyncio.run(
        service.prepare_start(
            principal=OWNER,
            agent_id=AGENT_ID,
            conversation_id="conversation-owned",
            expected_version=0,
            agent_version=AGENT_VERSION,
            strategy="agentic",
            config_hash=CONFIG_HASH,
            capability_hash=CAPABILITY_HASH,
            input_text="PRIVATE accepted Run input",
            idempotency_key=key,
            authenticated_context=_context(remaining_s=remaining_s),
        )
    )


# --- the live shape ---------------------------------------------------------


def test_the_live_three_second_admission_is_now_refused() -> None:
    """The measured defect: a run admitted with ~3 s of credential.

    On the live stack four such runs were ADMITTED with 66, 3, 107 and 21
    seconds of credential and then ran for 121-135 s, dying mid-flight with an
    `AuthorizationExpiredError` that named the run rather than the cause. Each
    of those four is refused here, at admission, before any Run row exists.
    """

    service, transactions, _preflight = _service()

    # control: a credential that comfortably outlasts the agent's 400 s wall
    # clock still admits, so the refusals below are the floor talking and not a
    # harness that refuses everything.
    assert _start(service, remaining_s=3600, key="control").owner_principal_id == OWNER.id

    for observed in (66, 3, 107, 21):
        with pytest.raises(RunCredentialTooShort) as raised:
            _start(service, remaining_s=observed, key=f"live-{observed}")
        assert raised.value.remaining_seconds == pytest.approx(observed, abs=1)
        assert raised.value.required_seconds == 420

    # red: refused AT ADMISSION means no command ever reached the transaction.
    assert transactions.calls == []


def test_the_refusal_says_what_to_do() -> None:
    """Fail loudly: the message must name the remedy, not merely reject."""

    service, _transactions, _preflight = _service()
    with pytest.raises(RunCredentialTooShort) as raised:
        _start(service, remaining_s=3, key="actionable")
    detail = raised.value.detail
    assert "Renew the access token and resubmit." in detail
    assert "420s required" in detail
    assert "terminated mid-flight" in detail


# --- mutation triple: the floor removed -------------------------------------


def test_floor_removed_mutation_triple(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mutant:` delete the floor -> the live 3 s admission comes back.

    Patching the derivation `ne.api.run_submission` imports is exactly the
    "guard removed" mutant: every other check in `_authenticated_context` still
    runs, so anything that still refuses would be refusing for another reason.
    """

    service, transactions, _preflight = _service()

    # control: floor intact, the 3 s credential is refused.
    with pytest.raises(RunCredentialTooShort):
        _start(service, remaining_s=3, key="triple-control")
    assert transactions.calls == []

    # mutant: the floor derivation returns nothing at all.
    monkeypatch.setattr(
        "ne.api.run_submission.run_credential_floor_seconds",
        lambda _unused: 0,
    )
    # red: with the floor gone the doomed run is admitted again -- which is the
    # pre-fix behaviour, and proves the refusal above was the floor's doing.
    command = _start(service, remaining_s=3, key="triple-mutant")
    assert command.authenticated_context is not None


def test_floor_at_zero_still_refuses_an_already_expired_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mutant:` floor 0 -> the ZERO-MARGIN check must still refuse.

    The floor is a deployment judgement; "already expired" is not. Correctness
    must not have been made to depend on the policy number, so with the floor
    mutated to zero an expired credential is still refused -- and as the plain
    `RunSubmissionConflict` it always was, not as the new typed refusal.
    """

    service, transactions, _preflight = _service()
    monkeypatch.setattr(
        "ne.api.run_submission.run_credential_floor_seconds",
        lambda _unused: 0,
    )
    with pytest.raises(RunSubmissionConflict) as raised:
        _start(service, remaining_s=-1, key="expired-at-zero-floor")
    # red: it is NOT the credential-too-short refusal; the older guard caught it.
    assert not isinstance(raised.value, RunCredentialTooShort)
    assert transactions.calls == []


# --- mutation triple: the floor is per-agent, not a constant -----------------


def test_the_floor_tracks_the_agents_own_wall_clock_mutation_triple() -> None:
    """`mutant:` move `max_steps` -> the same credential flips verdict.

    This is the anti-vacuity test. A hardcoded 420 would pass every assertion
    above; it fails here, because a one-step agent enforced at 10 s has no
    business refusing a 300 s credential that a 40-step agent must refuse.
    """

    credential_s = 300

    # control: 40 steps -> 400 s wall clock + 20 s dispatch = 420 s floor.
    wide, wide_transactions, _p1 = _service(agent_max_steps=40)
    with pytest.raises(RunCredentialTooShort) as raised:
        _start(wide, remaining_s=credential_s, key="wide")
    assert raised.value.required_seconds == 420
    assert wide_transactions.calls == []

    # mutant: the same submission against a 1-step agent -> 30 s floor.
    narrow, _t2, _p2 = _service(agent_max_steps=1)
    # red: admitted. A constant floor cannot produce this line.
    assert _start(narrow, remaining_s=credential_s, key="narrow") is not None

    # And the boundary itself moves with the budget, in both directions.
    mid, _t3, _p3 = _service(agent_max_steps=9)
    assert _start(mid, remaining_s=110, key="mid-ok") is not None
    with pytest.raises(RunCredentialTooShort) as raised:
        _start(mid, remaining_s=109, key="mid-refused")
    assert raised.value.required_seconds == 110


def test_equality_admits() -> None:
    """A credential holding EXACTLY the floor covers every permitted instant."""

    service, _transactions, _preflight = _service(agent_max_steps=9)
    assert _start(service, remaining_s=110, key="exactly-the-floor") is not None


# --- the derivation itself --------------------------------------------------


def _seconds_per_step_from_source() -> int:
    """`_SECONDS_PER_STEP`, read out of the caps module's SOURCE.

    Same idiom as `test_approval_window_is_honourable`: read the deployment's
    declared seconds-per-step where it is written rather than restating it here,
    so moving it reds the derivation instead of silently rescaling the floor.
    """

    match = re.search(
        r"^_SECONDS_PER_STEP = (\d+)$", AGENT_CAPS.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match is not None, "_SECONDS_PER_STEP not found in ne.api.agent_execution_caps"
    return int(match.group(1))


@pytest.mark.parametrize("max_steps", [1, 9, 10, 23, 40, 64])
def test_the_floor_is_the_enforced_wall_clock_plus_the_allowance(max_steps: int) -> None:
    """`mutant:` every legal `max_steps` -> the floor tracks the runner's clock.

    ⚠ WHAT THIS DOES **NOT** CATCH, SAID PLAINLY. This is a CONSISTENCY guard:
    both sides of every assertion are derived from the same two numbers, so
    moving `_SECONDS_PER_STEP` (10 -> 5) or the dispatch allowance (20 -> 0)
    moves both sides together and this file's parametrization stays green. It
    is not vacuous, but the value it protects is protected elsewhere: both
    mutations are caught by `test_the_deployment_bound_is_the_registry_maximum`,
    which pins `max(per-agent floor over deploy/agents/registry.toml)` against
    the SHIPPED constant `RUN_CREDENTIAL_FLOOR_BOUND_SECONDS = 420` -- 10 -> 5
    makes that maximum 220 and 20 -> 0 makes it 400, and neither is 420. What
    THIS test owns is that the floor is one derivation over the runner's own
    clock at every legal budget, not a constant wearing a function's name.
    """

    budgets = _budgets(max_steps)
    per_step = _seconds_per_step_from_source()
    assert enforced_wall_clock_seconds(budgets) == float(max_steps * per_step)
    # The clock the floor is built on is the SAME one the caps module serves to
    # the console and the executing adapter -- not a second derivation.
    assert enforced_agentic_caps(budgets, ()).wall_clock_seconds == enforced_wall_clock_seconds(
        budgets
    )
    assert run_credential_floor_seconds(budgets) == (
        max_steps * per_step + RUN_CREDENTIAL_DISPATCH_ALLOWANCE_SECONDS
    )


# --- cross-artifact drift ---------------------------------------------------


def _console_renew_trigger_s() -> int:
    match = re.search(
        r"export const RUN_CREDENTIAL_MIN_REMAINING_SECONDS = (\d+);",
        CONSOLE_CLIENT.read_text(encoding="utf-8"),
    )
    assert match is not None, (
        "RUN_CREDENTIAL_MIN_REMAINING_SECONDS not found in the console client -- "
        "the server floor has lost the constant it is pinned against"
    )
    return int(match.group(1))


def _registry_floors() -> dict[str, int]:
    registry = tomllib.loads(AGENT_REGISTRY.read_text(encoding="utf-8"))
    return {
        str(agent["agent_id"]): run_credential_floor_seconds(
            _budgets(int(agent["budgets"]["max_steps"]))
        )
        for agent in registry["agents"]
    }


def test_the_deployment_bound_is_the_registry_maximum() -> None:
    """`RUN_CREDENTIAL_FLOOR_BOUND_SECONDS` is derived, and reds if it drifts.

    `mutant:` raise any registry agent's `max_steps` past the current maximum
    and the derived value moves while the constant does not -- so the legacy
    chat leg, which uses this bound because it cannot name its own budgets,
    cannot silently become LESS restrictive than the per-agent floor.
    """

    floors = _registry_floors()
    assert floors, f"no agents in {AGENT_REGISTRY} -- the bound cannot be derived"
    assert max(floors.values()) == RUN_CREDENTIAL_FLOOR_BOUND_SECONDS


def test_the_console_renew_trigger_covers_every_agents_floor() -> None:
    """The console must never renew to a token the server then refuses.

    `RUN_CREDENTIAL_MIN_REMAINING_SECONDS` is a browser-side FAIL-OPEN renew
    TRIGGER; it is not and cannot be the authority for a server refusal (it
    binds the console alone, and TypeScript cannot read an agent's budgets). So
    Python owns the derivation and the console is pinned AGAINST it: a trigger
    below some agent's floor is a console that declines to renew a token the
    server will refuse -- an unrecoverable submit loop.

    `mutant:` lower the TypeScript constant, or raise a registry `max_steps`,
    and exactly one side of this inequality moves.
    """

    trigger = _console_renew_trigger_s()
    floors = _registry_floors()
    worst = max(floors, key=lambda agent_id: floors[agent_id])
    assert trigger >= floors[worst], (
        f"the console renews at {trigger}s but {worst} needs {floors[worst]}s at "
        "admission -- a renewed token would still be refused"
    )
    assert trigger >= RUN_CREDENTIAL_FLOOR_BOUND_SECONDS


def test_a_freshly_minted_token_clears_every_reachable_floor() -> None:
    """The realm must be able to mint a credential that satisfies the floor.

    The complementary direction to the console pin: a floor above the realm's
    `accessTokenLifespan` would refuse EVERY run, renewed or not. Checked
    against the structural maximum (`AgentBudgets.max_steps` is validated
    `le=64`), not merely today's registry, so adding a maximally budgeted agent
    cannot walk the deployment into that state unnoticed.
    """

    lifespan = json.loads(DEV_REALM.read_text(encoding="utf-8"))["accessTokenLifespan"]
    assert lifespan >= run_credential_floor_seconds(_budgets(64))


def test_the_disclosure_guard_was_not_weakened() -> None:
    """`ProtectedSourceResultGuard.authorization` still refuses at `>=`.

    The admission floor exists so fewer runs reach that guard's refusal, NOT so
    the refusal happens less often for the runs that do. Loosening the
    comparison -- to `>`, to a leeway, to a snapshot re-read -- would make this
    symptom disappear by disclosing past a verified expiry, so the exact
    comparison is pinned in the source it lives in.
    """

    source = SOURCE_RESULTS.read_text(encoding="utf-8")
    assert "if now.timestamp() >= context.expires_at:" in source, (
        "the protected-disclosure expiry comparison moved -- the admission floor "
        "is not a licence to weaken the last line of defence"
    )


# --- the second admission leg, and what the floor deliberately does NOT touch -


def test_the_legacy_chat_admission_leg_is_floored_too() -> None:
    """`chat._protect_legacy_input` seals input for a DURABLE turn workflow.

    That workflow makes protected disclosures minutes later against this same
    frozen `expires_at`, so it is an admission and it is floored -- otherwise
    "the console protects itself and nothing else is protected" would still be
    true of every non-canonical chat agent.

    It uses the deployment BOUND rather than a per-agent floor because a
    pre-010G `agent_name` is a chat adapter with no manifest to read; the bound
    is the fail-closed choice.

    ⭐ AND IT BITES ON THE BOUND'S OWN VALUE. Refusing the measured 3 s
    credential says nothing about 420: it stays green at a bound of 60, or 6.
    So the leg is also driven one second BELOW the worst floor any registry
    agent could have demanded, which is the invariant the bound exists for --
    "never LESS restrictive than the per-agent floor would have been".
    `mutant:` bound 420 -> 60 and that credential is admitted. `red:` the
    second refusal below.
    """

    from types import SimpleNamespace

    from fastapi import HTTPException

    from ne.api.deps import ApiState
    from ne.api.routers.chat import _protect_legacy_input
    from ne.api.turn_wire import PROTECTED_TRANSPORT_GENERATION
    from ne.runtime.source_results import authenticated_request_scope

    class _Store:
        def __init__(self) -> None:
            self.claims = 0

        async def claim_protected_payload(self, **_kwargs: Any) -> Any:
            self.claims += 1
            return SimpleNamespace(row_id="row-1", plaintext_hash="a" * 64)

    def _call(store: _Store, *, remaining_s: int) -> Any:
        state = cast(
            ApiState,
            SimpleNamespace(
                agent_transport_generation=PROTECTED_TRANSPORT_GENERATION,
                agent_run_store=store,
            ),
        )
        with authenticated_request_scope(_context(remaining_s=remaining_s)):
            return asyncio.run(
                _protect_legacy_input(
                    state=state,
                    principal=OWNER,
                    conversation_id="conv-1",
                    attachment_id="turn-1",
                    attempt_id="attempt-1",
                    prompt="PRIVATE legacy turn input",
                )
            )

    # control: a credential that clears the bound seals the input as before.
    store = _Store()
    ref, context = _call(store, remaining_s=3600)
    assert ref.protected_ref == "row-1"
    assert context.principal_id == OWNER.id
    assert store.claims == 1

    # mutant: the live 3 s credential.
    refused = _Store()
    with pytest.raises(HTTPException) as raised:
        _call(refused, remaining_s=3)
    # red: 401 with the remedy, and NOTHING was sealed -- refused before the
    # payload crossed the process boundary, not after.
    assert raised.value.status_code == 401
    assert "Renew the access token and resubmit." in str(raised.value.detail)
    assert f"{RUN_CREDENTIAL_FLOOR_BOUND_SECONDS}s required" in str(raised.value.detail)
    assert refused.claims == 0

    # mutant: one second under the WORST per-agent floor in the deployed
    # registry -- a credential a real Agent Run would have been refused for.
    worst = max(_registry_floors().values())
    short = _Store()
    with pytest.raises(HTTPException) as under:
        _call(short, remaining_s=worst - 1)
    # red: the bound is not allowed to be more permissive than the per-agent
    # floor it stands in for. This is what reds when the bound is lowered.
    assert under.value.status_code == 401
    assert short.claims == 0


def test_cancel_is_deliberately_not_floored() -> None:
    """Blast radius: a Run can still be CANCELLED on a dying credential.

    Cancelling is the one control a caller with seconds left genuinely needs,
    it does not freeze a context into anything, and it cannot outlive itself.
    Flooring it would turn "your token is nearly out" into "and you may not
    stop the run that is burning it". `cancel` never calls
    `_authenticated_context`, and this pins that it stays that way.
    """

    from ne.runtime.source_results import authenticated_request_scope

    service, transactions, _preflight = _service()
    with authenticated_request_scope(_context(remaining_s=3)):
        accepted = asyncio.run(
            service.cancel(
                principal=OWNER,
                agent_id=AGENT_ID,
                run_id="run-owned",
                expected_version=1,
                agent_version=AGENT_VERSION,
                strategy="agentic",
                config_hash=CONFIG_HASH,
                capability_hash=CAPABILITY_HASH,
                idempotency_key="cancel-on-a-dying-credential",
            )
        )
    assert accepted.status == "cancelling"
    assert len(transactions.calls) == 1


# --- the caller-facing mapping, which is the point of the whole change -------


_START_BODY = {
    "conversation_id": "conversation-owned",
    "expected_version": 0,
    "agent_version": AGENT_VERSION,
    "strategy": "agentic",
    "config_hash": CONFIG_HASH,
    "capability_snapshot_hash": CAPABILITY_HASH,
    "input": "PRIVATE run input",
}


def _runs_api(exc: BaseException) -> Any:
    """A `POST /v1/agents/{id}/runs` whose submission raises exactly ``exc``.

    The ROUTE -- its `except` arms and their order -- is the artifact under
    test here, so the service is a stub. Which exception the real service
    raises for a short credential is pinned above, against the real service.
    """

    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ne.api.deps import api_state, current_principal
    from ne.api.routers import runs as runs_router

    class _Raises:
        async def start(self, **_kwargs: Any) -> Any:
            raise exc

    app = FastAPI()
    app.include_router(runs_router.router)
    app.dependency_overrides[api_state] = lambda: SimpleNamespace(
        agent_run_store=object(), agent_run_enqueuer=object()
    )
    app.dependency_overrides[current_principal] = lambda: OWNER
    app.dependency_overrides[runs_router.run_submission_service] = _Raises
    return TestClient(app)


def _start_run(exc: BaseException) -> Any:
    return _runs_api(exc).post(
        f"/v1/agents/{AGENT_ID}/runs",
        json=_START_BODY,
        headers={"Idempotency-Key": "start-once"},
    )


def test_the_runs_api_answers_401_with_the_remedy_not_the_opaque_409() -> None:
    """⛔ `POST /v1/agents/{id}/runs` IS the customer API integration path.

    It is the path this whole change is justified by, so its caller-facing
    answer is pinned rather than left to the `except` arms' current order.
    A scripted client that gets the generic 409 learns nothing it can act on --
    it cannot tell "renew your token" from "this Run is already cancelling" --
    and retries the same dead credential, which is exactly the behaviour the
    401 exists to replace.

    `mutant:` replace the `except RunCredentialTooShort` arm with the opaque
    `HTTPException(409, "conflict")`, or merely move it BELOW the
    `RunSubmissionConflict` arm it subclasses (a reorder is the likelier
    accident, and has the identical effect) -> the status is 409 and the
    remedy is gone. `red:` every assertion in the first block.
    """

    refused = _start_run(
        RunCredentialTooShort(
            remaining_seconds=3,
            required_seconds=RUN_CREDENTIAL_FLOOR_BOUND_SECONDS,
        )
    )

    assert refused.status_code == 401
    detail = refused.json()["detail"]
    assert "3s remaining" in detail
    assert f"{RUN_CREDENTIAL_FLOOR_BOUND_SECONDS}s required" in detail
    assert "Renew the access token and resubmit." in detail
    # The refusal names a credential; it must not be cacheable.
    assert refused.headers["Cache-Control"] == "private, no-store"

    # control: the arm it is ordered IN FRONT OF still answers the opaque 409.
    # Without this the 401 above would also be satisfied by a blanket remapping
    # of every conflict to 401, which would be a different (and wrong) change.
    conflict = _start_run(RunSubmissionConflict("idempotency key reused"))
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "request conflicts with current Run state"
    # ⛔ And the opaque 409 stays opaque: a conflict must never leak the
    # submission's own message to the caller.
    assert "idempotency key reused" not in conflict.text
