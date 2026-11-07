// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The rules behind the Entities directory.
//
// Two of these assertions are the ones that matter. A template that declares an
// argument must NOT be offered — offering it means running it with an invented
// value and presenting a subset as the whole. And a row whose citation is not an
// entity urn must NOT acquire a key — a key guessed from a cell is a link to
// whatever record happens to be at the other end of the guess.

import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { VerifiedAnswerWire, VerifiedTemplateWire } from "../api/types";
import {
  classifyDirectoryFailure,
  directoryCountText,
  directoryRows,
  directoryTemplates,
  displayOrder,
  isZeroArg,
  truncationNote,
} from "./directory";

function template(over: Partial<VerifiedTemplateWire>): VerifiedTemplateWire {
  return {
    template_id: "t",
    version: 1,
    title: "T",
    tool_name: "read.kg.t",
    args_schema: { type: "object", properties: {} },
    columns: ["account", "name"],
    citation_var: "account",
    label_var: null,
    max_rows: null,
    ...over,
  };
}

function answer(over: Partial<VerifiedAnswerWire>): VerifiedAnswerWire {
  return {
    template_id: "accounts-directory",
    template_version: 1,
    args: {},
    columns: ["account", "name"],
    rows: [],
    citations: [],
    truncated: false,
    row_limit: 500,
    returned_rows: 0,
    total_rows: 0,
    truncation_reason: null,
    config_hash: "h",
    ontology_version: "1",
    executed_at: "2026-08-03T00:00:00Z",
    ...over,
  };
}

describe("which templates can open the corpus", () => {
  it("offers one that declares no arguments", () => {
    expect(isZeroArg({ type: "object", properties: {} })).toBe(true);
    expect(isZeroArg({ type: "object" })).toBe(true);
  });

  it("refuses one that declares an argument, even an optional one", () => {
    expect(isZeroArg({ type: "object", properties: { region: { type: "string" } } })).toBe(false);
    expect(
      isZeroArg({ type: "object", properties: { region: { type: "string" } }, required: [] }),
    ).toBe(false);
  });

  it("refuses one that requires a field its properties never described", () => {
    expect(isZeroArg({ type: "object", properties: {}, required: ["region"] })).toBe(false);
  });

  it("lists the runnable ones by the server's own titles, A–Z", () => {
    const offered = directoryTemplates([
      template({ template_id: "orders", title: "Every order visible to the caller." }),
      template({
        template_id: "by-region",
        title: "Accounts in one region.",
        args_schema: { type: "object", properties: { region: { type: "string" } } },
      }),
      template({ template_id: "accounts", title: "Every account visible to the caller." }),
    ]);
    expect(offered.map((t) => t.template_id)).toEqual(["accounts", "orders"]);
  });

  it("offers nothing at all when every registered template takes an argument", () => {
    expect(
      directoryTemplates([
        template({
          template_id: "by-region",
          args_schema: { type: "object", properties: { region: { type: "string" } } },
        }),
      ]),
    ).toEqual([]);
  });
});

describe("the display order — label leads, identifier trails", () => {
  it("hoists the declared label first and demotes the citation column to last", () => {
    // Positional, exact: the identifier-first served order is REORDERED. A rule
    // that echoed the served order returns [0, 1, 2] here and fails.
    expect(displayOrder(["ticket", "priority", "subject"], "ticket", "subject")).toEqual([2, 1, 0]);
    // The identifier is DEMOTED, never dropped: every index appears exactly once.
    expect(
      [...displayOrder(["ticket", "priority", "subject"], "ticket", "subject")].sort(),
    ).toEqual([0, 1, 2]);
  });

  it("keeps middle columns in the template's own order", () => {
    expect(displayOrder(["id", "a", "b", "label"], "id", "label")).toEqual([3, 1, 2, 0]);
  });

  it("with no declared label, renders the served order verbatim — no invented label", () => {
    // The degrade case: identifier-led because the identifier is then the only
    // identity the server vouched for. `name` being present does NOT promote it.
    expect(displayOrder(["account", "name"], "account", null)).toEqual([0, 1]);
  });

  it("ignores a label naming no served column, and a label equal to the citation", () => {
    // Defensive: served data wins over a stale declaration; never a crash, never
    // a hole in the table.
    expect(displayOrder(["account", "name"], "account", "zzz")).toEqual([0, 1]);
    expect(displayOrder(["account", "name"], "account", "account")).toEqual([0, 1]);
  });

  it("still leads with the label when the citation column is not served", () => {
    expect(displayOrder(["name", "status"], "account", "name")).toEqual([0, 1]);
    expect(displayOrder(["status", "name"], "account", "name")).toEqual([1, 0]);
  });

  it("treats an ABSENT label_var exactly like a declared null — the rolling-upgrade wire", () => {
    // An API serving the wire shape from BEFORE `label_var` joined the template
    // contract omits the key entirely, so `template.label_var` arrives
    // `undefined`, not `null` — and `VerifiedAnswers` passes it through
    // unguarded. No cast here: the signature itself must admit the value the
    // wire really carries, or the type is vouching for a shape the server does
    // not send.
    const oldWire = { citation_var: "account" } as Partial<VerifiedTemplateWire>;
    expect(displayOrder(["account", "name"], "account", oldWire.label_var)).toEqual([0, 1]);
    expect(displayOrder(["account", "name"], "account", undefined)).toEqual([0, 1]);
    // The degrade is the SAME one `null` gets: served order verbatim, no
    // invented label — never a promoted `name`, never a dropped column.
    expect(displayOrder(["name", "status", "account"], "account", undefined)).toEqual([0, 1, 2]);
    // The control: a label that IS declared still leads, so the branch above is
    // the absent case and not an unconditional passthrough.
    expect(displayOrder(["account", "name"], "account", "name")).toEqual([1, 0]);
  });
});

describe("the served answer as rows", () => {
  it("keeps the server's order and pairs each row with its own citation", () => {
    const rows = directoryRows(
      answer({
        rows: [
          ["urn:ne:entity:k-2", "Harbor Metals LLC"],
          ["urn:ne:entity:k-1", "Cascade Ventures LLC"],
        ],
        citations: ["urn:ne:entity:k-2", "urn:ne:entity:k-1"],
        returned_rows: 2,
        total_rows: 2,
      }),
    );
    // Harbor first because the SERVER sent it first — not re-sorted here.
    expect(rows.map((r) => r.cells[1])).toEqual(["Harbor Metals LLC", "Cascade Ventures LLC"]);
    expect(rows.map((r) => r.entityKey)).toEqual(["k-2", "k-1"]);
    expect(rows.map((r) => r.index)).toEqual([0, 1]);
  });

  it("gives a row NO key when its citation is not an entity urn", () => {
    const rows = directoryRows(
      answer({
        rows: [["https://crm.example/acct/7", "Harbor Metals LLC"]],
        citations: ["https://crm.example/acct/7"],
        returned_rows: 1,
        total_rows: 1,
      }),
    );
    expect(rows[0]!.citation).toBe("https://crm.example/acct/7");
    expect(rows[0]!.entityKey).toBeNull();
  });

  it("gives a row NO key when the server sent no citation for it", () => {
    const rows = directoryRows(
      answer({ rows: [["a", "b"]], citations: [], returned_rows: 1, total_rows: 1 }),
    );
    expect(rows[0]!.citation).toBeNull();
    expect(rows[0]!.entityKey).toBeNull();
  });
});

describe("the counts and the cap", () => {
  it("counts the served rows when nothing is filtered", () => {
    expect(directoryCountText(120, 120)).toBe("120 rows");
  });

  it("counts the shown rows against the window the filter ran over", () => {
    expect(directoryCountText(3, 120)).toBe("3 of 120 rows");
  });

  it("says nothing about a cap the server did not apply", () => {
    expect(truncationNote(answer({ returned_rows: 120, total_rows: 120 }))).toBeNull();
  });

  it("states the server's cap with the server's own total", () => {
    expect(truncationNote(answer({ truncated: true, returned_rows: 500, total_rows: 1200 }))).toBe(
      "Showing 500 of 1200 rows.",
    );
    // …through the shared count format, so a five-figure corpus groups its
    // digits here exactly as it does in every other count on the product.
    expect(truncationNote(answer({ truncated: true, returned_rows: 500, total_rows: 12000 }))).toBe(
      "Showing 500 of 12,000 rows.",
    );
  });

  it("stops at the number it has when the server vouched for no total", () => {
    expect(truncationNote(answer({ truncated: true, returned_rows: 500, total_rows: null }))).toBe(
      "Showing 500 rows.",
    );
  });
});

describe("what a failed directory read was", () => {
  it("calls a 403 a refusal", () => {
    expect(classifyDirectoryFailure(new ApiError("forbidden", 403))).toBe("refused");
  });

  it("calls a 503 a deployment fact", () => {
    expect(classifyDirectoryFailure(new ApiError("off", 503))).toBe("off");
  });

  it("does NOT call a broken read a role boundary", () => {
    expect(classifyDirectoryFailure(new ApiError("boom", 500))).toBe("error");
    expect(classifyDirectoryFailure(new ApiError("unknown-template", 404))).toBe("error");
    expect(classifyDirectoryFailure(new TypeError("network"))).toBe("error");
  });
});
