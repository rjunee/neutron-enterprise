// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.2 — THE TOOLBAR STANDARD. One 40px row directly above every table:
//
//   [search-within] [filter controls…] [spacer] [count] [export]
//
// Approvals, Audit and Search each need exactly this row, and Approvals — the
// highest-frequency screen in the product — shipped with no toolbar, no filter
// and no count at all. Building it three times is how the three end up
// disagreeing about what "active filter" means, so it is built once.
//
// THE COUNT IS ALWAYS LIVE and always describes the FILTERED window, because a
// count that describes the fetched window while the screen shows a filtered one
// is a number that is wrong on screen — the class of defect this whole rebuild
// exists to remove. The screen passes the count; the toolbar renders it beside
// the rows it counts.

import { useCallback, useId } from "react";
import type { ChangeEvent, KeyboardEvent, ReactNode } from "react";

export function Toolbar({
  search,
  filters,
  count,
  actions,
}: {
  /** The client-side substring filter over the fetched window (§2.2). */
  search?: {
    value: string;
    onChange: (next: string) => void;
    /** "Filter {things}…" */
    placeholder: string;
  };
  /** Filter selects and toggle chips, left to right. */
  filters?: ReactNode;
  /** "{n} {things}", extended by state where the screen says so. */
  count: ReactNode;
  /** Export, then the screen's primary action. */
  actions?: ReactNode;
}) {
  return (
    <div className="ne-toolbar">
      {search ? <SearchWithin {...search} /> : null}
      {filters ? <div className="ne-toolbar__filters">{filters}</div> : null}
      <div className="ne-toolbar__spacer" />
      <output className="ne-toolbar__count">{count}</output>
      {actions ? <div className="ne-toolbar__actions">{actions}</div> : null}
    </div>
  );
}

/** Cleared with Esc (§2.2), and focused by `/` (§2.6 — the shell binds the key
 * and focuses the nearest `.ne-searchwithin__input`). */
function SearchWithin({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
}) {
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Escape" && value !== "") {
        event.stopPropagation();
        onChange("");
      }
    },
    [onChange, value],
  );
  return (
    <input
      type="search"
      className="ne-searchwithin__input"
      value={value}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
      onKeyDown={onKeyDown}
    />
  );
}

/** A compact labelled select. The label is visible — a select whose only label
 * is its own current value tells you what it says, never what it is. */
export function FilterSelect<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (next: T) => void;
  /** Additive, defaults to false. Set when the option list is server-derived
   * and the server has nothing to offer yet (or nothing at all) — an inert
   * control is honest; a control listing invented values is not. */
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <span className="ne-filter">
      <label className="ne-filter__label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="ne-filter__select"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as T)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  );
}

/** A binary filter as a pressed/unpressed chip. Carries its state in
 * `aria-pressed` AND in its own border weight — never in colour alone. */
export function ToggleChip({
  label,
  pressed,
  onChange,
}: {
  label: string;
  pressed: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      className={`ne-chip${pressed ? " ne-chip--on" : ""}`}
      aria-pressed={pressed}
      onClick={() => onChange(!pressed)}
    >
      {label}
    </button>
  );
}

/** §2.2: an active non-default filter renders a DISMISSIBLE chip, so the reason
 * a list is short is always visible next to the list. */
export function FilterChip({ label, onDismiss }: { label: string; onDismiss: () => void }) {
  return (
    <span className="ne-chip ne-chip--active">
      {label}
      <button
        type="button"
        className="ne-chip__dismiss"
        aria-label={`Clear ${label}`}
        onClick={onDismiss}
      >
        <span aria-hidden="true">×</span>
      </button>
    </span>
  );
}

/** The quiet toolbar button — Export CSV and any screen-level secondary action. */
export function ToolbarButton({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button type="button" className="ne-btn ne-btn--quiet" onClick={onClick} disabled={disabled}>
      {label}
    </button>
  );
}

/** The one primary action per title row / toolbar. Names the consequence
 * (§2.5), never "Submit". */
export function PrimaryButton({
  label,
  onClick,
  disabled,
  type = "button",
}: {
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  return (
    <button type={type} className="ne-btn ne-btn--primary" onClick={onClick} disabled={disabled}>
      {label}
    </button>
  );
}
