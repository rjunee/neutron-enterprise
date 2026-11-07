// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The one place the run-compare URL is spelled. The receipt's exit builds an
// href with this, and the compare route parses the same four parameter names —
// a link and its reader that cannot drift apart. Each side is (agent, run) as
// two explicit parameters: no delimiter convention to parse, nothing to guess.

/** The compare view's route. Registered in App.tsx beside the other /agents
 * routes; the static segment outranks `/agents/:agentId` in the router. */
export const RUN_COMPARE_PATH = "/agents/compare";

/** One side of a comparison: a Run named by the two served ids that address it. */
export interface RunCompareRef {
  readonly agentId: string;
  readonly runId: string;
}

export const RUN_COMPARE_PARAMS = {
  a: { agent: "a_agent", run: "a_run" },
  b: { agent: "b_agent", run: "b_run" },
} as const;

/** The compare href, with whichever sides the caller already knows. */
export function runCompareHref(a?: RunCompareRef | null, b?: RunCompareRef | null): string {
  const search = new URLSearchParams();
  if (a) {
    search.set(RUN_COMPARE_PARAMS.a.agent, a.agentId);
    search.set(RUN_COMPARE_PARAMS.a.run, a.runId);
  }
  if (b) {
    search.set(RUN_COMPARE_PARAMS.b.agent, b.agentId);
    search.set(RUN_COMPARE_PARAMS.b.run, b.runId);
  }
  const query = search.toString();
  return query === "" ? RUN_COMPARE_PATH : `${RUN_COMPARE_PATH}?${query}`;
}

/** One side, read back off the URL. Both parameters or nothing: half an
 * address names no Run, and the view then offers the picker instead. */
export function runCompareRefFromSearch(
  search: URLSearchParams,
  side: keyof typeof RUN_COMPARE_PARAMS,
): RunCompareRef | null {
  const agentId = search.get(RUN_COMPARE_PARAMS[side].agent);
  const runId = search.get(RUN_COMPARE_PARAMS[side].run);
  if (agentId === null || agentId === "" || runId === null || runId === "") return null;
  return { agentId, runId };
}
