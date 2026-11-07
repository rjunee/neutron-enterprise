// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE PRE-SUBMIT CAPABILITY GUARD.
//
// Lifted out of the deleted AgentLauncher, unchanged. A Run is admitted against
// an exact agent version, definition hash and capability snapshot; if any of
// them has moved under the reader since the eligibility read, the server will
// refuse the submission. Saying so BEFORE the click is the difference between
// "refresh before continuing" and an opaque 409.
//
// It computes no authorization of its own and never could: every value below is
// a server field compared to another server field, and the only verdict it
// reaches is "these two server answers disagree, so do not send yet."

import type { AgentCapabilityPreflight, AgentDetailView } from "./types";

export function exactHash(value: string | null): value is string {
  return value !== null && /^[0-9a-f]{64}$/.test(value);
}

export function capabilityMismatch(
  agent: AgentDetailView,
  preflight: AgentCapabilityPreflight,
): string | null {
  const capabilities = preflight.capabilities;
  if (!agent.available || !preflight.available) return "This Agent is unavailable.";
  if (capabilities === null) return "Eligible capabilities are unavailable.";
  if (
    preflight.agent_id !== agent.agent_id ||
    preflight.version !== agent.version ||
    preflight.definition_hash !== agent.config_hash ||
    capabilities.agent_version !== agent.version ||
    capabilities.agent_definition_hash !== agent.config_hash
  ) {
    return "Agent eligibility changed. Refresh before continuing.";
  }
  if (!exactHash(capabilities.capability_hash)) {
    return "Eligible capabilities are unavailable.";
  }
  if (
    preflight.model.allowed_strategies.some(
      (strategy) => !agent.allowed_strategies.includes(strategy),
    )
  ) {
    return "Agent model eligibility changed. Refresh before continuing.";
  }
  return null;
}
