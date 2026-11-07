# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The SHIPPED approval window must be one this deployment can actually honour.

⭐ THE DOCUMENT IS THE FIX; THE GUARD IS A TRIPWIRE ON IT. Declaring 180 s makes
the durable row's `expires_at` 180 s, so a slow approval dies as a VISIBLE
approval expiry instead of a four-hour promise stranding as
`authorization_expired` at minute fifteen — that is what removes the measured
12.7% failure. `_require_honourable_approval_window` (ne.runtime.middleware) then
refuses a canonical Run park whose DECLARED window outruns the run owner's frozen
authorization; on the built-in floor, and on the `cfg.expiry_s` fallback, it
warns and parks, because refusal there buys legibility rather than safety and a
floor is not a promise anyone made. Remove the declaration and the defect returns
intact with the guard still in place. This module is what stops the document from
drifting back into a promise the platform cannot keep.

⚠ AND FROM DRIFTING WIDER THAN ITS DERIVATION. The document declares the window
under `approval_windows.actions`, NOT `.tiers`, because everything below is
derived from one situation — the pre-park stretch of a Run executed by an agent
that holds a tier-2 write grant. A per-tier entry would impose the same three
minutes on every tier-2/3 approval on every path, including the conversation
park, which has no durable resume seam for the horizon to collide with. See
`test_the_declaration_is_scoped_to_the_actions_it_derives`.

THE VALUE IS DERIVED, NOT CHOSEN, AND EVERY INPUT IS READ FROM ITS ENFORCEMENT
SITE — none of them is retyped here, because a constant this module owns is a
constant this module cannot catch being wrong:

    window = RUN_CREDENTIAL_MIN_REMAINING_SECONDS   (console source)
           - enforced per-attempt wall clock        (registry.toml x caps module)
           - clock-skew leeway                      (ne.identity.config)
           = 420 - 230 - 10
           = 180 s

⛔ WHAT THIS IS NOT: A WORST-CASE BOUND, and the module does not claim one.
`RUN_CREDENTIAL_MIN_REMAINING_SECONDS` is a FAIL-OPEN RENEW TRIGGER, not a
floor — `packages/ne-console/src/api/client.ts` returns early when no renew hook
is installed and swallows a failed renew ("Fail-open to the current token") —
and it binds the console alone, so a run started from the API or the CLI never
passes through it. So 180 s is THE LARGEST WINDOW THAT HOLDS WHEN THE CONSOLE'S
RENEW SUCCEEDS.

DECLARED RESIDUALS — all five, stated rather than smoothed:

1. a run admitted on a fail-open renew, or started outside the console, can
   still refuse at park;
2. THE DERIVATION RESERVES NOTHING FOR THE RESUMED ATTEMPT. The 230 s subtrahend
   is ONE attempt's enforced wall clock — the pre-park stretch. After the
   approver decides, the resume runs the SAME cap again, so at the maximum park
   time the resumed attempt begins with essentially none of the owner's
   authorization left and the delayed-seam guard refuses it. 180 s is the
   largest window that can be PARKED honourably; it is not a window inside which
   a park is guaranteed to RESUME. Reserving a second 230 s would leave
   `420 - 230 - 230 - 10 < 0`, i.e. no honourable window at all on this
   deployment — which is the finding, not a reason to hide the term;
3. dispatch latency is an OMITTED TERM. Nothing between the console's freshness
   check and the parking attempt's first step — admission, enqueue, worker
   pickup — is subtracted, and the headroom at the agent's full budget is
   `420 - 230 - 180 = 10 s`, already spent on leeway. Measured pre-loop work is
   ~0.11 s, so it errs safe today; it is not bounded by anything;
4. the leeway input is a class default, not a deployed value — see
   `_clock_skew_leeway_s`, which anchors it;
5. ⭐ IT IS STILL INTERIM, AND F1 HAS NOW LANDED WITHOUT MOVING IT. The standing
   approval survives the owner's token: on a tier-2/3 park — where `tiers.rego`
   resolves a non-NULL standing lifetime, floors 7 d and 2 d — a decision taken
   past the horizon is written as a standing approval in the same transaction
   that terminalizes the attempt, and the owner's next authenticated run
   re-parks, re-checks every stamp live and consumes it. That changes what this
   ceiling COSTS, not the ceiling itself: every input of the derivation above is
   unchanged, the park-time guard still refuses a DECLARED window longer
   than the owner's remaining authorization, and
   `deploy/opa/data/ne/config/data.json` still declares 180 s. Raising the
   number needs one of those inputs to move, and F1 moved none of them. F1's
   rescue also does not reach the workflow write legs named at the end of this
   docstring: that park resolves a NULL standing lifetime (the PDP is asked
   about tier 1 and answers one), and a NULL lifetime mints nothing. 180 s is
   small because THIS deployment's horizon is small, not because three minutes
   is the right answer to "how long should a person get to approve an external
   email".

The subtrahend is the repo's ENFORCED permission, not an observed sample: an
agent's `max_steps` times the caps module's `_SECONDS_PER_STEP` is the wall
clock `ne.runtime.runner` hard-cancels an attempt at.

⭐ AND THE AGENT IS DERIVED TOO — THAT IS WHAT MAKES 230 A BOUND RATHER THAN A
COINCIDENCE. Naming one agent id here would enforce its number without bounding
anything: a tier-2 write grant moving to (or appearing on) a longer-budgeted
agent would leave every assertion green while the real subtrahend grew. So the
set is READ FROM THE DATA: every agent in `deploy/agents/registry.toml` whose
`tool_grants` contain an action that `tiers.rego` classifies at TIER 2 OR
HIGHER — and the classifying rules are parsed out of `tiers.rego` itself, BOTH
arms of them (the `startswith` prefixes and the `contains` infixes such as
`.pii.`), not retyped — and the subtrahend is the MAXIMUM enforced wall clock
across that set. Two agents qualify today and they TIE at the maximum:
`agt_01k3customersupport000000000` (`write.external.email.send`) and
`agt_01k3inertparkselftest0000000` (`write.external.demo.send`), both at
`max_steps` 23 -> 230 s. The tie is deliberate, not a coincidence: NE-INERT-BUDGET
raised the self-test agent from 6 TO THE CEILING THIS DERIVATION SETS, because at
6 its 60 s wall clock could not survive one governed knowledge read on this
deployment (108-207 s) and so it never parked. It went to 23 and not one step
further, precisely so the MAXIMUM — and therefore the published window — does
not move. `test_the_parking_ceiling_covers_the_governed_read_ladder` below pins
that relationship in both directions.

What that pins, stated no wider than it is: raising `max_steps` on ANY agent
that holds a tier-2 write grant reds this module, and so does GRANTING a tier-2
write action to an agent whose budget is larger — the drift that actually
happened to this registry in generation 19, and the one a hand-picked agent id
was structurally incapable of seeing. It does NOT pin an agent that has no such
grant: raising `max_steps` on the read-only Meridian Analyst is correctly
invisible here, because that agent cannot park a tier-2 approval.

⛔ AND IT IS NOT AN ENUMERATION OF EVERY PARKABLE ACTION — the grant-derived set
bounds the DERIVED CEILING for declared entries and claims nothing about the
universe of parks. The guard-reachable parkable set is
`{write.external.email.send}` PLUS the workflow write legs
(`write.internal.crm.note` under `write_approval_floor_scope(TIER_2)`), and no
grant-classification scan can see the second: it is FLOOR-DRIVEN, not
grant-driven — a tier-1 action escalated by an ambient scope the PDP never sees.
An earlier revision asserted `parkable == {"write.external.email.send"}` and
`set(declared) == parkable`; both were FALSE, not merely incomplete, and both are
gone. Under the declared-only guard scope, completeness is no longer a refusal
condition anyway.
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ne.identity.config import OidcConfig
from ne.runtime.errors import ApprovalWindowUnhonourableError
from ne.runtime.middleware import _require_honourable_approval_window
from ne.runtime.source_results import (
    authenticated_request_scope,
    current_authenticated_context,
)

from ne.contracts.identity import AuthenticatedRequestContext  # isort: skip

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DOCUMENT = REPO_ROOT / "deploy" / "opa" / "data" / "ne" / "config" / "data.json"
CONSOLE_CLIENT = REPO_ROOT / "packages" / "ne-console" / "src" / "api" / "client.ts"
AGENT_REGISTRY = REPO_ROOT / "deploy" / "agents" / "registry.toml"
AGENT_CAPS = REPO_ROOT / "packages" / "ne-api" / "src" / "ne" / "api" / "agent_execution_caps.py"
DEPLOY_ROOT = REPO_ROOT / "deploy"

TIERS_REGO = REPO_ROOT / "packages" / "ne-govern" / "policies" / "ne" / "govern" / "tiers.rego"

#: The logger the guard emits its floor-route warning on — the module under
#: test, named rather than left to the root logger so `caplog` cannot pass on
#: some unrelated warning emitted elsewhere in the same block.
_GUARD_LOGGER = "ne.runtime.middleware"


def _run_credential_renew_trigger_s() -> int:
    """`RUN_CREDENTIAL_MIN_REMAINING_SECONDS`, read from the console source.

    Same technique as `ne-identity`'s `test_realm_config.py`: the number is owned
    by the console, so this reads it there instead of keeping a second copy that
    can silently disagree.

    Named a TRIGGER rather than a floor deliberately — see the module docstring:
    the console fails open around it, so nothing derived from it is a bound.
    """

    source = CONSOLE_CLIENT.read_text(encoding="utf-8")
    match = re.search(r"export const RUN_CREDENTIAL_MIN_REMAINING_SECONDS = (\d+);", source)
    assert match is not None, (
        "RUN_CREDENTIAL_MIN_REMAINING_SECONDS not found in the console client — "
        "the approval-window derivation has lost its first input"
    )
    return int(match.group(1))


def _seconds_per_step() -> int:
    """`_SECONDS_PER_STEP`, read out of `ne.api.agent_execution_caps`'s SOURCE.

    Read as text rather than imported: `ne-api` is not a dependency of
    `ne-runtime` in either direction that permits an import here (it depends on
    this package), and the value is wanted as the deployment's declared
    seconds-per-step, exactly as the caps module spells it.
    """

    source = AGENT_CAPS.read_text(encoding="utf-8")
    match = re.search(r"^_SECONDS_PER_STEP = (\d+)$", source, re.MULTILINE)
    assert match is not None, (
        "_SECONDS_PER_STEP not found in ne.api.agent_execution_caps — the "
        "approval-window derivation has lost its second input"
    )
    return int(match.group(1))


def action_classification_rules(tiers: str = "0123") -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`tiers.rego`'s action grammar for the given tier digits, parsed not retyped.

    Parsed out of the policy rather than restated, so a change to the
    classification itself (a new `write.external.*`-like prefix, a new
    infix like `.pii.`) reaches every derivation built on it instead of
    silently widening the set of actions while the callers keep measuring the
    old one.

    ⚠ THE INFIX ARM IS NOT DECORATION. `tiers.rego` classifies tier 2 by
    `contains(input.action, ".pii.")` under ANY prefix, so a rule set read as
    prefixes alone is NARROWER than the policy, not wider — which is exactly
    how `test_middleware_with_durable_gate`'s `ACTION_MAX` scan (which reuses
    this with the default, all-tier digits) missed a whole class of names.

    Returns `(startswith prefixes, contains infixes)`.
    """

    source = TIERS_REGO.read_text(encoding="utf-8")
    prefixes = tuple(
        match.group(2)
        for match in re.finditer(
            rf'^candidate_tiers contains ([{tiers}]) if startswith\(input\.action, "([^"]+)"\)',
            source,
            re.MULTILINE,
        )
    )
    infixes = tuple(
        match.group(2)
        for match in re.finditer(
            rf'^candidate_tiers contains ([{tiers}]) if contains\(input\.action, "([^"]+)"\)',
            source,
            re.MULTILINE,
        )
    )
    assert prefixes, (
        f"no startswith rules for tiers [{tiers}] found in {TIERS_REGO} — the "
        "approval-window derivation cannot tell which grants can park"
    )
    return prefixes, infixes


def _tier_2_or_higher_action_rules() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The action patterns `tiers.rego` classifies at tier 2 or higher."""

    return action_classification_rules("23")


def _classified_tier_2_or_higher(action: str) -> bool:
    """Does `tiers.rego` put this action at tier 2 or above?"""

    prefixes, infixes = _tier_2_or_higher_action_rules()
    return any(action.startswith(prefix) for prefix in prefixes) or any(
        infix in action for infix in infixes
    )


def _agents_that_can_park() -> dict[str, int]:
    """Every registry agent holding a tier-2-or-higher grant, to its `max_steps`.

    The SET is derived, not named: a tier-2 write grant is what lets an agent
    reach a park at all, so the registry's own `tool_grants` decide membership.
    Lifecycle is deliberately not filtered — a manifest that still carries the
    grant is a manifest that can be reactivated without anyone re-deriving this
    window.
    """

    registry = tomllib.loads(AGENT_REGISTRY.read_text(encoding="utf-8"))
    return {
        str(agent["agent_id"]): int(agent["budgets"]["max_steps"])
        for agent in registry["agents"]
        if any(_classified_tier_2_or_higher(str(grant)) for grant in agent.get("tool_grants", ()))
    }


def _enforced_attempt_wall_clock_s() -> int:
    """The LONGEST enforced per-attempt wall clock among agents that can park.

    `enforced_agentic_caps` computes `max_steps * _SECONDS_PER_STEP` and
    `ne.runtime.runner` hard-cancels the attempt there, so this is a PERMISSION
    the repo grants — not a measurement of one run on one box. Taking the
    maximum over the derived set is what makes it a bound: whichever agent
    holds the grant, its budget is already subtracted.
    """

    parkers = _agents_that_can_park()
    assert parkers, (
        f"no agent in {AGENT_REGISTRY} holds a tier-2-or-higher grant — either the "
        "registry lost the grant or the classification parse did; the "
        "approval-window derivation cannot name its subtrahend"
    )
    return max(parkers.values()) * _seconds_per_step()


#: `IdentitySettings` is pydantic-settings with `env_prefix="NE_IDENTITY_"` and
#: `env_nested_delimiter="__"`, so this is the environment variable that
#: overrides `OidcConfig.leeway_seconds` (validator range 0-60).
_LEEWAY_ENV_VAR = "NE_IDENTITY_OIDC__LEEWAY_SECONDS"


#: An ASSIGNMENT of `_LEEWAY_ENV_VAR`, in any shape `deploy/` actually uses:
#: `NAME=value` (env files, compose `environment:` lists), `NAME: value` (compose
#: mappings, ConfigMap `data:`) and `name: NAME` (Helm/k8s `env:` entries). A
#: bare MENTION does not match — the data document's own derivation prose names
#: the variable to explain this very anchor, and a substring scan would trip on
#: itself.
_LEEWAY_ASSIGNMENT = re.compile(
    rf"(?:name:\s*[\"']?{_LEEWAY_ENV_VAR}|{_LEEWAY_ENV_VAR}\s*[:=])",
    re.IGNORECASE,
)


def _deployment_files_setting_leeway() -> list[str]:
    """Every file under `deploy/` that ASSIGNS the leeway override, if any."""

    hits: list[str] = []
    for path in sorted(DEPLOY_ROOT.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _LEEWAY_ASSIGNMENT.search(text):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def _clock_skew_leeway_s() -> int:
    """`OidcConfig.leeway_seconds` — the skew the verifier already budgets.

    The horizon this window is compared against is a verified `exp`, and
    verification accepts it plus or minus this much, so the window has to leave
    it unspent.

    ⚠ THE WEAKEST OF THE THREE INPUTS, AND IT IS ANCHORED HERE RATHER THAN LEFT
    LOOKING DERIVED. 420 and 230 are read from the artifacts this deployment
    actually ships (the console source, the agent registry); this one is a CLASS
    DEFAULT, and `_LEEWAY_ENV_VAR` overrides it anywhere in the range the
    validator permits. At leeway 60 the honourable ceiling is 130 s and a
    shipped 180 would be unhonourable with nothing here noticing. So the anchor
    is the second assertion: the class default is the deployed value ONLY while
    no deployment artifact sets that variable, and the moment one does this
    module goes RED and names the file instead of silently continuing to
    subtract 10.
    """

    overrides = _deployment_files_setting_leeway()
    assert not overrides, (
        f"{_LEEWAY_ENV_VAR} is set by {overrides} — the approval-window derivation "
        f"subtracts OidcConfig's class default, which is no longer what this "
        f"deployment runs; re-derive the window against the configured value"
    )
    default = OidcConfig.model_fields["leeway_seconds"].default
    assert isinstance(default, int | float), (
        "OidcConfig.leeway_seconds has no numeric default — the approval-window "
        "derivation has lost its third input"
    )
    return int(default)


def _honourable_window_s() -> int:
    """The derivation itself, re-run from the three enforcement sites."""

    return (
        _run_credential_renew_trigger_s()
        - _enforced_attempt_wall_clock_s()
        - _clock_skew_leeway_s()
    )


def _shipped_windows() -> dict[str, float]:
    """The windows this deployment DECLARES, keyed by the action each covers.

    Read from `approval_windows.actions`, not `.tiers`, and the difference is
    the point — see `test_the_declaration_is_scoped_to_the_actions_it_derives`.
    """

    document = json.loads(DATA_DOCUMENT.read_text(encoding="utf-8"))
    windows = document["approval_windows"]
    assert "tiers" not in windows, (
        "the deployment document declares a per-TIER window; the derivation is about "
        "one agent-parked action's pre-park stretch and says nothing about every other "
        "tier-2/3 approval on every other path — declare it per action"
    )
    return {str(k): float(v) for k, v in windows["actions"].items()}


def _owner_context(*, now: datetime, remaining_s: float) -> AuthenticatedRequestContext:
    """The run owner's frozen snapshot with `remaining_s` left at `now`."""

    return AuthenticatedRequestContext(
        principal_id="alice",
        authorization_groups=(),
        department="engineering",
        issuer="https://issuer.example.test",
        expires_at=int((now + timedelta(seconds=remaining_s)).timestamp()),
    )


def test_the_derivations_inputs_are_the_enforced_ones() -> None:
    """Each input is what its ENFORCEMENT site says, and they compose to 180.

    Asserted separately from the window comparison so a drifting input names
    itself instead of surfacing as a bare arithmetic mismatch.

    MUTATION-CHECKS, both of which the derivation must catch:

    * raise `max_steps` for `agt_01k3customersupport000000000` in
      `deploy/agents/registry.toml` by one (23 -> 24) and this goes RED at the
      wall-clock assertion — 240, not 230;
    * add `write.external.email.send` to the read-only Meridian Analyst's
      `tool_grants` (`max_steps` 40) and this goes RED the same way — 400, not
      230 — because the SET of parking agents is read from `tool_grants`, not
      named here. A hand-picked agent id enforced 230 without bounding it; this
      bounds it.
    """

    assert _run_credential_renew_trigger_s() == 420
    assert _seconds_per_step() == 10
    assert _agents_that_can_park() == {
        "agt_01k3customersupport000000000": 23,
        # NE-ITEM1-INERT-PARK. The operator self-test agent holds
        # `write.external.demo.send` — tier 2 by the SAME `write.external.`
        # rule — so it is a parking agent and this map must name it.
        # NE-INERT-BUDGET moved it 6 -> 23: at 6 its enforced wall clock was
        # 60 s, which is less than the ONE governed knowledge read its ladder
        # must serve to park at all (108-207 s measured), so it could not park
        # on this deployment. 23 is the ceiling this very derivation sets, so
        # it TIES Customer Support rather than exceeding it and the maximum —
        # hence the published window — is unchanged.
        "agt_01k3inertparkselftest0000000": 23,
    }
    assert _enforced_attempt_wall_clock_s() == 230  # max_steps 23 * 10 s/step
    assert _clock_skew_leeway_s() == 10
    assert _honourable_window_s() == 180  # 420 - 230 - 10


#: The id of the operator self-test agent whose ONLY purpose is to park a Tier-2
#: approval with zero outward effect. Named here — unlike the parking SET, which
#: is derived — because the test below is about THIS agent's ability to reach its
#: own park, not about the ceiling's membership.
_SELF_TEST_AGENT = "agt_01k3inertparkselftest0000000"

#: Seconds the self-test agent's ladder costs before it can park, measured on
#: THIS deployment and stated as a floor rather than an average.
#:
#: ~24 s of warm per-run overhead ahead of the first tool call, plus ONE governed
#: `read.knowledge.search`, which `ne.brain.pipeline` recorded at 108138-207318 ms
#: over 24 h (n=8) because `ne-pilot-tei-rerank-1` runs `BAAI/bge-reranker-v2-m3`
#: on 12 CPUs with no GPU. 24 + 108 = 132, and the cheapest ladder actually
#: observed end to end was ~135 s.
#:
#: ⛔ THE READ IS NOT OPTIONAL AND THE FLOOR CANNOT BE ENGINEERED DOWN HERE.
#: `middleware._run_source_references` refuses a Tier-2 park whose Run served no
#: protected source reference, so the agent cannot skip the read and park anyway.
#:
#: ⚠ A FLOOR, NOT A BOUND — SAID PLAINLY BECAUSE THE GAP IS REAL. The SLOWEST of
#: the eight observed reads (207.3 s) plus the warm overhead is ~231 s, which does
#: NOT fit the 230 s the ceiling below permits, so a tail draw is still
#: hard-cancelled before the park. That residual belongs to the CPU-only reranker
#: and cannot be bought off in the registry: the next step up would shrink a
#: published governed number. See the budget derivation in
#: `deploy/agents/registry.toml`.
_GOVERNED_READ_LADDER_FLOOR_S = 135


def test_the_parking_ceiling_covers_the_governed_read_ladder() -> None:
    """The self-test agent can REACH its park, and reaching it costs no window.

    ⭐ WHY THIS IS A RELATIONSHIP AND NOT A NUMBER. Pinning `max_steps == 23`
    would be satisfied by a future pair that is individually right and jointly
    incoherent — a shrunken declared window with the budget left at 23, or a
    budget trimmed "for tidiness" back under the ladder it has to pay for. Both
    inputs are therefore DERIVED here from their own enforcement sites and
    compared against each other:

        ceiling wall clock = renew trigger - leeway - the LARGEST DECLARED window
                           = 420 - 10 - 180
                           = 230 s

    which is the same arithmetic as `_honourable_window_s`, solved for the
    subtrahend instead of the window. Nothing in this test retypes 23, 230 or 180.

    THE THREE THINGS IT PINS, and what each one catches:

    1. THE LADDER FITS UNDER THE CEILING AT ALL. If the declared window grows, or
       the console's renew trigger shrinks, the ceiling falls below the cost of
       one governed read and this deployment can no longer prove item 1 with any
       budget — a finding, and one that should red LOUDLY rather than be
       discovered by a fourth silent live attempt.
    2. THE SELF-TEST AGENT'S ENFORCED CLOCK CLEARS THE LADDER. This is the defect
       being closed: at `max_steps` 6 the clock was 60.0 s against a ~135 s
       ladder, so three live attempts produced zero parks.
    3. IT SITS EXACTLY AT THE CEILING, NOT BELOW IT. Since the worst observed
       draw does NOT fit even at the ceiling, there is no headroom to donate; a
       value below the ceiling would be strictly worse for no gain. And it may
       not sit ABOVE it — that is the same assertion, from the other side, as
       "the published 180 s window does not move".

    MUTATION-CHECKS:

    * `max_steps` 23 -> 6 for the self-test agent (the revert) and leg 2 goes RED:
      60.0 s does not clear the 135 s floor;
    * `max_steps` 23 -> 24 and leg 3 goes RED (241 > 230), as do
      `test_the_derivations_inputs_are_the_enforced_ones`,
      `test_the_shipped_window_fits_the_remaining_authorization` and
      `test_the_shipped_window_actually_parks_against_the_derived_horizon` —
      because the maximum, and therefore the published window, moved;
    * `max_steps` 23 -> 20 and leg 3 goes RED: it stopped taking the seconds the
      derivation already permits it;
    * raise the declared window in `deploy/opa/data/ne/config/data.json` past
      275 s (`420 - 10 - 135`) and leg 1 goes RED: no `max_steps` this
      deployment can grant would then cover one governed read, so the self-test
      could not park at ANY budget.
    """

    ceiling_wall_clock_s = (
        _run_credential_renew_trigger_s()
        - _clock_skew_leeway_s()
        - int(max(_shipped_windows().values()))
    )

    # 1. The ceiling this deployment's own declaration leaves is wide enough for
    #    the ladder a Tier-2 park requires. Not tautological: both sides move.
    assert ceiling_wall_clock_s >= _GOVERNED_READ_LADDER_FLOOR_S, (
        f"the honourable ceiling is {ceiling_wall_clock_s}s but one governed read "
        f"ladder costs at least {_GOVERNED_READ_LADDER_FLOOR_S}s — NO budget can "
        "park a Tier-2 approval on this deployment; fix the reranker or the "
        "declared window, not the registry"
    )

    parkers = _agents_that_can_park()
    assert _SELF_TEST_AGENT in parkers, (
        f"{_SELF_TEST_AGENT} no longer holds a tier-2 grant — the operator "
        "self-test can no longer park the lifecycle it exists to prove"
    )
    self_test_wall_clock_s = parkers[_SELF_TEST_AGENT] * _seconds_per_step()

    # 2. It can actually reach its own park. This is what 6 could not do.
    assert self_test_wall_clock_s >= _GOVERNED_READ_LADDER_FLOOR_S, (
        f"{_SELF_TEST_AGENT} is enforced at {self_test_wall_clock_s}s, under the "
        f"{_GOVERNED_READ_LADDER_FLOOR_S}s its governed-read ladder costs before it "
        "can park; it will be hard-cancelled every time and prove nothing"
    )

    # 3. Exactly at the ceiling: all the margin available, and no window spent.
    assert self_test_wall_clock_s == ceiling_wall_clock_s, (
        f"{_SELF_TEST_AGENT} is enforced at {self_test_wall_clock_s}s against a "
        f"{ceiling_wall_clock_s}s ceiling — below it wastes margin the worst "
        "observed read still needs, above it shrinks the published approval window"
    )

    # And the ceiling really is the binding constraint on the whole parking set,
    # so leg 3 above is a statement about the published window and not just about
    # one agent's row.
    assert _enforced_attempt_wall_clock_s() == ceiling_wall_clock_s
    assert _honourable_window_s() == max(_shipped_windows().values())


def test_the_shipped_window_fits_the_remaining_authorization() -> None:
    """Every shipped window is <= the derived remaining-at-park.

    Not a worst case and not claimed as one (module docstring): it is the
    largest window that holds when the console's renew succeeds. What it is NOT
    is a value fitted to the MEDIAN observed run — that would only move the
    failure from 100% of parks to an intermittent fraction of them, which is the
    shape of the defect being removed.

    MUTATION-CHECK: raise the declared value in
    `deploy/opa/data/ne/config/data.json` by one step (180 -> 181, or to any
    rounder-looking 300/600) and this goes RED. Lowering
    `RUN_CREDENTIAL_MIN_REMAINING_SECONDS` in the console, raising the
    `max_steps` of an agent that holds a tier-2 grant, or MOVING such a grant
    onto a longer-budgeted agent, reds it the same way — every input, including
    which agents can park, is read rather than retyped.
    """

    honourable_ceiling = _honourable_window_s()
    windows = _shipped_windows()
    assert windows, "the deployment document declares no windows at all"
    for action, window_s in sorted(windows.items()):
        assert window_s <= honourable_ceiling, (
            f"the {action!r} window {window_s}s exceeds the {honourable_ceiling}s this "
            "deployment can honour; every park of that action would be refused"
        )


def test_the_shipped_window_actually_parks_against_the_derived_horizon() -> None:
    """The arithmetic above is checked against the REAL guard, not restated.

    An off-by-one in the comparison, or a unit slip between the document and the
    check, would satisfy the inequality test and still refuse every park. This
    drives `_require_honourable_approval_window` itself with exactly the derived
    owner horizon.

    Driven with `declared=True`, because that is what a shipped
    `approval_windows.actions` entry IS — and it is the only provenance the
    guard refuses on, so it is the only one under which this arithmetic can
    cost anything.

    MUTATION-CHECK: raise the declared value in the data document by one step and
    this goes RED with `ApprovalWindowUnhonourableError` — the shipped window no
    longer parks against the horizon it was derived from.
    """

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    derived_remaining = _honourable_window_s()
    context = _owner_context(now=now, remaining_s=derived_remaining)
    for action, window_s in sorted(_shipped_windows().items()):
        with authenticated_request_scope(context):
            _require_honourable_approval_window(
                action=action, now=now, expiry_seconds=window_s, declared=True
            )  # must not raise for ANY shipped action

    # Non-vacuous: one second more than the shipped window is refused, so the
    # call above is passing because the value fits, not because the guard is inert.
    with authenticated_request_scope(context), pytest.raises(ApprovalWindowUnhonourableError):
        _require_honourable_approval_window(
            action="write.external.email.send",
            now=now,
            expiry_seconds=float(derived_remaining + 1),
            declared=True,
        )


def test_the_declaration_is_scoped_to_the_actions_it_derives() -> None:
    """Every DECLARED window is inside the derived ceiling, and the one the
    derivation is about still exists.

    ⛔ WHY PER-ACTION AND NOT PER-TIER. `tiers.rego` resolves the window
    action-first, then tier, then the built-in floor, so a `tiers: {"2": 180}`
    entry would apply this derivation to EVERY tier-2/3 approval on EVERY path.
    That includes the conversation park (`DurableApprovalGate.request_approval`
    -> `_submit_fresh`), which has no durable resume seam for the horizon to
    collide with — `ne.api.workflows` dispatches its `_park` as a background
    `asyncio.create_task`, so the request returns while it polls and nothing
    re-binds the owner's admitted context afterwards. It would have gone from
    the 4 h/24 h floor to three minutes, with the escalation SLA derived down to
    90 s, for a reason that says nothing about it. The derivation is about one
    thing — the pre-park stretch of a Run executed by an agent holding a tier-2
    write grant — so it is declared for exactly the action that describes.

    ⛔ WHAT THIS TEST NO LONGER CLAIMS, because the claim was FALSE rather than
    incomplete. It used to assert `parkable == {"write.external.email.send"}`
    and `set(declared) == parkable`, on the theory that the class tier of an
    agent's granted actions enumerates the tier-2 parks. It does not. The
    guard-reachable parkable set is that action PLUS the workflow write legs
    (`write.internal.crm.note` under `write_approval_floor_scope(TIER_2)`), and
    no grant-classification scan can ever see the second: it is FLOOR-DRIVEN,
    not grant-driven — a tier-1 action escalated by an ambient scope the PDP is
    never shown. That is precisely the park the first cut of the guard refused.
    Declared-ToolSpec tier and provisional escalation are two more counterexamples
    to the same enumeration.

    AND UNDER THE DECLARED-ONLY GUARD SCOPE, COMPLETENESS IS NO LONGER A REFUSAL
    CONDITION. An action with no declared window resolves the built-in floor,
    which the guard warns on and parks — the same behaviour as before the check
    existed. So what is left to pin is the two things that ARE load-bearing:
    every declared window is within the derived ceiling, and the one action the
    derivation is about still has a declaration (without it, the 12.7% returns).

    MUTATION-CHECK: drop `write.external.email.send` from the document's
    `actions` map and this goes RED on the existence pin; raise any declared
    window past the ceiling and it goes RED on the ceiling assertion.
    """

    declared = _shipped_windows()
    ceiling = _honourable_window_s()
    assert declared, "the deployment document declares no windows at all"
    for action, window_s in sorted(declared.items()):
        assert window_s <= ceiling, (
            f"the declared {action!r} window {window_s}s exceeds the {ceiling}s "
            "this deployment can honour, so every park of it would be REFUSED — "
            "a declared window is the one provenance the guard fails closed on"
        )
    # THE EXISTENCE PIN. This entry is the actual fix for the measured failure:
    # it is what makes the durable row expire in three minutes instead of
    # promising four hours it cannot keep. Deleting it brings the defect back
    # with every test but this one still green.
    assert "write.external.email.send" in declared, (
        "the deployment no longer declares a window for write.external.email.send — "
        "its parks revert to tiers.rego's 14400 s floor, which is the four-hour "
        "promise that stranded 12.7% of runs; the guard does NOT catch this, it "
        "warns and parks on a floor"
    )
    # And the registry-derived set is still non-empty, because the CEILING above
    # is derived from it. This is the only thing that set bounds.
    parkable = {
        str(grant)
        for agent in tomllib.loads(AGENT_REGISTRY.read_text(encoding="utf-8"))["agents"]
        for grant in agent.get("tool_grants", ())
        if _classified_tier_2_or_higher(str(grant))
    }
    assert "write.external.email.send" in parkable, (
        "no agent holds the grant the derivation is about — re-derive the ceiling"
    )


def test_the_document_carries_its_own_derivation() -> None:
    """A bare `180` in a JSON file is a magic number; the document explains it.

    JSON cannot carry comments, so the derivation rides as an inert
    `data.ne.config._derivation` key — inert because `tiers.rego` reads only
    `approval_windows.actions` and `approval_windows.tiers`. It must name all
    three inputs AND the scope limit, so neither the number nor the claim
    attached to it can drift away from the other in review.
    """

    document = json.loads(DATA_DOCUMENT.read_text(encoding="utf-8"))
    derivation = document["_derivation"]
    for value in (
        _run_credential_renew_trigger_s(),
        _enforced_attempt_wall_clock_s(),
        _clock_skew_leeway_s(),
        _honourable_window_s(),
    ):
        assert str(value) in derivation, (
            f"the data document's derivation does not name {value}; it and the "
            "shipped number can now drift apart in review"
        )
    # And it does not quietly claim to deliver the floor's four hours.
    assert "14400" not in derivation
    # It states the SCOPE rather than passing the value off as a worst-case
    # bound: the console's renew is fail-open and binds only the console, so a
    # bound is exactly what this cannot be. Removing that sentence is the
    # overclaim this assertion exists to stop.
    lowered = derivation.lower()
    assert "fail-open" in lowered
    assert "renew" in lowered
    assert "not a worst-case bound" in lowered


# --- the guard's PROVENANCE condition: what it refuses, and what it does not --


def test_a_floor_resolved_park_is_not_refused_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A window nobody declared is not a promise anybody made — so it PARKS.

    This is the half of the pair that costs successes when it is wrong. The
    shipped guard refused here, which refuses approvals that succeed on `main`:
    the built-in tier-2 floor (14400 s) against any realistic session horizon is
    unhonourable on EVERY park, so an unconditional check turns a 12.7% silent
    failure into a 100% loud one for every action without a declaration.

    Refusal here buys legibility, not safety. The park executes nothing unsafe;
    if the approval outruns the horizon the resume seam terminalizes
    `authorization_expired`, which is the pre-existing, already-handled outcome.
    So the guard says so — once, at WARNING, naming the action, the resolved
    window and the owner horizon — and gets out of the way.

    MUTATION-CHECK: make the guard unconditional again (drop the
    `if not declared:` branch) and this goes RED with
    `ApprovalWindowUnhonourableError`.
    """

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    context = _owner_context(now=now, remaining_s=900)
    floor_s = 14400.0  # tiers.rego's built-in tier-2 floor, 16x the horizon

    with authenticated_request_scope(context), caplog.at_level("WARNING", logger=_GUARD_LOGGER):
        _require_honourable_approval_window(
            action="write.external.email.send",
            now=now,
            expiry_seconds=floor_s,
            declared=False,
        )  # must NOT raise

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    message = warnings[0].getMessage()
    # The three facts the ruling asks the event to carry.
    assert "write.external.email.send" in message
    assert "14400" in message
    assert str(int(context.expires_at)) in message


def test_a_declared_window_that_outruns_the_horizon_is_refused_before_anything_durable() -> None:
    """The other half: a promise this deployment WROTE DOWN and cannot keep.

    Same clocks, same overrun, same action — only the provenance differs, which
    is exactly the discrimination the bundle-1.4.0 bit exists to make. Here the
    refusal is the right answer: somebody configured a window the deployment's
    own horizon cannot honour, and saying so costs no approval row, no
    checkpoint and no approver minute, because the guard runs before
    `create_wait`.

    MUTATION-CHECK: flip `declared=True` to `declared=False` and this goes RED
    with DID NOT RAISE — proving the two halves of this pair differ ONLY in
    provenance.
    """

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    context = _owner_context(now=now, remaining_s=900)

    with authenticated_request_scope(context), pytest.raises(ApprovalWindowUnhonourableError) as e:
        _require_honourable_approval_window(
            action="write.external.email.send",
            now=now,
            expiry_seconds=14400.0,
            declared=True,
        )
    assert e.value.action == "write.external.email.send"
    assert e.value.window_seconds == 14400.0
    # The published sentence still discloses nothing the horizon solves from.
    assert "900" not in e.value.safe_reason
    assert str(int(context.expires_at)) not in e.value.safe_reason


def test_no_bound_context_means_no_horizon_to_compare(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Outside `authenticated_request_scope` the guard returns — silently.

    There is no second clock to compare against, so there is nothing to refuse
    and nothing to warn about: inventing a horizon here would be the only way to
    get this wrong. It is not a bypass — the canonical Run park always executes
    inside `authenticated_request_scope` (`ne.worker.workflows` binds the run's
    admitted context around every execution step), and any path reaching a
    protected materialization or a resume without one is refused by
    `require_authenticated_context` with its own typed error.

    Both provenances are driven, because the early return has to precede the
    provenance test as well as the comparison. No test covered this branch at
    all before; a `raise` added here would have gone unnoticed.

    MUTATION-CHECK: delete the `if context is None: return` guard and both calls
    go RED — `float(None.expires_at)` raises `AttributeError`.
    """

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    assert current_authenticated_context() is None  # the premise, asserted

    with caplog.at_level("WARNING", logger=_GUARD_LOGGER):
        for declared in (False, True):
            _require_honourable_approval_window(
                action="write.external.email.send",
                now=now,
                expiry_seconds=86400.0,
                declared=declared,
            )
    assert caplog.records == []  # no horizon, no finding to report
