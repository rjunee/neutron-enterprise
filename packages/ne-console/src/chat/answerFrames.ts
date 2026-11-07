// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// CLASSIFY THE STREAM BEFORE RENDERING IT (round-2 blocker, three reviewers).
//
// What was on screen, verbatim, on the flagship answer pane:
//
//     <think> Okay, the user is asking which supplier contracts renew this
//     quarter. Let me check the available tools… </think>
//     {"tool_call": {"name": "read.knowledge.search", "arguments": {...}}}
//
// — rendered in ANSWER typography, while GOVERNED TOOL CALLS read 0 and the panel
// below said "No tool calls yet". A tool call was printed in the answer body
// while the console asserted none had occurred, on the screen whose own ANATOMY
// OF AN ANSWER legend refuses to draw an example sentence because "an invented
// answer rendered in answer typography is a fabricated artifact".
//
// THE RULE THIS RESTORES. The answer surface may contain ANSWER TEXT and nothing
// else. Anything else the runtime emits is a different kind of thing and gets a
// different frame:
//   * `<think>…</think>`     model reasoning. Not an answer, not evidence for one.
//                            Its body is suppressed from DOM, accessibility,
//                            clipboard and export; callers use only the fact
//                            that a span was observed.
//   * a `tool_call` frame    an INTENT the runtime failed to dispatch. It never
//                            passed a policy decision point, so it must not be
//                            counted as a governed tool call — and it must not be
//                            silently dropped either, because a turn that emitted
//                            one did not complete. Rendered as RAW — UNCLASSIFIED
//                            FRAME in mono, and the turn is marked FAILED.
//   * everything else        the answer.
//
// WHAT THIS IS NOT. It is not a repair of the agent loop — a runtime that emits a
// tool call as text instead of dispatching it is broken, and this module's job is
// to say so plainly rather than to launder it. The console cannot execute the
// call: dispatch runs through the governed executor server-side, which is where
// the PDP decision and the audit record come from.
//
// Pure. No React, no DOM: the same derivation feeds the rendered answer, the
// clipboard export (which was shipping the reasoning trace into documents handed
// to a challenge review) and the failed-turn count.

/** One frame the runtime emitted that is not answer text. */
export interface UnclassifiedFrame {
  /** "reasoning" is segregated but expected; "tool_call" means the loop broke. */
  kind: "tool_call";
  /** The frame verbatim, for the RAW disclosure. Never re-serialised. */
  raw: string;
  /** The tool name, when the frame parsed far enough to carry one. */
  name: string | null;
}

export interface ClassifiedAnswer {
  /** ONLY answer text. This is the only string the answer surface may render. */
  answer: string;
  /** Internal suppression signal. Callers MUST NOT render or export this body;
   * it remains available only to the pure structural guardian for classification. */
  reasoning: string;
  /** Tool-call frames emitted as text and never dispatched. */
  frames: UnclassifiedFrame[];
}

/** `<think>` opened but not yet closed — a still-streaming reasoning span. */
const OPEN_THINK = /<think>([\s\S]*)$/i;
const THINK_SPAN = /<think>([\s\S]*?)<\/think>/gi;

/**
 * A top-level `{"tool_call": …}` object emitted as message text.
 *
 * Deliberately anchored on the KEY rather than on brace matching: the runtime
 * emits it as a whole JSON object on its own, and a key-anchored scan with a
 * brace-depth walk is exact for that shape without pretending to be a JSON
 * parser for arbitrary prose that merely mentions the words.
 */
function findToolCallFrames(text: string): { stripped: string; frames: UnclassifiedFrame[] } {
  const frames: UnclassifiedFrame[] = [];
  let out = "";
  let i = 0;
  while (i < text.length) {
    const open = text.indexOf("{", i);
    if (open === -1) {
      out += text.slice(i);
      break;
    }
    // Cheap pre-check: only walk braces when the object plausibly declares one.
    const head = text.slice(open, open + 40);
    if (!/^\{\s*"tool_call"\s*:/.test(head)) {
      out += text.slice(i, open + 1);
      i = open + 1;
      continue;
    }
    let depth = 0;
    let end = -1;
    let inString = false;
    let escaped = false;
    for (let j = open; j < text.length; j += 1) {
      const ch = text[j];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === "\\") escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === "{") depth += 1;
      else if (ch === "}") {
        depth -= 1;
        if (depth === 0) {
          end = j + 1;
          break;
        }
      }
    }
    if (end === -1) {
      // Unterminated — still streaming. Hold it back rather than flashing a
      // half-written JSON object in answer typography.
      out += text.slice(i, open);
      frames.push({ kind: "tool_call", raw: text.slice(open), name: null });
      break;
    }
    const raw = text.slice(open, end);
    out += text.slice(i, open);
    frames.push({ kind: "tool_call", raw, name: toolNameOf(raw) });
    i = end;
  }
  return { stripped: out, frames };
}

function toolNameOf(raw: string): string | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && "tool_call" in parsed) {
      const call = (parsed as { tool_call?: unknown }).tool_call;
      if (call && typeof call === "object" && "name" in call) {
        const name = (call as { name?: unknown }).name;
        if (typeof name === "string") return name;
      }
    }
  } catch {
    // Not valid JSON — the frame is still unclassified, just unnamed.
  }
  return null;
}

/** Split one assistant message body into answer / reasoning / unclassified. */
export function classifyAnswer(text: string): ClassifiedAnswer {
  const reasoning: string[] = [];
  let body = text.replace(THINK_SPAN, (_m, inner: string) => {
    reasoning.push(inner.trim());
    return "";
  });
  // An unclosed span means the model is still reasoning: everything after the
  // opening tag is reasoning-so-far, and NONE of it is an answer yet.
  const open = OPEN_THINK.exec(body);
  if (open) {
    reasoning.push((open[1] ?? "").trim());
    body = body.slice(0, open.index);
  }
  const { stripped, frames } = findToolCallFrames(body);
  return {
    answer: stripped.replace(/\n{3,}/g, "\n\n").trim(),
    reasoning: reasoning.filter(Boolean).join("\n\n"),
    frames,
  };
}

/** True when the message body carried anything the answer surface may not draw.
 * Used by the guard test and by the FAILED-turn count. */
export function hasUnclassifiedFrames(text: string): boolean {
  return classifyAnswer(text).frames.length > 0;
}
