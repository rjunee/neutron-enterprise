// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import { RuntimeConfigContext } from "./AppBootstrap";
import { AuthStub } from "./test/authStub";
import { RUNTIME_CONFIG } from "./test/mocks/fixtures";
import { server } from "./test/mocks/server";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

const LEGACY_CASES: ReadonlyArray<readonly [string, string, string, readonly [string, string]]> = [
  [
    "/library?tab=documents&document=opaque%2Fdoc&version=4&keep=one&keep=two",
    "/knowledge/documents",
    "Documents",
    ["document", "opaque/doc"],
  ],
  [
    "/knowledge?document=opaque%2Fdoc&as_of=2026-08-20T12%3A00%3A00Z&keep=yes",
    "/knowledge/documents",
    "Documents",
    ["as_of", "2026-08-20T12:00:00Z"],
  ],
  [
    "/library?tab=entities&entity=urn%3Ane%3Aentity%3Aaccount%252Fbluepeak&keep=yes",
    "/knowledge/graph",
    "Graph",
    ["mode", "record"],
  ],
  [
    "/library?entity=urn%3Ane%3Aentity%3Aaccount%252Fbluepeak&anchor=one&citation=two&anchor=three&citation=four&keep=yes",
    "/knowledge/graph",
    "Graph",
    ["mode", "graph"],
  ],
  [
    "/knowledge?citation=one&anchor=two&citation=three&keep=yes",
    "/knowledge/graph",
    "Graph",
    ["mode", "graph"],
  ],
];

afterEach(() => {
  window.history.pushState({}, "", "/");
});

describe("canonical Knowledge Layer routes", () => {
  it.each(LEGACY_CASES)(
    "resolves the production legacy deep link %s through the canonical workspace",
    async (legacy, pathname, heading, [key, value]) => {
      server.use(
        http.get("*/api/v1/documents", () =>
          HttpResponse.json({
            documents: [],
            next_cursor: null,
            document_types: [],
            source_systems: [],
          }),
        ),
        http.get("*/api/v1/graph/entities", () =>
          HttpResponse.json({ entities: [], next_cursor: null }),
        ),
      );
      renderAt(legacy);
      expect(await screen.findByRole("heading", { name: heading, level: 1 })).toBeInTheDocument();
      await waitFor(() => expect(window.location.pathname).toBe(pathname));
      const params = new URLSearchParams(window.location.search);
      expect(params.has("tab")).toBe(false);
      expect(params.get(key)).toBe(value);
      expect(params.getAll("keep")).toEqual(legacy.includes("keep=one") ? ["one", "two"] : ["yes"]);
      if (legacy.includes("anchor=")) {
        expect(params.getAll("anchor")).toEqual(
          legacy.startsWith("/knowledge") ? ["two"] : ["one", "three"],
        );
      }
      if (legacy.includes("citation=")) {
        expect(params.getAll("citation")).toEqual(
          legacy.startsWith("/knowledge") ? ["one", "three"] : ["two", "four"],
        );
      }
    },
  );

  it("mounts Documents directly without erasing filter or unrelated query state", async () => {
    server.use(
      http.get("*/api/v1/documents", () =>
        HttpResponse.json({
          documents: [],
          next_cursor: null,
          document_types: ["pdf"],
          source_systems: ["m365"],
        }),
      ),
    );
    renderAt("/knowledge/documents?q=risk&type=pdf&keep=one&keep=two");
    expect(await screen.findByRole("heading", { name: "Documents", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
      "Knowledge Layer/Documents",
    );
    expect(document.title).toBe("Documents · Neutron Enterprise");
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(`${window.location.pathname}${window.location.search}`).toBe(
      "/knowledge/documents?q=risk&type=pdf&keep=one&keep=two",
    );
  });

  // THE FACTS MARGIN beside the title (design/round3/screens/documents.html).
  // The count comes from the settled directory read and carries the server's own
  // cursor, so a first page never presents itself as a total — and there is no
  // count at all until a read settles.
  it("carries the visible-document count in the Documents title row", async () => {
    server.use(
      http.get("*/api/v1/documents", () =>
        HttpResponse.json({
          documents: [
            {
              document_id: "doc-1",
              display_name: "Information security policy",
              source_system: "m365",
              version_seq: 3,
              content_hash: "hash-one",
              ingested_at: "2026-08-20T12:00:00Z",
              indexing: false,
              document_type: "document",
            },
            {
              document_id: "doc-2",
              display_name: "Incident response runbook",
              source_system: "m365",
              version_seq: 5,
              content_hash: "hash-two",
              ingested_at: "2026-08-20T12:00:00Z",
              indexing: false,
              document_type: "document",
            },
          ],
          next_cursor: "opaque-next",
          document_types: ["document"],
          source_systems: ["m365"],
        }),
      ),
      http.get("*/api/v1/documents/:id/journey", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderAt("/knowledge/documents");

    const title = await screen.findByRole("heading", { name: "Documents", level: 1 });
    const row = title.closest("header");
    await waitFor(() =>
      expect(row).toHaveTextContent("Documents2 documents loaded · more available"),
    );
    // The count describes the window on screen, never the library.
    expect(row).not.toHaveTextContent("2 documents visible");
  });

  it("shows no Documents count beside the title until the directory read settles", async () => {
    server.use(
      http.get("*/api/v1/documents", () => HttpResponse.json({ detail: "off" }, { status: 500 })),
    );
    renderAt("/knowledge/documents");

    const title = await screen.findByRole("heading", { name: "Documents", level: 1 });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load documents.");
    // A failed read reports NO count — not "0 documents visible", which would be
    // a number about a window nobody read.
    expect(title.closest("header")).toHaveTextContent("Documents");
    expect(title.closest("header")).not.toHaveTextContent("documents visible");
    expect(title.closest("header")).not.toHaveTextContent("0 document");
  });

  it("mounts bare Graph without inventing an entity when the served rail is empty", async () => {
    server.use(
      http.get("*/api/v1/graph/entities", () =>
        HttpResponse.json({ entities: [], next_cursor: null }),
      ),
    );
    renderAt("/knowledge/graph?q=missing&keep=one&keep=two");
    expect(await screen.findByRole("heading", { name: "Graph", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
      "Knowledge Layer/Graph",
    );
    expect(document.title).toBe("Graph · Neutron Enterprise");
    expect(`${window.location.pathname}${window.location.search}`).toBe(
      "/knowledge/graph?q=missing&keep=one&keep=two",
    );
  });

  it("mounts the read-only Ontology inspector inside the authenticated shell", async () => {
    server.use(
      http.get("*/api/v1/ontology/model", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );

    renderAt("/knowledge/ontology?section=rules&view=list&q=risk");

    expect(await screen.findByRole("heading", { name: "Ontology", level: 1 })).toBeInTheDocument();
    expect(document.title).toBe("Ontology · Neutron Enterprise");
    expect(screen.getByText(/role doesn.t include the ontology model/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toBeInTheDocument();
    expect(`${window.location.pathname}${window.location.search}`).toBe(
      "/knowledge/ontology?section=rules&view=list&q=risk",
    );
  });

  it("mounts disabled Verified Queries without redirecting or auto-running", async () => {
    let invokeCount = 0;
    server.use(
      http.get("*/api/v1/verified/templates", () =>
        HttpResponse.json({ enabled: false, registry_hash: "r".repeat(64), templates: [] }),
      ),
      http.post("*/api/v1/verified/invoke", () => {
        invokeCount += 1;
        return HttpResponse.json({ detail: "unexpected invocation" }, { status: 500 });
      }),
    );

    renderAt("/verified-queries?template=customers-in-counties");

    expect(
      await screen.findByRole("heading", { name: "Verified Queries", level: 1 }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Verified Queries · Neutron Enterprise");
    expect(
      await screen.findByText("Verified queries are not enabled in this deployment."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toBeInTheDocument();
    expect(`${window.location.pathname}${window.location.search}`).toBe(
      "/verified-queries?template=customers-in-counties",
    );
    expect(invokeCount).toBe(0);
  });
});
