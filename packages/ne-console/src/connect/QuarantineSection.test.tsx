// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TableStatus } from "../primitives/DataTable";
import { QuarantineSection } from "./QuarantineSection";

function renderSection(total: number | null, status: TableStatus) {
  return render(
    <QuarantineSection
      total={total}
      reasons={[]}
      status={status}
      items={null}
      onOpenItem={() => undefined}
      selectedItemId={null}
    />,
  );
}

describe("QuarantineSection", () => {
  it("keeps a cold load neutral with one truthful announcement", () => {
    renderSection(null, "loading");

    const heading = screen.getByRole("heading", { name: "Quarantine" });
    expect(heading).not.toHaveAttribute("title");
    expect(screen.queryByText("Quarantine — total unavailable")).not.toBeInTheDocument();
    const announcements = screen.getAllByRole("status");
    expect(announcements).toHaveLength(1);
    expect(announcements[0]).toHaveTextContent("Loading quarantine reasons");
  });

  it("labels a terminal unreadable total as unavailable", () => {
    renderSection(null, "ok");

    expect(screen.getByRole("heading", { name: "Quarantine — total unavailable" })).toHaveAttribute(
      "title",
      "The quarantine store could not be read",
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("preserves a readable terminal zero", () => {
    renderSection(0, "ok");

    const heading = screen.getByRole("heading", { name: "Quarantine — 0 items" });
    expect(heading).not.toHaveAttribute("title");
    expect(screen.getByText("Nothing in quarantine.")).toBeInTheDocument();
  });
});
