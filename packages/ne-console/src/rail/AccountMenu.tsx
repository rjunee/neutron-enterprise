// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.3 — THE ACCOUNT MENU. In order: who you are, theme, density, guided tour,
// sign out. That order is not arbitrary — identity first because it is the one
// thing people open this menu to check, and sign out last because it is the one
// thing nobody wants to hit by accident.
//
// It absorbs two controls that used to be permanent chrome: the guided-tour
// button (a rail row, on every route, for a thing most users open once) and the
// theme toggle. Both are preferences. Preferences belong behind the account
// button, not in the navigation.
//
// The theme and density radios write the SAME state Settings writes (rail/theme
// and primitives/density), so the two surfaces can never disagree.

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import { usePrincipal } from "../auth/PrincipalContext";
import { useDemo } from "../demo/DemoContext";
import { useDensity } from "../primitives/density";
import type { Density } from "../primitives/density";
import { useTheme } from "./theme";
import type { ThemeChoice } from "./theme";

const THEME_OPTIONS: ReadonlyArray<{ value: ThemeChoice; label: string }> = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
];

const DENSITY_OPTIONS: ReadonlyArray<{ value: Density; label: string }> = [
  { value: "compact", label: "Compact" },
  { value: "comfortable", label: "Comfortable" },
];

export function AccountMenu() {
  const { principal } = usePrincipal();
  const auth = useAuth();
  const { summon } = useDemo();
  const { theme, setTheme } = useTheme();
  const { density, setDensity } = useDensity();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDocDown(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const id = principal?.id ?? null;
  const groups = principal?.groups ?? [];
  // Showing the reader their OWN ID-token name is not a directory widening;
  // these claims are already held client-side. `/v1/me` still echoes none.
  const profile = auth.user?.profile;
  const claimName =
    (typeof profile?.name === "string" && profile.name.trim()) ||
    (typeof profile?.preferred_username === "string" && profile.preferred_username.trim()) ||
    null;
  const initial = (claimName ?? id ?? "?").slice(0, 1).toUpperCase();

  const signOut = useCallback(() => {
    void auth.signoutRedirect();
  }, [auth]);

  return (
    <div className="ne-account" ref={wrapRef}>
      <button
        type="button"
        className="ne-account__button"
        aria-expanded={open}
        aria-label="Account"
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">{initial}</span>
      </button>
      {open ? (
        <div className="ne-account__menu" role="menu" aria-label="Account">
          <div className="ne-account__identity">
            {/* The human identity is a statement. Exact technical identifiers
                remain available through the native disclosure below. */}
            <div className="ne-account__id">Signed in as {claimName ?? id ?? "—"}</div>
            {(claimName !== null && id !== null) || groups.length > 0 ? (
              <details className="ne-account__identity-details">
                <summary>Identity details</summary>
                {claimName !== null && id !== null ? (
                  <div className="ne-account__identity-row">
                    <span>Subject</span>
                    <span className="ne-mono">{id}</span>
                  </div>
                ) : null}
                {groups.length > 0 ? (
                  <div className="ne-account__identity-row">
                    <span>Access groups</span>
                    <ul className="ne-account__groups" aria-label="Access group identifiers">
                      {groups.map((group) => (
                        <li className="ne-mono" key={group}>
                          {group}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </details>
            ) : null}
          </div>

          <fieldset className="ne-account__group">
            <legend className="ne-account__legend">Theme</legend>
            {THEME_OPTIONS.map((option) => (
              <label className="ne-account__radio" key={option.value}>
                <input
                  type="radio"
                  name="ne-theme"
                  checked={theme === option.value}
                  onChange={() => setTheme(option.value)}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </fieldset>

          <fieldset className="ne-account__group">
            <legend className="ne-account__legend">Density</legend>
            {DENSITY_OPTIONS.map((option) => (
              <label className="ne-account__radio" key={option.value}>
                <input
                  type="radio"
                  name="ne-density"
                  checked={density === option.value}
                  onChange={() => setDensity(option.value)}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </fieldset>

          <button
            type="button"
            role="menuitem"
            className="ne-account__item"
            onClick={() => {
              setOpen(false);
              summon();
            }}
          >
            Guided tour
          </button>
          <button type="button" role="menuitem" className="ne-account__item" onClick={signOut}>
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}
