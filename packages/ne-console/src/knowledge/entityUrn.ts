// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The console-side contract point for the `urn:ne:entity:<key>` citation scheme
// that NE-135/136's verified-answer chips mint. A verified-answer row (or a
// ?entity= deep-link) carries an entity as `urn:ne:entity:<GraphNodeWire.key>`;
// parseEntityUrn extracts the key so the chamber can light that node. This is
// the SINGLE parser — if NE-135 lands a different shape, only this file changes
// (NE-134 build brief risk #3).

const PREFIX = "urn:ne:entity:";

/** `urn:ne:entity:<key>` -> `<key>` (the GraphNodeWire.key); anything else ->
 * null. The remainder after the prefix must be non-empty. */
export function parseEntityUrn(urn: string): string | null {
  if (!urn.startsWith(PREFIX)) return null;
  const key = urn.slice(PREFIX.length);
  return key.length > 0 ? key : null;
}
