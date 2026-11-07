// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ROUND 3 (CR-B8, screens/posture.html) — the annunciator TILE.
//
// One of twelve in a 4×3 instrument-panel grid: id · name · state, glyph plus
// word, never a lit block. This is the vocabulary NOTES.md calls out as
// historically broken ("three red blocks on a reader's home screen" for a
// boundary that was working exactly as configured), so it draws EXACTLY three
// states and no others:
//   ● PASSED — green text, no background, no border. The tile is quiet.
//   ⚠ FAILED — the page's only fault colour, and it earns TWO marks: the
//     state word AND the tile's own left edge (`ne-ann--fail`). Nothing else
//     on this screen may carry red.
//   ⊟ MANUAL — gated-neutral. Unverified is not broken, so this state is
//     visually calmer than FAILED, not a lesser shade of it.
//
// NO SOURCE-FILE PATHS (unchanged rule — .impeccable.md rule 2, owner
// 2026-07-31). Only id, name and state render on the tile; the caller's own
// docked pane is where the server's enforcement sentence and evidence class
// render, one click away — never the file list itself, which the wire still
// carries but nothing here or downstream repeats onto a screen.
//
// role="option" inside the caller's role="listbox": a tile click selects it,
// `aria-selected` is the one thing besides colour that marks the open tile
// (a click target is never conveyed by colour alone, per the closed-vocabulary
// rule the rest of this console already keeps).

import type { PostureConstraintView } from "../api/types";

/** The three states this tile may render. Deliberately narrower than
 * StatePill's closed vocabulary: a build-time attestation is never SERVING or
 * APPROVED, and this primitive should not compile if a caller tries. */
export type AnnunciatorState = "PASSED" | "FAILED" | "MANUAL";

const STATE_GLYPH: Record<AnnunciatorState, string> = {
  PASSED: "●",
  FAILED: "⚠",
  MANUAL: "⊟",
};

const STATE_LABEL: Record<AnnunciatorState, string> = {
  PASSED: "Passed",
  FAILED: "Failed",
  MANUAL: "Manual",
};

const STATE_MODIFIER: Record<AnnunciatorState, string> = {
  PASSED: "pass",
  FAILED: "fail",
  MANUAL: "manual",
};

export function AnnunciatorRow({
  constraint,
  state,
  selected,
  onSelect,
}: {
  constraint: PostureConstraintView;
  /** The caller derives this from the server's own `mode`/`attested` fields
   * (§14's evidence-presentation rule) — this primitive draws the state it is
   * given and computes no verdict of its own. */
  state: AnnunciatorState;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="option"
      id={constraint.id}
      aria-selected={selected}
      className={`ne-ann${state === "FAILED" ? " ne-ann--fail" : ""}`}
      onClick={onSelect}
    >
      <span className="ne-ann__id">{constraint.id}</span>
      <span className="ne-ann__name">{constraint.short}</span>
      <span className={`ne-ann__state ne-ann__state--${STATE_MODIFIER[state]}`}>
        <span aria-hidden="true">{STATE_GLYPH[state]}</span> {STATE_LABEL[state]}
      </span>
    </button>
  );
}
