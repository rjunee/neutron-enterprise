// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.1 — the ordering rules, at the pure seam.
//
// The rule that needs a test, rather than a comment, is NULLS LAST IN BOTH
// DIRECTIONS. It is the one a naive comparator gets wrong in exactly one of the
// two directions, and it fails silently: a descending click floats a screenful
// of em dashes over the rows the reader asked to see, and nothing throws.

import { describe, expect, it } from "vitest";
import { compareValues, filterRows, rowSearchText, sortRows } from "./tableSort";
import type { SortableColumn } from "./tableSort";

interface Row {
  name: string;
  cost: number | null;
  lane: string;
}

const ROWS: Row[] = [
  { name: "entry 10", cost: 5, lane: "private" },
  { name: "entry 9", cost: null, lane: "frontier" },
  { name: "entry 2", cost: 5, lane: "private" },
  { name: "entry 30", cost: 1, lane: "frontier" },
];

const COLUMNS: SortableColumn<Row>[] = [
  { key: "name", sortValue: (r) => r.name, searchText: (r) => r.name },
  { key: "cost", sortValue: (r) => r.cost },
  { key: "lane", searchText: (r) => r.lane },
];

const names = (rows: readonly Row[]) => rows.map((r) => r.name);

describe("sortRows", () => {
  it("sorts ascending, with numeric collation on strings", () => {
    // "entry 9" before "entry 10": lexical ordering would put 10 first, which is
    // wrong on every column that carries a number inside a label.
    expect(names(sortRows(ROWS, COLUMNS, { key: "name", dir: "asc" }))).toEqual([
      "entry 2",
      "entry 9",
      "entry 10",
      "entry 30",
    ]);
  });

  it("sorts descending", () => {
    expect(names(sortRows(ROWS, COLUMNS, { key: "name", dir: "desc" }))).toEqual([
      "entry 30",
      "entry 10",
      "entry 9",
      "entry 2",
    ]);
  });

  it("keeps a null value LAST in BOTH directions", () => {
    const asc = sortRows(ROWS, COLUMNS, { key: "cost", dir: "asc" });
    const desc = sortRows(ROWS, COLUMNS, { key: "cost", dir: "desc" });
    // The absent cost is last ascending…
    expect(asc[asc.length - 1]?.cost).toBeNull();
    // …AND last descending. Reversing direction reverses the comparison of two
    // PRESENT values; it does not promote an absent one to the top of the list.
    expect(desc[desc.length - 1]?.cost).toBeNull();
    // The control: the present values genuinely did reverse, so the assertion
    // above was not satisfied by a sort that did nothing at all.
    expect(asc.filter((r) => r.cost !== null).map((r) => r.cost)).toEqual([1, 5, 5]);
    expect(desc.filter((r) => r.cost !== null).map((r) => r.cost)).toEqual([5, 5, 1]);
  });

  it("is STABLE: equal values keep their served order", () => {
    const asc = sortRows(ROWS, COLUMNS, { key: "cost", dir: "asc" });
    // Both cost-5 rows are equal; "entry 10" was served before "entry 2".
    expect(names(asc.filter((r) => r.cost === 5))).toEqual(["entry 10", "entry 2"]);
  });

  it("returns the input untouched for no sort, an unknown key, or an unsortable column", () => {
    expect(sortRows(ROWS, COLUMNS, null)).toBe(ROWS);
    expect(sortRows(ROWS, COLUMNS, { key: "nope", dir: "asc" })).toBe(ROWS);
    // `lane` declares searchText but no sortValue — it is deliberately not
    // sortable, and asking for it must not silently sort by something else.
    expect(sortRows(ROWS, COLUMNS, { key: "lane", dir: "asc" })).toBe(ROWS);
  });
});

describe("compareValues", () => {
  it("compares numbers numerically and booleans false-before-true", () => {
    expect(compareValues(2, 10)).toBeLessThan(0);
    expect(compareValues(false, true)).toBeLessThan(0);
  });
});

describe("filterRows", () => {
  it("filters over every column's searchText, case-insensitively", () => {
    expect(names(filterRows(ROWS, COLUMNS, "FRONTIER"))).toEqual(["entry 9", "entry 30"]);
  });

  it("an empty or whitespace-only query filters nothing", () => {
    expect(filterRows(ROWS, COLUMNS, "")).toBe(ROWS);
    expect(filterRows(ROWS, COLUMNS, "   ")).toBe(ROWS);
  });

  it("only columns that declare searchText contribute", () => {
    // `cost` has no searchText, so a query of "5" matches nothing — the search
    // surface is declared per column, not inferred from whatever was rendered.
    expect(filterRows(ROWS, COLUMNS, "5")).toEqual([]);
    expect(rowSearchText(ROWS[0]!, COLUMNS)).toBe("entry 10 private");
  });
});
