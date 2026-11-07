// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7's copy table, spelled once so three tabs cannot drift into three wordings
// of the same fact. Every string here is the spec's, character for character —
// including the plain apostrophe.

/** §7: the knowledge graph is switched off in this deployment. This is neither
 * a fault nor a permission — it is what the deployment is, and it reads that
 * way. */
export const GRAPH_OFF = "The knowledge graph isn't enabled on this deployment.";

/** §7, the Entities input's empty result under the exact-key degradation. */
export const NO_ENTITY = "No entity with that key. Open one from a citation in Agents or Search.";

/** The Graph tab was anchored and the server answered with an empty subgraph.
 * The message NAMES the anchor that failed and says why (SPEC §12 item 8's
 * rule: a miss that renders like a design decision is a silent failure). */
export function noGraphMatch(
  anchorKeys: readonly string[],
  anchorCitations: readonly string[],
): string {
  if (anchorCitations.length > 0) {
    const refs = anchorCitations.map((ref) => `"${ref}"`).join(", ");
    return (
      `No graph entity cites ${refs} as evidence. ` +
      "This citation points at a document, and only sources an entity was extracted from resolve here."
    );
  }
  const keys = anchorKeys.map((key) => `"${key}"`).join(", ");
  return `No entity with key ${keys}. Open one from a citation in Agents or Search.`;
}

/** A node record read that came back empty, naming the key it asked for. */
export function noEntityWithKey(key: string): string {
  return `No entity with key "${key}".`;
}

/** §7, Entities idle — shown only when the deployment has no zero-argument
 * verified query to browse with. States WHY the screen is idle (the library is
 * authored, and nothing browsable is authored yet), not a capability hint. */
export const ENTITIES_IDLE =
  "No verified query on this deployment can browse without an input yet — queries are " +
  "authored per engagement. Open an entity from a citation in Agents or Search.";

/** §7, the authored-ness line. The directory picker's entries read like fixed
 * object types ("Every account…", "Every order…"); this is the sentence that
 * says what they actually are. It must NOT imply an in-product authoring flow —
 * templates are TOML artifacts a team authors and deploys; no screen builds
 * one. Spelled once, shared by Entities and Reference, because two wordings of
 * the same fact is how they drift. */
export const VERIFIED_AUTHORED =
  "Verified queries authored for this deployment — each runs on the server, " +
  "permission-trimmed and replay-checked. New queries are authored and deployed per " +
  "engagement, not built here.";

/** §7, Graph with nothing anchored. */
export const GRAPH_IDLE = "Anchor an entity to draw its neighborhood.";

/** §7, the quarantine lane is not set up in this deployment. */
export const QUARANTINE_UNCONFIGURED = "Violation tracking isn't configured on this deployment.";

/** §7, verified answers are switched off in this deployment. */
export const VERIFIED_OFF = "Verified answers are off on this deployment.";

/** §7, the server re-executed the verified answer and got the same result. */
export const REPLAY_MATCH = "Re-executed and reproduced.";

/** §7, the server re-executed it and did not. */
export const REPLAY_DIVERGED = "Re-execution returned a different result.";
