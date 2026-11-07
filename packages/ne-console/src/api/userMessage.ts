// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ONE PLACE A TRANSPORT FAILURE BECOMES A SENTENCE A READER CAN READ.
//
// WHY THIS FILE EXISTS. Every transport error carried exactly one string, and
// it was the client's own: "request to /v1/approvals/8f3a…/decision failed with
// HTTP 502". When the server sent a JSON `detail` the screens rendered that
// instead, so the defect stayed invisible in every test that stubbed a governed
// refusal — and showed only where no `detail` exists at all: a gateway 502, a
// proxy timeout, an HTML error page. Which is to say it was invisible until the
// room was watching. Twenty call sites each wrote `err.detail ?? err.message`,
// and twenty got it wrong the same way, because the fallback was wrong at the
// seam and every screen faithfully reproduced it. Fixing twenty call sites
// would have been the same defect waiting for the twenty-first.
//
// THE SERVER'S SENTENCE ALWAYS WINS. When the governed API sent a `detail`,
// that string is returned verbatim — it is the only account of what happened
// that is not the console guessing (primitives/states.tsx, SaveError). The
// sentences below exist only for when there is nothing to prefer them to.
//
// THEY ARE BARE FACTS, AND THAT IS DELIBERATE. This string lands in the slot a
// server `detail` lands in, and those are bare facts ("not in the Tier-3
// named-approver list"). The slot's OWNER supplies §2.4's next action — the
// decision pane's own line is already "Couldn't submit the decision. Try
// again.", the Answer Ledger's alone-rendered detail already sits beside a
// Retry button. Appending "Try again." here would print it twice.
//
// NO ENDPOINT PATH, NO STATUS CODE, EVER. §8 permits no endpoint path on the
// approvals screen, and a reader who came to decide an approval did not come to
// learn which route the console called.

/**
 * The house sentence for a transport failure the server gave no `detail` for.
 *
 * Keyed on status CLASS. Every arm is one clause, present tense, ≤ 12 words.
 */
function sentenceForStatus(status: number): string {
  // No HTTP response at all — fetch rejected, or a throw carrying 0/NaN.
  if (!Number.isFinite(status) || status < 100) return "The server couldn’t be reached.";
  if (status === 401) return "Your session has ended.";
  // The wording §2.4's Refused state uses, minus its own next action.
  if (status === 403) return "Your role doesn’t include this action.";
  if (status === 404) return "That’s no longer there.";
  if (status === 408 || status === 504) return "The server took too long to answer.";
  if (status === 409) return "Someone changed this first.";
  if (status === 429) return "Too many requests just now.";
  if (status >= 500) return "The server is unavailable.";
  return "The request wasn’t accepted.";
}

/**
 * The string a screen may render for a transport failure — ALWAYS populated.
 *
 * The server's `detail` verbatim when it sent one; otherwise the house sentence
 * for the status class. Never the client-composed developer string, which lives
 * on `developerMessage` for the console and for telemetry.
 */
export function transportUserMessage(status: number, detail?: string): string {
  const served = detail?.trim();
  if (served !== undefined && served !== "") return served;
  return sentenceForStatus(status);
}
