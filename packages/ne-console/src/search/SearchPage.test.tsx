// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §6 — SEARCH. What this file proves, in the order the spec states it:
//
//   * the query row's contract (placeholder, results size → the request, the
//     in-flight counter and its Cancel);
//   * the DEFAULT SORT IS RELEVANCE, and Source / Document / Score are really
//     sortable — asserted by ROW IDENTITY, because a row count is satisfied by
//     the wrong rows in the wrong order;
//   * every filter narrows to NAMED ROWS, again by identity;
//   * the four state strings, verbatim;
//   * a REFUSAL renders the server's refusal and an ERROR NEVER DOES — a role
//     boundary printed over a network failure is a false statement about
//     authorization, which is the one thing this product may not do;
//   * the comparison view: one table, the union, each identity's own column;
//   * the detail pane's served fields and its two ways out.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import type { AuthContextProps } from "react-oidc-context";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import { ApiError } from "../api/client";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";
import type { Citation, RetrievalGraphPathWire, RetrievalResult, ScoredChunk } from "../api/types";
import { AuthStub, TEST_ACCESS_TOKEN } from "../test/authStub";
import { PRINCIPAL, RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { truncateMiddle } from "./results";
import { SearchPage } from "./SearchPage";
import { clearSearchHistory, searchHistoryFor } from "./searchHistory";

// ---------------------------------------------------------------------------
// The second identity's seam. The real one opens an OIDC popup; the flow under
// test is what this screen does with the key, not how oidc-client-ts gets one.
// ---------------------------------------------------------------------------
const compare = vi.hoisted(() => ({
  blocked: false,
  acquire: null as null | (() => Promise<{ label: string }>),
  search: null as null | (() => Promise<unknown>),
  withdrawn: 0,
}));

vi.mock("../api/compareAuth", () => ({
  COMPARE_CALLBACK_PATH: "/auth/compare-callback",
  popupsBlocked: () => compare.blocked,
  acquireCompareKey: async () => {
    const key = await (compare.acquire ?? (() => Promise.reject(new Error("no key"))))();
    return {
      label: key.label,
      getAccessToken: () => "second-key",
      withdraw: async () => {
        compare.withdrawn += 1;
      },
    };
  },
  createCompareClient: () => ({
    search: async () => (compare.search ?? (() => Promise.reject(new Error("no search"))))(),
  }),
}));

// ---------------------------------------------------------------------------
// Fixtures. Three chunks, two of them cited from two DIFFERENT source systems
// and one cited from none — so "unmatched renders the em dash" and "a facet
// keeps exactly its own rows" are both provable on one result set.
// ---------------------------------------------------------------------------

function chunk(id: string, doc: string, score: number, text: string): ScoredChunk {
  return {
    chunk: { chunk_id: id, document_id: doc, text, metadata: { page: 4, section: "renewals" } },
    score,
  };
}

const CHUNKS: ScoredChunk[] = [
  chunk("c-1", "doc-zulu", 0.91, "Zulu renewal terms and the notice period."),
  chunk("c-2", "doc-alpha", 0.82, "Alpha onboarding checklist for new engineers."),
  chunk("c-3", "doc-mike", 0.75, "Mike incident report, uncited in this response."),
];

const CITATIONS: Citation[] = [
  {
    chunk_id: "c-1",
    document_id: "doc-zulu",
    source_system: "sharepoint",
    source_ref: "ref/doc-zulu",
    source_url: "https://sharepoint.example.com/doc-zulu",
  },
  {
    chunk_id: "c-2",
    document_id: "doc-alpha",
    source_system: "confluence",
    source_ref: "ref/doc-alpha",
  },
];

const GRAPH_PATHS: RetrievalGraphPathWire[] = [
  {
    hops: [
      {
        subject: {
          entity_class: "Company",
          entity_key: "co:acme",
          display: "Acme Holdings",
          assurance: "source",
          source_ref: "ref/doc-zulu",
        },
        edge_kind: "supplies",
        obj: {
          entity_class: "Company",
          entity_key: "co:globex",
          display: "Globex",
          assurance: "extracted",
          source_ref: "llm:abcdef0123456789:ref/doc-zulu",
        },
        source_system: "sharepoint",
        source_ref: "ref/doc-zulu",
        assurance: "source",
      },
    ],
  },
];

function result(over: Partial<RetrievalResult> = {}): RetrievalResult {
  return {
    chunks: CHUNKS,
    citations: CITATIONS,
    graph_paths: [],
    provisional_source_count: 0,
    // The server states completeness explicitly (015A0-ENFORCEMENT). A default
    // that omitted it would put every test on this page in the fail-closed
    // unstated branch and stop asserting the ordinary one.
    authorized_retrieval: {
      status: "complete",
      scanned_count: CHUNKS.length,
      returned_count: CHUNKS.length,
      scan_limit: 500,
    },
    ...over,
  };
}

/** Every test declares its own search response; nothing leans on the shared
 * handler, so a fixture change elsewhere cannot quietly move these assertions. */
function respondWith(body: RetrievalResult | (() => Promise<RetrievalResult>)) {
  server.use(
    http.post("*/api/v1/brain/search", async () =>
      HttpResponse.json(typeof body === "function" ? await body() : body),
    ),
  );
}

/** A response the TEST decides when to release. An in-flight assertion raced
 * against a wall-clock delay is a test that passes on a fast machine and fails
 * on a loaded one; this one has no clock in it at all. */
function heldResponse() {
  let release!: (body: RetrievalResult) => void;
  const held = new Promise<RetrievalResult>((resolve) => {
    release = resolve;
  });
  respondWith(() => held);
  return { release: () => release(result()) };
}

function renderSearch(config: Partial<ConsoleRuntimeConfig> = {}) {
  return render(
    <MemoryRouter initialEntries={["/search"]}>
      <RuntimeConfigContext.Provider value={{ ...RUNTIME_CONFIG, ...config }}>
        <AuthStub>
          <SearchPage />
        </AuthStub>
      </RuntimeConfigContext.Provider>
    </MemoryRouter>,
  );
}

async function search(query = "renewal") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Search connected sources"), query);
  await user.click(screen.getByRole("button", { name: "Search" }));
  return user;
}

function table() {
  return screen.getByRole("table", { name: "Search results" });
}

/** The data rows only — a state row (empty/refused/error) has one wide cell. */
function dataRows(): HTMLElement[] {
  return within(table())
    .getAllByRole("row")
    .slice(1)
    .filter((row) => within(row).queryAllByRole("cell").length > 1);
}

function cells(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent ?? "");
}

/** ROW IDENTITY, in render order. Every ordering and filtering assertion in this
 * file is made against this — never against a count. */
function documents(): string[] {
  return dataRows().map((row) => cells(row)[2] ?? "");
}

function countLine(): string {
  return document.querySelector(".ne-toolbar__count")?.textContent ?? "";
}

const objectUrl = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };

beforeEach(() => {
  compare.blocked = false;
  compare.acquire = null;
  compare.search = null;
  compare.withdrawn = 0;
  // The recent-searches store is module-level BY DESIGN (it must survive the
  // screen unmounting); tests must not inherit each other's visits.
  clearSearchHistory();
});

afterEach(() => {
  URL.createObjectURL = objectUrl.create;
  URL.revokeObjectURL = objectUrl.revoke;
});

describe("§6.1 — the query row", () => {
  it("is idle until someone searches, and says so in the spec's words", () => {
    renderSearch();
    expect(screen.getByText("Search documents across your connected sources.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Search results" })).not.toBeInTheDocument();
    // No toolbar before a run (§6.2).
    expect(document.querySelector(".ne-toolbar")).toBeNull();
  });

  it("carries the spec's placeholder", () => {
    renderSearch();
    expect(screen.getByPlaceholderText("Search connected sources")).toBeInTheDocument();
  });

  it("sends the chosen results size, and 10 by default", async () => {
    const sizes: number[] = [];
    server.use(
      http.post("*/api/v1/brain/search", async ({ request }) => {
        const body = (await request.json()) as { query: string; top_k: number };
        sizes.push(body.top_k);
        return HttpResponse.json(result());
      }),
    );
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    expect(sizes).toEqual([10]);

    await user.selectOptions(screen.getByLabelText("Results"), "50");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(sizes).toEqual([10, 50]));
  });

  it("issues exactly one governed request per submit", async () => {
    let requests = 0;
    server.use(
      http.post("*/api/v1/brain/search", () => {
        requests += 1;
        return HttpResponse.json(result());
      }),
    );
    renderSearch();
    await search();
    await screen.findByRole("table", { name: "Search results" });
    expect(requests).toBe(1);
  });

  it("shows the measured elapsed counter and a Cancel while a run is in flight", async () => {
    const held = heldResponse();
    renderSearch();
    await search();
    expect(await screen.findByText(/^Searching… \d+s$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    held.release();
    await screen.findByRole("table", { name: "Search results" });
    expect(screen.queryByText(/^Searching…/)).not.toBeInTheDocument();
  });
});

describe("§6.3 — the results table", () => {
  it("defaults to relevance: the server's own ranking, score descending", async () => {
    respondWith(result());
    renderSearch();
    await search();
    await screen.findByRole("table", { name: "Search results" });

    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
    expect(dataRows().map((row) => cells(row)[4])).toEqual(["0.91", "0.82", "0.75"]);
    expect(screen.getByRole("columnheader", { name: "Score" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("the # column is not sortable — it is the identity of the ranking", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    const rank = screen.getByRole("columnheader", { name: "#" });
    expect(within(rank).queryByRole("button")).toBeNull();
    // …and the rank a row carries survives a re-sort.
    await user.click(screen.getByRole("button", { name: "Document" }));
    expect(dataRows().map((row) => cells(row)[0])).toEqual(["2", "3", "1"]);
  });

  it("sorts by Source A–Z, and by Document, by row identity", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });

    await user.click(screen.getByRole("button", { name: "Source" }));
    // confluence, then sharepoint, then the uncited row (an absent value is not
    // the smallest value — it stays last).
    expect(documents()).toEqual(["doc-alpha", "doc-zulu", "doc-mike"]);
    expect(screen.getByRole("columnheader", { name: "Source" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    await user.click(screen.getByRole("button", { name: "Document" }));
    expect(documents()).toEqual(["doc-alpha", "doc-mike", "doc-zulu"]);
    await user.click(screen.getByRole("button", { name: "Document" }));
    expect(documents()).toEqual(["doc-zulu", "doc-mike", "doc-alpha"]);
  });

  it("sorting off relevance discloses that the sort is over the returned window", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    expect(screen.queryByText(/Sorted within/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(screen.getByText("Sorted within the 3 results returned.")).toBeInTheDocument();
  });

  it("renders the em dash for a chunk no citation matched — never an invented system", async () => {
    respondWith(result());
    renderSearch();
    await search();
    await screen.findByRole("table", { name: "Search results" });
    const uncited = dataRows()[2]!;
    expect(cells(uncited)[1]).toBe("—");
  });

  it("counts the rendered window, the measured run and the unconfirmed sources", async () => {
    respondWith(result({ provisional_source_count: 2 }));
    renderSearch();
    await search();
    await screen.findByRole("table", { name: "Search results" });
    expect(countLine()).toMatch(/^3 results · .+ · 2 unconfirmed$/);
  });
});

describe("§6.3 — the list's own identity ladder, hash last (owner defect 2026-08-04)", () => {
  const HASH = "cbd4eaec540b93f1a2b3c4d5e6f7089d9137b52";

  function identityChunk(
    id: string,
    doc: string,
    text: string,
    metadata: Record<string, unknown> = {},
  ): ScoredChunk {
    return { chunk: { chunk_id: id, document_id: doc, text, metadata }, score: 0.5 };
  }

  it("line one renders the highest served rung, and the hash ONLY when nothing better was served", async () => {
    respondWith(
      result({
        chunks: [
          identityChunk(
            "c-t",
            "doc-t",
            JSON.stringify({ content: "the body", title: "Master Services Agreement" }),
          ),
          identityChunk("c-h", "doc-h", "# 2025 Annual Engineering Summary\n\nThe year in review…"),
          identityChunk("c-s", "doc-s", "Plain prose, no envelope.", {
            source_id: "acme/docs/doc-0169",
          }),
          identityChunk("c-r", "doc-r", "Plain prose, no envelope.", { record_id: "REC-0169" }),
          // THE CONTROL (pins the render's row-10 behaviour): no title, no
          // heading, no source_id, no record_id — nothing was served, so the
          // hash is the honest headline, and ONLY here.
          identityChunk("c-hash", HASH, "Plain prose, no envelope, no metadata at all."),
        ],
        citations: [],
      }),
    );
    renderSearch();
    await search();
    await screen.findByRole("table", { name: "Search results" });

    expect(documents()).toEqual([
      "Master Services Agreement",
      "2025 Annual Engineering Summary",
      "acme/docs/doc-0169",
      "REC-0169",
      truncateMiddle(HASH),
    ]);

    const rows = dataRows();
    const identityCell = (index: number) => within(rows[index]!).getAllByRole("cell")[2]!;

    // Title and heading ARE the document's own name — plain text, none of the
    // fallback's dimmed/mono dressing.
    expect(identityCell(0).querySelector(".ne-mono")).toBeNull();
    expect(identityCell(1).querySelector(".ne-mono")).toBeNull();

    // Source-path and record-id are stand-ins for a name the document did not
    // serve — dimmed/mono with a tooltip naming what stands in, per the
    // render's `.ne-result__title .ne-mono` treatment.
    const sourcePathSpan = identityCell(2).querySelector(".ne-mono");
    expect(sourcePathSpan).not.toBeNull();
    expect(sourcePathSpan?.getAttribute("title")).toMatch(/resource path stands in/);

    const recordIdSpan = identityCell(3).querySelector(".ne-mono");
    expect(recordIdSpan).not.toBeNull();
    expect(recordIdSpan?.getAttribute("title")).toMatch(/record id stands in/);

    // THE CONTROL: the hash headlines here, and here only — dimmed/mono, its
    // tooltip naming exactly why (never silently indistinguishable from a
    // real name).
    const hashSpan = identityCell(4).querySelector(".ne-mono");
    expect(hashSpan).not.toBeNull();
    expect(hashSpan?.textContent).toBe(truncateMiddle(HASH));
    expect(hashSpan?.getAttribute("title")).toMatch(/No readable identity was served/);
  });
});

describe("§6.3 — term marks are a fact about the text, never the ranking", () => {
  it("marks only the verbatim query term in the excerpt, and leaves the score untouched", async () => {
    respondWith(result());
    renderSearch();
    // Only doc-zulu's chunk ("Zulu renewal terms and the notice period.")
    // contains "renewal" — doc-alpha and doc-mike do not.
    await search("renewal");
    await screen.findByRole("table", { name: "Search results" });

    const rows = dataRows();
    const excerptCell = (index: number) => within(rows[index]!).getAllByRole("cell")[3]!;

    const marks = excerptCell(0).querySelectorAll("mark.ne-hit");
    expect(marks).toHaveLength(1);
    expect(marks[0]?.textContent).toBe("renewal");

    // Rows with no verbatim occurrence of the query carry no mark at all.
    expect(excerptCell(1).querySelectorAll("mark.ne-hit")).toHaveLength(0);
    expect(excerptCell(2).querySelectorAll("mark.ne-hit")).toHaveLength(0);

    // The mark is a fact about the TEXT — the score stays the server's own
    // relevance claim, unrecomputed and unmarked.
    expect(cells(rows[0]!)[4]).toBe("0.91");
  });
});

describe("§6.2 — the filters narrow to named rows", () => {
  it("a source-system chip keeps exactly that system's rows", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });

    await user.click(screen.getByRole("button", { name: "confluence" }));
    expect(documents()).toEqual(["doc-alpha"]);
    expect(countLine()).toMatch(/^1 results · /);

    await user.click(screen.getByRole("button", { name: "sharepoint" }));
    expect(documents()).toEqual(["doc-zulu", "doc-alpha"]);
    // The uncited row named no system and is not swept into either.
    expect(documents()).not.toContain("doc-mike");
  });

  it("the search-within keeps exactly the rows whose served text matched", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });

    await user.type(screen.getByLabelText("Filter results…"), "onboarding");
    expect(documents()).toEqual(["doc-alpha"]);
  });

  it("filtering everything away says so, and offers the way back", async () => {
    respondWith(result());
    renderSearch();
    const user = await search("renewal");
    await screen.findByRole("table", { name: "Search results" });

    await user.type(screen.getByLabelText("Filter results…"), "zzzz");
    expect(screen.getByText("No matching results.")).toBeInTheDocument();
    // NOT the no-results copy: there ARE results, and this filter hid them.
    expect(screen.queryByText(/^No results for/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show all 3" }));
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
  });

  it("the sort select and the sortable headers are one sort state", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });

    await user.selectOptions(screen.getByLabelText("Sort"), "source");
    expect(documents()).toEqual(["doc-alpha", "doc-zulu", "doc-mike"]);

    await user.selectOptions(screen.getByLabelText("Sort"), "relevance");
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);

    // A header click onto a column the select does not name still leaves the
    // select naming the column that is actually in force.
    await user.click(screen.getByRole("button", { name: "Document" }));
    expect((screen.getByLabelText("Sort") as HTMLSelectElement).selectedOptions[0]?.text).toBe(
      "Document",
    );
  });

  it("a new run drops the previous run's filters", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "confluence" }));
    expect(documents()).toEqual(["doc-alpha"]);

    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(documents()).toHaveLength(3));
    expect(screen.getByRole("button", { name: "confluence" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});

describe("§6 — the state copy, verbatim", () => {
  it("no results names the query and the next move", async () => {
    respondWith(result({ chunks: [], citations: [] }));
    renderSearch();
    await search("kumquat");
    expect(
      await screen.findByText('No results for "kumquat". Try fewer or different terms.'),
    ).toBeInTheDocument();
  });

  it("a failed run says so and offers Retry — and NEVER claims a role boundary", async () => {
    server.use(
      http.post("*/api/v1/brain/search", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderSearch();
    await search();
    expect(await screen.findByText("Search failed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // THE BUG THIS ASSERTION EXISTS FOR: an unreachable or broken server is not
    // a refusal, and printing the refusal sentence over it tells the reader a
    // false thing about their own authorization.
    expect(screen.queryByText(/Your role doesn’t include/)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Contact your administrator");
  });

  it("a refusal renders the SERVER's refusal, and only for a refusal", async () => {
    server.use(
      http.post("*/api/v1/brain/search", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderSearch();
    await search();
    expect(
      await screen.findByText(
        /Your role doesn’t include search\. Contact your administrator for access\./,
      ),
    ).toBeInTheDocument();
    // The page still says which page refused, and offers no fabricated rows.
    expect(screen.getByRole("heading", { name: "Search", level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Search results" })).not.toBeInTheDocument();
    expect(screen.queryByText("Search failed.")).not.toBeInTheDocument();
  });

  it("a run past the deployment's deadline is a timeout, not a failure", async () => {
    respondWith(async () => {
      await delay(300);
      return result();
    });
    renderSearch({ search_deadline_ms: 20 });
    await search();
    expect(await screen.findByText("The search timed out.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("Search failed.")).not.toBeInTheDocument();
    expect(screen.queryByText("Search cancelled.")).not.toBeInTheDocument();
  });

  it("cancelling names the person who cancelled it, not a failure", async () => {
    const held = heldResponse();
    renderSearch();
    const user = await search();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Search cancelled.")).toBeInTheDocument();
    expect(screen.queryByText("Search failed.")).not.toBeInTheDocument();
    expect(screen.queryByText("The search timed out.")).not.toBeInTheDocument();
    held.release();
  });
});

describe("§6.5 — compare as a second identity", () => {
  /** The second identity received doc-alpha (same row, its own score) and
   * doc-mike, and was NOT sent doc-zulu. */
  function otherResult(): RetrievalResult {
    return {
      chunks: [CHUNKS[1]!, CHUNKS[2]!],
      citations: CITATIONS,
      graph_paths: [],
      provisional_source_count: 0,
    };
  }

  async function startComparison() {
    compare.acquire = async () => ({ label: "bob@example.com" });
    compare.search = async () => otherResult();
    // You were sent doc-zulu and doc-alpha; they were sent doc-alpha and
    // doc-mike. So the union carries a row only you received AND a row only they
    // received — the two cases the comparison exists to show.
    respondWith(result({ chunks: [CHUNKS[0]!, CHUNKS[1]!] }));
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));
    await screen.findByRole("columnheader", { name: "bob@example.com" });
    return user;
  }

  it("renders ONE table over the union, with each identity's own column", async () => {
    await startComparison();

    expect(
      within(table())
        .getAllByRole("columnheader")
        .map((header) => (header.textContent ?? "").replace(/[▲▼]/g, "")),
    ).toEqual(["#", "Source", "Document", "You", "bob@example.com"]);

    // Union, by row identity: your rows in your order, then theirs alone.
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
    expect(dataRows().map((row) => [cells(row)[3], cells(row)[4]])).toEqual([
      ["Returned (0.91)", "Not returned"],
      ["Returned (0.82)", "Returned (0.82)"],
      ["Not returned", "Returned (0.75)"],
    ]);
    // The second-only row was rank 2 in key B's response. It is not renumbered
    // to 3 merely because the browser appended it to a union.
    expect(dataRows().map((row) => cells(row)[0])).toEqual(["1", "2", "2"]);
  });

  it("labels each response's unconfirmed count in a comparison", async () => {
    compare.acquire = async () => ({ label: "bob@example.com" });
    const other = otherResult();
    compare.search = async () => ({ ...other, provisional_source_count: 1 });
    respondWith(result({ chunks: [CHUNKS[0]!, CHUNKS[1]!], provisional_source_count: 2 }));
    renderSearch();
    const user = await search();
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));

    await screen.findByRole("columnheader", { name: "bob@example.com" });
    expect(countLine()).toMatch(
      /^3 results · .+ · You: 2 unconfirmed · bob@example\.com: 1 unconfirmed$/,
    );
  });

  it("carries the spec's caption, verbatim", async () => {
    await startComparison();
    expect(
      screen.getByText("Each column is that identity’s own result set, filtered by the server."),
    ).toBeInTheDocument();
  });

  it("ending the comparison returns your own result set and withdraws the key", async () => {
    const user = await startComparison();
    await user.click(screen.getByRole("button", { name: "End comparison" }));
    await waitFor(() => expect(documents()).toEqual(["doc-zulu", "doc-alpha"]));
    expect(screen.queryByRole("columnheader", { name: "bob@example.com" })).not.toBeInTheDocument();
    expect(compare.withdrawn).toBe(1);
  });

  it("an unfinished sign-in says exactly that, and offers a retry", async () => {
    compare.acquire = async () => {
      throw new Error("popup closed");
    };
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));

    expect(await screen.findByText("Sign-in wasn’t completed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    // Your own results are untouched — a failed second sign-in is not a failed
    // search, and one column rendered as a comparison would prove nothing.
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
  });

  it("a blocked sign-in window fails in a millisecond, with the same sentence", async () => {
    compare.blocked = true;
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));
    expect(await screen.findByText("Sign-in wasn’t completed.")).toBeInTheDocument();
  });

  it("a second-identity refusal names THEIR role and preserves your served rows", async () => {
    compare.acquire = async () => ({ label: "bob@example.com" });
    compare.search = async () => {
      throw new ApiError("forbidden", 403, "search is not assigned to this role");
    };
    respondWith(result({ chunks: [CHUNKS[0]!, CHUNKS[1]!] }));
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));

    expect(
      await screen.findByText(
        "bob@example.com’s role doesn’t include search. Their result set was not returned.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("search is not assigned to this role")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn’t include/)).not.toBeInTheDocument();
    expect(documents()).toEqual(["doc-zulu", "doc-alpha"]);
    expect(screen.queryByRole("columnheader", { name: "bob@example.com" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End comparison" })).toBeInTheDocument();
  });

  it("a primary refusal during comparison keeps the existing Your-role full refusal", async () => {
    compare.acquire = async () => ({ label: "bob@example.com" });
    compare.search = async () => otherResult();
    let primaryCalls = 0;
    server.use(
      http.post("*/api/v1/brain/search", () => {
        primaryCalls += 1;
        return primaryCalls === 1
          ? HttpResponse.json(result())
          : HttpResponse.json({ detail: "forbidden" }, { status: 403 });
      }),
    );
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));

    expect(
      await screen.findByText(
        /Your role doesn’t include search\. Contact your administrator for access\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Search results" })).not.toBeInTheDocument();
  });

  it("the second identity's non-refusal failure is a failed run, not half a comparison", async () => {
    compare.acquire = async () => ({ label: "bob@example.com" });
    compare.search = async () => {
      throw new Error("second key refused");
    };
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));
    expect(await screen.findByText("Search failed.")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "bob@example.com" })).not.toBeInTheDocument();
  });
});

describe("§6.4 — the detail pane", () => {
  async function openFirstRow() {
    respondWith(result({ graph_paths: GRAPH_PATHS, provisional_source_count: 1 }));
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(dataRows()[0]!);
    return { user, pane: await screen.findByRole("dialog") };
  }

  it("shows the full chunk text, its metadata and the citation ref", async () => {
    const { pane } = await openFirstRow();
    // The run's own query ("renewal") verbatim-marks a word of this sentence
    // (CR-B7 term marks), so the full text is asserted by its container's
    // RECURSIVE text — getByText's default node-text match only concatenates
    // an element's OWN text nodes, not a <mark>-split descendant's.
    expect(pane.querySelector(".ne-search__full")?.textContent).toBe(
      "Zulu renewal terms and the notice period.",
    );
    expect(within(pane).getByText("renewals")).toBeInTheDocument();
    expect(within(pane).getByText("ref/doc-zulu")).toBeInTheDocument();
    expect(within(pane).getByText("0.91")).toBeInTheDocument();
  });

  it("offers each link only when the server resolved a target for it", async () => {
    const { user, pane } = await openFirstRow();
    expect(within(pane).getByRole("link", { name: "Open in source system" })).toHaveAttribute(
      "href",
      "https://sharepoint.example.com/doc-zulu",
    );
    // ENCODED: the ref is a source system's own pointer, and Graph reads it
    // back with URLSearchParams — so what lands there is the ref the server
    // sent, whatever characters it holds.
    expect(within(pane).getByRole("link", { name: "View in Graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?mode=graph&depth=1&citation=ref%2Fdoc-zulu",
    );

    // doc-alpha's citation carries no deep link, so there is no source-system
    // button — never a URL this console assembled. And NO traversal in this
    // response evidences ref/doc-alpha, so there is no Graph link either:
    // offered looser it lands on an empty graph (the owner's dead "View in
    // Graph" click, 2026-08-04) — Ask's own gate, applied here.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(dataRows()[1]!);
    const second = await screen.findByRole("dialog");
    expect(within(second).queryByRole("link", { name: "Open in source system" })).toBeNull();
    expect(within(second).queryByRole("link", { name: "View in Graph" })).toBeNull();
  });

  it("carries a ref with query-string characters to Graph intact", async () => {
    // A real source_ref is a source system's own pointer. Pasted raw into the
    // URL, the '&' starts a second parameter, the '#' starts a fragment and the
    // '+' becomes a space — Graph would then anchor on a ref the server never
    // sent, and show a record as if it were the cited one.
    const ref = "sp/site?a=1&b=2#frag+x";
    respondWith(
      result({
        citations: [
          { chunk_id: "c-1", document_id: "doc-zulu", source_system: "sp", source_ref: ref },
        ],
        // The link renders only for a ref the served traversal evidences, so
        // the fixture's traversal must cite this ref the way the server would.
        graph_paths: [
          {
            hops: [
              {
                subject: {
                  entity_class: "Company",
                  entity_key: "co:acme",
                  display: "Acme Holdings",
                  assurance: "source",
                  source_ref: ref,
                },
                edge_kind: "supplies",
                obj: {
                  entity_class: "Company",
                  entity_key: "co:globex",
                  display: "Globex",
                  assurance: "source",
                  source_ref: "ref/other",
                },
                source_system: "sp",
                source_ref: ref,
                assurance: "source",
              },
            ],
          },
        ],
      }),
    );
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(dataRows()[0]!);
    const pane = await screen.findByRole("dialog");

    const href = within(pane).getByRole("link", { name: "View in Graph" }).getAttribute("href")!;
    const url = new URL(href, "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/graph");
    expect(url.searchParams.get("mode")).toBe("graph");
    expect(url.searchParams.get("citation")).toBe(ref);
  });

  it("lists the result set's related entities, flagging the unconfirmed one", async () => {
    const { pane } = await openFirstRow();
    const related = within(pane).getByRole("list", { name: "Related entities" });
    expect(within(related).getByRole("link", { name: "Acme Holdings" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=co%3Aacme&mode=record&depth=1",
    );
    expect(within(related).getByRole("link", { name: "Globex" })).toBeInTheDocument();
    // Exactly one chip is unconfirmed — the one the server did not vouch for.
    expect(within(related).getAllByText("UNCONFIRMED")).toHaveLength(1);
  });

  it("has no related-entity block when the response carried no graph paths", async () => {
    respondWith(result());
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(dataRows()[0]!);
    const pane = await screen.findByRole("dialog");
    expect(within(pane).queryByRole("list", { name: "Related entities" })).toBeNull();
  });

  it("closes on Escape without leaving the list", async () => {
    const { user } = await openFirstRow();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
  });

  it("closes on Escape even when focus has left the pane — the stuck-pane defect", async () => {
    // The pane is aria-modal="false": a click on the page behind it parks
    // focus outside, and the owner then had NO way out (2026-08-04). Esc is a
    // document-level promise, not a pane-focus one.
    const { user, pane } = await openFirstRow();
    (document.activeElement as HTMLElement | null)?.blur();
    expect(pane.contains(document.activeElement)).toBe(false);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
  });

  it("closes from its Close control", async () => {
    const { user, pane } = await openFirstRow();
    await user.click(within(pane).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("§6.4 — the pane headline is the served human identity, hash last", () => {
  async function openPaneFor(chunks: ScoredChunk[], citations: Citation[] = CITATIONS) {
    respondWith(result({ chunks, citations }));
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(dataRows()[0]!);
    return await screen.findByRole("dialog");
  }

  const HASH = "cbd4eaec540b509ce7624647adac0516250aaaaa";

  it("an envelope title headlines the pane, and the hash stays in Document", async () => {
    const text = JSON.stringify({
      content: "# 2024 Renewal Agreement\n\nThis agreement renews…",
      title: "2024 Renewal Agreement",
    });
    const pane = await openPaneFor([chunk("c-1", HASH, 0.91, text)], []);
    expect(
      within(pane).getByRole("heading", { level: 2, name: "2024 Renewal Agreement" }),
    ).toBeInTheDocument();
    // The audit chain records the hash, so the pane keeps serving it — as the
    // Document field, not the headline.
    expect(within(pane).getByText(HASH)).toBeInTheDocument();
  });

  it("with no envelope title, the body's own leading heading — the owner's case", async () => {
    const pane = await openPaneFor(
      [
        {
          chunk: {
            chunk_id: "c-1",
            document_id: HASH,
            text: "# 2025 Annual Engineering Summary\n\nThe year in review…",
            metadata: { source_id: "acme/docs/doc-0169", record_id: "doc-0169" },
          },
          score: 0.91,
        },
      ],
      [],
    );
    expect(
      within(pane).getByRole("heading", { level: 2, name: "2025 Annual Engineering Summary" }),
    ).toBeInTheDocument();
    expect(within(pane).getByText(HASH)).toBeInTheDocument();
  });

  it("with no served title at all, falls back to the id it has — NEVER fabricates", async () => {
    // Plain prose, no source_id/record_id: the honest headline is the
    // document id itself, unchanged.
    const pane = await openPaneFor([chunk("c-1", "doc-zulu", 0.91, "Plain prose body.")]);
    expect(within(pane).getByRole("heading", { level: 2, name: "doc-zulu" })).toBeInTheDocument();
  });
});

describe("§6.4 — provenance renders as fields, not a blob", () => {
  it("lays the provenance stamp out one row per field, every value kept", async () => {
    const provenance = {
      provenance_class: "ingested",
      source: "connector:acme-fixture:doc-0169",
      actor: "connect-sync",
      written_at: "2026-08-04T19:42:18.911317+00:00",
      write_id: "073663ab",
      shape: "ne.connect.record@1",
    };
    respondWith(
      result({
        chunks: [
          {
            chunk: {
              chunk_id: "c-1",
              document_id: "doc-zulu",
              text: "Body.",
              metadata: { provenance },
            },
            score: 0.91,
          },
        ],
        citations: [],
      }),
    );
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(dataRows()[0]!);
    const pane = await screen.findByRole("dialog");

    // Every field, as its own labelled row — keys as terms, values verbatim.
    for (const [key, value] of Object.entries(provenance)) {
      expect(within(pane).getByText(key)).toBeInTheDocument();
      expect(within(pane).getByText(value)).toBeInTheDocument();
    }
    // And NOT as one serialized string: no JSON punctuation composite.
    expect(pane.textContent).not.toContain('{"provenance_class"');
  });
});

describe("recent searches — replay is a labelled capture, re-run is the server now", () => {
  /** A cache open must revalidate every held row through the current
   * principal-scoped retrieve seam. Tests override this per case when a row is
   * revoked or the validator is unavailable. */
  beforeEach(() => {
    server.use(
      http.post("*/api/v1/brain/retrieve", async ({ request }) => {
        const { chunk_ids } = (await request.json()) as { chunk_ids: string[] };
        return HttpResponse.json(
          result({ chunks: CHUNKS.filter((held) => chunk_ids.includes(held.chunk.chunk_id)) }),
        );
      }),
    );
  });

  /** History binds to the OIDC subject the tab is signed in as — the stub's
   * default is alice@example.com; a test that needs "someone else at the same
   * screen" overrides the subject. */
  function renderAs(sub = "alice@example.com") {
    return render(
      <MemoryRouter initialEntries={["/search"]}>
        <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
          <AuthStub
            value={
              {
                user: { access_token: TEST_ACCESS_TOKEN, profile: { sub } },
              } as unknown as Partial<AuthContextProps>
            }
          >
            <SearchPage />
          </AuthStub>
        </RuntimeConfigContext.Provider>
      </MemoryRouter>,
    );
  }
  const renderWithPrincipal = () => renderAs();

  /** A server whose answer the test can CHANGE between runs — the revocation
   * seam: first serve all three rows, then withhold doc-zulu. */
  function mutableSearch() {
    const state = { body: result(), requests: 0 };
    server.use(
      http.post("*/api/v1/brain/search", () => {
        state.requests += 1;
        return HttpResponse.json(state.body);
      }),
    );
    return state;
  }

  async function runOneSearch() {
    const view = renderWithPrincipal();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await waitFor(() => expect(searchHistoryFor("alice@example.com")).toHaveLength(1));
    return { view, user };
  }

  function historySection() {
    return screen.findByRole("region", { name: "Recent searches" });
  }

  it(
    "lists this visit's runs on the empty screen and replays the capture, labelled",
    { timeout: 15_000 },
    async () => {
      mutableSearch();
      const { view } = await runOneSearch();
      view.unmount();

      renderWithPrincipal();
      const section = await historySection();
      const open = within(section).getByRole("button", {
        name: 'Show captured results for "renewal"',
      });
      expect(section).toHaveTextContent("3 results");
      expect(section).toHaveTextContent("Held in this tab only — nothing is persisted.");

      const user = userEvent.setup();
      await user.click(open);
      // The capture, whole: the same three rows, under a banner that names the
      // capture time — a dated verdict, never presented as current truth.
      expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
      expect(
        screen.getByText(
          /^Cached results, captured .+ Current access re-validated; Search was not re-run\.$/,
        ),
      ).toBeInTheDocument();
      // And the query row is restored, so Search re-asks this same question.
      expect(screen.getByLabelText("Search connected sources")).toHaveValue("renewal");
    },
  );

  it("opens the cached capture when the API principal and group set are unchanged", async () => {
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json({ ...PRINCIPAL, groups: ["engineering", "all-employees"] }),
      ),
    );
    const live = mutableSearch();
    const { view } = await runOneSearch();
    view.unmount();

    // Order is not permission state. The same set in another server order
    // must keep the fast cached path.
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json({ ...PRINCIPAL, groups: ["all-employees", "engineering"] }),
      ),
    );
    renderWithPrincipal();
    const requestsBefore = live.requests;
    const user = userEvent.setup();
    await user.click(
      within(await historySection()).getByRole("button", {
        name: 'Show captured results for "renewal"',
      }),
    );

    expect(
      await screen.findByText(
        /^Cached results, captured .+ Current access re-validated; Search was not re-run\.$/,
      ),
    ).toBeInTheDocument();
    expect(live.requests).toBe(requestsBefore);
  });

  it("evicts and replaces captures when groups change under the same subject", async () => {
    const live = mutableSearch();
    const { view } = await runOneSearch();
    view.unmount();
    live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });
    server.use(
      http.get("*/api/v1/me", () => HttpResponse.json({ ...PRINCIPAL, groups: ["sales"] })),
    );

    renderWithPrincipal();
    const user = userEvent.setup();
    await user.click(
      within(await historySection()).getByRole("button", {
        name: 'Show captured results for "renewal"',
      }),
    );

    expect(
      await screen.findByText(
        "Fetched fresh results — the saved rows could not all be re-validated for your current access, so the capture was discarded.",
      ),
    ).toBeInTheDocument();
    expect(documents()).toEqual(["doc-alpha", "doc-mike"]);
    expect(live.requests).toBe(2);
    await waitFor(() => expect(searchHistoryFor("alice@example.com")).toHaveLength(1));
  });

  it("runs live when the API principal mapping changes under the same OIDC subject", async () => {
    const live = mutableSearch();
    const { view } = await runOneSearch();
    view.unmount();
    live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json({ ...PRINCIPAL, id: "alice-remapped@example.com" }),
      ),
    );

    renderWithPrincipal();
    const user = userEvent.setup();
    await user.click(
      within(await historySection()).getByRole("button", {
        name: 'Show captured results for "renewal"',
      }),
    );

    await screen.findByText(/saved rows could not all be re-validated for your current access/);
    expect(documents()).toEqual(["doc-alpha", "doc-mike"]);
    expect(live.requests).toBe(2);
  });

  it("cannot resurrect a row revoked by ACL without any groups change", async () => {
    const live = mutableSearch();
    const { view } = await runOneSearch();
    view.unmount();
    live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });
    server.use(
      http.post("*/api/v1/brain/retrieve", async ({ request }) => {
        const { chunk_ids } = (await request.json()) as { chunk_ids: string[] };
        return HttpResponse.json(
          result({
            chunks: CHUNKS.filter(
              (held) => held.chunk.chunk_id !== "c-1" && chunk_ids.includes(held.chunk.chunk_id),
            ),
          }),
        );
      }),
    );

    renderWithPrincipal();
    const user = userEvent.setup();
    await user.click(
      within(await historySection()).getByRole("button", {
        name: 'Show captured results for "renewal"',
      }),
    );

    await screen.findByText(/saved rows could not all be re-validated for your current access/);
    expect(documents()).toEqual(["doc-alpha", "doc-mike"]);
    expect(documents()).not.toContain("doc-zulu");
    expect(live.requests).toBe(2);
  });

  it("fails to a live search without inventing an access-change claim when validation fails", async () => {
    const live = mutableSearch();
    const { view } = await runOneSearch();
    view.unmount();
    live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });
    server.use(http.post("*/api/v1/brain/retrieve", () => new HttpResponse(null, { status: 500 })));

    renderWithPrincipal();
    const user = userEvent.setup();
    await user.click(
      within(await historySection()).getByRole("button", {
        name: 'Show captured results for "renewal"',
      }),
    );

    await waitFor(() => expect(live.requests).toBe(2));
    expect(documents()).toEqual(["doc-alpha", "doc-mike"]);
    expect(screen.queryByText(/saved rows could not all be re-validated/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Cached results/)).not.toBeInTheDocument();
  });

  it("does not retain a live result when its capture context cannot be verified", async () => {
    mutableSearch();
    server.use(http.get("*/api/v1/me", () => new HttpResponse(null, { status: 500 })));
    const view = renderWithPrincipal();
    await search();
    await screen.findByRole("table", { name: "Search results" });
    await waitFor(() => expect(searchHistoryFor("alice@example.com")).toEqual([]));
    view.unmount();

    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL)));
    renderWithPrincipal();
    await screen.findByText("Search documents across your connected sources.");
    expect(screen.queryByRole("region", { name: "Recent searches" })).toBeNull();
  });

  it(
    "Re-run asks the server NOW — a row revoked since capture cannot survive it",
    { timeout: 15_000 },
    async () => {
      const live = mutableSearch();
      const { view } = await runOneSearch();
      view.unmount();

      // Between capture and replay, access to doc-zulu is revoked: the server
      // would no longer serve it.
      live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });

      renderWithPrincipal();
      const user = userEvent.setup();
      await user.click(
        within(await historySection()).getByRole("button", {
          name: 'Show captured results for "renewal"',
        }),
      );
      // The replay may show the capture — AS a capture (banner above). C7's line
      // is crossed only if these rows could pass as the server's current answer.
      expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
      const requestsBefore = live.requests;

      await user.click(screen.getByRole("button", { name: "Re-run" }));
      await waitFor(() => expect(documents()).toEqual(["doc-alpha", "doc-mike"]));
      // A real request, and the banner is gone: these rows are the server's own
      // present-tense verdict, doc-zulu no longer among them.
      expect(live.requests).toBe(requestsBefore + 1);
      expect(screen.queryByText(/Cached results/)).not.toBeInTheDocument();
    },
  );

  it(
    "the history row's own Re-run also queries live, never the capture",
    { timeout: 15_000 },
    async () => {
      const live = mutableSearch();
      const { view } = await runOneSearch();
      view.unmount();
      live.body = result({ chunks: [CHUNKS[1]!, CHUNKS[2]!] });

      renderWithPrincipal();
      const user = userEvent.setup();
      await user.click(
        within(await historySection()).getByRole("button", { name: 'Re-run "renewal"' }),
      );
      await waitFor(() => expect(documents()).toEqual(["doc-alpha", "doc-mike"]));
      expect(screen.queryByText(/Cached results/)).not.toBeInTheDocument();
    },
  );

  it(
    "history NEVER crosses principals: a new identity evicts the last one's entries",
    { timeout: 15_000 },
    async () => {
      mutableSearch();
      const { view } = await runOneSearch();
      view.unmount();
      expect(searchHistoryFor("alice@example.com")).toHaveLength(1);

      // The same tab, now signed in as someone else.
      renderAs("bob@example.com");
      await screen.findByText("Search documents across your connected sources.");
      // The screen under bob both shows nothing AND evicted alice's rows from
      // memory — proven by the store now being empty for alice herself.
      expect(screen.queryByRole("region", { name: "Recent searches" })).toBeNull();
      expect(searchHistoryFor("alice@example.com")).toEqual([]);
    },
  );

  it(
    "a comparison NEVER leaks the other identity's rows into history",
    { timeout: 15_000 },
    async () => {
      // Your server answer holds zulu+alpha; the second identity was ALSO sent
      // doc-mike, which you were not. doc-mike's presence in a replay is the
      // leak this test exists to catch: those rows were served under a key that
      // is withdrawn when the comparison ends.
      const live = mutableSearch();
      live.body = result({ chunks: [CHUNKS[0]!, CHUNKS[1]!] });
      const { view, user } = await runOneSearch();

      compare.acquire = async () => ({ label: "bob@example.com" });
      compare.search = async () => result({ chunks: [CHUNKS[2]!] });
      await user.click(screen.getByRole("button", { name: "Compare as another user…" }));
      await screen.findByRole("columnheader", { name: "bob@example.com" });
      // The union on screen really carries the other identity's row…
      expect(documents()).toEqual(["doc-zulu", "doc-alpha", "doc-mike"]);
      view.unmount();

      renderWithPrincipal();
      const section = await historySection();
      // …and the history holds ONE entry: the single-identity run, whose replay
      // is YOUR rows only — no doc-mike, no second column.
      expect(within(section).getAllByRole("listitem")).toHaveLength(1);
      const user2 = userEvent.setup();
      await user2.click(
        within(section).getByRole("button", { name: 'Show captured results for "renewal"' }),
      );
      expect(documents()).toEqual(["doc-zulu", "doc-alpha"]);
      expect(screen.queryByRole("columnheader", { name: "bob@example.com" })).toBeNull();
    },
  );

  it("an empty visit offers no history shell", { timeout: 15_000 }, async () => {
    renderWithPrincipal();
    expect(
      await screen.findByText("Search documents across your connected sources."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Recent searches" })).toBeNull();
  });
});

describe("§6.2 — export", () => {
  it("writes the rendered window, the spec's columns, and names the filters", async () => {
    const blobs: Blob[] = [];
    // jsdom implements no object-URL store; the two methods are replaced (and
    // restored in afterEach) rather than the whole URL global, which msw and the
    // router both still need.
    URL.createObjectURL = ((blob: Blob) => {
      blobs.push(blob);
      return "blob:stub";
    }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    respondWith(result());
    renderSearch();
    const user = await search("renewal");
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "confluence" }));
    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    const text = await blobs[0]!.text();
    const [preamble, header, ...rows] = text.trim().split("\r\n");
    expect(preamble).toContain("Query: renewal");
    expect(preamble).toContain("Source: confluence");
    expect(header).toBe("rank,source_system,document_id,chunk_id,score,excerpt");
    // Exactly the filtered window, by row identity.
    expect(rows).toHaveLength(1);
    expect(rows[0]).toContain("doc-alpha");
    expect(rows[0]).toContain("c-2");
    expect(text).not.toContain("doc-zulu");
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the server's retrieval status, on screen
// ===========================================================================

describe("§6 authority-limited pages", () => {
  it("says an empty exhausted page is authority-limited and does not say nothing matched", async () => {
    respondWith(
      result({
        chunks: [],
        citations: [],
        authorized_retrieval: {
          status: "exhausted",
          scanned_count: 500,
          returned_count: 0,
          scan_limit: 500,
        },
      }),
    );
    renderSearch();
    await search();
    expect(await screen.findByText("Authority-limited — results may be incomplete")).toBeVisible();
    expect(
      screen.getByText("No authorized results were returned before the scan bound."),
    ).toBeVisible();
    expect(
      screen.queryByText('No results for "renewal". Try fewer or different terms.'),
    ).toBeNull();
  });

  it("still says nothing matched for an empty page the server called complete", async () => {
    respondWith(
      result({
        chunks: [],
        citations: [],
        authorized_retrieval: {
          status: "complete",
          scanned_count: 40,
          returned_count: 0,
          scan_limit: 500,
        },
      }),
    );
    renderSearch();
    await search();
    expect(
      await screen.findByText('No results for "renewal". Try fewer or different terms.'),
    ).toBeVisible();
    expect(screen.queryByText("Authority-limited — results may be incomplete")).toBeNull();
  });

  it("renders the server's own three bounds beside a partial page that returned rows", async () => {
    respondWith(
      result({
        authorized_retrieval: {
          status: "partial",
          scanned_count: 400,
          returned_count: 3,
          scan_limit: 400,
        },
      }),
    );
    renderSearch();
    await search();
    expect(await screen.findByText("Authority-limited — results may be incomplete")).toBeVisible();
    expect(screen.getByText("400 scanned · 3 authorized · 400 scan bound")).toBeVisible();
    // The rows the server DID authorize are still the rows on screen: an
    // incomplete page is not an empty one.
    expect(dataRows()).toHaveLength(3);
  });

  it("keeps each compared identity's status independent instead of reporting one for both", async () => {
    compare.acquire = async () => ({ label: "bob@example.com" });
    // Their scan hit the bound; yours did not. One status could only be wrong
    // for one of them.
    compare.search = async () => ({
      chunks: [CHUNKS[1]!],
      citations: CITATIONS,
      graph_paths: [],
      provisional_source_count: 0,
      authorized_retrieval: {
        status: "partial" as const,
        scanned_count: 900,
        returned_count: 1,
        scan_limit: 900,
      },
    });
    respondWith(
      result({
        chunks: [CHUNKS[0]!],
        authorized_retrieval: {
          status: "complete",
          scanned_count: 1,
          returned_count: 1,
          scan_limit: 900,
        },
      }),
    );
    renderSearch();
    const user = await search();
    await screen.findByRole("table", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Compare as another user…" }));
    await screen.findByRole("columnheader", { name: "bob@example.com" });

    expect(
      await screen.findByText("bob@example.com — Authority-limited — results may be incomplete"),
    ).toBeVisible();
    expect(screen.getByText("900 scanned · 1 authorized · 900 scan bound")).toBeVisible();
    // Yours was complete and is not labelled as anything else.
    expect(screen.queryByText("Authority-limited — results may be incomplete")).toBeNull();
  });

  it("fails closed on a response that stated no completeness, without claiming authority", async () => {
    respondWith(result({ authorized_retrieval: undefined }));
    renderSearch();
    await search();
    expect(
      await screen.findByText("Completeness unstated — this page is not proven complete"),
    ).toBeVisible();
    // It is NOT rendered as the server's authority decision, because the server
    // reported none — and no bounds are invented to sit beneath it.
    expect(screen.queryByText("Authority-limited — results may be incomplete")).toBeNull();
    expect(screen.queryByText(/scan bound/)).toBeNull();
    // The rows the server did send are still the rows on screen.
    expect(dataRows()).toHaveLength(3);
  });

  it("refuses to call an empty unstated page a no-result", async () => {
    respondWith(result({ chunks: [], citations: [], authorized_retrieval: undefined }));
    renderSearch();
    await search();
    expect(
      await screen.findByText(
        "This page returned nothing and the server did not report whether more exists.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText('No results for "renewal". Try fewer or different terms.'),
    ).toBeNull();
  });

  it("shows no authority notice for a complete page that returned rows", async () => {
    respondWith(
      result({
        authorized_retrieval: {
          status: "complete",
          scanned_count: 3,
          returned_count: 3,
          scan_limit: 400,
        },
      }),
    );
    renderSearch();
    await search();
    await waitFor(() => expect(dataRows()).toHaveLength(3));
    expect(screen.queryByText("Authority-limited — results may be incomplete")).toBeNull();
  });
});
