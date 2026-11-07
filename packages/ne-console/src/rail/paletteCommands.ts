// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §3 rank 3 — WHAT AN ACTION ACTUALLY DOES.
//
// Two kinds of command, and the difference is where the effect lives:
//
//   1. THE PREFERENCE COMMANDS (theme, tour) act on state the shell already
//      owns. They take the same setter the account menu takes — the palette is
//      a third caller of one control, never a second implementation of it, so
//      the palette and the account menu cannot disagree about the theme.
//
//   2. THE SCREEN COMMANDS (export, re-verify) drive a control that belongs to
//      the screen the action jumped to. They press THE SCREEN'S OWN BUTTON.
//      That is deliberate and it is the honest seam: the export writes the
//      window that screen is rendering, with that screen's filters named in the
//      file's preamble, and the re-verification is that screen's own re-read.
//      A palette that built its own export would be exporting a window nobody
//      was shown, which is the one thing an audit export may never be.
//
// WHY IT WAITS. The jump and the command are one keystroke, but the screen is
// not mounted yet when the keystroke ends, and the ledger it needs has not been
// read. So a screen command polls for its control up to a bounded deadline, then
// gives up. Giving up is not silent: the deadline is only ever reached when the
// control is absent or disabled, which happens when the ledger was refused to
// this role, failed to load, or came back empty — and the reader is standing on
// the audit screen, which says which of those it was, in its own words. The
// palette does not repeat the claim.
//
// NOTHING HERE FABRICATES. The commands press controls and set preferences; no
// command invents a value, recomputes a verdict, or writes anything the server
// was not already going to be asked for by the screen itself.

import type { PaletteCommand } from "./paletteIndex";
import type { ThemeChoice } from "./theme";

/** How long a screen command waits for its control to mount and enable. Long
 * enough for a cold route + one governed read, short enough that a refused
 * ledger does not leave a keystroke apparently pending. */
const CONTROL_DEADLINE_MS = 5000;
const POLL_MS = 25;

/** The screen controls the two audit verbs drive, by the button text §13 gives
 * them. Rename one on the screen and the palette action stops working — which
 * is exactly what `paletteActions.test.tsx` presses end-to-end, through the real
 * Audit screen, so the rename fails there rather than in front of anyone. */
const EXPORT_CONTROL = "Export CSV";
const VERIFY_CONTROL = "Re-verify";

export interface PaletteCommandContext {
  /** The shell's theme setter (rail/theme) — the account menu's setter. */
  setTheme: (next: ThemeChoice) => void;
  /** The guided tour's summon (demo/DemoContext) — the account menu's summon. */
  summonTour: () => void;
  /** Overridable only so a test can prove the give-up path without waiting. */
  deadlineMs?: number;
}

/**
 * The theme that is ON SCREEN right now, which is the only thing "switch" can
 * sensibly be relative to. `system` is not a third visible state — it resolves
 * through the same media query the stylesheet resolves it through, so switching
 * away from `system` lands on the opposite of what the reader is looking at
 * rather than on whatever `system` happened to mean.
 */
export function renderedTheme(): "light" | "dark" {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark" || attr === "light") return attr;
  return window.matchMedia?.("(prefers-color-scheme: dark)")?.matches === true ? "dark" : "light";
}

/** What "Switch theme" switches TO. `system` stays reachable from the account
 * menu and Settings; the palette's verb is a two-state flip, as its label says. */
export function nextTheme(): ThemeChoice {
  return renderedTheme() === "dark" ? "light" : "dark";
}

/** The first enabled button on screen carrying exactly this text. Disabled is
 * treated as absent: a disabled control is the screen saying the thing cannot be
 * done, and clicking it would do nothing anyway. */
function enabledButton(text: string): HTMLButtonElement | null {
  for (const button of document.querySelectorAll("button")) {
    if (button.disabled) continue;
    if ((button.textContent ?? "").trim() === text) return button;
  }
  return null;
}

/**
 * Focus the screen's control and press it, once it exists. Resolves true when it
 * was pressed and false when the deadline passed without it becoming available.
 *
 * It focuses BEFORE it clicks so the effect is anchored somewhere visible: the
 * reader's focus lands on the control the action names, on the screen that owns
 * it, and a keyboard user is left standing where the thing they asked for lives.
 */
async function pressOnScreen(text: string, deadlineMs: number): Promise<boolean> {
  const expiry = Date.now() + deadlineMs;
  for (;;) {
    const button = enabledButton(text);
    if (button !== null) {
      button.focus();
      button.click();
      return true;
    }
    if (Date.now() >= expiry) return false;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

/**
 * Run one palette command. Resolves true when the effect happened.
 *
 * The switch is exhaustive over `PaletteCommand`: adding a member without
 * giving it an effect does not compile, so the class of defect this module was
 * written to fix — an action listed with nothing behind it — cannot return
 * through this door.
 */
export function runPaletteCommand(
  command: PaletteCommand,
  ctx: PaletteCommandContext,
): Promise<boolean> {
  const deadlineMs = ctx.deadlineMs ?? CONTROL_DEADLINE_MS;
  switch (command) {
    case "switch-theme":
      ctx.setTheme(nextTheme());
      return Promise.resolve(true);
    case "open-tour":
      ctx.summonTour();
      return Promise.resolve(true);
    case "export-audit":
      return pressOnScreen(EXPORT_CONTROL, deadlineMs);
    case "verify-chain":
      return pressOnScreen(VERIFY_CONTROL, deadlineMs);
  }
}
