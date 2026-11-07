// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — the source pane. The citation surviving challenge is the product.
//
// The load-bearing distinction is REFUSED versus UNREACHABLE. Telling a reader
// "you don't have access to this source" about a read that merely failed is a
// false statement about authorization, and it is the exact defect this pane is
// written to avoid — so both branches are held, separately, against the two
// answers a server can give.
//
// The ACL branch is also held: a chunk the server withholds renders the refusal
// and NEVER any content, which is what makes a dangling citation unusable as a
// way around a permission.

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Citation, RetrievalResult } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { CHAT_CITATION, SECRET_CITATION, SENTINEL } from "../test/mocks/fixtures";
import { SourcePane } from "./SourcePane";
import { resolvesToEntity } from "./citationEntity";

function open(citation: Citation) {
  return render(
    <MemoryRouter>
      <AuthStub>
        <BoundSourcePane citation={citation} />
      </AuthStub>
    </MemoryRouter>,
  );
}

function BoundSourcePane({ citation }: { citation: Citation }) {
  const client = useApiClient();
  return (
    <SourcePane
      citation={citation}
      client={client}
      principalId="alice@example.com"
      onClose={() => {}}
    />
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("SourcePane", () => {
  it("posts exactly the one chunk id and renders the record", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/brain/retrieve", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          chunks: [
            {
              chunk: {
                chunk_id: "chunk-alpha",
                document_id: "doc-alpha",
                text: "Alpha handbook contents.",
                metadata: { classification: "internal" },
              },
              score: 1,
            },
          ],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
        } satisfies RetrievalResult);
      }),
    );

    open(CHAT_CITATION);
    expect(await screen.findByText("Alpha handbook contents.")).toBeInTheDocument();
    expect(body).toEqual({ chunk_ids: ["chunk-alpha"] });
    // Metadata as a key/value list, exactly as the server sent it.
    expect(screen.getByText("classification")).toBeInTheDocument();
    expect(screen.getByText("internal")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in source system" })).toHaveAttribute(
      "href",
      CHAT_CITATION.source_url,
    );
  });

  it("titles the pane with the served human identity and lays provenance out as fields", async () => {
    // The same two owner defects the Search pane fixed (2026-08-04), held on
    // this pane too: the headline is the record's own heading — the hash never
    // headlines while anything better was served — and the provenance stamp is
    // labelled rows, not one JSON string.
    const provenance = {
      provenance_class: "ingested",
      source: "connector:acme-fixture:doc-0169",
      actor: "connect-sync",
    };
    server.use(
      http.post("*/api/v1/brain/retrieve", () =>
        HttpResponse.json({
          chunks: [
            {
              chunk: {
                chunk_id: "chunk-alpha",
                document_id: "cbd4eaec540b509ce7624647adac0516",
                text: "# 2025 Annual Engineering Summary\n\nThe year in review…",
                metadata: { record_id: "doc-0169", provenance },
              },
              score: 1,
            },
          ],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
        } satisfies RetrievalResult),
      ),
    );

    open(CHAT_CITATION);
    expect(
      await screen.findByRole("heading", { level: 2, name: "2025 Annual Engineering Summary" }),
    ).toBeInTheDocument();
    for (const [key, value] of Object.entries(provenance)) {
      expect(screen.getByText(key)).toBeInTheDocument();
      expect(screen.getByText(value)).toBeInTheDocument();
    }
    expect(document.body.textContent).not.toContain('{"provenance_class"');
  });

  it("renders the refusal and NO content for a chunk the server withholds", async () => {
    open(SECRET_CITATION);
    expect(await screen.findByText("You don't have access to this source.")).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SENTINEL))).not.toBeInTheDocument();
  });

  it("calls a read that could not be made an error — never a role boundary", async () => {
    server.use(http.post("*/api/v1/brain/retrieve", () => new HttpResponse(null, { status: 503 })));
    open(CHAT_CITATION);
    expect(await screen.findByText("Couldn't load the source.")).toBeInTheDocument();
    expect(screen.queryByText("You don't have access to this source.")).not.toBeInTheDocument();
  });

  it("retries the read that failed", async () => {
    let calls = 0;
    server.use(
      http.post("*/api/v1/brain/retrieve", () => {
        calls += 1;
        if (calls === 1) return new HttpResponse(null, { status: 503 });
        return HttpResponse.json({
          chunks: [
            {
              chunk: {
                chunk_id: "chunk-alpha",
                document_id: "doc-alpha",
                text: "Second time lucky.",
                metadata: {},
              },
              score: 1,
            },
          ],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
        } satisfies RetrievalResult);
      }),
    );
    open(CHAT_CITATION);
    await screen.findByText("Couldn't load the source.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Second time lucky.")).toBeInTheDocument();
  });

  it("offers the canonical Graph link only when the server's own graph resolves this record", async () => {
    const withPaths = (graph_paths: RetrievalResult["graph_paths"]) =>
      server.use(
        http.post("*/api/v1/brain/retrieve", () =>
          HttpResponse.json({
            chunks: [
              {
                chunk: {
                  chunk_id: "chunk-alpha",
                  document_id: "doc-alpha",
                  text: "Alpha handbook contents.",
                  metadata: {},
                },
                score: 1,
              },
            ],
            citations: [],
            graph_paths,
            provisional_source_count: 0,
          } satisfies RetrievalResult),
        ),
      );

    // The control: the graph holds no entity for this record, so no link.
    withPaths([]);
    const first = open(CHAT_CITATION);
    await screen.findByText("Alpha handbook contents.");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "View in Graph" })).not.toBeInTheDocument(),
    );
    first.unmount();

    // The same read, with this citation's own reference on a traversed relation.
    withPaths([
      {
        hops: [
          {
            subject: {
              entity_class: "Contract",
              entity_key: "c-1",
              display: "Contract 1",
              assurance: "source",
              source_ref: CHAT_CITATION.source_ref,
            },
            edge_kind: "renews",
            obj: {
              entity_class: "Party",
              entity_key: "p-1",
              display: "Party 1",
              assurance: "source",
              source_ref: "ref/other",
            },
            source_system: "sharepoint",
            source_ref: "ref/edge",
            assurance: "source",
          },
        ],
      },
    ]);
    open(CHAT_CITATION);
    await screen.findByText("Alpha handbook contents.");
    expect(await screen.findByRole("link", { name: "View in Graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?mode=graph&depth=1&citation=ref%2Fdoc-alpha",
    );
  });

  it("withdraws served source facts on same-client principal changes and ignores late reads", async () => {
    const bobRead = deferred<RetrievalResult>();
    const carolRead = deferred<RetrievalResult>();
    const served: RetrievalResult = {
      chunks: [
        {
          chunk: {
            chunk_id: "chunk-alpha",
            document_id: "doc-alpha",
            text: "ADA-SOURCE-BODY-SENTINEL",
            metadata: { owner: "ADA-SOURCE-METADATA-SENTINEL" },
          },
          score: 1,
        },
      ],
      citations: [],
      graph_paths: [
        {
          hops: [
            {
              subject: {
                entity_class: "Document",
                entity_key: "ada-doc",
                display: "Ada document",
                assurance: "source",
                source_ref: CHAT_CITATION.source_ref,
              },
              edge_kind: "mentions",
              obj: {
                entity_class: "Party",
                entity_key: "party-1",
                display: "Party 1",
                assurance: "source",
                source_ref: "ref/party-1",
              },
              source_system: "sharepoint",
              source_ref: "ref/edge-1",
              assurance: "source",
            },
          ],
        },
      ],
      provisional_source_count: 0,
    };
    const retrieve = vi
      .fn<() => Promise<RetrievalResult>>()
      .mockResolvedValueOnce(served)
      .mockImplementationOnce(() => bobRead.promise)
      .mockImplementationOnce(() => carolRead.promise);
    const sharedClient = { retrieve } as unknown as ApiClient;
    const pane = (principalId: string | null) => (
      <MemoryRouter>
        <AuthStub>
          <SourcePane
            citation={CHAT_CITATION}
            client={sharedClient}
            principalId={principalId}
            onClose={() => {}}
          />
        </AuthStub>
      </MemoryRouter>
    );
    const rendered = render(pane("principal-ada"));

    expect(await screen.findByText("ADA-SOURCE-BODY-SENTINEL")).toBeInTheDocument();
    expect(screen.getByText("ADA-SOURCE-METADATA-SENTINEL")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in source system" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View in Graph" })).toBeInTheDocument();

    rendered.rerender(pane(null));
    expect(screen.queryByText("ADA-SOURCE-BODY-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByText("ADA-SOURCE-METADATA-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in source system" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View in Graph" })).not.toBeInTheDocument();

    rendered.rerender(pane("principal-bob"));
    expect(screen.queryByText("ADA-SOURCE-BODY-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByText("ADA-SOURCE-METADATA-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in source system" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View in Graph" })).not.toBeInTheDocument();
    rendered.rerender(pane("principal-carol"));
    await act(async () => {
      bobRead.resolve({
        ...served,
        chunks: [
          { ...served.chunks[0]!, chunk: { ...served.chunks[0]!.chunk, text: "BOB-LATE-SOURCE" } },
        ],
      });
      await bobRead.promise;
    });
    expect(screen.queryByText("BOB-LATE-SOURCE")).not.toBeInTheDocument();

    await act(async () => {
      carolRead.resolve({
        ...served,
        chunks: [
          { ...served.chunks[0]!, chunk: { ...served.chunks[0]!.chunk, text: "CAROL-SOURCE" } },
        ],
      });
      await carolRead.promise;
    });
    expect(screen.getByText("CAROL-SOURCE")).toBeInTheDocument();
  });
});

describe("SourcePane — the m365 parsed-document citation (owner defect 2026-08-27)", () => {
  const M365_TEXT = JSON.stringify({
    id: "b!wBqUsy5qDUShBti-item",
    name: "Disaster Hardship Response SOP.md",
    web_url: "https://contoso.sharepoint.com/sites/ops/SOP.md",
    last_modified: "2026-08-01T00:00:00Z",
    graph_doc_type: "other",
    body_ingestion_status: "parsed",
    body: "Disaster Hardship Response SOP (2026)\n\nFor DR-4830, identify customers in the pilot counties.",
    body_blocks: [
      { kind: "heading", text: "Disaster Hardship Response SOP (2026)", level: 1 },
      { kind: "paragraph", text: "For DR-4830, identify customers in the pilot counties." },
    ],
    body_block_count: 2,
    body_schema_version: "ne.parsed_document/1",
    body_source_format: "md",
  });

  function serveChunk(text: string) {
    server.use(
      http.post("*/api/v1/brain/retrieve", () =>
        HttpResponse.json({
          chunks: [
            {
              chunk: {
                chunk_id: "chunk-alpha",
                document_id: "doc-alpha",
                text,
                metadata: {},
              },
              score: 1,
            },
          ],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
        } satisfies RetrievalResult),
      ),
    );
  }

  it("renders the document's prose and display name, never the raw envelope", async () => {
    serveChunk(M365_TEXT);
    open(CHAT_CITATION);
    // The title is the document's display name — not the source ref, not a hash.
    expect(
      await screen.findByRole("heading", { name: "Disaster Hardship Response SOP.md" }),
    ).toBeInTheDocument();
    // The body is the parsed prose. The JSON envelope must never render again.
    expect(
      screen.getByText(/For DR-4830, identify customers in the pilot counties\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/body_content_sha256|body_blocks|"body"/)).toBeNull();
  });

  it("links through to the document in Documents, by the citation's document id", async () => {
    serveChunk(M365_TEXT);
    open(CHAT_CITATION);
    expect(await screen.findByRole("link", { name: "View in Documents" })).toHaveAttribute(
      "href",
      "/knowledge/documents?document=doc-alpha",
    );
  });

  it("prefers the content envelope's own doc id for the Documents link when served", async () => {
    serveChunk(
      JSON.stringify({ content: "Alpha body.", doc_id: "doc-from-envelope", title: "Alpha" }),
    );
    open(CHAT_CITATION);
    expect(await screen.findByRole("link", { name: "View in Documents" })).toHaveAttribute(
      "href",
      "/knowledge/documents?document=doc-from-envelope",
    );
  });

  it("still renders a plain-prose chunk exactly as it arrived", async () => {
    serveChunk("Clean re-embedded prose, no envelope.");
    open(CHAT_CITATION);
    expect(await screen.findByText("Clean re-embedded prose, no envelope.")).toBeInTheDocument();
  });
});

describe("resolvesToEntity", () => {
  const ref = (source_ref: string) => ({
    entity_class: "Contract",
    entity_key: "k",
    display: "Contract",
    assurance: "source",
    source_ref,
  });
  const result = (subject: string, obj: string): RetrievalResult => ({
    chunks: [],
    citations: [],
    graph_paths: [
      {
        hops: [
          {
            subject: ref(subject),
            edge_kind: "renews",
            obj: ref(obj),
            source_system: "sharepoint",
            source_ref: "ref/edge",
            assurance: "source",
          },
        ],
      },
    ],
    provisional_source_count: 0,
  });

  it("is true only when this citation's own reference is on a traversed relation", () => {
    expect(resolvesToEntity(result("ref/doc-alpha", "ref/other"), CHAT_CITATION)).toBe(true);
    expect(resolvesToEntity(result("ref/other", "ref/doc-alpha"), CHAT_CITATION)).toBe(true);
    expect(resolvesToEntity(result("ref/other", "ref/another"), CHAT_CITATION)).toBe(false);
    expect(
      resolvesToEntity(
        { chunks: [], citations: [], graph_paths: [], provisional_source_count: 0 },
        CHAT_CITATION,
      ),
    ).toBe(false);
  });
});
