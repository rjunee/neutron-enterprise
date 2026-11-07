# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# SPEC §4.2 approval-tier classification. Rego v1 syntax (OPA 1.x: `if` /
# `contains` keywords are mandatory).
#
#   tier 0  read.* / draft.*            — autonomous, logged
#   tier 1  write.internal.*            — autonomous + daily digest
#   tier 2  write.external.* / *.pii.*  — per-item human approval
#   tier 3  finance.* / legal.*         — named approver + second check
#
# The effective tier is the STRICTER of the caller-declared tier
# (input.context.tier) and the action-class tier — max wins.
package ne.govern

# --- caller-declared tier -------------------------------------------------

declared_tier := input.context.tier

valid_declared if {
	is_number(declared_tier)
	declared_tier == round(declared_tier)
	declared_tier >= 0
	declared_tier <= 3
}

# --- action-class tier ----------------------------------------------------

candidate_tiers contains 0 if startswith(input.action, "read.")

candidate_tiers contains 0 if startswith(input.action, "draft.")

candidate_tiers contains 1 if startswith(input.action, "write.internal.")

candidate_tiers contains 2 if startswith(input.action, "write.external.")

candidate_tiers contains 2 if contains(input.action, ".pii.")

candidate_tiers contains 3 if startswith(input.action, "finance.")

candidate_tiers contains 3 if startswith(input.action, "legal.")

class_tier := max(candidate_tiers)

# --- effective tier: stricter-of declared vs action-class -----------------
#
# Classification is anchored on the ACTION CLASS: an action is only
# classified when it matches an action-class prefix (candidate_tiers is
# non-empty). The caller-declared tier can only ESCALATE (stricter-wins) an
# already-classified action — it can never classify an otherwise-unknown
# action. This preserves deny-by-default: an unclassified action denies
# regardless of any caller-declared tier (a caller cannot self-classify a
# `mystery.action` into an allowed/approval decision by declaring a tier).

effective_tier := max([declared_tier, class_tier]) if {
	valid_declared
	count(candidate_tiers) > 0
}

effective_tier := class_tier if {
	not valid_declared
	count(candidate_tiers) > 0
}

classified if is_number(effective_tier)

# --- provisional-context escalation (design §6.3, NE-T2X-5, A-P1-5) --------
#
# Write-class actions escalate exactly ONE tier when the retrieval-derived
# `context_provisional` flag is true: a Tier-1 write authorized on unconfirmed
# LLM-extracted context becomes a Tier-2 per-item-human-approval decision, so
# SPEC §4.2's "never auto-approve" semantics then apply (decision.rego rules
# (e2)/(f2)). `autonomous` (YOLO) mode explicitly opts OUT of escalation —
# that is the DECLARED meaning of YOLO (design §6.3), not an oversight: the
# flag still threads into the PDP input untouched, it simply never fires
# `escalates`. Absent/garbage `extraction_mode` fails TOWARD escalation: only
# the literal string "autonomous" is exempt (`yolo_exempt` is undefined, hence
# `not yolo_exempt` is true, for every other value including no key at all).
# `context_provisional` is type-strict: only the JSON boolean `true` counts —
# a string `"true"` or the number `1` does NOT escalate (mirrored bit-for-bit
# in the Cedar marshaller, `ne.govern.cedar.evaluator`). Escalation clamps at
# tier 3 by construction: `effective_tier <= 2` bounds `escalates`, so an
# already-tier-3 action (finance.*/legal.*) never escalates further — it
# reaches decision.rego rule (f) unescalated, the existing clamp.

provisional_context if input.context.context_provisional == true

yolo_exempt if input.context.extraction_mode == "autonomous"

write_class if startswith(input.action, "write.")

escalates if {
	classified
	write_class
	provisional_context
	not yolo_exempt
	effective_tier <= 2
}

# `final_tier` is the tier decision.rego's rules (c)-(f2) key on: unescalated
# actions (including every read/draft/finance/legal action, since `escalates`
# structurally requires `write_class`) pass `effective_tier` through
# unchanged; an escalating write-class action gets `effective_tier + 1`.
final_tier := effective_tier + 1 if escalates

final_tier := effective_tier if {
	classified
	not escalates
}

# --- approval window (owner requirement, 2026-08-30) ----------------------
#
# HOW LONG a human has to answer an approval is a PROPERTY OF THE ACTION,
# declared here beside its tier — not a hard-coded constant, and above all not
# a side effect of how long a login happens to last.
#
# That last point is the whole reason this exists. On 2026-08-28 four runs
# stranded mid-approval because the effective window was an ACCIDENT of the
# Keycloak access-token lifespan (~15 min): the run owner's frozen
# authorization snapshot expired, so the parked effect could never execute no
# matter what the approver did. Nobody chose 15 minutes; it was inherited from
# session length. Governance latency must be a policy decision.
#
# Resolution order, most specific first:
#
#   1. `data.ne.config.approval_windows.actions[<exact action>]`
#   2. `data.ne.config.approval_windows.tiers[<final tier as string>]`
#   3. the built-in floor below
#
# All three are seconds. Deployments override by supplying the data document;
# nothing here is baked into the bundle, so one deployment can give
# `write.external.email.send` ten minutes while another gives it a day.
#
# ⛔ THE BUILT-IN FLOOR IS NOT HONOURABLE ON ANY DEPLOYMENT SHIPPED TODAY, AND
# SAYING SO IS THE POINT. It was written as "deliberately GENEROUS relative to a
# session" — 4 hours for a tier-2 per-item approval, 24 hours for a tier-3
# named-approver decision — because the failure it replaced was "too short to
# answer". That reasoning was half the picture. Generosity here is not free: the
# run OWNER's frozen authorization snapshot expires on its own clock
# (`accessTokenLifespan`, 900 s on this platform's shipped realms), and a parked
# effect cannot execute past it. A window longer than that horizon is a promise
# nothing can keep.
#
# ⚠️ AND YET THE FLOOR IS NOT WHAT `ne.runtime.middleware.
# _require_honourable_approval_window` REFUSES, which is a correction to an
# earlier revision of this note. That guard refuses only a window a deployment
# DECLARED (the two rules above); on this floor it emits a WARNING on the
# `ne.runtime.middleware` logger -- a log line, not an audit row -- and
# lets the park PROCEED, terminalizing `authorization_expired` at the resume seam
# if the approval is slow. Refusing on a floor buys legibility, not safety -- the
# park executes nothing unsafe -- and a floor is not a promise anyone made, so
# refusing on it punishes a decision nobody took and costs parks that succeed
# today. What removes the failure is DECLARING a keepable window, below.
# They are kept at 4 h/24 h deliberately: they are
# what an approval window SHOULD be for a human, they are the target durable
# run-scoped authorization would make deliverable (SPEC.md:4560-4561 forbids it
# today — owner decision + SPEC amendment), and lowering them here would bake one
# deployment's session arithmetic into the product's governance defaults.
#
# ⚠️ SO A DEPLOYMENT THAT MEANS ITS APPROVAL WINDOWS MUST SUPPLY THE DATA
# DOCUMENT -- not to avoid a refusal (the floor parks), but because the floor
# writes 4 h into the durable row's `expires_at` and then dies as an opaque
# `authorization_expired` a quarter of an hour in, while a DECLARED 180 s dies as
# a visible approval expiry. This repo's own lives at
# `deploy/opa/data/ne/config/data.json` and is staged into the served bundle by
# `ne.cli.opa_bundle.stage_govern_policies` (fail-closed if absent, so a bundle
# never silently ships without this deployment's windows). It declares
# 180 s for `write.external.email.send` — declared as an ACTION override, NOT a
# per-tier one, because the derivation covers exactly one thing: the pre-park
# stretch of a Run executed by an agent holding a tier-2 write grant, and today
# that action is the only tier-2-or-higher grant any agent in
# `deploy/agents/registry.toml` holds. A per-tier entry would impose the same
# three minutes on every tier-2/3 approval on every path, including the
# conversation park (no durable resume seam -- `ne.api.workflows` dispatches its
# `_park` as a background `asyncio.create_task`, so the request returns while it
# polls, and nothing re-binds the owner's admitted context afterwards to enforce
# the horizon), which the derivation says nothing about. Everything else keeps the floor below.
# DERIVED from three ENFORCEMENT sites: the console's
# 420 s run-credential renew trigger, minus the 230 s enforced per-attempt wall
# clock (the LARGEST `max_steps` x 10 s/step among the agents whose `tool_grants`
# hold a tier-2-or-higher action, i.e. the ones that can park at all — today only
# `agt_01k3customersupport000000000`, `max_steps` 23), minus the 10 s of clock
# skew OIDC verification budgets. ⚠️ It is the largest window that holds when
# the console's renew SUCCEEDS, NOT a worst-case bound: that renew is fail-open
# and binds only the console, so a run admitted on a failed renew, or started
# from the API or CLI, can still refuse at park -- and BECAUSE it is declared,
# that refusal is real rather than a warning. ⚠️ It is also INTERIM: under the F1
# architecture the standing approval survives the owner's token and this value
# should be re-raised to what the business wants. Pinned by
# `packages/ne-runtime/tests/test_approval_window_is_honourable.py`. That is what
# this deployment can honour. It is three minutes, it is not four hours, and the
# gap is stated in docs/architecture/approval-horizon.md rather than papered over
# here.

default_approval_window_seconds := {
	"2": 14400,
	"3": 86400,
}

# (1) exact action override
approval_window_seconds := window if {
	window := data.ne.config.approval_windows.actions[input.action]
	is_number(window)
	window > 0
}

# (2) per-tier override for this deployment
approval_window_seconds := window if {
	not action_window_configured
	window := data.ne.config.approval_windows.tiers[format_int(final_tier, 10)]
	is_number(window)
	window > 0
}

# (3) built-in floor
approval_window_seconds := window if {
	not action_window_configured
	not tier_window_configured
	window := default_approval_window_seconds[format_int(final_tier, 10)]
}

action_window_configured if {
	window := data.ne.config.approval_windows.actions[input.action]
	is_number(window)
	window > 0
}

tier_window_configured if {
	window := data.ne.config.approval_windows.tiers[format_int(final_tier, 10)]
	is_number(window)
	window > 0
}

# --- approval-window PROVENANCE ------------------------------------------
#
# All three rules above write the SAME field, so a consumer cannot tell a
# number this deployment DECLARED from one the product shipped as a floor.
# This boolean is that distinction, and it exists because exactly one consumer
# needs it: `ne.runtime.middleware._require_honourable_approval_window` refuses
# a canonical Run park whose window outruns the run owner's authorization
# horizon, and it must refuse ONLY on (1) and (2).
#
# WHY. That refusal buys legibility, not safety. A park that outruns the
# horizon executes nothing unsafe — its worst case is the already-handled
# `authorization_expired` terminalization at the resume seam, its best case is
# a human who answers in time and a run that completes. Failing closed is right
# when proceeding is dangerous and wrong when proceeding merely risks a legible
# failure. A FLOOR IS NOT A PROMISE ANYONE MADE: refusing on (3) punishes a
# decision nobody took, and it costs successes that `main` delivers today.
# Refusing on (1)/(2) is a tripwire on a promise this deployment actually
# wrote down — that one is worth a loud failure.
#
# ⛔ DO NOT "SIMPLIFY" THIS BY MAKING RULE (3) STOP EMITTING. The floor is what
# writes 14400/86400 into the durable row's `expires_at`; delete it and the
# runtime's `cfg.expiry_s` fallback (24 h by default) silently replaces it,
# which is a semantic change nobody asked for. The floor must keep flowing;
# only its PROVENANCE is new.
default approval_window_declared := false

approval_window_declared if action_window_configured

approval_window_declared if tier_window_configured

# --- standing-approval lifetime (F1, standing approval + fresh-owner successor)
#
# HOW LONG A DECISION OUTLIVES THE ATTEMPT IT WAS ASKED FOR. Resolved right
# here, beside `approval_window_seconds`, because it is the same kind of fact:
# a property of the ACTION, declared next to its risk tier, never inherited
# from a session length.
#
# The two clocks are different questions and must not be confused:
#
#   `approval_window_seconds`   — how long the HUMAN has to answer. Measured
#                                 from park. Past it the request expires and
#                                 nobody can decide it any more.
#   `standing_approval_seconds` — how long an ANSWER stays usable once the
#                                 attempt that asked for it has terminalized
#                                 `authorization_expired`. Measured from the
#                                 decision. Past it the standing approval
#                                 lapses to `approval_lapsed` and authorizes
#                                 nothing; the successor parks for a fresh
#                                 human decision.
#
# Resolution order, most specific first — identical in shape to the window so
# one deployment document configures both:
#
#   1. `data.ne.config.standing_approvals.actions[<exact action>]`
#   2. `data.ne.config.standing_approvals.tiers[<final tier as string>]`
#   3. the built-in floor below
#
# All three are seconds. Zero, negative, non-numeric and boolean overrides are
# IGNORED rather than honoured, exactly as for the window: a zero standing
# lifetime would lapse every decision the instant it was made, which is the
# failure this machinery removes, not the one it should be able to configure.
#
# ⛔ KNOWN GAP, DELIBERATE, AND NOT SYMMETRIC WITH THE WINDOW: because those
# values are ignored rather than honoured, THERE IS NO WAY TO TURN THE CARRYOVER
# OFF for one action. A deployment that writes `0`, `-1` or `false` for an
# irreversible action does not disable its standing approval — the entry is
# treated as absent and the action falls through to the 7 d / 2 d floor.
#
# The asymmetry matters because the two clocks fail open in opposite
# directions. A WINDOW that falls back to a floor fails open to a LONGER time
# for a human to answer, which costs nothing anyone has to defend. A STANDING
# LIFETIME that falls back to a floor fails open to SEVEN DAYS of "this
# decision may be older than the execution it authorizes" — the exact
# concession this feature makes, granted by a deployment that was trying to
# refuse it.
#
# Not closed here because closing it is not a one-line change: `null` (the only
# value the runtime already reads as MINT NOTHING —
# `RunApprovalCheckpointStore._standing_lifetime`) would have to travel out of a
# TOTAL Rego rule, through `decision.rego`, the contracts validator, the Cedar
# mirror and its parity vectors, with its own tests. It is also unreachable on
# this deployment today: `deploy/opa/data/ne/config/data.json` declares no
# `standing_approvals` key at all, so every action gets the floor either way and
# no configuration exists for this rule to mis-honour. Documented in
# `docs/architecture/approval-horizon.md`.
#
# ⚠ AND A SECOND LATENT INVERSION OF THE SAME ASYMMETRY, DOCUMENTED HERE SO
# NOBODY INTRODUCES IT BY ACCIDENT: the tier this rule resolves against is
# `final_tier`, the tier the PDP decides. It is NOT necessarily the tier the
# park is gated at. The runtime gates on `middleware._effective_tier`, the MAX
# of the ToolSpec's declared tier, the Python `tier_for_action` mirror, the
# PDP's `final_tier`, the obligation floors and the ambient write-gate floor —
# a stricter-of set the PDP never sees. So if a ToolSpec or the mirror ever
# declared a tier ABOVE the PDP's, a Tier-3 park would carry the tier-2 SEVEN-DAY
# floor resolved here instead of tier 3's two days, granting the LONGER carryover
# to the MORE dangerous effect — precisely the inversion the floors below are
# built to prevent.
#
# Unreachable on both of today's live routes. `write.external.email.send`
# declares TIER_2 in its ToolSpec, which is what the mirror and the PDP also
# say, so all three agree and the max() changes nothing. The other route, the
# workflow write leg, escalates through the ambient write-gate floor AFTER the
# PDP has answered about tier 1 — and that answer carries no standing lifetime
# at all, so it mints nothing to get wrong. Anyone adding a ToolSpec or mirror
# tier above the PDP's must first make the two agree, or resolve this rule on
# the EFFECTIVE tier.
#
# The built-in floors are deliberately ASYMMETRIC and the asymmetry is the
# point. Tier 2 (external-facing, remediable) gets 7 days: an approver who
# decided on Friday should still be able to have that decision honoured when
# the owner logs back in on Monday. Tier 3 (financial/legal, IRREVERSIBLE)
# gets 2 days: what a standing approval concedes is that the decision may be
# OLDER than the execution it authorizes, and that concession has to be
# smallest exactly where the effect cannot be undone.
default_standing_approval_seconds := {
	"2": 604800,
	"3": 172800,
}

# (1) exact action override
standing_approval_seconds := lifetime if {
	lifetime := data.ne.config.standing_approvals.actions[input.action]
	is_number(lifetime)
	lifetime > 0
}

# (2) per-tier override for this deployment
standing_approval_seconds := lifetime if {
	not action_standing_configured
	lifetime := data.ne.config.standing_approvals.tiers[format_int(final_tier, 10)]
	is_number(lifetime)
	lifetime > 0
}

# (3) built-in floor
standing_approval_seconds := lifetime if {
	not action_standing_configured
	not tier_standing_configured
	lifetime := default_standing_approval_seconds[format_int(final_tier, 10)]
}

action_standing_configured if {
	lifetime := data.ne.config.standing_approvals.actions[input.action]
	is_number(lifetime)
	lifetime > 0
}

tier_standing_configured if {
	lifetime := data.ne.config.standing_approvals.tiers[format_int(final_tier, 10)]
	is_number(lifetime)
	lifetime > 0
}

# --- principal ------------------------------------------------------------

valid_principal if {
	is_string(input.principal.id)
	input.principal.id != ""
}
