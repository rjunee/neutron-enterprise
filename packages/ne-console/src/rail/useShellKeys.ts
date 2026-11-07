// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.6 — THE GLOBAL KEYBOARD CONTRACT, bound once for the whole shell.
//
//   ⌘K / Ctrl+K   the command palette
//   /             focus the nearest search / filter field
//   g then key    go to a destination (§2.6's letters)
//
// "NONE MAY SHADOW TYPING IN AN INPUT." That is the rule every single-letter
// shortcut gets wrong, so it is enforced here in one place: every binding except
// ⌘K is skipped while focus is in a text field, a select, or a contenteditable
// region. ⌘K survives because a modified chord cannot be mistaken for typing.
//
// The `g` chord is time-boxed. Without a timeout, a `g` pressed and abandoned
// leaves the shell armed indefinitely, and the next unrelated `p` navigates to
// Posture out of nowhere.
//
// THE CHORD AGREES WITH THE RAIL. Each press is checked against
// `shortcutRoutes` — the SAME filtered destinations the rail and the palette
// render — so a destination the rail does not show does nothing here either
// (server-refused, or its entitlement answer still in flight). This gates an
// affordance, not authorization: a hand-typed URL still mounts the route and
// still renders the server's own refusal (test/nav-refusal.test.tsx).

import { useEffect, useRef } from "react";
import { useNavigate } from "react-router";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { GO_KEYS, shortcutRoutes } from "./navSections";

const CHORD_MS = 1200;

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable === true
  );
}

export function useShellKeys({ onOpenPalette }: { onOpenPalette: () => void }): void {
  const navigate = useNavigate();
  const principal = usePrincipal();
  const armed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    function disarm() {
      armed.current = false;
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
    }

    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onOpenPalette();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTyping(event.target)) return;

      if (armed.current) {
        const to = GO_KEYS[event.key.toLowerCase()];
        disarm();
        // Looked up at press time so a mid-session verdict is honoured, and a
        // shortcut to a destination the rail does not show does nothing.
        if (to !== undefined && shortcutRoutes(entitlementsOf(principal)).has(to)) {
          event.preventDefault();
          void navigate(to);
        }
        return;
      }
      if (event.key === "g") {
        armed.current = true;
        timer.current = setTimeout(disarm, CHORD_MS);
        return;
      }
      if (event.key === "/") {
        // The nearest search field, in document order: the toolbar's
        // search-within if the screen has one, otherwise the screen's own
        // search input. Both carry .ne-searchwithin__input.
        const field = document.querySelector<HTMLInputElement>(".ne-searchwithin__input");
        if (field) {
          event.preventDefault();
          field.focus();
          field.select();
        }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      disarm();
    };
  }, [navigate, onOpenPalette, principal]);
}
