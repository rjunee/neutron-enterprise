# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# NE-169: subagent.route — the frontier sub-agent routing AUTHORITY (design
# §10.2). The dispatcher consults the PDP where the child lane is resolved;
# route_tier is computed by the TRUSTED dispatcher from the server-registered
# AgentDefinition (ne-runtime dispatch._route_tier), never from model input.
# Frontier grant mechanism is a deliberate follow-up parameter — v1 is
# default-deny.
package ne.govern

subagent_route if input.action == "subagent.route"

candidate_tiers contains 0 if {
	subagent_route
	input.context.route_tier == "private"
}

candidate_tiers contains 0 if {
	subagent_route
	input.context.route_tier == "unpinned"
}

subagent_route_frontier_denied if {
	subagent_route
	input.context.route_tier == "frontier"
}
