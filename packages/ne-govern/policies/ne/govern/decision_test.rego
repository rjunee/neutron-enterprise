# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# OPA-native unit tests for the SPEC §4.2 decision function. Runs via
# `opa test packages/ne-govern/policies -v` on any host with the opa binary
# (excluded from built bundles). Rego v1 syntax.
package ne.govern_test

import data.ne.govern

test_tier0_read if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "read.document",
		"resource": "doc:1",
		"context": {},
	}
	d.effect == "allow"
	d.tier == 0
	d.policy_version == "1.5.0"
}

test_tier0_draft if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "draft.email",
		"resource": "draft:1",
		"context": {},
	}
	d.effect == "allow"
	d.tier == 0
}

test_tier1_internal_write if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.note",
		"resource": "note:1",
		"context": {},
	}
	d.effect == "allow"
	d.tier == 1
	d.obligations.daily_digest == true
}

test_tier2_external_write if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.external.post",
		"resource": "post:1",
		"context": {},
	}
	d.effect == "require-approval"
	d.tier == 2
	d.obligations.per_item_human_approval == true
}

test_tier2_pii if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "export.pii.customer",
		"resource": "cust:1",
		"context": {},
	}
	d.effect == "require-approval"
	d.tier == 2
}

test_tier3_finance if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "finance.wire",
		"resource": "acct:1",
		"context": {},
	}
	d.effect == "require-approval"
	d.tier == 3
	d.obligations.named_approver == true
	d.obligations.second_check == true
}

test_tier3_legal if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "legal.filing",
		"resource": "case:1",
		"context": {},
	}
	d.effect == "require-approval"
	d.tier == 3
}

test_declared_tier_honored if {
	# read.* is class tier 0, but the caller declares tier 2 -> stricter wins.
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "read.document",
		"resource": "doc:1",
		"context": {"tier": 2},
	}
	d.effect == "require-approval"
	d.tier == 2
}

test_declared_0_finance_escalates_to_3 if {
	# Caller declares tier 0, but finance.* is class tier 3 -> stricter wins.
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "finance.wire",
		"resource": "acct:1",
		"context": {"tier": 0},
	}
	d.effect == "require-approval"
	d.tier == 3
}

test_declared_tier_invalid_falls_back if {
	# An out-of-range declared tier (5) is ignored; class tier 0 applies.
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "read.document",
		"resource": "doc:1",
		"context": {"tier": 5},
	}
	d.effect == "allow"
	d.tier == 0
}

test_unclassified_deny if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "mystery.action",
		"resource": "x:1",
		"context": {},
	}
	d.effect == "deny"
	d.reason == "unclassified action"
}

test_unclassified_with_declared_tier_still_deny if {
	# A caller cannot self-classify an unknown action by declaring a tier.
	# The declared tier only escalates a classified action; it never
	# substitutes for action-class classification (deny-by-default holds).
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "mystery.action",
		"resource": "x:1",
		"context": {"tier": 0},
	}
	d.effect == "deny"
	d.reason == "unclassified action"
}

test_missing_principal_deny if {
	d := govern.decision with input as {
		"principal": {"id": ""},
		"action": "read.document",
		"resource": "doc:1",
		"context": {},
	}
	d.effect == "deny"
	d.reason == "missing or invalid principal"
}

# --- NE-T2X-5 (design §6.3, A-P1-5): provisional-context escalation --------

test_provisional_write_internal_escalates if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.notes.append",
		"resource": "note:1",
		"context": {"tier": 1, "context_provisional": true, "extraction_mode": "read_flagged"},
	}
	d.effect == "require-approval"
	d.tier == 2
	d.reason == "tier 2: write on provisional context — escalated one tier, per-item human approval required"
	d.obligations.per_item_human_approval == true
	d.obligations.provisional_context == true
}

# YOLO PIN: `autonomous` mode threads the flag but does NOT escalate — this is
# the DECLARED meaning of YOLO (design §6.3), not an oversight. A later reader
# must not "fix" this into an escalation.
test_provisional_yolo_threads_but_does_not_escalate if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.notes.append",
		"resource": "note:1",
		"context": {"tier": 1, "context_provisional": true, "extraction_mode": "autonomous"},
	}
	d.effect == "allow"
	d.tier == 1
	d.obligations.daily_digest == true
	not d.obligations.provisional_context
}

test_provisional_read_does_not_escalate if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "read.document",
		"resource": "doc:1",
		"context": {"context_provisional": true, "extraction_mode": "read_flagged"},
	}
	d.effect == "allow"
	d.tier == 0
}

test_provisional_finance_clamps_at_3_via_existing_rule if {
	# finance.* is already tier 3; escalation requires effective_tier <= 2, so
	# this reaches the EXISTING rule (f), not (f2) — the reason string proves
	# which rule fired (no "escalated one tier" wording, no
	# provisional_context obligation).
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "finance.payment.initiate",
		"resource": "acct:1",
		"context": {"context_provisional": true, "extraction_mode": "read_flagged"},
	}
	d.effect == "require-approval"
	d.tier == 3
	d.reason == "tier 3: finance/legal action — named approver plus a second check"
	not d.obligations.provisional_context
}

test_provisional_absent_mode_fails_toward_escalation if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.notes.append",
		"resource": "note:1",
		"context": {"context_provisional": true},
	}
	d.effect == "require-approval"
	d.tier == 2
	d.obligations.provisional_context == true
}

test_provisional_nonboolean_string_does_not_escalate if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.notes.append",
		"resource": "note:1",
		"context": {"context_provisional": "true", "extraction_mode": "read_flagged"},
	}
	d.effect == "allow"
	d.tier == 1
}

test_provisional_write_external_escalates_to_3 if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.external.email.send",
		"resource": "post:1",
		"context": {"context_provisional": true, "extraction_mode": "read_flagged"},
	}
	d.effect == "require-approval"
	d.tier == 3
	d.reason == "tier 3: write on provisional context — escalated one tier, named approver plus a second check"
	d.obligations.named_approver == true
	d.obligations.second_check == true
	d.obligations.provisional_context == true
}

test_provisional_absent_control_allows if {
	# Bidirectional control: the SAME action with no provisional keys at all.
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "write.internal.notes.append",
		"resource": "note:1",
		"context": {},
	}
	d.effect == "allow"
	d.tier == 1
}

# --- approval window: a per-action policy property (owner req, 2026-08-30) ---
#
# The window a human gets to answer an approval is declared BY POLICY, per
# action, and is overridable per deployment. It is emphatically not derived
# from a session lifetime -- that accident is what stranded four runs on
# 2026-08-28.
#
# NOTE ON PLACEMENT. This is asserted on the RULE, not on decision.obligations,
# and that is deliberate. `obligations` is the Rego/Cedar PARITY SURFACE --
# packages/ne-govern/tests/test_cedar_parity.py compares the two engines' documents
# bit-for-bit -- and Cedar carries obligations as STATIC @ne_obligations
# annotations per rule. A window resolved from `data` per action cannot be a
# static annotation, so emitting it into obligations breaks parity BY
# CONSTRUCTION. It was emitted there first, CI caught it, and the correct home
# is a PolicyDecision field (the `tier` precedent, added in contracts 1.2.0)
# mirrored in the Cedar evaluator -- not an obligation.

test_tier2_window_falls_back_to_the_builtin_floor if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}}
	w == 14400
}

test_tier3_window_falls_back_to_the_builtin_floor if {
	w := govern.approval_window_seconds with input as {"action": "finance.wire.send", "principal": {"id": "user:ops"}, "context": {}}
	w == 86400
}

test_a_deployment_can_set_the_window_per_tier if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.approval_windows as {"tiers": {"2": 600}}
	w == 600
}

test_an_exact_action_override_beats_the_tier_default if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.approval_windows as {"tiers": {"2": 600}, "actions": {"write.external.email.send": 86400}}
	w == 86400
}

test_an_override_for_a_different_action_does_not_leak if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.approval_windows as {"actions": {"finance.wire.send": 60}}
	w == 14400
}

# A garbage or non-positive override must not silently become the window: it
# falls through to the next resolution step rather than yielding 0 or a string,
# because a zero window would make every approval instantly stale -- the exact
# failure mode this whole change exists to remove.
test_a_zero_window_is_ignored_not_honoured if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.email.send": 0}}
	w == 14400
}

test_a_non_numeric_window_is_ignored_not_honoured if {
	w := govern.approval_window_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.email.send": "600"}}
	w == 14400
}

# An escalated tier-1 write (provisional context) becomes a tier-2 approval, so
# it must resolve a tier-2 window -- the escalation changes final_tier, which is
# what the resolution keys on.
test_an_escalated_write_resolves_the_escalated_tier_window if {
	w := govern.approval_window_seconds with input as {"action": "write.internal.crm.note", "principal": {"id": "user:ops"}, "context": {"context_provisional": true}}
	w == 14400
}

# Autonomous tiers have no approval to time-box, so the rule is UNDEFINED for
# them -- not zero, not a default. An answer where none is meaningful is noise
# that downstream code eventually reads as meaningful.
test_autonomous_tiers_resolve_no_window if {
	not govern.approval_window_seconds with input as {"action": "read.kb.search", "principal": {"id": "user:ops"}, "context": {}}
}

# --- the window rides ON THE DECISION (SPEC §12 item 5, bundle 1.3.0) --------
#
# The resolution rule above proved the POLICY could answer; these prove the
# ANSWER travels in the one document the runtime actually evaluates
# (/v1/data/ne/govern/decision). The defect this closes: the runtime read
# decision.obligations["approval_window_seconds"], the value lived in a sibling
# rule, and _approval_window_s returned None on every path that ever executed.
# The carrier is a FIRST-CLASS key (the `tier` precedent), NOT an obligation —
# obligations are the Rego/Cedar parity surface and Cedar obligations are
# static per-rule annotations that cannot express a data-resolved value.

test_a_tier2_decision_carries_its_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}}
	d.approval_window_seconds == 14400
	not d.obligations.approval_window_seconds
}

test_a_tier3_decision_carries_its_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "finance.wire", "resource": "acct:1", "context": {}}
	d.approval_window_seconds == 86400
}

test_a_configured_action_window_reaches_the_decision if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.email.send", "resource": "m:1", "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.email.send": 600}}
	d.approval_window_seconds == 600
}

test_an_escalated_write_decision_carries_the_escalated_windows_tier if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.internal.crm.note", "resource": "n:1", "context": {"context_provisional": true}}
	d.effect == "require-approval"
	d.approval_window_seconds == 14400
}

test_an_allow_decision_carries_a_null_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "read.kb.search", "resource": "kb:1", "context": {}}
	d.approval_window_seconds == null
}

test_a_deny_decision_carries_a_null_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "mystery.action", "resource": "x:1", "context": {}}
	d.approval_window_seconds == null
}

# --- window PROVENANCE (bundle 1.4.0) ---------------------------------------
#
# The three resolution steps all write `approval_window_seconds`, so a consumer
# could not tell a number this deployment DECLARED from one the product ships
# as a floor. `approval_window_declared` is that distinction, and the runtime's
# park-time coherence check is its only consumer: it refuses a promise the
# deployment wrote down and warns-and-proceeds on a floor, because refusing on
# a floor punishes a decision nobody took.

test_the_builtin_floor_is_not_a_declaration if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}}
	d.approval_window_seconds == 14400
	d.approval_window_declared == false
}

test_a_tier3_builtin_floor_is_not_a_declaration if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "finance.wire", "resource": "acct:1", "context": {}}
	d.approval_window_seconds == 86400
	d.approval_window_declared == false
}

test_an_action_override_is_a_declaration if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.email.send", "resource": "m:1", "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.email.send": 600}}
	d.approval_window_seconds == 600
	d.approval_window_declared == true
}

test_a_tier_override_is_a_declaration if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}} with data.ne.config.approval_windows as {"tiers": {"2": 600}}
	d.approval_window_seconds == 600
	d.approval_window_declared == true
}

# A declaration for a DIFFERENT action leaves this one on the floor, so the bit
# must follow the window that actually resolved -- not the mere presence of a
# document. Without this a deployment declaring one action's window would make
# every other action's floor look declared, and the guard would refuse them all.
test_a_declaration_for_another_action_does_not_make_this_one_declared if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}} with data.ne.config.approval_windows as {"actions": {"finance.wire.send": 60}}
	d.approval_window_seconds == 14400
	d.approval_window_declared == false
}

# A garbage override is a typo, not a declaration: the window falls through to
# the floor and the provenance falls through with it.
test_an_ignored_override_is_not_a_declaration if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.post": 0}}
	d.approval_window_seconds == 14400
	d.approval_window_declared == false
}

test_an_allow_decision_declares_no_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "read.kb.search", "resource": "kb:1", "context": {}}
	d.approval_window_seconds == null
	d.approval_window_declared == false
}

test_a_deny_decision_declares_no_window if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "mystery.action", "resource": "x:1", "context": {}}
	d.approval_window_seconds == null
	d.approval_window_declared == false
}

# --- the STANDING lifetime, resolved beside the window (F1, bundle 1.5.0) ----
#
# `approval_window_seconds` bounds how long the HUMAN has to answer.
# `standing_approval_seconds` bounds how long the ANSWER outlives the attempt
# it was asked for, once that attempt has terminalized `authorization_expired`.
# Two clocks, two questions, one resolution shape — so a deployment configures
# both in one document and neither is ever inherited from a session length.

test_tier2_standing_lifetime_falls_back_to_the_builtin_floor if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}}
	s == 604800
}

# Tier 3 is IRREVERSIBLE, so its floor is SHORTER than tier 2's, not longer:
# what a standing approval concedes is that the decision may be older than the
# execution, and that concession must be smallest where the effect cannot be
# undone.
test_tier3_standing_lifetime_is_shorter_than_tier2 if {
	s3 := govern.standing_approval_seconds with input as {"action": "finance.wire.send", "principal": {"id": "user:ops"}, "context": {}}
	s2 := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}}
	s3 == 172800
	s3 < s2
}

test_a_deployment_can_set_the_standing_lifetime_per_tier if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.standing_approvals as {"tiers": {"2": 3600}}
	s == 3600
}

test_an_exact_action_standing_override_beats_the_tier_default if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.standing_approvals as {"tiers": {"2": 3600}, "actions": {"write.external.email.send": 900}}
	s == 900
}

test_a_standing_override_for_a_different_action_does_not_leak if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.standing_approvals as {"actions": {"finance.wire.send": 60}}
	s == 604800
}

# Same discipline as the window: a zero lifetime would lapse every decision the
# instant it was made, so it is IGNORED, never honoured.
test_a_zero_standing_lifetime_is_ignored_not_honoured if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.standing_approvals as {"actions": {"write.external.email.send": 0}}
	s == 604800
}

test_a_non_numeric_standing_lifetime_is_ignored_not_honoured if {
	s := govern.standing_approval_seconds with input as {"action": "write.external.email.send", "principal": {"id": "user:ops"}, "context": {}} with data.ne.config.standing_approvals as {"actions": {"write.external.email.send": "600"}}
	s == 604800
}

test_an_escalated_write_resolves_the_escalated_tier_standing_lifetime if {
	s := govern.standing_approval_seconds with input as {"action": "write.internal.crm.note", "principal": {"id": "user:ops"}, "context": {"context_provisional": true}}
	s == 604800
}

test_autonomous_tiers_resolve_no_standing_lifetime if {
	not govern.standing_approval_seconds with input as {"action": "read.kb.search", "principal": {"id": "user:ops"}, "context": {}}
}

# The two clocks are INDEPENDENT: configuring one must not move the other.
test_configuring_the_window_does_not_move_the_standing_lifetime if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.email.send", "resource": "m:1", "context": {}} with data.ne.config.approval_windows as {"actions": {"write.external.email.send": 21600}}
	d.approval_window_seconds == 21600
	d.standing_approval_seconds == 604800
}

test_a_tier2_decision_carries_its_standing_lifetime if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.post", "resource": "post:1", "context": {}}
	d.standing_approval_seconds == 604800
	not d.obligations.standing_approval_seconds
}

test_a_tier3_decision_carries_its_standing_lifetime if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "finance.wire", "resource": "acct:1", "context": {}}
	d.standing_approval_seconds == 172800
}

test_a_configured_action_standing_lifetime_reaches_the_decision if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "write.external.email.send", "resource": "m:1", "context": {}} with data.ne.config.standing_approvals as {"actions": {"write.external.email.send": 900}}
	d.standing_approval_seconds == 900
}

test_an_allow_decision_carries_a_null_standing_lifetime if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "read.kb.search", "resource": "kb:1", "context": {}}
	d.standing_approval_seconds == null
}

test_a_deny_decision_carries_a_null_standing_lifetime if {
	d := govern.decision with input as {"principal": {"id": "u1"}, "action": "mystery.action", "resource": "x:1", "context": {}}
	d.standing_approval_seconds == null
}
