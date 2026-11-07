// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.1 — the table standard, as rendered.
//
// The headline claim this file holds: SORTING IS A PROPERTY OF THE PRIMITIVE.
// The shipped console had zero sortable columns in the entire product, so the
// assertions here are deliberately about the mechanism (a <button> in the <th>,
// `aria-sort` on the ACTIVE header only, a direction that toggles) rather than
// about one screen's columns — a screen gets sorting by declaring `sortValue`
// and cannot get it wrong.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DataTable } from "./DataTable";
import type { Column } from "./DataTable";

interface Row {
  id: string;
  actor: string;
  cost: number | null;
}

const ROWS: Row[] = [
  { id: "a", actor: "zoe", cost: 3 },
  { id: "b", actor: "adam", cost: null },
  { id: "c", actor: "mona", cost: 11 },
];

const COLUMNS: Column<Row>[] = [
  { key: "actor", header: "Actor", cell: (r) => r.actor, sortValue: (r) => r.actor },
  { key: "cost", header: "Cost", cell: (r) => r.cost, sortValue: (r) => r.cost, numeric: true },
  // Deliberately UNSORTABLE — a column with no comparable order must not grow a
  // header button just because it sits in a sortable table.
  { key: "note", header: "Note", cell: () => "—" },
];

function renderTable(props: Partial<React.ComponentProps<typeof DataTable<Row>>> = {}) {
  return render(
    <DataTable
      columns={COLUMNS}
      rows={ROWS}
      rowKey={(r) => r.id}
      label="Test rows"
      things="rows"
      {...props}
    />,
  );
}

function bodyActors(): string[] {
  const rows = screen.getAllByRole("row").slice(1);
  return rows.map((r) => within(r).getAllByRole("cell")[0]?.textContent ?? "");
}

describe("DataTable — sorting (§2.1)", () => {
  it("renders a real table with column headers", () => {
    renderTable();
    expect(screen.getByRole("table", { name: "Test rows" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
  });

  it("makes exactly the columns that declare sortValue into header BUTTONS", () => {
    renderTable();
    expect(screen.getByRole("button", { name: /Actor/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cost/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Note/ })).toBeNull();
  });

  it("sorts on click and carries aria-sort on the ACTIVE header only", async () => {
    renderTable();
    // No sort declared: no header claims one.
    expect(screen.getAllByRole("columnheader").filter((h) => h.getAttribute("aria-sort"))).toEqual(
      [],
    );

    await userEvent.click(screen.getByRole("button", { name: /Actor/ }));
    expect(bodyActors()).toEqual(["adam", "mona", "zoe"]);
    const headers = screen.getAllByRole("columnheader");
    expect(headers[0]).toHaveAttribute("aria-sort", "ascending");
    // …and ONLY that one. `aria-sort="none"` on every other header is legal and
    // is noise on every announcement.
    expect(headers[1]?.getAttribute("aria-sort")).toBeNull();
    expect(headers[2]?.getAttribute("aria-sort")).toBeNull();
  });

  it("toggles direction on a second click of the same header", async () => {
    renderTable();
    const actor = screen.getByRole("button", { name: /Actor/ });
    await userEvent.click(actor);
    expect(bodyActors()).toEqual(["adam", "mona", "zoe"]);
    await userEvent.click(actor);
    expect(bodyActors()).toEqual(["zoe", "mona", "adam"]);
    expect(screen.getAllByRole("columnheader")[0]).toHaveAttribute("aria-sort", "descending");
  });

  it("honours the screen's stated default sort", () => {
    renderTable({ defaultSort: { key: "actor", dir: "desc" } });
    expect(bodyActors()).toEqual(["zoe", "mona", "adam"]);
    expect(screen.getAllByRole("columnheader")[0]).toHaveAttribute("aria-sort", "descending");
  });

  it("keeps an absent value last in both directions", async () => {
    renderTable();
    const cost = screen.getByRole("button", { name: /Cost/ });
    await userEvent.click(cost);
    expect(bodyActors()).toEqual(["zoe", "mona", "adam"]);
    await userEvent.click(cost);
    // adam's cost is null — still last, even though the direction reversed.
    expect(bodyActors()).toEqual(["mona", "zoe", "adam"]);
  });
});

describe("DataTable — rows, keyboard and states", () => {
  it("activates a row on click and on Enter, and is ONE tab stop", async () => {
    const onRowActivate = vi.fn();
    renderTable({ onRowActivate });
    await userEvent.click(screen.getAllByRole("row")[1]!);
    expect(onRowActivate).toHaveBeenCalledWith(ROWS[0]);

    // Roving tabindex: exactly one row is tabbable, the rest are reachable by
    // j/k and the arrow keys. A 500-row ledger is otherwise 500 tab stops.
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.filter((r) => r.getAttribute("tabindex") === "0")).toHaveLength(1);
    expect(rows.filter((r) => r.getAttribute("tabindex") === "-1")).toHaveLength(2);

    rows[0]!.focus();
    await userEvent.keyboard("j");
    expect(document.activeElement).toBe(rows[1]);
    await userEvent.keyboard("k");
    expect(document.activeElement).toBe(rows[0]);
    await userEvent.keyboard("{Enter}");
    expect(onRowActivate).toHaveBeenCalledTimes(2);
  });

  it("renders the em dash for an absent cell, and nothing else", () => {
    render(
      <DataTable
        columns={[{ key: "cost", header: "Cost", cell: (r: Row) => r.cost, numeric: true }]}
        rows={[{ id: "b", actor: "adam", cost: null }]}
        rowKey={(r) => r.id}
        label="Costs"
        things="costs"
      />,
    );
    const cell = screen.getAllByRole("cell")[0]!;
    // NOT "0", NOT "$0.00", NOT blank.
    expect(cell.textContent).toBe("—");
  });

  it("loads as skeleton rows in the table's own geometry, with a hidden announcement", () => {
    const { container } = renderTable({ status: "loading" });
    expect(screen.getByRole("status")).toHaveTextContent("Loading rows");
    expect(container.querySelectorAll(".ne-skeleton").length).toBeGreaterThan(0);
    // Never a spinner paragraph, and never the rows underneath it.
    expect(screen.queryByText("zoe")).toBeNull();
  });

  it("renders the screen's empty, refused and error blocks in the table body", () => {
    const { rerender } = render(
      <DataTable
        columns={COLUMNS}
        rows={[]}
        rowKey={(r: Row) => r.id}
        label="Test rows"
        things="rows"
        empty={<span>No pending approvals.</span>}
      />,
    );
    expect(screen.getByText("No pending approvals.")).toBeInTheDocument();

    rerender(
      <DataTable
        columns={COLUMNS}
        rows={[]}
        rowKey={(r: Row) => r.id}
        label="Test rows"
        things="rows"
        status="refused"
        refused={<span>refused block</span>}
      />,
    );
    expect(screen.getByText("refused block")).toBeInTheDocument();

    rerender(
      <DataTable
        columns={COLUMNS}
        rows={[]}
        rowKey={(r: Row) => r.id}
        label="Test rows"
        things="rows"
        status="error"
        error={<span>error block</span>}
      />,
    );
    expect(screen.getByText("error block")).toBeInTheDocument();
  });
});

// §2.4 EMPTY. The shipped defect: a table with zero rows rendered its full
// header row — six sort buttons and an `aria-sort` claiming an order over an
// empty set — above one sentence, which reads as a half-finished render. The
// assertions below are about the STRUCTURE, not one screen's copy: a header row
// heads rows, so with zero rows there is no header row, and a screen that
// supplies no copy still gets a sentence rather than a blank strip.
describe("DataTable — an empty result set (§2.4)", () => {
  function renderEmpty(props: Partial<React.ComponentProps<typeof DataTable<Row>>> = {}) {
    return render(
      <DataTable
        columns={COLUMNS}
        rows={[]}
        rowKey={(r: Row) => r.id}
        label="Test rows"
        things="rows"
        {...props}
      />,
    );
  }

  it("renders the screen's copy and NO header row — never naked headers", () => {
    renderEmpty({ empty: <span>No pending approvals.</span> });
    expect(screen.getByText("No pending approvals.")).toBeInTheDocument();
    // The defect, asserted directly: not one column header, and not one sort
    // control, over a table that has nothing to head.
    expect(screen.queryAllByRole("columnheader")).toEqual([]);
    expect(screen.queryAllByRole("button")).toEqual([]);
    // …and no header row hiding as a plain row: the state block is the only row.
    expect(screen.getAllByRole("row")).toHaveLength(1);
  });

  it("names the fact even when the screen supplies no copy — never a blank body", () => {
    // `empty` is optional, so it can be unset. An unset `empty` must not be a
    // blank strip under headers; the primitive falls back to §2.4's grammar
    // built from `things`.
    const { container } = renderEmpty();
    expect(screen.getByText("No rows.")).toBeInTheDocument();
    expect(screen.queryAllByRole("columnheader")).toEqual([]);
    expect(container.querySelector(".ne-td--state")?.textContent).not.toBe("");
  });

  it("KEEPS the header row while loading — skeleton rows are the table's geometry", () => {
    // The control for the rule above. Loading is not empty: real rows are one
    // tick away, and tearing the header row down and rebuilding it is the
    // flicker this primitive exists to prevent.
    renderEmpty({ status: "loading" });
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
  });

  it("KEEPS the header row when rows arrive", () => {
    renderTable();
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
  });
});

// An additive per-row state class (used by Home for the breached-deadline edge).
// Like sorting, it is a property of the primitive: a screen names which rows earn
// the class and cannot smear it across every row by accident.
describe("DataTable — rowModifier", () => {
  function byActor(name: string): HTMLElement {
    return screen
      .getAllByRole("row")
      .slice(1)
      .find((r) => within(r).getAllByRole("cell")[0]?.textContent === name)!;
  }

  it("applies the class to the rows it names and to no others", () => {
    renderTable({ rowModifier: (r) => ((r.cost ?? 0) > 5 ? "ne-tr--urgent" : undefined) });
    expect(byActor("mona")).toHaveClass("ne-tr--urgent"); // cost 11
    // The control: rows the modifier returns undefined for are untouched, so a
    // table that supplies none renders exactly as before.
    expect(byActor("zoe")).not.toHaveClass("ne-tr--urgent"); // cost 3
    expect(byActor("adam")).not.toHaveClass("ne-tr--urgent"); // cost null
  });

  it("adds to the base row classes rather than replacing them", () => {
    renderTable({ onRowActivate: () => {}, rowModifier: () => "ne-tr--urgent" });
    const row = screen.getAllByRole("row")[1]!;
    expect(row).toHaveClass("ne-tr");
    expect(row).toHaveClass("ne-tr--clickable");
    expect(row).toHaveClass("ne-tr--urgent");
  });
});
