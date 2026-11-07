# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# OPA-native unit tests for the NE-169 subagent.route rule (routing AUTHORITY,
# design §10.2). Runs via `opa test packages/ne-govern/policies -v` on any host
# with the opa binary (excluded from built bundles). Rego v1 syntax.
package ne.govern_test

import data.ne.govern

test_subagent_route_private_allows if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {"route_tier": "private", "route_task": "ap.extract", "route_lane": "private-vllm"},
	}
	d.effect == "allow"
	d.tier == 0

	# NE-T2X-5 bumped the bundle to 1.2.0 (the provisional-context escalation
	# rules). This literal is a deliberate "did you mean to bump?" tripwire and
	# it did its job — but note the coupling: TWO behavioural routing tests
	# (here and decision_test.rego:20) each carry a version literal that must be
	# hand-bumped in lockstep with version.rego + .manifest, and this unit's
	# first pass bumped only the sibling. The canonical pin is elsewhere and
	# unaffected: test_bundle.py::test_manifest_revision_matches_policy_version
	# asserts .manifest == version.rego.
	d.policy_version == "1.5.0"
}

test_subagent_route_unpinned_allows if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {"route_tier": "unpinned"},
	}
	d.effect == "allow"
	d.tier == 0
}

test_subagent_route_frontier_denied_by_default if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {"route_tier": "frontier"},
	}
	d.effect == "deny"
	d.tier == null
	d.reason == "subagent.route: frontier-lane routing requires an explicit grant — default deny"
}

test_subagent_route_unknown_tier_unclassified_denies if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {"route_tier": "mystery"},
	}
	d.effect == "deny"
	d.reason == "unclassified action"
}

test_subagent_route_missing_context_denies if {
	d := govern.decision with input as {
		"principal": {"id": "u1"},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {},
	}
	d.effect == "deny"
	d.reason == "unclassified action"
}

test_subagent_route_frontier_invalid_principal_denies if {
	d := govern.decision with input as {
		"principal": {},
		"action": "subagent.route",
		"resource": "subagent:x",
		"context": {"route_tier": "frontier"},
	}
	d.effect == "deny"
	d.reason == "missing or invalid principal"
}
