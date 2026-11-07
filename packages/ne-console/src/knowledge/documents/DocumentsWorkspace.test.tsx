// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import type {
  DocumentJourneyReadWire,
  DocumentListQuery,
  DocumentListWire,
  DocumentStageEvidenceWire,
  DocumentVersionWire,
} from "../../api/types";
import { DocumentsWorkspace } from "./DocumentsWorkspace";
import type { DocumentRouteState } from "./documentRouteState";

const INITIAL_ROUTE: DocumentRouteState = {
  document: null,
  selector: { kind: "latest" },
  stage: null,
  query: "",
  documentType: null,
  source: "",
  includeStructured: false,
};

function listed(
  id = "doc-1",
  name = "Operating plan",
  patch: Partial<DocumentListWire> = {},
): DocumentListWire {
  return {
    documents: [
      {
        document_id: id,
        display_name: name,
        source_system: "m365",
        version_seq: 9,
        content_hash: "list-hash",
        ingested_at: "2026-08-20T12:00:00Z",
        indexing: false,
        document_type: "document",
      },
    ],
    next_cursor: null,
    document_types: ["document", "pdf"],
    source_systems: ["m365", "sharepoint"],
    // INTERIM (PD-DOC-TITLES): honest count of ".negraph.json" rows in the
    // ACL-visible corpus; a case exercising the toggle's count overrides this
    // via `patch`.
    operational_graph_count: 0,
    ...patch,
  };
}

/** The SERVED version's history row, as the server sends it. Defaults to the row
 * the default journey serves, so a lineage case states only the field it is
 * exercising and inherits everything else from the shared fixture. */
function version(patch: Partial<DocumentVersionWire> = {}): DocumentVersionWire {
  return {
    version_seq: 7,
    content_hash: "served-hash-seven",
    ingested_at: "2026-08-20T12:00:00Z",
    ingested_audit_seq: 10,
    superseded_at: null,
    superseded_audit_seq: null,
    chunk_count: 2,
    status: "current",
    ...patch,
  };
}

function journey(patch: Partial<DocumentJourneyReadWire> = {}): DocumentJourneyReadWire {
  return {
    document_id: "doc-1",
    display_name: "Operating plan",
    source_system: "m365",
    selected_version: 7,
    selected_content: "served version seven",
    diff: "--- v6\n+++ v7\n@@ -1 +1 @@\n-old\n+new",
    stages: [
      {
        stage: "parse",
        status: "complete",
        occurred_at: "2026-08-20T12:00:00Z",
        audit_seq: 10,
        detail: "Parsed.",
      },
      {
        stage: "chunk",
        status: "complete",
        occurred_at: "2026-08-20T12:01:00Z",
        audit_seq: 11,
        detail: "Chunked.",
      },
    ],
    versions: [
      {
        version_seq: 6,
        content_hash: "served-hash-six",
        ingested_at: "2026-08-19T12:00:00Z",
        ingested_audit_seq: 8,
        superseded_at: "2026-08-20T12:00:00Z",
        superseded_audit_seq: 9,
        chunk_count: 1,
        status: "superseded",
      },
      version(),
    ],
    document_type: "document",
    selected_content_hash: "served-hash-seven",
    comparison: { state: "available", predecessor_version: 6 },
    ...patch,
  };
}

function stage(patch: Partial<DocumentStageEvidenceWire> = {}): DocumentStageEvidenceWire {
  return {
    document_id: "doc-1",
    version: 7,
    content_hash: "served-hash-seven",
    document_type: "document",
    stage: "parse",
    status: "complete",
    occurred_at: "2026-08-20T12:00:00Z",
    audit_seq: 10,
    detail: "Parsed.",
    facts: [],
    artifacts: [],
    semantic_chunks: null,
    comparison: { state: "available", predecessor_version: 6 },
    graph_evidence: null,
    ...patch,
  };
}

function fakeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    listDocuments: vi.fn().mockResolvedValue(listed()),
    documentJourney: vi.fn().mockResolvedValue(journey()),
    documentStageEvidence: vi.fn().mockResolvedValue(stage()),
    ...overrides,
  } as unknown as ApiClient;
}

function Harness({
  client,
  ownerId = "principal-1",
  initialRoute = INITIAL_ROUTE,
}: {
  client: ApiClient;
  ownerId?: string;
  initialRoute?: DocumentRouteState;
}) {
  const [route, setRoute] = useState(initialRoute);
  return (
    <MemoryRouter>
      <DocumentsWorkspace
        client={client}
        ownerId={ownerId}
        route={route}
        onRouteChange={(next) => setRoute(next)}
      />
    </MemoryRouter>
  );
}

function StaticHarness({
  client,
  ownerId,
  route = INITIAL_ROUTE,
  onRouteChange,
}: {
  client: ApiClient;
  ownerId: string;
  route?: DocumentRouteState;
  onRouteChange: (next: DocumentRouteState) => void;
}) {
  return (
    <MemoryRouter>
      <DocumentsWorkspace
        client={client}
        ownerId={ownerId}
        route={route}
        onRouteChange={onRouteChange}
      />
    </MemoryRouter>
  );
}

/** The identity band — the one strip that carries name, lineage, served
 * version, content hash, audit receipt and the version control. Scoped lookups
 * go through here so a band assertion can never be satisfied by the version
 * history table further down the page. */
function band(name = "Operating plan"): HTMLElement {
  const header = screen.getByRole("heading", { name }).closest("header");
  if (header === null) throw new Error("no identity band");
  return header;
}

describe("DocumentsWorkspace", () => {
  it("uses the first visible server row and binds stage evidence to the served version", async () => {
    const client = fakeClient();
    render(<Harness client={client} />);

    expect(await screen.findByRole("heading", { name: "Operating plan" })).toBeInTheDocument();
    expect(screen.getByText("served version seven")).toBeInTheDocument();
    await waitFor(() =>
      expect(client.documentStageEvidence).toHaveBeenCalledWith(
        "doc-1",
        7,
        "parse",
        expect.any(AbortSignal),
      ),
    );
    expect(screen.getByText("m365 · document · served v7")).toBeInTheDocument();
    // The served row (v7) is NOT versions[0] in the shared fixture — v6 sits
    // ahead of it — so a pill reading the first row reports RETIRED here.
    expect(
      screen.getByLabelText("ACTIVE — Latest version in this document’s lineage."),
    ).toBeInTheDocument();
  });

  // The served version's pill must report the lineage status the server put on
  // THAT version's own history row, and must say nothing about authority: the
  // journey payload carries no authorization field. These cases differ only in
  // the served row's status, so a pill wired to a constant, to the wrong row, or
  // to a swapped mapping fails at least one while the others stay green.
  // ======================= THE CONTENT-LEADS COMPOSITION =======================
  // design/round3/screens/documents.html. The screen used to reach the document
  // itself sixth — after the head, the hash block, the version selector, the
  // pipeline and the stage evidence — and then render it as a monospace <pre>.
  // These cases pin the order and the surface; each carries the complement that
  // proves nothing was thrown away to get there.

  it("leads with the served content and demotes provenance beneath it", async () => {
    render(<Harness client={fakeClient()} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const identity = band();
    const content = screen.getByRole("heading", { name: "Content" });
    const provenance = screen.getByRole("heading", { name: "Provenance & versions" });
    const pipeline = screen.getByRole("list", { name: "Document pipeline" });

    const follows = (first: Element, second: Element) =>
      Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);

    expect(follows(identity, content)).toBe(true);
    expect(follows(content, provenance)).toBe(true);
    expect(follows(provenance, pipeline)).toBe(true);
    // …and the served text is prose on the page, not a monospace dump.
    const served = screen.getByText("served version seven");
    expect(served.tagName).toBe("P");
    expect(served.closest("pre")).toBeNull();

    // COMPLEMENT — demoted is not deleted. The whole evidentiary trail is still
    // on the page, below the content it accounts for.
    expect(screen.getByRole("heading", { name: "Ingestion pipeline" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Version history" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Version comparison: v6 → v7" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Document version history")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "PARSE evidence" })).toBeInTheDocument();
  });

  it("renders served content in blocks that keep every newline the server sent", async () => {
    const content =
      '# Information Security Policy\n\nLine one of a block.\nLine two of the same block.\n\n{\n  "record": "structured"\n}';
    const client = fakeClient({
      documentJourney: vi.fn().mockResolvedValue(journey({ selected_content: content })),
    });
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const blocks = Array.from(document.querySelectorAll(".ne-documents__prose p"));
    expect(blocks.map((block) => block.textContent)).toEqual([
      "# Information Security Policy",
      "Line one of a block.\nLine two of the same block.",
      '{\n  "record": "structured"\n}',
    ]);

    // COMPLEMENT — the console re-marks nothing. A leading "#" is text the
    // server sent, not a heading the console may promote, and the structured
    // block keeps its own line structure rather than being reflowed as prose.
    expect(document.querySelector(".ne-documents__prose h1")).toBeNull();
    expect(document.querySelector(".ne-documents__prose h2")).toBeNull();
    expect(document.querySelector(".ne-documents__prose h3")).toBeNull();
    expect(document.querySelector(".ne-documents__prose pre")).toBeNull();
  });

  it("reports an empty served version as an absence, never as zero words", async () => {
    const empty = fakeClient({
      documentJourney: vi.fn().mockResolvedValue(journey({ selected_content: "   \n\n  " })),
    });
    const first = render(<Harness client={empty} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const absent = screen.getByText(/The server returned no content for this version\./);
    expect(within(absent).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("served v7 · no content returned")).toBeInTheDocument();
    expect(screen.queryByText(/0 words/)).toBeNull();
    expect(document.querySelector(".ne-documents__prose")).toBeNull();
    first.unmount();

    // COMPLEMENT — a served version that DOES carry content is counted, so the
    // absence above is a real branch and not the only thing this can render.
    render(<Harness client={fakeClient()} />);
    await screen.findByRole("heading", { name: "Operating plan" });
    expect(screen.getByText("served v7 · 3 words")).toBeInTheDocument();
    expect(screen.queryByText(/no content returned/)).toBeNull();
  });

  it("keeps the version comparison collapsed until it is asked for", async () => {
    render(<Harness client={fakeClient()} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const compare = document.querySelector("details.ne-documents__compare");
    expect(compare).toBeInstanceOf(HTMLDetailsElement);
    expect((compare as HTMLDetailsElement).open).toBe(false);
    // What changed is still ANSWERABLE without opening anything: the heading
    // names the two versions being compared, on the summary itself.
    expect(
      screen.getByRole("heading", { name: "Version comparison: v6 → v7" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByText("Compare versions"));
    expect((compare as HTMLDetailsElement).open).toBe(true);
    expect(screen.getByLabelText("Version comparison")).toBeInTheDocument();
    expect(screen.getByLabelText("Version comparison evidence")).toBeInTheDocument();
  });

  it("reports the settled directory count upward and stays silent while it is unread", async () => {
    const onDirectorySummary = vi.fn();
    const client = fakeClient({
      listDocuments: vi
        .fn()
        .mockResolvedValue(listed("doc-1", "Operating plan", { next_cursor: "more" })),
    });
    const paged = render(
      <MemoryRouter>
        <DocumentsWorkspace
          client={client}
          ownerId="principal-1"
          route={INITIAL_ROUTE}
          onRouteChange={() => undefined}
          onDirectorySummary={onDirectorySummary}
        />
      </MemoryRouter>,
    );
    await screen.findByText("Operating plan");

    // Nothing was counted before the read settled…
    expect(onDirectorySummary).toHaveBeenCalledWith(null);
    // …and the settled count carries the server's own cursor, so a page is
    // never presented as a total.
    expect(onDirectorySummary).toHaveBeenLastCalledWith({ loaded: 1, more: true });
    paged.unmount();

    // COMPLEMENT — a failed read withdraws the count rather than leaving the
    // last one standing beside the title.
    const failed = vi.fn();
    render(
      <MemoryRouter>
        <DocumentsWorkspace
          client={fakeClient({ listDocuments: vi.fn().mockRejectedValue(new Error("off")) })}
          ownerId="principal-1"
          route={INITIAL_ROUTE}
          onRouteChange={() => undefined}
          onDirectorySummary={failed}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load documents.");
    expect(failed).toHaveBeenLastCalledWith(null);
  });

  it("reads the served version's lineage standing off its own history row", async () => {
    render(
      <Harness
        client={fakeClient({
          documentJourney: vi.fn().mockResolvedValue(
            journey({
              versions: [
                version({ version_seq: 8, content_hash: "served-hash-eight" }),
                version({
                  status: "superseded",
                  superseded_at: "2026-08-20T13:00:00Z",
                  superseded_audit_seq: 12,
                }),
              ],
            }),
          ),
        })}
      />,
    );

    // Served version is 7 — the SUPERSEDED row — while a newer current row sits
    // first in the list. Binding to versions[0], or to whichever row is
    // "current", reports ACTIVE here instead.
    expect(
      await screen.findByLabelText("RETIRED — Superseded by a later version in this lineage."),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("ACTIVE — Latest version in this document’s lineage."),
    ).not.toBeInTheDocument();
  });

  it("reports a served version that is still indexing as not yet the latest", async () => {
    render(
      <Harness
        client={fakeClient({
          documentJourney: vi
            .fn()
            .mockResolvedValue(journey({ versions: [version({ status: "indexing" })] })),
        })}
      />,
    );

    expect(
      await screen.findByLabelText(
        "SYNCING — Indexing; not yet the latest version in this lineage.",
      ),
    ).toBeInTheDocument();
  });

  it("declines to state a lineage standing for a served version with no history row", async () => {
    render(
      <Harness
        client={fakeClient({
          documentJourney: vi.fn().mockResolvedValue(
            // Every row here is for some OTHER version, so the console holds no
            // status for the version it is serving and must not borrow one.
            journey({ versions: [version({ version_seq: 6 })] }),
          ),
        })}
      />,
    );

    expect(
      await screen.findByLabelText("UNKNOWN — Served version is missing from the version history."),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("ACTIVE — Latest version in this document’s lineage."),
    ).not.toBeInTheDocument();
  });

  it("renders every audit-unbound journey stage without raw success truth", async () => {
    const rawDetails = ["parsed", "chunked", "ACL resolved", "embedded", "graph waiting", "commit"];
    const stages = (["parse", "chunk", "acl", "embed", "graph", "supersede"] as const).map(
      (stageName) => ({
        stage: stageName,
        status: "unavailable" as const,
        occurred_at: null,
        audit_seq: null,
        detail: "evidence unavailable: no bound audit receipt",
      }),
    );
    const client = fakeClient({
      documentJourney: vi.fn().mockResolvedValue(journey({ stages })),
    });

    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const timeline = screen.getByRole("list", { name: "Document pipeline" });
    expect(within(timeline).getAllByText("UNAVAILABLE")).toHaveLength(6);
    expect(within(timeline).getAllByText("Audit unavailable")).toHaveLength(6);
    expect(within(timeline).getAllByText("Unavailable")).toHaveLength(6);
    expect(within(timeline).queryByText("OK")).not.toBeInTheDocument();
    expect(within(timeline).queryByText("Audit pending")).not.toBeInTheDocument();
    for (const detail of rawDetails) {
      expect(within(timeline).queryByText(detail)).not.toBeInTheDocument();
    }
  });

  it("shows exact selected-version evidence and drives every selector from served versions", async () => {
    const client = fakeClient();
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    // §2.7: the hash truncates on screen and carries its EXACT value in `title`
    // and the clipboard. The band renders the served version's hash and nothing
    // else — a chip found here can only be the served one.
    const identity = band();
    expect(within(identity).getByText("SHA-256")).toBeInTheDocument();
    expect(within(identity).getByTitle("served-hash-seven")).toBeInTheDocument();
    expect(within(identity).getByText("served-h")).toBeInTheDocument();
    expect(
      within(identity).getByRole("button", { name: "Copy selected content hash" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Selected-version evidence · Audit #10" }),
    ).toHaveAttribute("href", "/audit?seq=10");
    expect(
      screen.getByRole("heading", { name: "Version comparison: v6 → v7" }),
    ).toBeInTheDocument();

    const selector = screen.getByRole("combobox", { name: "Visible version" });
    expect(within(selector).getByRole("option", { name: "Latest visible" })).toBeInTheDocument();
    expect(within(selector).getByRole("option", { name: "Version 7" })).toBeInTheDocument();
    expect(within(selector).getByRole("option", { name: "As-of instant" })).toBeInTheDocument();

    const callsBeforeEditing = vi.mocked(client.documentJourney).mock.calls.length;
    await userEvent.selectOptions(selector, "as_of");
    const asOfInput = screen.getByRole("textbox", { name: "As-of ISO-8601" });
    expect(selector).toHaveValue("as_of");
    expect(asOfInput).toHaveFocus();
    expect(client.documentJourney).toHaveBeenCalledTimes(callsBeforeEditing);

    await userEvent.type(asOfInput, "2026-08-20T19:00:00Z");
    await userEvent.click(screen.getByRole("button", { name: "Show as of" }));
    await waitFor(() =>
      expect(client.documentJourney).toHaveBeenLastCalledWith(
        "doc-1",
        { as_of: "2026-08-20T19:00:00Z" },
        expect.any(AbortSignal),
      ),
    );
    expect(selector).toHaveValue("as_of");
  });

  it("drops an As-of draft synchronously when the principal changes on one client", async () => {
    let releaseBobList: ((value: DocumentListWire) => void) | undefined;
    let releaseBobJourney: ((value: DocumentJourneyReadWire) => void) | undefined;
    const client = fakeClient({
      listDocuments: vi
        .fn()
        .mockResolvedValueOnce(listed())
        .mockImplementationOnce(
          () =>
            new Promise<DocumentListWire>((resolve) => {
              releaseBobList = resolve;
            }),
        ),
      documentJourney: vi
        .fn()
        .mockResolvedValueOnce(journey())
        .mockImplementationOnce(
          () =>
            new Promise<DocumentJourneyReadWire>((resolve) => {
              releaseBobJourney = resolve;
            }),
        ),
    });
    const { rerender } = render(<Harness client={client} ownerId="alice" />);
    await screen.findByRole("heading", { name: "Operating plan" });

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Visible version" }),
      "as_of",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "As-of ISO-8601" }),
      "alice-owner-sentinel",
    );
    expect(screen.getByDisplayValue("alice-owner-sentinel")).toHaveFocus();

    rerender(<Harness client={client} ownerId="bob" />);
    expect(screen.queryByDisplayValue("alice-owner-sentinel")).not.toBeInTheDocument();
    expect(screen.queryByText("served version seven")).not.toBeInTheDocument();

    await act(async () => {
      releaseBobList?.(listed());
      releaseBobJourney?.(journey());
      await Promise.resolve();
    });
    await screen.findByRole("heading", { name: "Operating plan" });
    expect(screen.getByRole("combobox", { name: "Visible version" })).toHaveValue("latest");
    expect(screen.getByRole("textbox", { name: "As-of ISO-8601" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "As-of ISO-8601" })).not.toHaveFocus();
  });

  it("drops an As-of draft synchronously when the client changes for one principal", async () => {
    const clientA = fakeClient();
    let releaseClientBList: ((value: DocumentListWire) => void) | undefined;
    let releaseClientBJourney: ((value: DocumentJourneyReadWire) => void) | undefined;
    const clientB = fakeClient({
      listDocuments: vi.fn(
        () =>
          new Promise<DocumentListWire>((resolve) => {
            releaseClientBList = resolve;
          }),
      ),
      documentJourney: vi.fn(
        () =>
          new Promise<DocumentJourneyReadWire>((resolve) => {
            releaseClientBJourney = resolve;
          }),
      ),
    });
    const { rerender } = render(<Harness client={clientA} ownerId="principal-1" />);
    await screen.findByRole("heading", { name: "Operating plan" });

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Visible version" }),
      "as_of",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "As-of ISO-8601" }),
      "client-a-sentinel",
    );
    expect(screen.getByDisplayValue("client-a-sentinel")).toHaveFocus();

    rerender(<Harness client={clientB} ownerId="principal-1" />);
    expect(screen.queryByDisplayValue("client-a-sentinel")).not.toBeInTheDocument();
    expect(screen.queryByText("served version seven")).not.toBeInTheDocument();

    await act(async () => {
      releaseClientBList?.(listed());
      releaseClientBJourney?.(journey());
      await Promise.resolve();
    });
    await screen.findByRole("heading", { name: "Operating plan" });
    expect(screen.getByRole("combobox", { name: "Visible version" })).toHaveValue("latest");
    expect(screen.getByRole("textbox", { name: "As-of ISO-8601" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "As-of ISO-8601" })).not.toHaveFocus();
  });

  it("links SUPERSEDE to only its served predecessor and audit evidence", async () => {
    const supersede = {
      stage: "supersede" as const,
      status: "complete" as const,
      occurred_at: "2026-08-20T12:02:00Z",
      audit_seq: 12,
      detail: "Predecessor superseded.",
    };
    const client = fakeClient({
      documentJourney: vi.fn(async (_id, selector) =>
        journey({
          selected_version: selector?.version ?? 7,
          stages: [...journey().stages, supersede],
        }),
      ),
      documentStageEvidence: vi.fn(async (_id, version, stageName) =>
        stage({
          version,
          stage: stageName,
          audit_seq: stageName === "supersede" ? 12 : 10,
          comparison: { state: "available", predecessor_version: 6 },
        }),
      ),
    });
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    await userEvent.click(screen.getByRole("button", { name: /SUPERSEDE/ }));
    const auditLinks = await screen.findAllByRole("link", { name: "Audit #12" });
    expect(auditLinks).toHaveLength(2);
    for (const link of auditLinks) {
      expect(link).toHaveAttribute("href", "/audit?seq=12");
    }
    expect(screen.getByRole("button", { name: "Predecessor v6" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open predecessor" }));
    await waitFor(() =>
      expect(client.documentJourney).toHaveBeenLastCalledWith(
        "doc-1",
        { version: 6 },
        expect.any(AbortSignal),
      ),
    );
  });

  it("sends filters and the opaque server cursor without deriving facets", async () => {
    const calls: Array<DocumentListQuery | undefined> = [];
    const client = fakeClient({
      listDocuments: vi.fn(async (query?: DocumentListQuery) => {
        calls.push(query);
        if (query?.cursor === "opaque.next+/=") {
          return listed("doc-2", "Second plan", {
            document_types: ["document"],
            source_systems: ["m365"],
          });
        }
        return listed("doc-1", "Operating plan", { next_cursor: "opaque.next+/=" });
      }),
    });
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search documents…" }), {
      target: { value: "policy" },
    });
    await waitFor(() => expect(calls.some((query) => query?.q === "policy")).toBe(true));

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "pdf");
    await waitFor(() => expect(calls.some((query) => query?.type === "pdf")).toBe(true));

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() =>
      expect(
        calls.some((query) => query?.cursor === "opaque.next+/=" && query.type === "pdf"),
      ).toBe(true),
    );
    expect(await screen.findByText("Second plan")).toBeInTheDocument();
    expect(screen.getByText("2 loaded")).toBeInTheDocument();
  });

  // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
  // ".negraph.json" connector filename convention; REMOVE when the real
  // `structured` flag lands on the document wire. Default excludes
  // operational-graph records; the toggle is the explicit opt-in.
  it("the Include operational graph records toggle renders off by default and drives the structured query param", async () => {
    const calls: Array<DocumentListQuery | undefined> = [];
    const client = fakeClient({
      listDocuments: vi.fn(async (query?: DocumentListQuery) => {
        calls.push(query);
        return listed(undefined, undefined, { operational_graph_count: 3 });
      }),
    });
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    const toggle = await screen.findByRole("button", {
      name: "Include operational graph records (3)",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(calls.every((query) => query?.structured === undefined)).toBe(true);

    await userEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
    await waitFor(() => expect(calls.some((query) => query?.structured === true)).toBe(true));

    await userEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
    await waitFor(() =>
      expect(calls.at(-1)?.structured === undefined || calls.at(-1)?.structured === false).toBe(
        true,
      ),
    );
  });

  // INTERIM (PD-DOC-TITLES): the count beside the toggle is the server's own
  // honest tally over the ACL-visible corpus, never a page-local guess —
  // while the list has not settled there is no count, and the label says so
  // by omitting the parenthetical rather than showing a fabricated number.
  it("omits the operational graph records count while the list has not settled", async () => {
    let releaseList: ((value: DocumentListWire) => void) | undefined;
    const pending = new Promise<DocumentListWire>((resolve) => {
      releaseList = resolve;
    });
    const client = fakeClient({
      listDocuments: vi.fn(() => pending),
    });
    render(<Harness client={client} />);

    expect(
      screen.getByRole("button", { name: "Include operational graph records" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Include operational graph records \(/ }),
    ).not.toBeInTheDocument();

    await act(async () => {
      releaseList?.(listed(undefined, undefined, { operational_graph_count: 5 }));
      await pending;
    });
    await screen.findByRole("button", { name: "Include operational graph records (5)" });
  });

  it("resets pagination when a filter fails and is retried during load-more", async () => {
    let releaseCursor: ((value: DocumentListWire) => void) | undefined;
    const cursorPage = new Promise<DocumentListWire>((resolve) => {
      releaseCursor = resolve;
    });
    let filteredAttempts = 0;
    const client = fakeClient({
      listDocuments: vi.fn((query?: DocumentListQuery) => {
        if (query?.cursor !== undefined) return cursorPage;
        if (query?.q === "policy") {
          filteredAttempts += 1;
          return filteredAttempts === 1
            ? Promise.reject(new Error("temporarily off"))
            : Promise.resolve(
                listed("doc-2", "Filtered document", { next_cursor: "filtered-next" }),
              );
        }
        return Promise.resolve(
          listed("doc-1", "Initial document", { next_cursor: "initial-next" }),
        );
      }),
    });
    render(<Harness client={client} />);
    await screen.findByText("Initial document");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    fireEvent.change(screen.getByRole("searchbox", { name: "Search documents…" }), {
      target: { value: "policy" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load documents.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Filtered document")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    releaseCursor?.(listed("stale-doc", "Stale cursor document"));
    await Promise.resolve();
    expect(screen.queryByText("Stale cursor document")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
  });

  it("resets pagination immediately when the principal changes during load-more", async () => {
    let releaseCursor: ((value: DocumentListWire) => void) | undefined;
    const cursorPage = new Promise<DocumentListWire>((resolve) => {
      releaseCursor = resolve;
    });
    let calls = 0;
    const client = fakeClient({
      listDocuments: vi.fn((query?: DocumentListQuery) => {
        calls += 1;
        if (query?.cursor !== undefined) return cursorPage;
        return Promise.resolve(
          calls === 1
            ? listed("alice-doc", "Alice document", { next_cursor: "alice-next" })
            : listed("bob-doc", "Bob document", { next_cursor: "bob-next" }),
        );
      }),
    });
    const { rerender } = render(<Harness client={client} ownerId="alice" />);
    await screen.findByText("Alice document");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    rerender(<Harness client={client} ownerId="bob" />);
    expect(await screen.findByText("Bob document")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    releaseCursor?.(listed("stale-doc", "Alice stale cursor"));
    await Promise.resolve();
    expect(screen.queryByText("Alice stale cursor")).not.toBeInTheDocument();
  });

  it("resets pagination immediately when the client changes during load-more", async () => {
    let releaseCursor: ((value: DocumentListWire) => void) | undefined;
    const cursorPage = new Promise<DocumentListWire>((resolve) => {
      releaseCursor = resolve;
    });
    const firstClient = fakeClient({
      listDocuments: vi.fn((query?: DocumentListQuery) =>
        query?.cursor === undefined
          ? Promise.resolve(listed("doc-1", "First client", { next_cursor: "first-next" }))
          : cursorPage,
      ),
    });
    const secondClient = fakeClient({
      listDocuments: vi
        .fn()
        .mockResolvedValue(listed("doc-2", "Second client", { next_cursor: "second-next" })),
    });
    const { rerender } = render(<Harness client={firstClient} ownerId="principal-1" />);
    await screen.findByText("First client");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    rerender(<Harness client={secondClient} ownerId="principal-1" />);
    expect(await screen.findByText("Second client")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    releaseCursor?.(listed("stale-doc", "First client stale cursor"));
    await Promise.resolve();
    expect(screen.queryByText("First client stale cursor")).not.toBeInTheDocument();
  });

  it("selects stage and version through route state while preserving served truth", async () => {
    const client = fakeClient({
      documentStageEvidence: vi.fn(async (_id, version, stageName) =>
        stage({
          version,
          stage: stageName,
          detail: `${stageName} v${version}`,
          semantic_chunks:
            stageName === "chunk"
              ? {
                  state: "available",
                  reason: null,
                  chunks: [{ identifier: `doc-1:v${version}:0000`, size_bytes: version }],
                  truncated: false,
                }
              : null,
        }),
      ),
      documentJourney: vi.fn(async (_id, selector) =>
        journey({ selected_version: selector?.version ?? 7 }),
      ),
    });
    render(<Harness client={client} />);
    await screen.findByRole("heading", { name: "Operating plan" });

    await userEvent.click(screen.getByRole("button", { name: /CHUNK/ }));
    expect(await screen.findByText("chunk v7")).toBeInTheDocument();
    expect(screen.getByText("doc-1:v7:0000")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "v7" }));
    await waitFor(() =>
      expect(client.documentJourney).toHaveBeenLastCalledWith(
        "doc-1",
        { version: 7 },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() =>
      expect(client.documentStageEvidence).toHaveBeenLastCalledWith(
        "doc-1",
        7,
        "chunk",
        expect.any(AbortSignal),
      ),
    );
  });

  it("aborts an obsolete owner list and ignores its late result", async () => {
    let release: ((value: DocumentListWire) => void) | undefined;
    const first = new Promise<DocumentListWire>((resolve) => {
      release = resolve;
    });
    const signals: AbortSignal[] = [];
    const listDocuments = vi
      .fn()
      .mockImplementationOnce((_query, signal: AbortSignal) => {
        signals.push(signal);
        return first;
      })
      .mockImplementationOnce((_query, signal: AbortSignal) => {
        signals.push(signal);
        return Promise.resolve(listed("doc-2", "New owner document"));
      });
    const client = fakeClient({ listDocuments });
    const { rerender } = render(<Harness client={client} ownerId="principal-1" />);

    rerender(<Harness client={client} ownerId="principal-2" />);
    expect(signals[0]?.aborted).toBe(true);
    expect(await screen.findByText("New owner document")).toBeInTheDocument();
    release?.(listed("doc-1", "Old owner document"));
    await Promise.resolve();
    expect(screen.queryByText("Old owner document")).not.toBeInTheDocument();
  });

  it("stops presenting settled data as soon as its owner changes", async () => {
    let release: ((value: DocumentListWire) => void) | undefined;
    const nextOwner = new Promise<DocumentListWire>((resolve) => {
      release = resolve;
    });
    const client = fakeClient({
      listDocuments: vi
        .fn()
        .mockResolvedValueOnce(listed("doc-1", "Old owner document"))
        .mockImplementationOnce(() => nextOwner),
    });
    const { rerender } = render(<Harness client={client} ownerId="principal-1" />);
    expect(await screen.findByText("Old owner document")).toBeInTheDocument();

    rerender(<Harness client={client} ownerId="principal-2" />);
    expect(screen.queryByText("Old owner document")).not.toBeInTheDocument();
    expect(screen.queryByText("served version seven")).not.toBeInTheDocument();
    expect(screen.getAllByText("Loading…").length).toBeGreaterThan(0);

    release?.(listed("doc-2", "New owner document"));
    expect(await screen.findByText("New owner document")).toBeInTheDocument();
  });

  it("stops presenting settled data when the client changes for the same owner", async () => {
    let release: ((value: DocumentListWire) => void) | undefined;
    const nextClient = new Promise<DocumentListWire>((resolve) => {
      release = resolve;
    });
    const firstClient = fakeClient({
      listDocuments: vi.fn().mockResolvedValue(listed("doc-1", "First client document")),
    });
    const secondClient = fakeClient({ listDocuments: vi.fn().mockReturnValue(nextClient) });
    const { rerender } = render(<Harness client={firstClient} ownerId="principal-1" />);
    expect(await screen.findByText("First client document")).toBeInTheDocument();

    rerender(<Harness client={secondClient} ownerId="principal-1" />);
    expect(screen.queryByText("First client document")).not.toBeInTheDocument();
    expect(screen.queryByText("served version seven")).not.toBeInTheDocument();

    release?.(listed("doc-2", "Second client document"));
    expect(await screen.findByText("Second client document")).toBeInTheDocument();
  });

  it("invalidates an already-served journey for an exact selector change", async () => {
    let release: ((value: DocumentJourneyReadWire) => void) | undefined;
    const nextJourney = new Promise<DocumentJourneyReadWire>((resolve) => {
      release = resolve;
    });
    const selectorSeven = { kind: "version" as const, version: 7 };
    const client = fakeClient({
      documentJourney: vi
        .fn()
        .mockResolvedValueOnce(journey({ selected_content: "version seven private content" }))
        .mockReturnValueOnce(nextJourney),
    });
    const onRouteChange = vi.fn();
    const { rerender } = render(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{ ...INITIAL_ROUTE, document: "doc-1", selector: selectorSeven }}
        onRouteChange={onRouteChange}
      />,
    );
    expect(await screen.findByText("version seven private content")).toBeInTheDocument();

    rerender(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{
          ...INITIAL_ROUTE,
          document: "doc-1",
          selector: { kind: "as_of", asOf: "2026-08-20T20:00:00Z" },
        }}
        onRouteChange={onRouteChange}
      />,
    );
    expect(screen.queryByText("version seven private content")).not.toBeInTheDocument();
    expect(screen.queryByText("served-hash-seven")).not.toBeInTheDocument();

    release?.(
      journey({
        selected_version: 8,
        selected_content: "as-of version eight content",
        selected_content_hash: "served-hash-eight",
      }),
    );
    expect(await screen.findByText("as-of version eight content")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "As-of ISO-8601" })).toHaveValue(
      "2026-08-20T20:00:00Z",
    );
  });

  it("rejects a journey resolved before cleanup when the selector changes", async () => {
    let releaseOld: ((value: DocumentJourneyReadWire) => void) | undefined;
    let releaseNew: ((value: DocumentJourneyReadWire) => void) | undefined;
    const oldJourney = new Promise<DocumentJourneyReadWire>((resolve) => {
      releaseOld = resolve;
    });
    const newJourney = new Promise<DocumentJourneyReadWire>((resolve) => {
      releaseNew = resolve;
    });
    const client = fakeClient({
      documentJourney: vi.fn().mockReturnValueOnce(oldJourney).mockReturnValueOnce(newJourney),
    });
    const onRouteChange = vi.fn();
    const { rerender } = render(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{
          ...INITIAL_ROUTE,
          document: "doc-1",
          selector: { kind: "version", version: 7 },
        }}
        onRouteChange={onRouteChange}
      />,
    );
    await waitFor(() => expect(client.documentJourney).toHaveBeenCalledTimes(1));

    releaseOld?.(journey({ selected_content: "late version seven" }));
    rerender(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{
          ...INITIAL_ROUTE,
          document: "doc-1",
          selector: { kind: "version", version: 8 },
        }}
        onRouteChange={onRouteChange}
      />,
    );
    await Promise.resolve();
    expect(screen.queryByText("late version seven")).not.toBeInTheDocument();

    releaseNew?.(journey({ selected_version: 8, selected_content: "version eight" }));
    expect(await screen.findByText("version eight")).toBeInTheDocument();
  });

  it("rejects a late journey response after the exact selector has changed", async () => {
    let releaseOld: ((value: DocumentJourneyReadWire) => void) | undefined;
    let releaseNew: ((value: DocumentJourneyReadWire) => void) | undefined;
    const client = fakeClient({
      documentJourney: vi
        .fn()
        .mockReturnValueOnce(
          new Promise<DocumentJourneyReadWire>((resolve) => {
            releaseOld = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<DocumentJourneyReadWire>((resolve) => {
            releaseNew = resolve;
          }),
        ),
    });
    const onRouteChange = vi.fn();
    const { rerender } = render(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{
          ...INITIAL_ROUTE,
          document: "doc-1",
          selector: { kind: "version", version: 7 },
        }}
        onRouteChange={onRouteChange}
      />,
    );
    await waitFor(() => expect(client.documentJourney).toHaveBeenCalledTimes(1));

    rerender(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{
          ...INITIAL_ROUTE,
          document: "doc-1",
          selector: { kind: "version", version: 8 },
        }}
        onRouteChange={onRouteChange}
      />,
    );
    await waitFor(() => expect(client.documentJourney).toHaveBeenCalledTimes(2));
    await act(async () => {
      releaseOld?.(journey({ selected_content: "late version seven" }));
      await Promise.resolve();
    });
    expect(screen.queryByText("late version seven")).not.toBeInTheDocument();

    releaseNew?.(journey({ selected_version: 8, selected_content: "current version eight" }));
    expect(await screen.findByText("current version eight")).toBeInTheDocument();
  });

  it("never presents PARSE evidence under a newly-selected CHUNK stage", async () => {
    let releaseChunk: ((value: DocumentStageEvidenceWire) => void) | undefined;
    const chunkEvidence = new Promise<DocumentStageEvidenceWire>((resolve) => {
      releaseChunk = resolve;
    });
    const selector = { kind: "version" as const, version: 7 };
    const client = fakeClient({
      documentStageEvidence: vi
        .fn()
        .mockResolvedValueOnce(stage({ stage: "parse", detail: "Old PARSE evidence" }))
        .mockReturnValueOnce(chunkEvidence),
    });
    const onRouteChange = vi.fn();
    const { rerender } = render(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{ ...INITIAL_ROUTE, document: "doc-1", selector, stage: "parse" }}
        onRouteChange={onRouteChange}
      />,
    );
    expect(await screen.findByText("Old PARSE evidence")).toBeInTheDocument();

    rerender(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{ ...INITIAL_ROUTE, document: "doc-1", selector, stage: "chunk" }}
        onRouteChange={onRouteChange}
      />,
    );
    expect(screen.queryByText("Old PARSE evidence")).not.toBeInTheDocument();

    releaseChunk?.(stage({ stage: "chunk", detail: "Current CHUNK evidence" }));
    expect(await screen.findByText("Current CHUNK evidence")).toBeInTheDocument();
  });

  it("rejects late PARSE evidence after CHUNK is selected", async () => {
    let releaseParse: ((value: DocumentStageEvidenceWire) => void) | undefined;
    let releaseChunk: ((value: DocumentStageEvidenceWire) => void) | undefined;
    const client = fakeClient({
      documentStageEvidence: vi
        .fn()
        .mockReturnValueOnce(
          new Promise<DocumentStageEvidenceWire>((resolve) => {
            releaseParse = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<DocumentStageEvidenceWire>((resolve) => {
            releaseChunk = resolve;
          }),
        ),
    });
    const onRouteChange = vi.fn();
    const selector = { kind: "version" as const, version: 7 };
    const { rerender } = render(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{ ...INITIAL_ROUTE, document: "doc-1", selector, stage: "parse" }}
        onRouteChange={onRouteChange}
      />,
    );
    await waitFor(() => expect(client.documentStageEvidence).toHaveBeenCalledTimes(1));

    rerender(
      <StaticHarness
        client={client}
        ownerId="principal-1"
        route={{ ...INITIAL_ROUTE, document: "doc-1", selector, stage: "chunk" }}
        onRouteChange={onRouteChange}
      />,
    );
    await waitFor(() => expect(client.documentStageEvidence).toHaveBeenCalledTimes(2));
    await act(async () => {
      releaseParse?.(stage({ stage: "parse", detail: "Late PARSE evidence" }));
      await Promise.resolve();
    });
    expect(screen.queryByText("Late PARSE evidence")).not.toBeInTheDocument();

    releaseChunk?.(stage({ stage: "chunk", detail: "Current CHUNK evidence" }));
    expect(await screen.findByText("Current CHUNK evidence")).toBeInTheDocument();
  });

  it("rejects an Alice page resolved before cleanup and cannot auto-install it for Bob", async () => {
    let releaseAlice: ((value: DocumentListWire) => void) | undefined;
    let releaseBob: ((value: DocumentListWire) => void) | undefined;
    const alice = new Promise<DocumentListWire>((resolve) => {
      releaseAlice = resolve;
    });
    const bob = new Promise<DocumentListWire>((resolve) => {
      releaseBob = resolve;
    });
    const client = fakeClient({
      listDocuments: vi.fn().mockReturnValueOnce(alice).mockReturnValueOnce(bob),
    });
    const onRouteChange = vi.fn();
    const { rerender } = render(
      <StaticHarness client={client} ownerId="alice" onRouteChange={onRouteChange} />,
    );
    await waitFor(() => expect(client.listDocuments).toHaveBeenCalledTimes(1));

    releaseAlice?.(listed("alice-doc", "Alice private document"));
    rerender(<StaticHarness client={client} ownerId="bob" onRouteChange={onRouteChange} />);
    await Promise.resolve();
    expect(screen.queryByText("Alice private document")).not.toBeInTheDocument();
    expect(onRouteChange).not.toHaveBeenCalled();

    releaseBob?.(listed("bob-doc", "Bob visible document"));
    await waitFor(() =>
      expect(onRouteChange).toHaveBeenCalledWith(expect.objectContaining({ document: "bob-doc" }), {
        replace: true,
      }),
    );
  });

  it("composes inside the App shell without creating a nested main landmark", async () => {
    const client = fakeClient();
    render(
      <main>
        <Harness client={client} />
      </main>,
    );
    await screen.findByRole("heading", { name: "Operating plan" });

    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Document evidence workspace" })).toBeInTheDocument();
  });

  it("renders permission refusal separately from a retryable read error", async () => {
    const refusedClient = fakeClient({
      documentJourney: vi.fn().mockRejectedValue(new ApiError("forbidden", 403)),
    });
    const { unmount } = render(<Harness client={refusedClient} />);
    expect(await screen.findByText(/doesn’t include this document/)).toBeInTheDocument();
    unmount();

    const errorClient = fakeClient({
      documentJourney: vi.fn().mockRejectedValue(new Error("off")),
    });
    render(<Harness client={errorClient} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t load the document journey.",
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders predecessor-hidden comparison without rendering a diff", async () => {
    const client = fakeClient({
      documentJourney: vi
        .fn()
        .mockResolvedValue(
          journey({ comparison: { state: "predecessor_hidden", predecessor_version: 6 } }),
        ),
    });
    render(<Harness client={client} />);

    expect(
      await screen.findByText("The predecessor is not visible to this identity."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Version comparison for v7" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Version comparison evidence")).toBeNull();
    expect(document.querySelector(".ne-documents__diff")).toBeNull();
  });

  it("binds comparison sides and version-history audits to exact served identities", async () => {
    render(<Harness client={fakeClient()} />);

    const evidence = await screen.findByLabelText("Version comparison evidence");
    expect(within(evidence).getByText("Before").parentElement).toHaveTextContent("v6");
    expect(within(evidence).getByText("After").parentElement).toHaveTextContent("v7");
    expect(
      within(evidence).getByRole("button", { name: "Copy before v6 content hash" }),
    ).toBeInTheDocument();
    expect(
      within(evidence).getByRole("button", { name: "Copy after v7 content hash" }),
    ).toBeInTheDocument();
    expect(within(evidence).getByTitle("served-hash-six")).toBeInTheDocument();
    expect(within(evidence).getByTitle("served-hash-seven")).toBeInTheDocument();

    const history = screen.getByLabelText("Document version history");
    expect(within(history).getByRole("link", { name: "Ingest audit #8" })).toHaveAttribute(
      "href",
      "/audit?seq=8",
    );
    expect(within(history).getByRole("link", { name: "Supersede audit #9" })).toHaveAttribute(
      "href",
      "/audit?seq=9",
    );
    expect(within(history).getByRole("link", { name: "Ingest audit #10" })).toHaveAttribute(
      "href",
      "/audit?seq=10",
    );
    expect(within(history).getByText("Not superseded")).toBeInTheDocument();

    // The SERVED row is marked where it sits. Position in the table is not the
    // claim — v7 is the SECOND row in this fixture, so a marker bound to
    // versions[0] would land on the superseded row instead.
    const served = within(history).getByRole("button", { name: "v7" }).closest("tr");
    const superseded = within(history).getByRole("button", { name: "v6" }).closest("tr");
    expect(served).toHaveClass("ne-documents__versionrow--served");
    expect(superseded).not.toHaveClass("ne-documents__versionrow--served");
  });

  it("uses only the server comparison state and fails closed on missing comparison fields", async () => {
    const unavailable = fakeClient({
      documentJourney: vi.fn().mockResolvedValue(journey({ diff: "" })),
    });
    const first = render(<Harness client={unavailable} />);

    expect(
      await screen.findByText("The server did not return the available comparison diff."),
    ).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.queryByText("No content changes from the visible predecessor.")).toBeNull();
    expect(document.querySelector(".ne-documents__diff")).toBeNull();
    first.unmount();

    const unchanged = fakeClient({
      documentJourney: vi
        .fn()
        .mockResolvedValue(journey({ comparison: { state: "unchanged", predecessor_version: 6 } })),
    });
    const second = render(<Harness client={unchanged} />);
    expect(
      await screen.findByText("No content changes from the visible predecessor."),
    ).toBeInTheDocument();
    expect(document.querySelector(".ne-documents__diff")).toBeNull();
    second.unmount();

    const unidentified = fakeClient({
      documentJourney: vi
        .fn()
        .mockResolvedValue(
          journey({ comparison: { state: "available", predecessor_version: null } }),
        ),
    });
    render(<Harness client={unidentified} />);
    expect(
      await screen.findByText("The server did not identify the version used for this comparison."),
    ).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(document.querySelector(".ne-documents__diff")).toBeNull();
  });

  it("shows missing predecessor evidence as unavailable without inventing a hash", async () => {
    const currentVersion = journey().versions.find((version) => version.version_seq === 7)!;
    const client = fakeClient({
      documentJourney: vi.fn().mockResolvedValue(journey({ versions: [currentVersion] })),
    });
    render(<Harness client={client} />);

    const evidence = await screen.findByLabelText("Version comparison evidence");
    expect(within(evidence).getByText("Before").parentElement).toHaveTextContent(
      "v6 · Content hash unavailable",
    );
    expect(
      within(evidence).queryByRole("button", { name: "Copy before v6 content hash" }),
    ).toBeNull();
    expect(
      within(evidence).getByRole("button", { name: "Copy after v7 content hash" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Version comparison")).toBeInTheDocument();
  });

  it("names a first version without deriving a predecessor from version history", async () => {
    const client = fakeClient({
      documentJourney: vi
        .fn()
        .mockResolvedValue(
          journey({ comparison: { state: "first_version", predecessor_version: null } }),
        ),
    });
    render(<Harness client={client} />);

    expect(await screen.findByText("No predecessor exists for v7.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Version comparison for v7" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Version comparison evidence")).toBeNull();
    expect(document.querySelector(".ne-documents__diff")).toBeNull();
  });
});
