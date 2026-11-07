// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Cedar mirror of the normative Rego bundle (policies/ne/govern/{decision,tiers,
// version}.rego). Eight mutually-exclusive, COMPLETE rules — a total function
// over every input — exactly like decision.rego's eight tier-decision
// `decision := {...} if {...}` definitions (a)-(f2) (decision.rego's rule (g),
// the NE-169 subagent-route-frontier deny, has never been mirrored here — the
// rule itself is decision.rego's ninth definition and its trigger predicate
// lives in the separate subagent.rego file, out of this bundle's scope both
// before and after this change). There is no Cedar equivalent of Rego's
// aggregate document (this file never assembles a JSON object itself) —
// instead the ANNOTATION-BORNE part of the NE decision document (effect /
// tier / reason / obligations / policy_version) rides on each rule's
// annotations, while approval_window_seconds and approval_window_declared are
// resolved by CedarEvaluator._approval_window_for from the deployment's
// window mapping (a per-action value resolved from data cannot be a static
// annotation — that is why it is a decision FIELD and not an obligation), and
// ne.govern.cedar.evaluator.CedarEvaluator is the ONLY reader of those
// annotations — it probes each rule's boolean condition (never the annotated
// keyword) through the real Cedar engine and enforces, at runtime, that
// EXACTLY ONE rule's condition is true for any given input (the
// totality/exclusivity invariant) — a parser or condition defect that breaks
// this fails CLOSED as PolicyBackendError, never open.
//
// C12 / conservatism (belt-and-suspenders on TOP of the runtime invariant
// above): the Cedar EFFECT KEYWORD on every rule is chosen conservatively —
// ONLY the two NE-"allow" rules ((c) tier 0, (d) tier 1) are Cedar `permit` —
// every NE-"deny" and NE-"require-approval" rule ((a), (b), (e), (f), and the
// two NE-T2X-5 escalation rules (e2), (f2) — including tier-2 and tier-3, both
// unescalated AND escalated) is a Cedar `forbid`. This means a hypothetical
// plain-Cedar consumer that evaluated this policy set directly (ignoring the
// annotations and the evaluator entirely) could only ever be MORE restrictive
// than the NE semantics, never less: a tier-2/3 action can never resolve to a
// bare-Cedar Allow from this file, by construction.
//
// FRAGMENTS (inlined in every rule below — named here only for the mutual-
// exclusivity argument that follows):
//   P   := context.principal_id != ""
//   C0  := context.ne_action like "read.*"  ||  context.ne_action like "draft.*"
//   C1  := context.ne_action like "write.internal.*"
//   C2  := context.ne_action like "write.external.*"  ||  context.ne_action like "*.pii.*"
//   C3  := context.ne_action like "finance.*"  ||  context.ne_action like "legal.*"
//   CL  := C0 || C1 || C2 || C3                          (classified: tiers.rego's `classified`)
//   Dk  := context has declared_tier && context.declared_tier == k   (k in 1, 2, 3)
//   ESC := context has context_provisional &&
//          context.context_provisional == true &&
//          !(context has extraction_mode && context.extraction_mode == "autonomous") &&
//          context.ne_action like "write.*"
//          (tiers.rego's `escalates`, minus the `effective_tier <= 2` bound —
//          that bound is enforced STRUCTURALLY here instead: ESC is only ever
//          ANDed into the (d)/(e)/(e2)/(f2) conditions, which already require
//          effective_tier in {1, 2} via C1/D1 or C2/D2 — see the DERIVATION
//          note below.)
//
// MUTUAL-EXCLUSIVITY ARGUMENT (rule-by-rule, mirrors tiers.rego's
// stricter-of/max composition, extended one level for NE-T2X-5):
//
//   * !P            -> only (a) matches (every other rule requires P).
//   * P && !CL      -> only (b) matches (every tier rule (c)-(f2) requires CL).
//   * P && CL       -> exactly one of (c)/(d)/(e)/(e2)/(f)/(f2) matches:
//       (f)  fires iff (C3||D3)                                -- tier 3, unescalated
//       (f2) fires iff !(C3||D3) && (C2||D2) &&  ESC            -- tier 2->3 escalated
//       (e)  fires iff !(C3||D3) && (C2||D2) && !ESC            -- tier 2, unescalated
//       (e2) fires iff !(C3||D3) && !(C2||D2) && (C1||D1) &&  ESC  -- tier 1->2 escalated
//       (d)  fires iff !(C3||D3) && !(C2||D2) && (C1||D1) && !ESC  -- tier 1, unescalated
//       (c)  fires iff !(C3||D3) && !(C2||D2) && !(C1||D1)      -- tier 0 (ESC is
//                                                                   structurally
//                                                                   false here: ESC
//                                                                   requires
//                                                                   `ne_action like
//                                                                   "write.*"`, and a
//                                                                   tier-0-only
//                                                                   action is never
//                                                                   write.*-prefixed
//                                                                   — see DERIVATION)
//     These six conditions partition {P && CL} exactly (each subsequent rule
//     negates every higher-tier trigger the previous rules tested, and within
//     a tier the ESC / !ESC split is itself exhaustive and disjoint — ESC is
//     a plain boolean, so exactly one of ESC/!ESC holds for any given input),
//     so for any P&&CL input precisely one of (c)/(d)/(e2)/(e)/(f2)/(f) is
//     true.
//
// DERIVATION (NE-T2X-5 escalation is DERIVED from, not invented alongside,
// tiers.rego's max/+1 composition — design §6.3): tiers.rego computes
// `escalates := classified && write_class && provisional_context &&
// not yolo_exempt && effective_tier <= 2`. `write_class` requires the
// `write.` action prefix; NO candidate_tiers rule ever maps a `write.`-
// prefixed action to tier 0 (only `read.*`/`draft.*` do), so an
// effective_tier-0 action can never satisfy `write_class` — this is exactly
// why (c) needs no ESC guard at all: ESC is unconditionally false whenever
// C1||C2||C3 are all false. Likewise `effective_tier <= 2` in Rego means
// escalation can only originate from an effective_tier of 1 or 2 — which is
// exactly the (d)/(e) branches here — never from the (f) branch (effective
// tier 3), so (f) needs no ESC exclusion either (ESC being true there would
// be vacuous: no C3||D3 input can also make write_class necessary for the
// SAME effective-tier reason `escalates` never fires past tier 2). `final_tier
// := effective_tier + 1 if escalates` is realized here by ROUTING: instead of
// computing a scalar and re-testing it, (e2)/(f2) simply fire in exactly the
// input space Rego's `escalates` predicate covers at effective_tier 1/2
// respectively, and (d)/(e) exclude that same space via `!(ESC)` — so `(d)
// unescalated -> tier 1` / `(e2) escalated -> tier 2` and `(e) unescalated ->
// tier 2` / `(f2) escalated -> tier 3` are exactly Rego's `final_tier ==
// effective_tier` vs `final_tier == effective_tier + 1` split, without ever
// needing a `final_tier` variable in Cedar's rule language.
//
// EQUIVALENCE TO tiers.rego's stricter-of composition: tiers.rego computes
// class_tier := max(candidate_tiers) (the set of ALL matching action-class
// tiers, not just the highest) and effective_tier := max(declared_tier,
// class_tier) when the declared tier is valid (is_number, round-equal,
// 0<=x<=3), else class_tier alone. Declared_tier marshalling (in
// CedarEvaluator.evaluate) admits only ints/integral-floats in i64 range as
// `declared_tier`, EXACTLY mirroring tiers.rego's valid_declared filter
// (booleans/strings/fractional floats/out-of-representable-range values are
// OMITTED from Cedar's context entirely, so Dk can never fire for them) — the
// 0..3 RANGE check itself is enforced here, in Cedar, by Dk's `== k` equality
// (k in {1,2,3}) — a declared_tier outside {0,1,2,3} (e.g. -1, 10) simply never
// satisfies any Dk, so it can never escalate, exactly like an invalid
// tiers.rego declared_tier falling back to class_tier alone. A declared tier of
// 0 needs no D0 fragment: the tier-0 rule (c) is defined as the ABSENCE of any
// D1/D2/D3 (or C1/C2/C3) escalation — declared 0 and "no declared tier at all"
// collapse to the identical unescalated case, matching tiers.rego's max(0,
// class_tier) == class_tier when class_tier >= 0. Because Dk ORs directly into
// each tier's OWN class condition (Ck || Dk) rather than computing a single
// scalar "effective_tier", a declared tier can only ESCALATE (OR only ever
// turns a false disjunct true) — it can never suppress a higher Ck that is
// already true (see (f): C3||D3 is true whenever C3 alone is true, regardless
// of any lower declared value) — the same one-directional "stricter-of" the
// Rego enforces via max().

@id("a_invalid_principal")
@ne_effect("deny")
@ne_tier("null")
@ne_reason("missing or invalid principal")
@ne_obligations("{}")
@policy_version("1.5.0")
// (a) missing or invalid principal -> deny
forbid (principal, action, resource) when {
	context.principal_id == ""
};

@id("b_unclassified")
@ne_effect("deny")
@ne_tier("null")
@ne_reason("unclassified action")
@ne_obligations("{}")
@policy_version("1.5.0")
// (b) valid principal, but the action classifies to no tier -> deny
forbid (principal, action, resource) when {
	context.principal_id != "" &&
	!(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	)
};

@id("c_tier0")
@ne_effect("allow")
@ne_tier("0")
@ne_reason("tier 0: read/internal draft — autonomous, logged")
@ne_obligations("{}")
@policy_version("1.5.0")
// (c) tier 0 -> allow (autonomous, logged)
permit (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	!(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	) &&
	!(
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context has declared_tier && context.declared_tier == 2)
	) &&
	!(
		context.ne_action like "write.internal.*" ||
		(context has declared_tier && context.declared_tier == 1)
	)
};

@id("d_tier1")
@ne_effect("allow")
@ne_tier("1")
@ne_reason("tier 1: internal write — autonomous, surfaced in the daily digest")
@ne_obligations("{\"daily_digest\": true}")
@policy_version("1.5.0")
// (d) tier 1 -> allow with daily-digest obligation. `!(ESC)` excludes the
// NE-T2X-5 provisional-escalation case (e2) picks up instead.
permit (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	!(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	) &&
	!(
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context has declared_tier && context.declared_tier == 2)
	) &&
	(
		context.ne_action like "write.internal.*" ||
		(context has declared_tier && context.declared_tier == 1)
	) &&
	!(
		context has context_provisional &&
		context.context_provisional == true &&
		!(context has extraction_mode && context.extraction_mode == "autonomous") &&
		context.ne_action like "write.*"
	)
};

@id("e_tier2")
@ne_effect("require-approval")
@ne_tier("2")
@ne_reason("tier 2: external/PII action — per-item human approval required")
@ne_obligations("{\"per_item_human_approval\": true}")
@policy_version("1.5.0")
// (e) tier 2 -> require-approval (per-item human). `!(ESC)` excludes the
// NE-T2X-5 provisional-escalation case (f2) picks up instead.
forbid (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	!(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	) &&
	(
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context has declared_tier && context.declared_tier == 2)
	) &&
	!(
		context has context_provisional &&
		context.context_provisional == true &&
		!(context has extraction_mode && context.extraction_mode == "autonomous") &&
		context.ne_action like "write.*"
	)
};

@id("e2_tier2_provisional")
@ne_effect("require-approval")
@ne_tier("2")
@ne_reason("tier 2: write on provisional context — escalated one tier, per-item human approval required")
@ne_obligations("{\"per_item_human_approval\": true, \"provisional_context\": true}")
@policy_version("1.5.0")
// (e2) NE-T2X-5 (design §6.3, A-P1-5): (d)'s effective-tier-1 fragment PLUS
// ESC — a write.internal.* (or declared-tier-1) action escalates from tier 1
// to tier 2 when the retrieval-derived context is provisional and the mode is
// not "autonomous" (YOLO threads the flag but never reaches this rule — see
// the DERIVATION note above the rule set).
forbid (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	!(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	) &&
	!(
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context has declared_tier && context.declared_tier == 2)
	) &&
	(
		context.ne_action like "write.internal.*" ||
		(context has declared_tier && context.declared_tier == 1)
	) &&
	(
		context has context_provisional &&
		context.context_provisional == true &&
		!(context has extraction_mode && context.extraction_mode == "autonomous") &&
		context.ne_action like "write.*"
	)
};

@id("f2_tier3_provisional")
@ne_effect("require-approval")
@ne_tier("3")
@ne_reason("tier 3: write on provisional context — escalated one tier, named approver plus a second check")
@ne_obligations("{\"named_approver\": true, \"second_check\": true, \"provisional_context\": true}")
@policy_version("1.5.0")
// (f2) NE-T2X-5 (design §6.3, A-P1-5): (e)'s effective-tier-2 fragment PLUS
// ESC — a write.external.*/*.pii.* (or declared-tier-2) action escalates from
// tier 2 to tier 3 under the same provisional/non-YOLO condition.
forbid (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	!(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	) &&
	(
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context has declared_tier && context.declared_tier == 2)
	) &&
	(
		context has context_provisional &&
		context.context_provisional == true &&
		!(context has extraction_mode && context.extraction_mode == "autonomous") &&
		context.ne_action like "write.*"
	)
};

@id("f_tier3")
@ne_effect("require-approval")
@ne_tier("3")
@ne_reason("tier 3: finance/legal action — named approver plus a second check")
@ne_obligations("{\"named_approver\": true, \"second_check\": true}")
@policy_version("1.5.0")
// (f) tier 3 -> require-approval (named approver + second check)
forbid (principal, action, resource) when {
	context.principal_id != "" &&
	(
		(context.ne_action like "read.*" || context.ne_action like "draft.*") ||
		context.ne_action like "write.internal.*" ||
		(context.ne_action like "write.external.*" || context.ne_action like "*.pii.*") ||
		(context.ne_action like "finance.*" || context.ne_action like "legal.*")
	) &&
	(
		(context.ne_action like "finance.*" || context.ne_action like "legal.*") ||
		(context has declared_tier && context.declared_tier == 3)
	)
};
