// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §3 — THE COMMAND PALETTE. ⌘K, or the top bar's search field, over any screen.
//
// It is the answer to the measure of done: "count the clicks to the thing
// someone came for." With an approval request id in an email, the path used to
// be rail → Approvals → scan. It is now ⌘K, paste, Enter.
//
// Listbox semantics, properly: the INPUT keeps focus and owns
// `aria-activedescendant`, the options carry `role="option"` and
// `aria-selected`. Moving focus into the list instead would break typing, which
// is the only thing anyone does here.

import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import type { ApprovalOwner } from "../approvals/approvalOwner";
import { entitlementsOf } from "../auth/entitlements";
import type { PrincipalState } from "../auth/PrincipalContext";
import { useDemo } from "../demo/DemoContext";
import { flattenSections, NAV_FOOT, NAV_SECTIONS, visibleSections } from "./navSections";
import { runPaletteCommand } from "./paletteCommands";
import { hydrateRecents, isPaletteCommand, NO_MATCH_COPY, paletteResults } from "./paletteIndex";
import type { PaletteResult } from "./paletteIndex";
import { useTheme } from "./theme";

const RECENTS_KEY_PREFIX = "ne-palette-recents:v2:";
const MAX_RECENTS = 10;

function parseStoredRecents(key: string): PaletteResult[] {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Shape-checked on the way in: a corrupt or hand-edited entry must not
    // render as a row that navigates somewhere undefined or claims a command
    // this build does not have. Live action/destination reconciliation happens
    // once the principal-filtered destination registry is available below.
    return parsed.filter(
      (r): r is PaletteResult =>
        typeof r === "object" &&
        r !== null &&
        typeof (r as PaletteResult).id === "string" &&
        typeof (r as PaletteResult).label === "string" &&
        (typeof (r as PaletteResult).to === "string" || (r as PaletteResult).to === null) &&
        ((r as PaletteResult).command === undefined ||
          isPaletteCommand((r as PaletteResult).command)),
    );
  } catch {
    return [];
  }
}

function ownerRecentsKey(principalId: string): string {
  return `${RECENTS_KEY_PREFIX}${encodeURIComponent(principalId)}`;
}

function readStoredRecents(principalId: string): PaletteResult[] {
  return parseStoredRecents(ownerRecentsKey(principalId));
}

function writeRecents(principalId: string, next: readonly PaletteResult[]): void {
  try {
    localStorage.setItem(ownerRecentsKey(principalId), JSON.stringify(next.slice(0, MAX_RECENTS)));
  } catch {
    // Recents are a convenience; losing them costs nothing that matters.
  }
}

export function CommandPalette({
  onClose,
  owner,
  principal,
  ready,
}: {
  onClose: () => void;
  owner: ApprovalOwner;
  principal: PrincipalState;
  ready: boolean;
}) {
  const principalId = ready ? owner.principalId : null;
  return (
    <CommandPaletteWorkspace
      key={`${owner.generation}:${ready ? principalId : "unbound"}`}
      onClose={onClose}
      ownerGeneration={owner.generation}
      principal={principal}
      principalId={principalId}
      ready={ready}
    />
  );
}

function CommandPaletteWorkspace({
  onClose,
  ownerGeneration,
  principal,
  principalId,
  ready,
}: {
  onClose: () => void;
  ownerGeneration: string;
  principal: PrincipalState;
  principalId: string | null;
  ready: boolean;
}) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // The theme setter and the tour's summon, taken from the SAME two controls the
  // account menu takes them from (§1.3) — the palette runs those controls, it
  // does not own a second copy of them.
  const { setTheme } = useTheme();
  const { summon } = useDemo();
  const [query, setQuery] = useState("");
  // The cursor is stored WITH the query it belongs to. FIRST RESULT PRESELECTED
  // is not a thing to remember to do after each keystroke — it falls out of the
  // fact that a cursor for a different query is not this query's cursor. An
  // effect that reset it afterwards would render the stale selection first, and
  // a cursor left at index 5 opens whatever lands there after the next letter.
  const [selection, setSelection] = useState<{ query: string; cursor: number }>({
    query: "",
    cursor: 0,
  });
  const persistedRecents = useMemo(
    () => (principalId === null ? [] : readStoredRecents(principalId)),
    [principalId],
  );
  const [sessionRecents, setSessionRecents] = useState<{
    ownerGeneration: string;
    rows: PaletteResult[];
  } | null>(null);
  const storedRecents =
    sessionRecents?.ownerGeneration === ownerGeneration ? sessionRecents.rows : persistedRecents;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listId = useId();

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    const paletteRoot = dialog?.parentElement;
    const shell = paletteRoot?.parentElement;
    if (dialog === null || paletteRoot === null || !shell?.classList.contains("ne-shell")) return;
    const background = [...shell.children].filter(
      (element): element is HTMLElement =>
        element instanceof HTMLElement && element !== paletteRoot,
    );
    const previouslyInert = background.map(
      (element) => [element, element.hasAttribute("inert")] as const,
    );
    for (const [element] of previouslyInert) element.setAttribute("inert", "");
    const containFocus = (event: FocusEvent) => {
      if (!dialog.contains(event.target as Node)) inputRef.current?.focus();
    };
    document.addEventListener("focusin", containFocus);
    return () => {
      document.removeEventListener("focusin", containFocus);
      for (const [element, wasInert] of previouslyInert) {
        if (!wasInert) element.removeAttribute("inert");
      }
    };
  }, []);

  useLayoutEffect(() => {
    inputRef.current?.focus();
  }, []);

  const destinations = useMemo(() => {
    if (!ready) return [];
    const sections = visibleSections(NAV_SECTIONS, entitlementsOf(principal));
    return [...flattenSections(sections), NAV_FOOT];
  }, [principal, ready]);

  const recents = useMemo(
    () => hydrateRecents(storedRecents, destinations),
    [destinations, storedRecents],
  );

  const results = useMemo(
    () => paletteResults({ query, destinations, recents }),
    [query, destinations, recents],
  );

  const cursor =
    selection.query === query ? Math.min(selection.cursor, Math.max(0, results.length - 1)) : 0;
  const setCursor = useCallback(
    (next: number | ((current: number) => number)) => {
      setSelection((prev) => {
        const current = prev.query === query ? prev.cursor : 0;
        return { query, cursor: typeof next === "function" ? next(current) : next };
      });
    },
    [query],
  );

  const open = useCallback(
    (result: PaletteResult, newTab: boolean) => {
      if (principalId === null) return;
      const next = [result, ...recents.filter((r) => r.id !== result.id)].slice(0, MAX_RECENTS);
      setSessionRecents({ ownerGeneration, rows: next });
      writeRecents(principalId, next);
      onClose();

      // A COMMAND RUNS WHERE YOU ARE, so ⌘Enter cannot carry it: a second tab
      // starts from a cold shell this one cannot reach into, and the reader
      // would be left watching a new tab do nothing. The modifier degrades to
      // plain Enter for a command row rather than opening that tab.
      const inNewTab = newTab && result.command === undefined;
      if (result.to !== null) {
        if (inNewTab) {
          window.open(result.to, "_blank", "noopener");
          return;
        }
        void navigate(result.to);
      }
      if (result.command !== undefined) {
        // Fire-and-forget: the screen command waits for the screen it was just
        // sent to, and this palette is already unmounted by then.
        void runPaletteCommand(result.command, { setTheme, summonTour: summon });
      }
    },
    [navigate, onClose, ownerGeneration, principalId, recents, setTheme, summon],
  );

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setCursor((c) => Math.min(results.length - 1, c + 1));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setCursor((c) => Math.max(0, c - 1));
        return;
      }
      if (event.key === "Enter") {
        const chosen = results[cursor];
        if (chosen) {
          event.preventDefault();
          open(chosen, event.metaKey || event.ctrlKey);
        }
      }
    },
    [cursor, open, results, setCursor],
  );

  const activeId = results[cursor] ? `${listId}-${results[cursor].id}` : undefined;
  const captureDialogKeys = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (dialog === null) return;
      const focusable = [
        ...dialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (first === undefined || last === undefined) return;
      const active = document.activeElement;
      if (
        (event.shiftKey && (active === first || !dialog.contains(active))) ||
        (!event.shiftKey && (active === last || !dialog.contains(active)))
      ) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    },
    [onClose],
  );

  return (
    <div className="ne-palette__scrim" onClick={onClose}>
      <div
        ref={dialogRef}
        className="ne-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Search or jump to"
        onClick={(event) => event.stopPropagation()}
        onKeyDownCapture={captureDialogKeys}
      >
        <input
          ref={inputRef}
          className="ne-palette__input"
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={activeId}
          aria-autocomplete="list"
          aria-label="Search or jump to"
          placeholder="Search or jump to…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
        />
        {results.length === 0 ? (
          <p className="ne-palette__empty">{NO_MATCH_COPY}</p>
        ) : (
          <ul className="ne-palette__list" role="listbox" id={listId}>
            {results.map((result, index) => (
              <li
                key={result.id}
                id={`${listId}-${result.id}`}
                role="option"
                aria-selected={index === cursor}
                className={`ne-palette__option${index === cursor ? " ne-palette__option--active" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={(event) => open(result, event.metaKey || event.ctrlKey)}
              >
                <span className="ne-palette__label">{result.label}</span>
                <span className="ne-palette__kind">{result.kind}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
