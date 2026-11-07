// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// CITATION CLICK-THROUGH + the ACL-safe branch. Clicking a chip drives
// /brain/retrieve with exactly the one chunk_id; a permitted chunk renders its
// text and an external source link; a forbidden chunk (empty retrieval) renders
// the access-denied message and NEVER any content — proving a user can't reach
// a source they lack ACL for even via a dangling citation.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { Citation } from "../api/types";
import { server } from "../test/mocks/server";
import { AuthStub } from "../test/authStub";
import { CHAT_CITATION, SECRET_CITATION, SENTINEL } from "../test/mocks/fixtures";
import { CitationChip } from "./CitationChip";
import { EvidencePanel } from "./EvidencePanel";

/** A minimal harness: a chip that, on click, opens the EvidencePanel. */
function ChipHarness({ citation }: { citation: Citation }) {
  const [selected, setSelected] = useState<Citation | null>(null);
  return (
    // The panel now offers an explicit "look this record up in the knowledge
    // graph" route link (the graph is a SEPARATE question from the record), so
    // the harness needs a router.
    <MemoryRouter>
      <AuthStub>
        <CitationChip index={1} citation={citation} onSelect={setSelected} />
        {selected && (
          <EvidencePanel key={selected.chunk_id} citation={selected} onClose={() => {}} />
        )}
      </AuthStub>
    </MemoryRouter>
  );
}

describe("EvidencePanel click-through", () => {
  it("posts exactly [chunk_id] and renders the chunk text + external source link", async () => {
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
                metadata: {},
              },
              score: 1,
            },
          ],
          citations: [],
        });
      }),
    );

    render(<ChipHarness citation={CHAT_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    expect(await screen.findByText("Alpha handbook contents.")).toBeInTheDocument();
    expect(body).toEqual({ chunk_ids: ["chunk-alpha"] });

    const link = screen.getByRole("link", { name: /Open in sharepoint/ });
    expect(link).toHaveAttribute("href", CHAT_CITATION.source_url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  // -------------------------------------------------------------------------
  // NO HTTP VERBS AND NO PATHS AT THE READER
  // -------------------------------------------------------------------------
  // This panel is one click from any citation — the main Ask screen. It used to
  // narrate its own plumbing three times: "Reading it back from GET
  // /v1/brain/retrieve under your key…", the same path again in the provenance
  // line, and "Could not load evidence: request to /v1/brain/retrieve failed
  // with HTTP 500" straight out of the transport. The FACT a reader needs is
  // that the evidence is re-read from the source under their own permissions.

  /** Anything that reads as plumbing rather than as a sentence about access. */
  const PLUMBING = /\bGET\b|\bPOST\b|\bHTTP\b|\/v1\/|\/api\/|retrieve/i;

  it("names the re-read in plain English while it is loading — no verb, no path", async () => {
    server.use(
      http.post("*/api/v1/brain/retrieve", async () => {
        // Never resolves: the panel is pinned in its loading state.
        await new Promise(() => {});
        return HttpResponse.json({ chunks: [], citations: [] });
      }),
    );

    render(<ChipHarness citation={CHAT_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    const loading = await screen.findByRole("status");
    expect(loading).toHaveTextContent("Reading this record back from the source");
    expect(loading).toHaveTextContent("under your own permissions");
    expect(loading.textContent).not.toMatch(PLUMBING);
  });

  it("states the provenance without naming the endpoint that served it", async () => {
    render(<ChipHarness citation={CHAT_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    const panel = await screen.findByRole("complementary", { name: /this citation came from/ });
    const provenance = await screen.findByText(/Re-read from the source just now/);
    expect(provenance).toHaveTextContent("under your own permissions");
    expect(provenance).toHaveTextContent("The server re-checked your access");
    expect(panel.textContent).not.toMatch(PLUMBING);
  });

  it("gives a failed read a fact and a way out, not the transport's sentence", async () => {
    let calls = 0;
    server.use(
      http.post("*/api/v1/brain/retrieve", () => {
        calls += 1;
        // The first read fails the way the deployed one did: a 500, whose
        // ApiError keeps the transport's own string on `developerMessage`.
        if (calls === 1) return new HttpResponse(null, { status: 500 });
        return HttpResponse.json({
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
        });
      }),
    );

    render(<ChipHarness citation={CHAT_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent("Couldn\u2019t load this evidence.");
    expect(error.textContent).not.toMatch(PLUMBING);
    expect(error.textContent).not.toMatch(/500/);

    // A read error with no way to run the read again is a dead end.
    await userEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Alpha handbook contents.")).toBeInTheDocument();
    expect(calls).toBe(2);
  });

  it("draws a REFUSED read as refused, not as a failure with a Retry that cannot work", async () => {
    // The defect: every rejection collapsed into the read-error state, so a 403
    // — a role boundary correctly held — rendered "Couldn't load this evidence."
    // in the fault colour beside a Retry that would be refused identically every
    // time. primitives/states.tsx is explicit that a refusal is neutral and
    // never a fault.
    let calls = 0;
    server.use(
      http.post("*/api/v1/brain/retrieve", () => {
        calls += 1;
        return HttpResponse.json({ detail: "not in the finance department" }, { status: 403 });
      }),
    );

    render(<ChipHarness citation={CHAT_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    expect(await screen.findByText(/Your role doesn’t include/)).toBeInTheDocument();
    expect(screen.getByText("GATED")).toBeInTheDocument();

    const panel = screen.getByRole("complementary", { name: /this citation came from/ });
    // Not a fault: no alert, no "couldn't load", and no button offering to run
    // a read the server has already answered.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(panel.textContent).not.toMatch(/Couldn’t load/);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    // Still no plumbing and no content.
    expect(panel.textContent).not.toMatch(PLUMBING);
    expect(calls).toBe(1);
  });

  it("renders access-denied and NO content for a forbidden citation (empty retrieval)", async () => {
    // The default handler returns empty for chunk-secret (not in the permitted set).
    render(<ChipHarness citation={SECRET_CITATION} />);
    await userEvent.click(screen.getByRole("button", { name: /Citation 1/ }));

    expect(await screen.findByText(/don't have access/i)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SENTINEL))).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
