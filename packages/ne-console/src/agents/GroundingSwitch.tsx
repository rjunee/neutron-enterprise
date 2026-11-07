// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// GROUNDING IS ONE COMPACT SWITCH.
//
// It was a two-button segmented control — [ Grounded ][ Ungrounded ] — and the
// owner on seeing it: "its ugly as shit. Cant you just put a regular toggle
// switch instead of two buttons that take up so much space?" The composer
// footer also carries the model picker and the caps note, and the segment
// dominated both.
//
// So: one `role="switch"` with `aria-checked`, roughly a checkbox's footprint.
// The earlier honesty concern — a switch labelled "Grounded" leaves its off
// position unnamed, so off reads as "grounding is broken" — is answered by the
// WORD BESIDE THE SWITCH SAYING THE CURRENT STATE: "Grounded" when on,
// "Ungrounded" when off. Both states stay named; a reader never has to flip
// the control to learn what its resting position means. The accessible name
// stays the stable "Grounding" (the state is `aria-checked`, not the name).
//
// "How will this be answered" is a property of the question being composed, so
// it still lives in the composer's own footer beside the model — and once a
// conversation is open the mode is BOUND (chosen at session creation), so the
// caller renders this disabled with the bound state rather than unmounting it.

export function GroundingSwitch({
  grounded,
  onChange,
  disabled = false,
  title,
}: {
  readonly grounded: boolean;
  readonly onChange: (grounded: boolean) => void;
  readonly disabled?: boolean;
  /** Why the control is in the state it is in — e.g. the bound-at-start note a
   * disabled switch carries on an open conversation. Hover text; never a
   * second label. */
  readonly title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={grounded}
      aria-label="Grounding"
      className="ag-gswitch"
      disabled={disabled}
      title={title}
      onClick={() => onChange(!grounded)}
    >
      <span className="ag-gswitch__track" aria-hidden="true">
        <span className="ag-gswitch__thumb" />
      </span>
      {/* The state, said in a word — so OFF is "Ungrounded", never a bare
          unlit switch whose meaning has to be discovered by flipping it. */}
      <span className="ag-gswitch__word">{grounded ? "Grounded" : "Ungrounded"}</span>
    </button>
  );
}
