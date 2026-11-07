// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE EXECUTION STRATEGY, SAID OUT LOUD.
//
// SPEC (PRE-DEMO-REHEARSAL-012): "There is no silent deterministic fallback
// labelled agentic and no decorative mode badge inferred merely from the
// presence of chat or a tool call." So this component takes the SERVER's
// `default_strategy` and renders it. It has no other input and no fallback:
// there is deliberately no way to reach it from `workflow !== null`, from the
// presence of an `input_schema`, from the absence of a model candidate, or from
// any other client-side tell. A caller that does not hold the server's answer
// passes `null` and gets nothing rendered, which is the honest outcome — a
// badge is a claim about how execution is authorized, and the console is not
// the authority for that claim.
//
// The two strategies are not drawn with equal weight, and that asymmetry is the
// point. Agentic is the resting state of a conversational surface and reads
// quietly. Deterministic is the fact the owner said must be "shown prominently":
// the plan is fixed before the run starts, the model cannot choose the next
// action, and the execution surface itself looks different because of it.

import type { AgentExecutionStrategyWire } from "../api/types";

/** The literal copy, held here so a test can assert the string rather than a
 * JSX subtree, the way `chat/askCopy.ts` holds Ask's. */
export const STRATEGY_AGENTIC = "Agentic";
export const STRATEGY_DETERMINISTIC = "Deterministic";
export const STRATEGY_AGENTIC_GLOSS = "model-directed within caps";
export const STRATEGY_DETERMINISTIC_GLOSS = "fixed plan";

export function strategyWord(strategy: AgentExecutionStrategyWire): string {
  return strategy === "deterministic" ? STRATEGY_DETERMINISTIC : STRATEGY_AGENTIC;
}

export function strategyGloss(strategy: AgentExecutionStrategyWire): string {
  return strategy === "deterministic" ? STRATEGY_DETERMINISTIC_GLOSS : STRATEGY_AGENTIC_GLOSS;
}

/**
 * @param strategy The server's `default_strategy`. `null` renders NOTHING —
 *   never a guess and never a placeholder badge.
 * @param gloss Whether to append the one-phrase explanation. The catalog table
 *   omits it (the column is narrow and the word alone is the fact); the agent
 *   header and the launcher carry it.
 */
export function StrategyBadge({
  strategy,
  gloss = false,
}: {
  strategy: AgentExecutionStrategyWire | null;
  gloss?: boolean;
}) {
  if (strategy === null) return null;
  return (
    <span
      className={`ne-strategy ne-strategy--${strategy}`}
      data-strategy={strategy}
      aria-label={`Execution strategy: ${strategyWord(strategy)}`}
    >
      <span className="ne-strategy__word">{strategyWord(strategy)}</span>
      {gloss ? <span className="ne-strategy__gloss">{strategyGloss(strategy)}</span> : null}
    </span>
  );
}
