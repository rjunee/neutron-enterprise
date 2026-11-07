// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  parseDocumentRouteState,
  serializeDocumentRouteState,
  type DocumentRouteState,
} from "./documentRouteState";

const LATEST: DocumentRouteState = {
  document: null,
  selector: { kind: "latest" },
  stage: null,
  query: "",
  documentType: null,
  source: "",
  includeStructured: false,
};

describe("document route state", () => {
  it("consumes the exact Answer Ledger legacy version link without interpreting the document id", () => {
    const url = new URL(
      "/library?tab=documents&document=quarterly%2Freview%2Braw&version=004&citation=one&citation=two",
      "https://console.invalid",
    );

    expect(parseDocumentRouteState(url.searchParams)).toEqual({
      ...LATEST,
      document: "quarterly/review+raw",
      selector: { kind: "version", version: 4 },
    });
  });

  it("parses an as-of selector and only accepts named stage/type filter values", () => {
    expect(
      parseDocumentRouteState(
        "document=&as_of=2026-08-18T12%3A30%3A00-04%3A00&stage=embed&q=%20SOP%20&type=document&source=M365",
      ),
    ).toEqual({
      document: "",
      selector: { kind: "as_of", asOf: "2026-08-18T12:30:00-04:00" },
      stage: "embed",
      query: " SOP ",
      documentType: "document",
      source: "M365",
      includeStructured: false,
    });
    expect(parseDocumentRouteState("stage=unknown&type=docx")).toEqual(LATEST);
  });

  // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
  // ".negraph.json" connector filename convention; REMOVE when the real
  // `structured` flag lands on the document wire. Default (no `structured`
  // param, or any value other than "1") EXCLUDES operational-graph records.
  it("parses the interim operational-graph opt-in only from the exact '1' value", () => {
    expect(parseDocumentRouteState("structured=1")).toEqual({
      ...LATEST,
      includeStructured: true,
    });
    expect(parseDocumentRouteState("structured=true")).toEqual(LATEST);
    expect(parseDocumentRouteState("structured=0")).toEqual(LATEST);
  });

  it.each([
    ["version=2&as_of=2026-08-18T12%3A30%3A00Z", "conflicting_selectors"],
    ["version=0", "invalid_version"],
    ["version=-1", "invalid_version"],
    ["version=1.5", "invalid_version"],
    ["version=9007199254740992", "invalid_version"],
    ["as_of=August%2018", "invalid_as_of"],
    ["as_of=2026-08-18T12%3A30%3A00", "invalid_as_of"],
    ["as_of=2026-02-30T12%3A30%3A00Z", "invalid_as_of"],
  ])("fails closed for %s", (query, reason) => {
    expect(parseDocumentRouteState(query).selector).toMatchObject({ kind: "invalid", reason });
  });

  it.each(["document", "version", "as_of", "stage", "q", "type", "source", "structured", "cursor"])(
    "fails closed instead of choosing between duplicate %s parameters",
    (key) => {
      const params = new URLSearchParams("tab=documents&citation=one&citation=two");
      params.append(key, key === "version" ? "1" : "first");
      params.append(key, key === "version" ? "2" : "second");

      expect(parseDocumentRouteState(params)).toEqual({
        ...LATEST,
        selector: {
          kind: "invalid",
          reason: "ambiguous_owned_parameter",
          rawVersion: null,
          rawAsOf: null,
        },
      });
      expect(params.getAll("citation")).toEqual(["one", "two"]);
    },
  );

  it("preserves an opaque document id byte-for-byte after URL decoding", () => {
    const opaque = "  Doc/Case+Sensitive ";
    const parsed = parseDocumentRouteState(
      `document=${encodeURIComponent(opaque)}&version=1&cursor=ephemeral`,
    );

    expect(parsed.document).toBe(opaque);
    const serialized = serializeDocumentRouteState(parsed, "cursor=stale&citation=a&citation=b");
    expect(serialized.get("document")).toBe(opaque);
    expect(serialized.has("cursor")).toBe(false);
    expect(serialized.getAll("citation")).toEqual(["a", "b"]);
  });

  it("serializes one selector, preserves unrelated repeated parameters, and always drops cursor", () => {
    const params = serializeDocumentRouteState(
      {
        document: "Doc/+ opaque",
        selector: { kind: "version", version: 7 },
        stage: "graph",
        query: "Debit Card",
        documentType: "document",
        source: "m365-sharepoint",
        includeStructured: false,
      },
      "tab=documents&citation=one&citation=two&as_of=stale&cursor=secret-server-token",
    );

    expect(params.get("document")).toBe("Doc/+ opaque");
    expect(params.get("version")).toBe("7");
    expect(params.has("as_of")).toBe(false);
    expect(params.has("cursor")).toBe(false);
    expect(params.get("stage")).toBe("graph");
    expect(params.get("q")).toBe("Debit Card");
    expect(params.get("type")).toBe("document");
    expect(params.get("source")).toBe("m365-sharepoint");
    expect(params.has("structured")).toBe(false);
    expect(params.get("tab")).toBe("documents");
    expect(params.getAll("citation")).toEqual(["one", "two"]);
    expect(parseDocumentRouteState(params).selector).toEqual({ kind: "version", version: 7 });
  });

  // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
  // ".negraph.json" connector filename convention; REMOVE when the real
  // `structured` flag lands on the document wire. Omitted (the default)
  // EXCLUDES operational-graph records from the list.
  it("serializes the interim operational-graph opt-in as structured=1, and omits it when off", () => {
    const on = serializeDocumentRouteState({ ...LATEST, includeStructured: true });
    expect(on.get("structured")).toBe("1");
    expect(parseDocumentRouteState(on).includeStructured).toBe(true);

    const off = serializeDocumentRouteState({ ...LATEST, includeStructured: false });
    expect(off.has("structured")).toBe(false);
  });

  it("switches version/as-of/latest without retaining a conflicting selector", () => {
    const asOf = serializeDocumentRouteState(
      { ...LATEST, document: "doc", selector: { kind: "as_of", asOf: "2026-08-18T16:30:00Z" } },
      "document=old&version=3",
    );
    expect(asOf.get("as_of")).toBe("2026-08-18T16:30:00Z");
    expect(asOf.has("version")).toBe(false);

    const version = serializeDocumentRouteState(
      { ...LATEST, document: "doc", selector: { kind: "version", version: 8 } },
      asOf,
    );
    expect(version.get("version")).toBe("8");
    expect(version.has("as_of")).toBe(false);

    const latest = serializeDocumentRouteState(LATEST, version);
    expect(latest.has("version")).toBe(false);
    expect(latest.has("as_of")).toBe(false);
    expect(latest.has("document")).toBe(false);
  });

  it("refuses to mint an invalid selector or non-positive version", () => {
    expect(() =>
      serializeDocumentRouteState({
        ...LATEST,
        selector: {
          kind: "invalid",
          reason: "conflicting_selectors",
          rawVersion: "1",
          rawAsOf: "2026-08-18T12:30:00Z",
        },
      }),
    ).toThrow("cannot serialize invalid document selector state");
    expect(() =>
      serializeDocumentRouteState({ ...LATEST, selector: { kind: "version", version: 0 } }),
    ).toThrow("document version must be a positive safe integer");
  });
});
