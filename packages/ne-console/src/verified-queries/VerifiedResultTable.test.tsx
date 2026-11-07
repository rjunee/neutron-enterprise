// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { VerifiedInvokeWire, VerifiedTemplateWire } from "../api/types";
import { VerifiedResultTable } from "./VerifiedResultTable";

const TEMPLATE = {
  template_id: "accounts",
  version: 2,
  title: "Every visible account.",
  citation_var: "account",
  label_var: "name",
} as VerifiedTemplateWire;

function result(
  replayMatch: boolean,
  rows = [["urn:ne:entity:acct-1", "Acme"]],
): VerifiedInvokeWire {
  return {
    replay_match: replayMatch,
    answer: {
      template_id: "accounts",
      template_version: 2,
      args: {},
      columns: ["account", "name"],
      rows,
      citations: rows.map(() => "urn:ne:entity:acct-1"),
      truncated: false,
      row_limit: 100,
      returned_rows: rows.length,
      total_rows: rows.length,
      truncation_reason: null,
      config_hash: "c".repeat(64),
      ontology_version: "meridian-v1",
      executed_at: "2026-08-20T12:00:00Z",
    },
  };
}

function renderResult(value: VerifiedInvokeWire, filter = "") {
  return render(
    <MemoryRouter>
      <VerifiedResultTable
        result={value}
        template={TEMPLATE}
        filter={filter}
        sort={{ key: "column-1", dir: "asc" }}
        onFilter={vi.fn()}
        onSort={vi.fn()}
      />
    </MemoryRouter>,
  );
}

describe("VerifiedResultTable", () => {
  it("shows the served human citation label, immutable id, canonical Record link, and replay verdict", () => {
    renderResult(result(true));
    expect(screen.getByRole("link", { name: "Acme" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=acct-1&mode=record&depth=1",
    );
    expect(screen.getAllByText("urn:ne:entity:acct-1").length).toBeGreaterThan(0);
    expect(screen.getByText("Server replay matched this result.")).toBeInTheDocument();
    expect(screen.getByText("meridian-v1")).toBeInTheDocument();
  });

  it("renders server divergence and zero rows as distinct truth states", () => {
    renderResult(result(false, []));
    expect(screen.getByText("This verified query returned zero rows.")).toBeInTheDocument();
    expect(screen.getByText("Server replay diverged from this result.")).toBeInTheDocument();
  });

  it("distinguishes a local shown-row filter miss from a server zero-row result", () => {
    renderResult(result(true), "does-not-match");
    expect(screen.getByText("0 of 1 returned rows")).toBeInTheDocument();
    expect(screen.getByText("No returned rows match the shown-row filter.")).toBeInTheDocument();
    expect(screen.queryByText("This verified query returned zero rows.")).not.toBeInTheDocument();
    expect(screen.queryByText("Acme")).not.toBeInTheDocument();
  });
});
