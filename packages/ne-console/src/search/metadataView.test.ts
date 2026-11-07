// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// metadataView: a structured metadata value becomes labelled rows, LOSSLESSLY;
// everything that is not a plain object renders exactly the scalar text the
// pane always rendered. The motivating value is the provenance stamp.

import { describe, expect, it } from "vitest";
import { metadataView } from "./metadataView";

/** The real shape (ne-brain write/contracts.py ProvenanceStamp, serialized
 * under metadata.provenance) as it reached the owner's screen. */
const PROVENANCE = {
  provenance_class: "ingested",
  source: "connector:acme-fixture:doc-0169",
  actor: "connect-sync",
  written_at: "2026-08-04T19:42:18.911317+00:00",
  write_id: "073663ab",
  shape: "ne.connect.record@1",
};

describe("structures become fields", () => {
  it("renders the provenance object as one row per field, every value verbatim", () => {
    const view = metadataView(PROVENANCE);
    expect(view.kind).toBe("fields");
    if (view.kind !== "fields") throw new Error("unreachable");
    // LOSSLESS: exactly the object's own keys, in its own order, and each
    // value the server's own string — a timestamp is not reformatted.
    expect(view.fields.map(([key]) => key)).toEqual([
      "provenance_class",
      "source",
      "actor",
      "written_at",
      "write_id",
      "shape",
    ]);
    expect(view.fields.map(([, inner]) => (inner.kind === "scalar" ? inner.text : null))).toEqual([
      "ingested",
      "connector:acme-fixture:doc-0169",
      "connect-sync",
      "2026-08-04T19:42:18.911317+00:00",
      "073663ab",
      "ne.connect.record@1",
    ]);
  });

  it("a string the server serialized an object into is unwrapped the same way", () => {
    const view = metadataView(JSON.stringify(PROVENANCE));
    expect(view.kind).toBe("fields");
    if (view.kind !== "fields") throw new Error("unreachable");
    expect(view.fields).toHaveLength(6);
  });

  it("recurses into nested objects without dropping a level", () => {
    const view = metadataView({ outer: { inner: "value" }, flat: 4 });
    if (view.kind !== "fields") throw new Error("expected fields");
    const [outerKey, outerView] = view.fields[0]!;
    expect(outerKey).toBe("outer");
    if (outerView.kind !== "fields") throw new Error("expected nested fields");
    expect(outerView.fields).toEqual([["inner", { kind: "scalar", text: "value" }]]);
    expect(view.fields[1]).toEqual(["flat", { kind: "scalar", text: "4" }]);
  });
});

describe("everything else stays the scalar it always was", () => {
  it("prose, numbers and booleans", () => {
    expect(metadataView("renewals")).toEqual({ kind: "scalar", text: "renewals" });
    expect(metadataView(4)).toEqual({ kind: "scalar", text: "4" });
    expect(metadataView(true)).toEqual({ kind: "scalar", text: "true" });
  });

  it("an array is a value, not a table", () => {
    expect(metadataView([1, 2])).toEqual({ kind: "scalar", text: "[1,2]" });
  });

  it("a string that merely opens with a brace renders verbatim", () => {
    expect(metadataView("{not json")).toEqual({ kind: "scalar", text: "{not json" });
  });

  it("a JSON string that is not an object renders verbatim", () => {
    expect(metadataView("[1,2]")).toEqual({ kind: "scalar", text: "[1,2]" });
  });

  it("an empty object renders as a value, never as a blank", () => {
    expect(metadataView({})).toEqual({ kind: "scalar", text: "{}" });
  });
});
