// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TRANSCRIPT MODULE — the public surface, and the whole of it.
//
// Two consumers, each with its own adapter and its own wire shapes: Ask builds
// TranscriptTurn[] from the SSE frames it already classifies (chat/
// answerFrames.ts, chat/turnFailure.ts); the Agents Run workspace builds them
// from agents/types.ts. Neither is imported here, and this module reaches for
// neither — it takes a view model and draws it.
//
//   <Transcript turns={…} label="Answers" />   the thread
//   <TranscriptTurnView turn={…} />            one turn, for a single-turn view
//   partitionTurnSteps(steps)                  the promotion rule, as a rule
//   bandTally / hasLiveStep / formatSeconds    what the band's summary is made of
//
// The scrolling surface, the composer, the session rail and the docked panes
// stay with the screens that own them.

export * from "./types";
export * from "./promotion";
export * from "./Transcript";
