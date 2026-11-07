// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  type AssuranceHopLike,
  isProvisionalAssurance,
  parseExtractedSourceRef,
  provisionalEvidenceForHop,
} from "./assurance";

describe("isProvisionalAssurance", () => {
  it("is false for the vouched-for labels", () => {
    expect(isProvisionalAssurance("source")).toBe(false);
    expect(isProvisionalAssurance("confirmed")).toBe(false);
  });

  it("is true for extracted and any unrecognized/garbage label (fail toward flagging)", () => {
    expect(isProvisionalAssurance("extracted")).toBe(true);
    expect(isProvisionalAssurance("garbage")).toBe(true);
    expect(isProvisionalAssurance("")).toBe(true);
    expect(isProvisionalAssurance("Source")).toBe(true); // case-sensitive, no fuzzy match
  });
});

describe("parseExtractedSourceRef", () => {
  it("parses a valid llm:<16hex>:<ref> source_ref", () => {
    expect(parseExtractedSourceRef("llm:0123456789abcdef:doc-42#chunk-3")).toEqual({
      sha16: "0123456789abcdef",
      evidenceRef: "doc-42#chunk-3",
    });
  });

  it("returns null for the wrong prefix", () => {
    expect(parseExtractedSourceRef("source:0123456789abcdef:doc-42")).toBeNull();
  });

  it("returns null for a 15-hex (too-short) sha", () => {
    expect(parseExtractedSourceRef("llm:0123456789abcde:doc-42")).toBeNull();
  });

  it("returns null for a 17-hex (too-long) sha", () => {
    expect(parseExtractedSourceRef("llm:0123456789abcdef0:doc-42")).toBeNull();
  });

  it("returns null for an empty trailing ref", () => {
    expect(parseExtractedSourceRef("llm:0123456789abcdef:")).toBeNull();
  });

  it("returns null for non-hex characters in the sha position", () => {
    expect(parseExtractedSourceRef("llm:0123456789abcdeg:doc-42")).toBeNull();
  });

  it("returns null for uppercase hex (lowercase-only grammar)", () => {
    expect(parseExtractedSourceRef("llm:0123456789ABCDEF:doc-42")).toBeNull();
  });
});

/** A hop with THREE independently-settable refs (edge, subject, obj) — every
 * case below uses DISTINCT ref strings per element, so an assertion that
 * passes only because two refs happen to be equal is impossible. */
function hop(overrides: {
  assurance: string;
  subjectAssurance: string;
  objAssurance: string;
}): AssuranceHopLike {
  return {
    assurance: overrides.assurance,
    source_ref: "edge-ref",
    subject: { assurance: overrides.subjectAssurance, source_ref: "subject-ref" },
    obj: { assurance: overrides.objAssurance, source_ref: "obj-ref" },
  };
}

describe("provisionalEvidenceForHop", () => {
  it("edge-priority: an extracted edge + an extracted subject -> returns the EDGE's own {assurance, ref}", () => {
    const h = hop({
      assurance: "extracted",
      subjectAssurance: "extracted",
      objAssurance: "source",
    });
    expect(provisionalEvidenceForHop(h)).toEqual({ assurance: "extracted", sourceRef: "edge-ref" });
  });

  it("vouched edge + extracted subject -> returns the SUBJECT's ref, NOT the edge's", () => {
    const h = hop({
      assurance: "source",
      subjectAssurance: "extracted",
      objAssurance: "confirmed",
    });
    const result = provisionalEvidenceForHop(h);
    expect(result).toEqual({ assurance: "extracted", sourceRef: "subject-ref" });
    // Explicit inequality guard: the exact mismatch this unit fixes would
    // return the edge's ref here instead.
    expect(result?.sourceRef).not.toBe("edge-ref");
  });

  it("vouched edge + vouched subject, extracted obj -> returns the OBJ's ref", () => {
    const h = hop({
      assurance: "confirmed",
      subjectAssurance: "source",
      objAssurance: "extracted",
    });
    const result = provisionalEvidenceForHop(h);
    expect(result).toEqual({ assurance: "extracted", sourceRef: "obj-ref" });
    expect(result?.sourceRef).not.toBe("edge-ref");
    expect(result?.sourceRef).not.toBe("subject-ref");
  });

  it("all vouched (source/confirmed mix) -> null (control)", () => {
    const h = hop({ assurance: "source", subjectAssurance: "confirmed", objAssurance: "source" });
    expect(provisionalEvidenceForHop(h)).toBeNull();
  });

  it("an unknown-garbage endpoint label is flagged too, carrying THAT endpoint's own ref (fail-toward-flagging)", () => {
    const h = hop({
      assurance: "source",
      subjectAssurance: "source",
      objAssurance: "tampered-value",
    });
    expect(provisionalEvidenceForHop(h)).toEqual({
      assurance: "tampered-value",
      sourceRef: "obj-ref",
    });
  });
});
