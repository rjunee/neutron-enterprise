// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE COMPOSER — ONE CARD, AND EVERYTHING ABOUT ANSWERING IS INSIDE IT.
//
// The question is the thing, so it takes the card's whole top: a full-width
// textarea, Enter sends, Shift+Enter opens a line. During a turn the button
// becomes Stop, so the reader always has a way out of a turn that is not
// finishing.
//
// UNDER IT, IN THE CARD'S OWN BORDERED FOOTER, the two "how will this be
// answered" controls — the served model and the grounding segment — plus the
// server's execution caps and the single primary Ask. They used to float in the
// chrome above the thread, owned by nothing: the grounding switch in particular
// sat beside four filled blue buttons and read as a fifth competing element.
// Subordinating them to the question inside one bordered row is what makes the
// cherenkov budget hold — the Ask button is the only primary on the screen.
//
// CONTROLLED from the screen so retrying a failed turn refills the box without
// sending: a governed turn is never started by anything but a deliberate press.
//
// A COMPOSER THAT WILL NOT SEND MUST SAY SO. THIS ONE USED TO GO QUIET.
//
// `submit()` returns early on `!modelReady` and on `disabled`, and the send
// button greys on the same two. Neither state put a single word on the screen.
// So a reader typed a question, pressed Enter, and NOTHING happened — no error,
// no refusal, no note: the surface swallowed the press and kept the text. On a
// slow read (the model catalog, an agent's run envelope) that arrives seconds
// AFTER the page looks ready, the composer appears to work and then silently
// stops working, which is worse than being disabled from the start, because the
// reader has already committed a question to it.
//
// `blockedReason` closes that. It is the owning screen's own sentence about
// WHICH precondition is missing — never a generic "try again", and never this
// component's guess: `MessageInput` knows only that it will not send, not why.
// It renders as a live region beside the send control whenever there is one, so
// the reason arrives at the moment the composer locks rather than being
// something the reader has to go and find.

import type { ChangeEvent, FormEvent, KeyboardEvent, ReactNode } from "react";
import { ASK_PLACEHOLDER, ASK_STOP, ASK_SUBMIT } from "./askCopy";

export function MessageInput({
  busy,
  value,
  onChange,
  onSubmit,
  onStop,
  controls,
  modelReady,
  disabled = false,
  blockedReason = null,
  placeholder = ASK_PLACEHOLDER,
  capsNote = null,
}: {
  busy: boolean;
  value: string;
  onChange: (next: string) => void;
  onSubmit: (prompt: string) => void;
  onStop: () => void;
  /** The served "how will this be answered" controls — model and grounding. */
  controls: ReactNode;
  modelReady: boolean;
  /** The agent is unavailable. The surface stays visible and legible, disabled
   * rather than hidden, so the reader can still read what they were doing. */
  disabled?: boolean;
  /** WHY this composer will not send, in the owning screen's own words. Shown
   * only while it actually will not send, and never authored here — a screen
   * that cannot name the missing precondition passes null and the composer
   * stays silent rather than inventing a cause. */
  blockedReason?: string | null;
  placeholder?: string;
  /** The SERVED execution caps. `null` renders nothing rather than a guess. */
  capsNote?: string | null;
}) {
  // Exactly the condition `submit()` refuses on, minus the two that are the
  // reader's own doing (an empty box, a turn already running). One expression,
  // so the sentence below cannot drift out of step with the behaviour it
  // describes.
  const willNotSend = disabled || !modelReady;

  function submit() {
    const trimmed = value.trim();
    if (trimmed === "" || busy || !modelReady || disabled) return;
    onSubmit(trimmed);
    onChange("");
  }

  function onFormSubmit(event: FormEvent) {
    event.preventDefault();
    submit();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    submit();
  }

  return (
    <div className="ag-composer">
      <form className="ag-composer__card" onSubmit={onFormSubmit}>
        <textarea
          className="ag-composer__text"
          rows={3}
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          aria-label={placeholder}
          onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="ag-composer__ctl">
          {controls}
          {capsNote === null ? null : (
            <span className="ag-caps" title="Server-enforced execution caps for this agent">
              {capsNote}
            </span>
          )}
          {willNotSend && !busy && blockedReason !== null ? (
            <span className="ag-composer__blocked" role="status">
              {blockedReason}
            </span>
          ) : null}
          {busy ? (
            <button
              type="button"
              className="ne-btn ne-btn--quiet ag-composer__send"
              onClick={onStop}
            >
              {ASK_STOP}
            </button>
          ) : (
            <button
              type="submit"
              className="ne-btn ne-btn--primary ag-composer__send"
              disabled={value.trim() === "" || !modelReady || disabled}
            >
              {ASK_SUBMIT}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
