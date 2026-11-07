// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — Library › Reference. Three sections, three independent governed reads.
//
// The section-isolation test is the one that matters: one section's refusal
// must not blank the other two, and a section the DEPLOYMENT switched off must
// say so rather than borrowing either the refusal or the error.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { ONTOLOGY_VIOLATIONS_UNCONFIGURED } from "../test/mocks/ontologyFixtures";
import { LibraryPage } from "./LibraryPage";

function renderReference() {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={["/library?tab=reference"]}>
        <LibraryPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

/** The first cell of every body row of a named table — the row's identity. */
function firstCells(name: string): string[] {
  return within(screen.getByRole("table", { name }))
    .getAllByRole("row")
    .slice(1)
    .map((row) => (within(row).getAllByRole("cell")[0]?.textContent ?? "").trim());
}

describe("Reference — ontology classes", () => {
  it("defaults to Label A–Z and puts the unlabelled class last", async () => {
    renderReference();
    await screen.findByRole("table", { name: "Ontology classes" });
    await waitFor(() =>
      expect(firstCells("Ontology classes")).toEqual([
        "Account",
        "Agent",
        "continuant",
        "Document",
        "entity",
        "Information Content Entity",
        "Person",
        // The server sent no label for this class; the em dash is the whole of
        // what the console will say about that.
        "—",
      ]),
    );
  });

  it("captions the section with the source versions the server reported", async () => {
    renderReference();
    expect(await screen.findByText("bfo 2020 · cco 2.1 · neop 0.1")).toBeInTheDocument();
  });

  it("filters to exactly the matching class", async () => {
    renderReference();
    await screen.findByRole("table", { name: "Ontology classes" });
    await userEvent.type(screen.getByLabelText("Filter classes…"), "Person");
    await waitFor(() => expect(firstCells("Ontology classes")).toEqual(["Person"]));
    expect(screen.getByText("1 classes")).toBeInTheDocument();
  });
});

describe("Reference — shapes", () => {
  it("renders each compiled shape, and says nothing about a shape with no target class", async () => {
    renderReference();
    const table = await screen.findByRole("table", { name: "Shapes" });
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) =>
        within(row)
          .getAllByRole("cell")
          .map((cell) => (cell.textContent ?? "").trim()),
      );
    expect(rows).toEqual([
      [
        "https://neutron.enterprise/schema/shapes/AccountShape",
        "https://neutron.enterprise/schema/operational/Account",
        "per-write",
      ],
      ["https://neutron.enterprise/schema/shapes/GlobalCardinalityShape", "—", "global"],
    ]);
  });
});

describe("Reference — quarantined changes", () => {
  it("lists the parked batches newest first, and marks the whole-graph ones", async () => {
    renderReference();
    await screen.findByRole("table", { name: "Quarantined changes" });
    await waitFor(() =>
      expect(firstCells("Quarantined changes")).toEqual(["quar-1c0d55ee8801", "quar-7f3a19b2c4d5"]),
    );
    const table = screen.getByRole("table", { name: "Quarantined changes" });
    expect(within(table).getAllByText("Yes")).toHaveLength(2);
  });

  it("says the lane is not configured when the server says so", async () => {
    server.use(
      http.get("*/api/v1/ontology/violations", () =>
        HttpResponse.json(ONTOLOGY_VIOLATIONS_UNCONFIGURED),
      ),
    );
    renderReference();
    expect(
      await screen.findByText("Violation tracking isn't configured on this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Quarantined changes" })).toBeNull();
  });
});

describe("Reference — one section's answer is not another's", () => {
  it("refuses only the section the server refused, and keeps the rest readable", async () => {
    server.use(
      http.get("*/api/v1/ontology/shapes", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderReference();
    await screen.findByRole("table", { name: "Ontology classes" });

    const shapes = screen.getByRole("table", { name: "Shapes" });
    expect(within(shapes).getByText(/Your role doesn.t include the ontology/)).toBeInTheDocument();
    // The other two sections answered, and still show their own data.
    await waitFor(() => expect(firstCells("Ontology classes")[0]).toBe("Account"));
    expect(screen.getByRole("table", { name: "Quarantined changes" })).toBeInTheDocument();
    // The verified runner now has one canonical home, never a hidden fourth section here.
    expect(screen.queryByRole("table", { name: "Verified answers" })).toBeNull();
  });

  it("does not call a broken section read a role boundary", async () => {
    server.use(
      http.get("*/api/v1/ontology/classes", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderReference();
    expect(await screen.findByText("Couldn’t load ontology classes.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include the ontology/)).toBeNull();
  });
});
