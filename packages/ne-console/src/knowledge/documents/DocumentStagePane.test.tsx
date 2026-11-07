// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { DocumentStageEvidenceWire } from "../../api/types";
import { DocumentStagePane } from "./DocumentStagePane";

function evidence(patch: Partial<DocumentStageEvidenceWire> = {}): DocumentStageEvidenceWire {
  return {
    document_id: "doc-1",
    version: 3,
    content_hash: "1234567890abcdef",
    document_type: "document",
    stage: "chunk",
    status: "complete",
    occurred_at: "2026-08-20T12:00:00Z",
    audit_seq: 42,
    detail: "Chunk receipts recorded.",
    facts: [{ key: "count", label: "Receipt count", value: 1, available: true }],
    artifacts: [
      {
        kind: "chunk",
        label: "Record chunk",
        state: "available",
        ordinal: 0,
        identifier: "opaque-chunk-1",
        size_bytes: 144,
        artifact_count: null,
        dimensions: null,
        model: null,
        index_reference: null,
        fingerprint: null,
        numeric_preview: null,
        reason: null,
      },
    ],
    semantic_chunks: {
      state: "available",
      reason: null,
      chunks: [
        { identifier: "doc-1:0000", size_bytes: 61 },
        { identifier: "doc-1:0001", size_bytes: 89 },
      ],
      truncated: false,
    },
    comparison: { state: "available", predecessor_version: 2 },
    graph_evidence: null,
    ...patch,
  };
}

function show(value: DocumentStageEvidenceWire) {
  return render(
    <MemoryRouter>
      <DocumentStagePane evidence={value} />
    </MemoryRouter>,
  );
}

describe("DocumentStagePane", () => {
  it("renders the truthful bounded numeric preview count against full dimensions", () => {
    show(
      evidence({
        stage: "embed",
        artifacts: [
          {
            kind: "embedding",
            label: "Vector artifact receipt",
            state: "available",
            ordinal: 0,
            identifier: null,
            size_bytes: null,
            artifact_count: 1,
            dimensions: 12,
            model: "private/embed-v1",
            index_reference: `sha256:${"a".repeat(64)}`,
            fingerprint: "a".repeat(64),
            numeric_preview: [0.25, -1.5, 3],
            reason: null,
          },
        ],
        semantic_chunks: null,
      }),
    );

    expect(
      screen.getByRole("heading", { name: "Numeric preview (first 3 of 12)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Vector artifact receipt 1" })).toBeInTheDocument();
    expect(screen.getByText("-1.5")).toBeInTheDocument();
  });

  it("distinguishes reconstructed semantic chunks from the indexed record receipt", () => {
    show(evidence());

    expect(screen.getByRole("heading", { name: "Record chunk 1" })).toBeInTheDocument();
    expect(screen.getByText("opaque-chunk-1")).toBeInTheDocument();
    expect(screen.getByText("144 bytes")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Semantic chunks" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Semantic chunk 1" })).toBeInTheDocument();
    expect(screen.getByText("doc-1:0000")).toBeInTheDocument();
    expect(screen.getByText(/record chunk remains the indexed T1 artifact/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Audit #42" })).toHaveAttribute(
      "href",
      "/audit?seq=42",
    );
  });

  it("renders semantic evidence unavailability without relabeling the record receipt", () => {
    show(
      evidence({
        semantic_chunks: {
          state: "unavailable",
          reason: "semantic_chunks_not_recorded",
          chunks: [],
          truncated: false,
        },
      }),
    );

    expect(
      screen.getByText("Semantic chunks were not recorded for this version."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Record chunk 1" })).toBeInTheDocument();
  });

  it("renders an audit-unbound semantic reason and unavailable stage truth", () => {
    show(
      evidence({
        status: "unavailable",
        occurred_at: null,
        audit_seq: null,
        detail: "evidence unavailable: no bound audit receipt",
        semantic_chunks: {
          state: "unavailable",
          reason: "unbound_audit_receipt",
          chunks: [],
          truncated: false,
        },
      }),
    );

    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("Evidence unavailable — no completion implied")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Audit unavailable")).toBeInTheDocument();
    expect(screen.getByText(/no committed audit receipt is bound/i)).toBeInTheDocument();
    expect(screen.queryByText("Pending")).not.toBeInTheDocument();
    expect(screen.queryByText("8/20/2026", { exact: false })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Audit #/ })).not.toBeInTheDocument();
  });

  it.each([
    ["empty", "No artifacts were recorded."],
    ["skipped", "Source connector did not emit a receipt."],
    ["unavailable", "Not recorded for this version."],
    ["withheld", "Withheld for this identity."],
  ] as const)("renders the %s artifact state without inventing facts", (state, message) => {
    const reason = state === "skipped" ? message : state === "unavailable" ? "not_recorded" : null;
    show(
      evidence({
        artifacts: [
          { ...evidence().artifacts[0]!, state, identifier: null, size_bytes: null, reason },
        ],
      }),
    );

    expect(screen.getByText(state)).toBeInTheDocument();
    expect(
      screen.getByText(state === "withheld" ? /Access refused:/ : message),
    ).toBeInTheDocument();
    expect(screen.queryByText("opaque-chunk-1")).not.toBeInTheDocument();
  });

  it("distinguishes unavailable facts from served null values", () => {
    show(
      evidence({
        facts: [
          { key: "hidden", label: "Hidden fact", value: "must not render", available: false },
          { key: "null", label: "Served null", value: null, available: true },
        ],
      }),
    );

    expect(
      within(screen.getByText("Hidden fact").parentElement!).getByText("Not available"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByText("Served null").parentElement!).getByText("—"),
    ).toBeInTheDocument();
    expect(screen.queryByText("must not render")).not.toBeInTheDocument();
  });

  it.each([
    ["empty", "no visible graph evidence"],
    ["skipped", "graph capability disabled"],
    ["unavailable", "version-bound graph evidence receipt unavailable"],
  ] as const)("renders honest GRAPH %s truth without a destination", (state, reason) => {
    const { container } = show(
      evidence({
        stage: "graph",
        status: state === "skipped" ? "skipped" : "complete",
        facts: [],
        artifacts: [],
        graph_evidence: { state, reason, entities: [], relationships: [], truncated: false },
      }),
    );

    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(
      screen.getByText(
        state === "empty"
          ? "No visible graph evidence"
          : state === "skipped"
            ? "Graph stage skipped"
            : "Graph evidence unavailable",
      ),
    ).toBeInTheDocument();
    expect(container.querySelector('a[href*="graph"]')).toBeNull();
  });

  it("links served entities to Record and relationships to canonical neighborhoods", () => {
    const entities = Array.from({ length: 21 }, (_, index) => ({
      key: `opaque/entity+${index}`,
      entity_class: index === 0 ? "Procedure" : "Policy",
      display_name: `Entity ${index}`,
      source_system: "m365-sharepoint",
    }));
    const relationships = Array.from({ length: 21 }, (_, index) => ({
      key: `opaque/relationship+${index}`,
      edge_kind: "REFERENCES",
      subject: entities[index]!,
      object: entities[(index + 1) % entities.length]!,
    }));
    show(
      evidence({
        stage: "graph",
        facts: [],
        artifacts: [],
        graph_evidence: {
          state: "available",
          reason: null,
          entities,
          relationships,
          truncated: true,
        },
      }),
    );

    expect(screen.getAllByRole("link", { name: "Open record" })[0]).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=opaque%2Fentity%2B0&mode=record&depth=1",
    );
    expect(screen.getAllByRole("link", { name: "Subject neighborhood" })[0]).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=opaque%2Fentity%2B0&mode=graph&depth=1",
    );
    expect(screen.getAllByRole("link", { name: "Object neighborhood" })[0]).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=opaque%2Fentity%2B1&mode=graph&depth=1",
    );
    expect(screen.getByText("Evidence key: opaque/entity+0")).toBeInTheDocument();
    expect(screen.getByText("Evidence key: opaque/relationship+0")).toBeInTheDocument();
    expect(screen.queryByText("Entity 20")).not.toBeInTheDocument();
    expect(
      screen.getByText("Showing the first 20 served rows per evidence kind."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The governed server projection was truncated after ACL filtering."),
    ).toBeInTheDocument();
  });

  it("offers only the served predecessor from SUPERSEDE evidence", async () => {
    const onSelectVersion = vi.fn();
    show(
      evidence({
        stage: "supersede",
        comparison: { state: "available", predecessor_version: 2 },
        facts: [],
        artifacts: [],
      }),
    ).rerender(
      <MemoryRouter>
        <DocumentStagePane
          evidence={evidence({
            stage: "supersede",
            comparison: { state: "available", predecessor_version: 2 },
            facts: [],
            artifacts: [],
          })}
          onSelectVersion={onSelectVersion}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Served predecessor v2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open version comparison" })).toHaveAttribute(
      "href",
      "#ne-document-diff-heading",
    );
    await userEvent.click(screen.getByRole("button", { name: "Open predecessor" }));
    expect(onSelectVersion).toHaveBeenCalledWith(2);
  });

  it("names a first version without inventing a predecessor", () => {
    const { container } = show(
      evidence({
        stage: "supersede",
        comparison: { state: "first_version", predecessor_version: null },
        facts: [],
        artifacts: [],
      }),
    );

    expect(screen.getByText("First version — nothing superseded.")).toBeInTheDocument();
    expect(container.querySelector('a[href*="version"]')).toBeNull();
  });

  it.each([
    [
      "parse",
      { key: "parser", label: "Parser identity", value: "ne.parse/pdf@2", available: true },
    ],
    [
      "acl",
      { key: "acl_result", label: "Imported ACL result", value: "complete", available: true },
    ],
  ] as const)(
    "renders %s facts as complete evidence without a false artifact gap",
    (stage, fact) => {
      show(evidence({ stage, facts: [fact], artifacts: [], semantic_chunks: null }));

      expect(screen.getByText(fact.label)).toBeInTheDocument();
      expect(screen.getByText(String(fact.value))).toBeInTheDocument();
      expect(screen.getByText("Completed")).toBeInTheDocument();
      expect(screen.queryByText("This stage has no artifact projection.")).not.toBeInTheDocument();
    },
  );
});
