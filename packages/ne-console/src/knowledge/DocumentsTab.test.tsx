// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { DocumentJourneyWire } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { LibraryPage } from "./LibraryPage";

const STAGES: DocumentJourneyWire["stages"] = [
  ["parse", "complete"],
  ["chunk", "complete"],
  ["acl", "complete"],
  ["embed", "active"],
  ["graph", "waiting"],
  ["supersede", "waiting"],
].map(([stage, status], index) => ({
  stage: stage as DocumentJourneyWire["stages"][number]["stage"],
  status: status as DocumentJourneyWire["stages"][number]["status"],
  occurred_at: "2026-08-18T19:00:00Z",
  audit_seq: index < 3 ? 410 + index : null,
  detail: `${stage} detail`,
}));

const JOURNEY: DocumentJourneyWire = {
  document_id: "doc-1",
  display_name: "Servicing SOP.docx",
  source_system: "m365-sharepoint",
  selected_version: 2,
  selected_content: "v2 content",
  diff: null,
  stages: STAGES,
  versions: [
    {
      version_seq: 1,
      content_hash: "a".repeat(64),
      ingested_at: "2026-08-18T18:00:00Z",
      ingested_audit_seq: 400,
      superseded_at: "2026-08-18T19:00:00Z",
      superseded_audit_seq: 410,
      chunk_count: 3,
      status: "superseded",
    },
    {
      version_seq: 2,
      content_hash: "b".repeat(64),
      ingested_at: null,
      ingested_audit_seq: null,
      superseded_at: null,
      superseded_audit_seq: null,
      chunk_count: 4,
      status: "indexing",
    },
  ],
};

function renderDocuments() {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={["/library?tab=documents&document=doc-1"]}>
        <LibraryPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

function handlers(onQuery?: (url: URL) => void) {
  server.use(
    http.get("*/api/v1/documents", () =>
      HttpResponse.json({
        documents: [
          {
            document_id: "doc-1",
            display_name: "Servicing SOP.docx",
            source_system: "m365-sharepoint",
            version_seq: 2,
            content_hash: "b".repeat(64),
            ingested_at: null,
            indexing: true,
          },
        ],
      }),
    ),
    http.get("*/api/v1/documents/doc-1/journey", ({ request }) => {
      const url = new URL(request.url);
      onQuery?.(url);
      if (url.searchParams.get("version") === "1") {
        return HttpResponse.json({
          ...JOURNEY,
          selected_version: 1,
          selected_content: "v1 content",
          diff: "--- v0\n+++ v1\n+v1 content",
        });
      }
      return HttpResponse.json(JOURNEY);
    }),
  );
}

describe("Library documents — journey and versions", () => {
  it("shows the ordered live pipeline and opens an exact version diff", async () => {
    const queries: URL[] = [];
    handlers((url) => queries.push(url));
    renderDocuments();

    const pipeline = await screen.findByRole("list", { name: "Document pipeline" });
    expect(
      within(pipeline)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(
      expect.arrayContaining([
        expect.stringContaining("PARSE"),
        expect.stringContaining("EMBED"),
        expect.stringContaining("SUPERSEDE"),
      ]),
    );
    expect(within(pipeline).getByText("EMBED").closest("li")).toHaveClass(
      "ne-docjourney__stage--active",
    );

    await userEvent.click(screen.getByRole("tab", { name: "VERSIONS" }));
    const versions = screen.getByRole("table", { name: "Document versions" });
    expect(within(versions).getByText("v1")).toBeInTheDocument();
    expect(within(versions).getByText("3")).toBeInTheDocument();
    await userEvent.click(within(versions).getAllByRole("button", { name: "DIFF" })[0]!);

    expect(await screen.findByLabelText("Version diff")).toHaveTextContent("+v1 content");
    expect(queries.some((url) => url.searchParams.get("version") === "1")).toBe(true);
  });

  it("asks the server for the selected as-of instant", async () => {
    const queries: URL[] = [];
    handlers((url) => queries.push(url));
    renderDocuments();
    await screen.findByRole("list", { name: "Document pipeline" });

    fireEvent.change(screen.getByLabelText("Retrieve as of"), {
      target: { value: "2026-08-18T12:30" },
    });
    await userEvent.click(screen.getByRole("button", { name: "Retrieve" }));
    await waitFor(() => expect(queries.some((url) => url.searchParams.has("as_of"))).toBe(true));

    const afterAsOf = queries.length;
    await userEvent.click(screen.getByRole("button", { name: "Latest" }));
    await waitFor(() => {
      expect(queries.length).toBeGreaterThan(afterAsOf);
      expect([...queries].reverse()[0]?.search).toBe("");
    });
  });
});
