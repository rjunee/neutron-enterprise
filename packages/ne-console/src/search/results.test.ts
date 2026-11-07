// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §6 — the result-table rules, proven without a DOM.

import { describe, expect, it } from "vitest";
import type { Citation, RetrievalGraphPathWire, ScoredChunk } from "../api/types";
import {
  applySourceFilter,
  authorityLimitBounds,
  AUTHORITY_LIMITED_LINE,
  buildRows,
  COMPLETENESS_UNSTATED_LINE,
  markTerms,
  reportsOrdinaryNoResults,
  retrievalCompleteness,
  relatedEntities,
  searchCountText,
  sourceSystems,
  truncateMiddle,
  unionRows,
  withoutComparison,
} from "./results";

function chunk(id: string, doc: string, score: number, text = `text of ${doc}`): ScoredChunk {
  return { chunk: { chunk_id: id, document_id: doc, text, metadata: { page: 2 } }, score };
}

function citation(id: string, doc: string, system: string, url?: string): Citation {
  return {
    chunk_id: id,
    document_id: doc,
    source_system: system,
    source_ref: `ref/${doc}`,
    ...(url === undefined ? {} : { source_url: url }),
  };
}

const CHUNKS = [
  chunk("c-1", "doc-alpha", 0.91),
  chunk("c-2", "doc-bravo", 0.82),
  chunk("c-3", "doc-charlie", 0.75),
];

const CITATIONS = [
  citation("c-1", "doc-alpha", "sharepoint", "https://sharepoint.example.com/doc-alpha"),
  citation("c-2", "doc-bravo", "confluence"),
];

describe("buildRows", () => {
  it("keeps the server's order as the rank and the server's score as the score", () => {
    const rows = buildRows(CHUNKS, CITATIONS);
    expect(rows.map((r) => [r.rank, r.chunkId, r.score])).toEqual([
      [1, "c-1", 0.91],
      [2, "c-2", 0.82],
      [3, "c-3", 0.75],
    ]);
  });

  it("joins the citation by chunk id — and leaves an unmatched chunk with NO source", () => {
    const rows = buildRows(CHUNKS, CITATIONS);
    expect(rows.map((r) => r.sourceSystem)).toEqual(["sharepoint", "confluence", null]);
    // The absent fields are absent, not guessed from the document id.
    expect(rows[2]?.sourceRef).toBeNull();
    expect(rows[2]?.sourceUrl).toBeNull();
    // And a citation that carries no deep link does not gain one.
    expect(rows[1]?.sourceUrl).toBeNull();
    expect(rows[0]?.sourceUrl).toBe("https://sharepoint.example.com/doc-alpha");
  });

  it("carries the chunk's own metadata map through untouched", () => {
    expect(buildRows(CHUNKS, CITATIONS)[0]?.metadata).toEqual({ page: 2 });
  });

  it("unwraps a JSON envelope for the excerpt without inventing a document id", () => {
    const enveloped = chunk(
      "c-9",
      "doc-echo",
      0.4,
      JSON.stringify({ title: "Echo", content: "the readable body", doc_id: "OTHER" }),
    );
    const [row] = buildRows([enveloped], []);
    expect(row?.text).toBe("the readable body");
    expect(row?.excerpt).toBe("the readable body");
    // §6.3 names `chunks[].chunk.document_id` as the Document column's source.
    expect(row?.documentId).toBe("doc-echo");
  });
});

describe("unionRows", () => {
  const mine = buildRows([CHUNKS[0]!, CHUNKS[1]!], CITATIONS);
  const theirs = buildRows([CHUNKS[1]!, CHUNKS[2]!], CITATIONS);

  it("is your rows in your order, then only what they also received", () => {
    expect(unionRows(mine, theirs).map((r) => r.chunkId)).toEqual(["c-1", "c-2", "c-3"]);
  });

  it("carries each identity's own score, and null — never zero — for not returned", () => {
    expect(unionRows(mine, theirs).map((r) => [r.chunkId, r.score, r.otherScore])).toEqual([
      ["c-1", 0.91, null],
      ["c-2", 0.82, 0.82],
      ["c-3", null, 0.75],
    ]);
  });

  it("preserves served ranks instead of inventing a rank for the union", () => {
    expect(unionRows(mine, theirs).map((r) => [r.chunkId, r.rank])).toEqual([
      ["c-1", 1],
      ["c-2", 2],
      // c-3 was rank 2 in THEIR response. A displayed 3 would be client-made.
      ["c-3", 2],
    ]);
  });

  it("ending the comparison leaves your own result set exactly as served", () => {
    const back = withoutComparison(unionRows(mine, theirs));
    expect(back.map((r) => [r.rank, r.chunkId, r.score])).toEqual([
      [1, "c-1", 0.91],
      [2, "c-2", 0.82],
    ]);
    expect(back.every((r) => r.otherScore === undefined)).toBe(true);
  });
});

describe("the source-system facet", () => {
  const rows = buildRows(CHUNKS, CITATIONS);

  it("lists the distinct served systems A–Z and nothing for an uncited row", () => {
    expect(sourceSystems(rows)).toEqual(["confluence", "sharepoint"]);
  });

  it("no selection filters nothing", () => {
    expect(applySourceFilter(rows, [])).toHaveLength(3);
  });

  it("a selection keeps exactly the rows that named it — by row identity", () => {
    expect(applySourceFilter(rows, ["confluence"]).map((r) => r.chunkId)).toEqual(["c-2"]);
    expect(applySourceFilter(rows, ["sharepoint", "confluence"]).map((r) => r.chunkId)).toEqual([
      "c-1",
      "c-2",
    ]);
    // The uncited row is never swept into a system it did not name.
    expect(applySourceFilter(rows, ["sharepoint"]).map((r) => r.chunkId)).not.toContain("c-3");
  });
});

describe("searchCountText", () => {
  it("counts the rendered window and the measured run", () => {
    expect(searchCountText(3, 1240, 0)).toBe("3 results · 1.2 s");
  });

  it("extends with the response's unconfirmed count, and only when there is one", () => {
    expect(searchCountText(3, 1240, 2)).toBe("3 results · 1.2 s · 2 unconfirmed");
    expect(searchCountText(0, 400, 0)).toBe("0 results · 400 ms");
  });

  it("attributes each comparison response's unconfirmed count to its identity", () => {
    expect(searchCountText(3, 1240, 2, { identity: "bob@example.com", provisional: 1 })).toBe(
      "3 results · 1.2 s · You: 2 unconfirmed · bob@example.com: 1 unconfirmed",
    );
    expect(searchCountText(3, 1240, 0, { identity: "bob@example.com", provisional: 0 })).toBe(
      "3 results · 1.2 s",
    );
  });
});

describe("truncateMiddle", () => {
  it("leaves a short id alone", () => {
    expect(truncateMiddle("doc-alpha")).toBe("doc-alpha");
  });

  it("keeps the head AND the tail — the tail is what distinguishes two ids", () => {
    const long = "contracts/2024/renewal/acme-supplier-agreement-v3";
    const cut = truncateMiddle(long, 20);
    expect(cut).toHaveLength(20);
    expect(cut.startsWith("contracts/")).toBe(true);
    expect(cut.endsWith("v3")).toBe(true);
  });
});

describe("markTerms — a fact about the text, never a claim about the ranking", () => {
  it("marks the one verbatim occurrence of a single-word query", () => {
    expect(markTerms("written notice period begins", "notice")).toEqual([
      { text: "written ", marked: false },
      { text: "notice", marked: true },
      { text: " period begins", marked: false },
    ]);
  });

  it("marks two adjacent query words as ONE span, not two abutting ones", () => {
    // "notice" and "period" are adjacent in both the query and the text.
    expect(markTerms("the notice period begins", "termination notice period")).toEqual([
      { text: "the ", marked: false },
      { text: "notice period", marked: true },
      { text: " begins", marked: false },
    ]);
  });

  it("still marks a lone occurrence of one query word on its own", () => {
    // "notice" appears without "period" beside it — a single-word span.
    expect(markTerms("written notice.", "termination notice period")).toEqual([
      { text: "written ", marked: false },
      { text: "notice", marked: true },
      { text: ".", marked: false },
    ]);
  });

  it("is case-insensitive: the query's casing need not match the text's", () => {
    expect(markTerms("Termination clause 9", "termination")).toEqual([
      { text: "Termination", marked: true },
      { text: " clause 9", marked: false },
    ]);
  });

  it("NEVER marks a query word embedded inside a larger word — whole-word only", () => {
    // "notice" is a real substring of "noticeable", but not a verbatim
    // occurrence of the query term: marking it would be a false claim about
    // what matched.
    expect(markTerms("a noticeable change", "notice")).toEqual([
      { text: "a noticeable change", marked: false },
    ]);
  });

  it("marks every occurrence, not just the first", () => {
    expect(markTerms("notice given; notice received.", "notice")).toEqual([
      { text: "notice", marked: true },
      { text: " given; ", marked: false },
      { text: "notice", marked: true },
      { text: " received.", marked: false },
    ]);
  });

  it("an empty query marks nothing — the text renders unchanged, never blank", () => {
    expect(markTerms("plain body text", "")).toEqual([{ text: "plain body text", marked: false }]);
    expect(markTerms("plain body text", "   ")).toEqual([
      { text: "plain body text", marked: false },
    ]);
  });

  it("marks nothing when none of the query's words occur in the text", () => {
    expect(markTerms("an unrelated sentence", "kumquat")).toEqual([
      { text: "an unrelated sentence", marked: false },
    ]);
  });
});

describe("relatedEntities", () => {
  const paths: RetrievalGraphPathWire[] = [
    {
      hops: [
        {
          subject: {
            entity_class: "Company",
            entity_key: "co:acme",
            display: "Acme",
            assurance: "source",
            source_ref: "ref/a",
          },
          edge_kind: "supplies",
          obj: {
            entity_class: "Company",
            entity_key: "co:globex",
            display: "Globex",
            assurance: "extracted",
            source_ref: "llm:abcdef0123456789:ref/b",
          },
          source_system: "sharepoint",
          source_ref: "ref/edge",
          assurance: "source",
        },
        {
          subject: {
            entity_class: "Company",
            entity_key: "co:globex",
            display: "Globex",
            assurance: "extracted",
            source_ref: "llm:abcdef0123456789:ref/b",
          },
          edge_kind: "supplies",
          obj: {
            entity_class: "Company",
            entity_key: "co:initech",
            display: "Initech",
            assurance: "confirmed",
            source_ref: "ref/c",
          },
          source_system: "sharepoint",
          source_ref: "ref/edge2",
          assurance: "source",
        },
      ],
    },
  ];

  it("is one chip per entity, first appearance first", () => {
    expect(relatedEntities(paths).map((e) => e.entityKey)).toEqual([
      "co:acme",
      "co:globex",
      "co:initech",
    ]);
  });

  it("flags exactly the entities the server did not mark as vouched for", () => {
    expect(relatedEntities(paths).map((e) => e.provisional)).toEqual([false, true, false]);
  });

  it("has no chips when the response carried no paths", () => {
    expect(relatedEntities([])).toEqual([]);
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the retrieval status rules
// ===========================================================================

describe("the authorized-retrieval status", () => {
  const partial = {
    status: "partial",
    scanned_count: 400,
    returned_count: 3,
    scan_limit: 400,
  } as const;

  it("carries partial through with the server's three bounds, unmodified", () => {
    expect(retrievalCompleteness(partial)).toEqual({
      kind: "limited",
      status: "partial",
      scanned: 400,
      returned: 3,
      scanLimit: 400,
    });
  });

  it("carries exhausted through as exhausted rather than folding it into partial", () => {
    expect(
      retrievalCompleteness({
        status: "exhausted",
        scanned_count: 500,
        returned_count: 0,
        scan_limit: 500,
      }),
    ).toEqual({ kind: "limited", status: "exhausted", scanned: 500, returned: 0, scanLimit: 500 });
  });

  it("reports a complete page as complete, the one state that proves a page whole", () => {
    expect(
      retrievalCompleteness({
        status: "complete",
        scanned_count: 3,
        returned_count: 3,
        scan_limit: 400,
      }),
    ).toEqual({ kind: "complete" });
  });

  it("fails closed to unstated for a response that carried no envelope", () => {
    expect(retrievalCompleteness(undefined)).toEqual({ kind: "unstated" });
  });

  it("states the spec sentence as a literal, with no count interpolated into it", () => {
    expect(AUTHORITY_LIMITED_LINE).toBe("Authority-limited — results may be incomplete");
  });

  it("says something different where the server said nothing, claiming no authority decision", () => {
    expect(COMPLETENESS_UNSTATED_LINE).toBe(
      "Completeness unstated — this page is not proven complete",
    );
    expect(COMPLETENESS_UNSTATED_LINE).not.toBe(AUTHORITY_LIMITED_LINE);
  });

  it("labels the bounds as scan accounting and names no corpus total", () => {
    expect(authorityLimitBounds({ scanned: 400, returned: 3, scanLimit: 400 })).toBe(
      "400 scanned · 3 authorized · 400 scan bound",
    );
  });

  it("permits an ordinary no-result only for a page the server called complete", () => {
    expect(
      reportsOrdinaryNoResults({
        status: "complete",
        scanned_count: 40,
        returned_count: 0,
        scan_limit: 500,
      }),
    ).toBe(true);
  });

  it("refuses an ordinary no-result for both authority-limited statuses and for silence", () => {
    expect(reportsOrdinaryNoResults(partial)).toBe(false);
    expect(
      reportsOrdinaryNoResults({
        status: "exhausted",
        scanned_count: 500,
        returned_count: 0,
        scan_limit: 500,
      }),
    ).toBe(false);
    expect(reportsOrdinaryNoResults(undefined)).toBe(false);
  });
});
