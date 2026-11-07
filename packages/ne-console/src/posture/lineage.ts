// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ONE derivation of "which configuration is in force, and since when".
//
// It lived inside landing/LandingPage.tsx, which was fine while the home screen
// was the only reader. The status strip now states the same fact on its config
// chip — a human sentence on the face, the 64-character hash in the drawer —
// and two independent readings of one payload is how two surfaces come to
// disagree. It is here, imported by both, and by neither's page module (which
// would be an import cycle: LandingPage → PageFrame → StatusStrip).

/** The config_hash span currently in force (`to === null`), if the server sent
 * one. Pure; exported for its own test. */
export function spanInForce(
  history: Array<{ hash: string; from: string; to: string | null }>,
): { hash: string; from: string } | null {
  const span = history.find((s) => s.to === null);
  return span ? { hash: span.hash, from: span.from } : null;
}
