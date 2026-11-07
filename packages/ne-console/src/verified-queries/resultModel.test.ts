// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { VerifiedAnswerWire, VerifiedTemplateWire } from "../api/types";
import {
  verifiedResultColumns,
  verifiedResultCsv,
  verifiedResultRows,
  verifiedResultWindow,
} from "./resultModel";

const TEMPLATE = {
  template_id: "payments",
  version: 3,
  title: "Payments due in a validated window.",
  citation_var: "loan",
  label_var: "name",
} as VerifiedTemplateWire;
const ANSWER = {
  columns: ["loan", "name", "amount"],
  rows: [
    ["urn:ne:entity:loan-2", "Team Zulu", "20"],
    ["urn:ne:entity:loan-1", "Team Alpha", "10"],
    ["urn:ne:entity:loan-3", "Excluded", "30"],
  ],
  citations: ["urn:ne:entity:loan-2", "urn:ne:entity:loan-1", "urn:ne:entity:loan-3"],
} as VerifiedAnswerWire;

describe("verified result model", () => {
  it("uses one filtered/sorted window for the table and CSV", () => {
    const rows = verifiedResultRows(ANSWER);
    const columns = verifiedResultColumns(ANSWER, TEMPLATE);
    const sort = { key: "column-1", dir: "asc" } as const;
    const window = verifiedResultWindow(rows, columns, "team", sort);
    expect(window.map((row) => row.cells[1])).toEqual(["Team Alpha", "Team Zulu"]);
    const csv = verifiedResultCsv({
      template: TEMPLATE,
      columns,
      rows,
      filter: "team",
      sort,
      now: new Date("2026-08-20T12:00:00Z"),
    });
    expect(csv.indexOf("Team Alpha")).toBeLessThan(csv.indexOf("Team Zulu"));
    expect(csv).not.toContain("Excluded");
    expect(csv).toContain("Search: team; Sort: column-1 asc");
    expect(csv).toContain("name,amount,loan,Entity citation");
  });

  it("neutralizes formula-leading served cells in the export", () => {
    const answer = { ...ANSWER, rows: [["urn:ne:entity:x", "=CMD()", "1"]] };
    const rows = verifiedResultRows(answer);
    const columns = verifiedResultColumns(answer, TEMPLATE);
    expect(
      verifiedResultCsv({ template: TEMPLATE, columns, rows, filter: "", sort: null }),
    ).toContain("'=CMD()");
  });
});
