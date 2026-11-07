# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# The TOTAL decision function. Nine mutually-exclusive complete rules cover every
# input (no `default` rule — a default body cannot reference policy_version).
# Every decision object carries "policy_version" so the answer is traceable to
# the exact bundle that decided it.
#
# Bundle 1.3.0 added "approval_window_seconds": the policy-resolved approval
# window, first-class on every require-approval rule and null elsewhere.
#
# Bundle 1.4.0 adds "approval_window_declared" beside it: TRUE only when the
# window came from a DEPLOYMENT DECLARATION
# (`data.ne.config.approval_windows.actions[action]` or `.tiers[tier]`), FALSE
# for `tiers.rego`'s built-in floor and for every rule that emits a null window.
# It exists for one consumer — the runtime's park-time coherence check, which
# refuses a promise this deployment wrote down and never a floor it merely
# inherited. See the provenance note in tiers.rego.
#
# Bundle 1.5.0 adds "standing_approval_seconds" (F1), the SECOND clock: how long
# an approver's decision outlives the attempt it was made for, resolved by
# `tiers.rego` exactly like the window (exact action, then per tier, then a
# built-in floor — 7 d tier 2, 2 d tier 3) and emitted first-class on the same
# four require-approval rules, null everywhere else. Null means MINT NO STANDING
# APPROVAL: the decision dies with its attempt, never "standing forever".
#
# Effect strings MUST equal the DecisionEffect
# contract values: "allow" / "deny" / "require-approval".
#
# C12: tier-3 actions can NEVER resolve to "allow" in this bundle — only
# "require-approval" with a second_check obligation.
#
# NE-T2X-5 (design §6.3, A-P1-5): rules (c)-(f) now key on `final_tier`
# (tiers.rego) rather than `effective_tier` directly, and (d)/(e)/(f) each add
# `not escalates` — two NEW complete rules, (e2)/(f2), pick up exactly the
# cases `escalates` diverts: a write-class action under provisional context
# escalates one tier (Tier 1 -> Tier 2 via (e2), Tier 2 -> Tier 3 via (f2)).
# Totality argument for {valid_principal && classified} (mirrors tiers.rego's
# stricter-of/max composition, extended one level):
#   (C3||D3)                              -> (f)  [effective_tier == 3; escalates
#                                                   is structurally false here,
#                                                   since it requires
#                                                   effective_tier <= 2 — the
#                                                   `not escalates` guard on (f)
#                                                   is therefore a no-op for this
#                                                   branch, but IS load-bearing
#                                                   below]
#   else (C2||D2):   escalates -> (f2)    [effective_tier == 2, write-class,
#                                           provisional, not yolo — escalates to
#                                           final_tier 3; WITHOUT `not escalates`
#                                           on (f), this same input's final_tier
#                                           of 3 would double-match (f) — the
#                                           guard on (f) is what keeps (f)/(f2)
#                                           disjoint]
#                     !escalates -> (e)    [effective_tier == 2, unescalated]
#   else (C1||D1):   escalates -> (e2)    [effective_tier == 1, write-class,
#                                           provisional, not yolo — escalates to
#                                           final_tier 2]
#                     !escalates -> (d)    [effective_tier == 1, unescalated]
#   else                                  -> (c)  [effective_tier == 0; escalates
#                                                   requires write_class, which is
#                                                   never true for a read.*/draft.*
#                                                   -only classified action, so no
#                                                   guard is needed]
# `escalates` is a single boolean per input, so within each effective_tier
# branch exactly one of the two `final_tier`-keyed rules can fire, and
# `final_tier` never collides ACROSS branches ({0,1,2,3} unescalated map
# 1:1 to (c)/(d)/(e)/(f); escalated final_tier is only ever 2 (from
# effective_tier 1, -> (e2)) or 3 (from effective_tier 2, -> (f2)) — the
# `not escalates` guard on (e)/(f) is what stops those two rules from ALSO
# matching the escalated final_tier==2/final_tier==3 cases.
package ne.govern

# (a) missing or invalid principal -> deny
decision := {
	"effect": "deny",
	"tier": null,
	"reason": "missing or invalid principal",
	"obligations": {},
	"approval_window_seconds": null,
	"approval_window_declared": false,
	"standing_approval_seconds": null,
	"policy_version": policy_version,
} if {
	not valid_principal
}

# (b) valid principal, but the action classifies to no tier -> deny
decision := {
	"effect": "deny",
	"tier": null,
	"reason": "unclassified action",
	"obligations": {},
	"approval_window_seconds": null,
	"approval_window_declared": false,
	"standing_approval_seconds": null,
	"policy_version": policy_version,
} if {
	valid_principal
	not classified
	not subagent_route_frontier_denied
}

# (c) tier 0 -> allow (autonomous, logged)
decision := {
	"effect": "allow",
	"tier": 0,
	"reason": "tier 0: read/internal draft — autonomous, logged",
	"obligations": {},
	"approval_window_seconds": null,
	"approval_window_declared": false,
	"standing_approval_seconds": null,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	final_tier == 0
}

# (d) tier 1 -> allow with daily-digest obligation
decision := {
	"effect": "allow",
	"tier": 1,
	"reason": "tier 1: internal write — autonomous, surfaced in the daily digest",
	"obligations": {"daily_digest": true},
	"approval_window_seconds": null,
	"approval_window_declared": false,
	"standing_approval_seconds": null,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	not escalates
	final_tier == 1
}

# (e) tier 2 -> require-approval (per-item human)
decision := {
	"effect": "require-approval",
	"tier": 2,
	"reason": "tier 2: external/PII action — per-item human approval required",
	"obligations": {"per_item_human_approval": true},
	"approval_window_seconds": approval_window_seconds,
	"approval_window_declared": approval_window_declared,
	"standing_approval_seconds": standing_approval_seconds,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	not escalates
	final_tier == 2
}

# (f) tier 3 -> require-approval (named approver + second check)
decision := {
	"effect": "require-approval",
	"tier": 3,
	"reason": "tier 3: finance/legal action — named approver plus a second check",
	"obligations": {"named_approver": true, "second_check": true},
	"approval_window_seconds": approval_window_seconds,
	"approval_window_declared": approval_window_declared,
	"standing_approval_seconds": standing_approval_seconds,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	not escalates
	final_tier == 3
}

# (e2) tier 1 -> 2 escalation: write-class action, provisional context, not
# YOLO-exempt (design §6.3, A-P1-5). The approval-queue rendering lists WHICH
# facts were unconfirmed (`provisional_context` obligation) so the approver
# sees them before deciding.
decision := {
	"effect": "require-approval",
	"tier": 2,
	"reason": "tier 2: write on provisional context — escalated one tier, per-item human approval required",
	"obligations": {"per_item_human_approval": true, "provisional_context": true},
	"approval_window_seconds": approval_window_seconds,
	"approval_window_declared": approval_window_declared,
	"standing_approval_seconds": standing_approval_seconds,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	escalates
	final_tier == 2
}

# (f2) tier 2 -> 3 escalation: write-class action, provisional context, not
# YOLO-exempt (design §6.3, A-P1-5).
decision := {
	"effect": "require-approval",
	"tier": 3,
	"reason": "tier 3: write on provisional context — escalated one tier, named approver plus a second check",
	"obligations": {"named_approver": true, "second_check": true, "provisional_context": true},
	"approval_window_seconds": approval_window_seconds,
	"approval_window_declared": approval_window_declared,
	"standing_approval_seconds": standing_approval_seconds,
	"policy_version": policy_version,
} if {
	valid_principal
	classified
	escalates
	final_tier == 3
}

# (g) NE-169: subagent.route resolved to the frontier lane -> deny (frontier
# routing authority DEFAULTS TO DENY; the explicit grant is a follow-up
# parameter — consistent with the NE-161 runtime gate).
#
# Mutual exclusivity: a frontier subagent.route is never classified (no
# candidate_tiers rule fires for it), so only this rule (g) fires for it —
# rule (b) now explicitly excludes it via `not subagent_route_frontier_denied`.
# Rule (a) still wins over (g) on an invalid principal, since (g) requires
# `valid_principal`.
decision := {
	"effect": "deny",
	"tier": null,
	"reason": "subagent.route: frontier-lane routing requires an explicit grant — default deny",
	"obligations": {},
	"approval_window_seconds": null,
	"approval_window_declared": false,
	"standing_approval_seconds": null,
	"policy_version": policy_version,
} if {
	valid_principal
	subagent_route_frontier_denied
}
