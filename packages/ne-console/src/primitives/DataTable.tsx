// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.1 — THE TABLE STANDARD, as one primitive.
//
// Measured on the shipped console: zero sortable columns in the entire product.
// Not "a few screens missed it" — zero, because sorting was never anything's
// job. So it is this primitive's job, and no screen implements it again: a
// column that declares `sortValue` is sortable, and the header button, the
// `aria-sort` attribute, the direction glyph, the toggle semantics and the
// comparator all arrive with it.
//
// What the standard buys, in one place:
//   * a real <table> with <th scope="col"> — a sortable header is a <button>
//     INSIDE its <th>, so the row of headers is still a row of headers and the
//     control's accessible name is the column name;
//   * `aria-sort` on the ACTIVE header only. `aria-sort="none"` on every other
//     header is legal and is noise on every announcement;
//   * tabular figures + right alignment on numeric and time columns, so a
//     column of numbers can be compared by eye down its own edge;
//   * ONE row height, read from --row-height, which is a user setting
//     (primitives/density.tsx) — a table never picks its own;
//   * whole-row activation with a visible focus ring, j/k/↑/↓ row movement,
//     Enter to open, `o` for the row overflow menu (§2.6);
//   * skeleton rows in the table's OWN geometry plus a visually-hidden
//     role="status" — never a spinner paragraph;
//   * §2.1 column resilience: a null cell renders the em dash and nothing else;
//   * §2.4 empty: NO HEADER ROW over zero rows, and never a blank body — see
//     `emptyBlock` and `headed` below.
//
// THIN CLIENT (C7). Sorting reorders rows the server already sent. It computes
// no verdict, no total and no permission. When the fetched window is partial the
// screen says so through `note` — "Sorted within the loaded 500 rows" — because
// a sort over a page is a different claim from a sort over a table.

import { useCallback, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { Empty } from "./states";
import { sortRows } from "./tableSort";
import type { SortDirection, SortState, SortValue } from "./tableSort";

export interface Column<Row> {
  /** Stable id. Also the sort key and the CSV header key. */
  key: string;
  /** Sentence case (§0 vocabulary rules). Rendered 12px, dim. */
  header: string;
  /** The cell. Return `null`/`undefined`/"" and the em dash is rendered. */
  cell: (row: Row) => ReactNode;
  /** Supplying this MAKES THE COLUMN SORTABLE. Omit it and the header is inert. */
  sortValue?: (row: Row) => SortValue;
  /** Right-aligned + tabular figures: every numeric and time column. */
  numeric?: boolean;
  /** Monospace cell type: identifiers and hashes. */
  mono?: boolean;
  /** Column width, e.g. "120px". */
  width?: string;
  /** Text this column contributes to the toolbar's search-within filter. */
  searchText?: (row: Row) => string;
  /** Direction applied on the FIRST click of this header. Times and counts read
   * newest/largest first; names read A→Z. Defaults to ascending. */
  firstSort?: SortDirection;
}

export type TableStatus = "loading" | "ok" | "refused" | "error";

export interface DataTableProps<Row> {
  columns: readonly Column<Row>[];
  rows: readonly Row[];
  /** Stable identity per row — also the selection key. */
  rowKey: (row: Row) => string;
  /** The table's accessible name, e.g. "Pending approvals". */
  label: string;
  /** Plural noun for the loading announcement: "Loading {things}". */
  things: string;
  /** §2.1: the default sort is stated per screen. Uncontrolled unless `sort`. */
  defaultSort?: SortState;
  /** Controlled sort — pass with `onSortChange` to hoist it (deep links, CSV). */
  sort?: SortState;
  onSortChange?: (next: SortState) => void;
  /** Row click / Enter. Opens the detail pane or the drill-down route. */
  onRowActivate?: (row: Row) => void;
  /** An extra state class on a row's `<tr>`, e.g. `ne-tr--urgent` for a breached
   * deadline. Return `undefined` for the ordinary rows. Additive: a table that
   * supplies none renders exactly as before. The modifier is a presentation flag
   * only — it never reorders rows and the served order is unchanged. */
  rowModifier?: (row: Row) => string | undefined;
  /** The row the detail pane is currently showing. */
  selectedKey?: string | null;
  status?: TableStatus;
  /** Rendered INSTEAD OF the table's rows and header row when `rows` is empty
   * and status is "ok". Optional: a screen that supplies none still gets §2.4's
   * grammar from `things`, never a blank strip. */
  empty?: ReactNode;
  /** Refused (403) body — §2.4's centred block, supplied by the screen. */
  refused?: ReactNode;
  /** Error body — §2.4's "Couldn't load {things}." + Retry. */
  error?: ReactNode;
  /** Right-aligned per-row overflow menu, revealed on row hover/focus. */
  rowMenu?: (row: Row) => ReactNode;
  /** Text under the table: pagination, or the partial-window sort disclosure. */
  note?: ReactNode;
  className?: string;
}

const SKELETON_ROWS = 3;

/** §2.1 column resilience: a null field renders the em dash, and the em dash is
 * the only thing it renders. No inferred zero, no "n/a", no blank cell that
 * reads as a rendering bug. */
function AbsentCell() {
  return <span className="ne-absent">—</span>;
}

function cellClass<Row>(column: Column<Row>): string {
  return ["ne-td", column.numeric ? "ne-num" : "", column.mono ? "ne-mono" : ""]
    .filter(Boolean)
    .join(" ");
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  label,
  things,
  defaultSort,
  sort: controlledSort,
  onSortChange,
  onRowActivate,
  rowModifier,
  selectedKey = null,
  status = "ok",
  empty,
  refused,
  error,
  rowMenu,
  note,
  className,
}: DataTableProps<Row>) {
  const [ownSort, setOwnSort] = useState<SortState | null>(defaultSort ?? null);
  const sort = controlledSort ?? ownSort;

  const setSort = useCallback(
    (next: SortState) => {
      if (onSortChange) onSortChange(next);
      if (controlledSort === undefined) setOwnSort(next);
    },
    [controlledSort, onSortChange],
  );

  const toggle = useCallback(
    (column: Column<Row>) => {
      if (column.sortValue === undefined) return;
      if (sort !== null && sort.key === column.key) {
        setSort({ key: column.key, dir: sort.dir === "asc" ? "desc" : "asc" });
        return;
      }
      setSort({ key: column.key, dir: column.firstSort ?? "asc" });
    },
    [setSort, sort],
  );

  const ordered = useMemo(() => sortRows(rows, columns, sort), [rows, columns, sort]);

  // ROW FOCUS — a roving tabindex over <tr>. ONE tab stop for the whole table;
  // j/k and the arrow keys move inside it (§2.6). Without this a 500-row audit
  // ledger is 500 tab stops between the toolbar and the pagination row.
  const [focused, setFocused] = useState(0);
  const bodyRef = useRef<HTMLTableSectionElement | null>(null);
  // CLAMPED DURING RENDER, not corrected afterwards in an effect. A shrinking
  // filter result must never leave the roving tabindex pointing past the end,
  // and an effect that repairs it renders the broken state first.
  const active = ordered.length === 0 ? 0 : Math.min(focused, ordered.length - 1);

  const focusRow = useCallback((index: number) => {
    setFocused(index);
    bodyRef.current?.querySelectorAll<HTMLTableRowElement>("tr.ne-tr")[index]?.focus();
  }, []);

  const onRowKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTableRowElement>, row: Row, index: number) => {
      switch (event.key) {
        case "ArrowDown":
        case "j":
          event.preventDefault();
          focusRow(Math.min(ordered.length - 1, index + 1));
          break;
        case "ArrowUp":
        case "k":
          event.preventDefault();
          focusRow(Math.max(0, index - 1));
          break;
        case "Home":
          event.preventDefault();
          focusRow(0);
          break;
        case "End":
          event.preventDefault();
          focusRow(ordered.length - 1);
          break;
        case "Enter":
          if (onRowActivate) {
            event.preventDefault();
            onRowActivate(row);
          }
          break;
        case "o": {
          const trigger = event.currentTarget.querySelector<HTMLElement>(".ne-rowmenu__trigger");
          if (trigger) {
            event.preventDefault();
            trigger.click();
          }
          break;
        }
      }
    },
    [focusRow, onRowActivate, ordered.length],
  );

  const colCount = columns.length + (rowMenu ? 1 : 0);

  // §2.4 EMPTY, OWNED BY THE PRIMITIVE — two failures, one place.
  //
  // (1) `empty` is optional, so it can simply be unset, and an unset `empty`
  //     rendered `<td colSpan={n}></td>`: a header row over a literally blank
  //     strip. Every call site was one missing prop away from that, and several
  //     are. The primitive therefore holds a floor in §2.4's grammar — one
  //     sentence naming the fact — built from `things`, which every call site
  //     already supplies. A screen's own copy still wins, so the per-screen
  //     verbatim strings are unaffected.
  const emptyBlock = empty ?? <Empty message={`No ${things}.`} />;

  // (2) The header row rendered UNCONDITIONALLY. That is the shipped defect on
  //     Approvals: seven column headers — six of them sort buttons, one
  //     carrying `aria-sort="ascending"`, a claim of order over an empty set —
  //     standing above a single sentence. It reads as a half-finished render,
  //     and §8 states the requirement literally: "No pending approvals. — one
  //     line, nothing else." A header row heads rows. With zero rows there is
  //     nothing for it to head, so it is not rendered.
  //
  //     LOADING KEEPS ITS HEADERS: skeleton rows ARE the table's own geometry
  //     and real rows are one tick away, so tearing the header row down and
  //     rebuilding it is the flicker this primitive exists to prevent.
  const headed = !(status === "ok" && ordered.length === 0);

  function bodyContent(): ReactNode {
    if (status === "loading") {
      return Array.from({ length: SKELETON_ROWS }, (_, i) => (
        <tr className="ne-tr ne-tr--skeleton" key={`skeleton-${i}`} aria-hidden="true">
          {columns.map((column) => (
            <td className={cellClass(column)} key={column.key}>
              <span className="ne-skeleton" />
            </td>
          ))}
          {rowMenu ? <td className="ne-td ne-td--menu" /> : null}
        </tr>
      ));
    }
    if (status !== "ok") {
      return (
        <tr className="ne-tr ne-tr--state">
          <td className="ne-td ne-td--state" colSpan={colCount}>
            {status === "refused" ? refused : error}
          </td>
        </tr>
      );
    }
    if (ordered.length === 0) {
      return (
        <tr className="ne-tr ne-tr--state">
          <td className="ne-td ne-td--state" colSpan={colCount}>
            {emptyBlock}
          </td>
        </tr>
      );
    }
    return ordered.map((row, index) => {
      const key = rowKey(row);
      const selected = selectedKey !== null && selectedKey === key;
      const classes = [
        "ne-tr",
        selected ? "ne-tr--selected" : "",
        onRowActivate ? "ne-tr--clickable" : "",
        rowModifier?.(row) ?? "",
      ]
        .filter(Boolean)
        .join(" ");
      return (
        <tr
          key={key}
          className={classes}
          tabIndex={index === active ? 0 : -1}
          aria-selected={selected || undefined}
          onFocus={() => setFocused(index)}
          onClick={onRowActivate ? () => onRowActivate(row) : undefined}
          onKeyDown={(event) => onRowKeyDown(event, row, index)}
        >
          {columns.map((column) => {
            const content = column.cell(row);
            const absent = content === null || content === undefined || content === "";
            return (
              <td className={cellClass(column)} key={column.key}>
                {absent ? <AbsentCell /> : content}
              </td>
            );
          })}
          {rowMenu ? (
            <td
              className="ne-td ne-td--menu"
              onClick={(event) => {
                // The row's own click opens the detail pane; the overflow menu
                // is the row's SECONDARY action and must not also open it.
                event.stopPropagation();
              }}
            >
              {rowMenu(row)}
            </td>
          ) : null}
        </tr>
      );
    });
  }

  return (
    <div className={`ne-table__wrap${className ? ` ${className}` : ""}`}>
      {status === "loading" ? (
        <span className="ne-visually-hidden" role="status">
          Loading {things}
        </span>
      ) : null}
      <table className="ne-table" aria-label={label}>
        {headed ? (
          <thead>
            <tr>
              {columns.map((column) => {
                const sortable = column.sortValue !== undefined;
                const isActive = sortable && sort !== null && sort.key === column.key;
                const ariaSort = isActive
                  ? sort.dir === "asc"
                    ? "ascending"
                    : "descending"
                  : undefined;
                return (
                  <th
                    scope="col"
                    key={column.key}
                    className={`ne-th${column.numeric ? " ne-num" : ""}`}
                    style={column.width ? { width: column.width } : undefined}
                    aria-sort={ariaSort}
                  >
                    {sortable ? (
                      <button type="button" className="ne-th__sort" onClick={() => toggle(column)}>
                        <span>{column.header}</span>
                        <span className="ne-th__glyph" aria-hidden="true">
                          {isActive ? (sort.dir === "asc" ? "▲" : "▼") : ""}
                        </span>
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                );
              })}
              {rowMenu ? <th scope="col" className="ne-th ne-th--menu" /> : null}
            </tr>
          </thead>
        ) : null}
        <tbody ref={bodyRef}>{bodyContent()}</tbody>
      </table>
      {note ? <div className="ne-table__note">{note}</div> : null}
    </div>
  );
}
