// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.3 — THE TOP BAR, 48px: rail collapse · search field · system status ·
// account. That is the whole inventory, and everything in it is either global or
// about the person using it. Nothing page-specific lives here — the per-page
// StatusStrip that used to sit in this band is deleted (§1.3): a page is a title
// row and content.

import { useCallback } from "react";

export function TopBar({
  collapsed,
  onToggleRail,
  onOpenPalette,
  children,
}: {
  collapsed: boolean;
  onToggleRail: () => void;
  onOpenPalette: () => void;
  /** The right cluster: status chip + account menu. */
  children: React.ReactNode;
}) {
  const open = useCallback(() => onOpenPalette(), [onOpenPalette]);
  return (
    <header className="ne-topbar">
      <button
        type="button"
        className="ne-topbar__collapse"
        aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
        aria-pressed={collapsed}
        onClick={onToggleRail}
      >
        <span aria-hidden="true">☰</span>
      </button>
      {/* A BUTTON STYLED AS AN INPUT, not an input. It opens the palette, and
          the palette owns the typing. A real input here would collect the first
          few keystrokes into a field that then throws them away. */}
      <button type="button" className="ne-topbar__search" onClick={open}>
        <span className="ne-topbar__searchtext">Search or jump to…</span>
        <span className="ne-topbar__kbd" aria-hidden="true">
          ⌘K
        </span>
      </button>
      <div className="ne-topbar__spacer" />
      {children}
    </header>
  );
}
