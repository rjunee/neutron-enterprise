// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — the Library shell: the title row, the four tabs, and the rule that the
// tab is addressable so a palette entry and a link can both land on one.
//
// The last test is the §21.3 acceptance check, run against the RENDERED screen
// rather than the source: no visible string, tooltip, placeholder or accessible
// name may name a route, a verb, a build ticket or a source file. The screen
// this replaces failed that on every one of its states.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { LibraryPage } from "./LibraryPage";

function renderAt(entry: string) {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={[entry]}>
        <LibraryPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("Library — the shell", () => {
  it("is a title row and four tabs, opening on Entities", () => {
    renderAt("/library");
    expect(screen.getByRole("heading", { name: "Library", level: 1 })).toBeInTheDocument();
    const tabs = screen.getByRole("tablist", { name: "Library sections" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((t) => t.textContent),
    ).toEqual(["Documents", "Entities", "Graph", "Reference"]);
    expect(screen.getByRole("tab", { name: "Entities" })).toHaveAttribute("aria-selected", "true");
  });

  it("has no prose between the title and the first control", () => {
    renderAt("/library");
    const title = screen.getByRole("heading", { name: "Library", level: 1 });
    // The title row holds the heading and the tabs, and has no room for a
    // purpose sentence — §1.4 removes the slot rather than the sentence.
    const titleRow = title.closest("header")!;
    expect(titleRow.querySelector("p")).toBeNull();
  });

  it("opens the tab a link names", async () => {
    renderAt("/library?tab=reference");
    expect(screen.getByRole("tab", { name: "Reference" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("table", { name: "Ontology classes" })).toBeInTheDocument();
  });

  it("moves between tabs on click", async () => {
    renderAt("/library");
    await userEvent.click(screen.getByRole("tab", { name: "Graph" }));
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Anchor an entity to draw its neighborhood.")).toBeInTheDocument();
  });
});

/** Everything a person can read or hear on the screen right now. */
function visibleText(): string {
  const parts: string[] = [document.body.textContent ?? ""];
  for (const el of document.body.querySelectorAll("[title],[aria-label],[placeholder]")) {
    parts.push(
      el.getAttribute("title") ?? "",
      el.getAttribute("aria-label") ?? "",
      el.getAttribute("placeholder") ?? "",
    );
  }
  return parts.join("\n");
}

const INTERNALS: ReadonlyArray<[string, RegExp]> = [
  ["an endpoint path", /\/v1\//],
  ["an HTTP verb", /\b(?:GET|POST|PATCH|DELETE) \//],
  ["a status code", /\bHTTP [0-9]/],
  ["a build ticket", /NE-[0-9]{2,}/],
  ["a source file", /\.(?:tsx?|py)\b/],
];

describe("Library — no repo internals reach the screen", () => {
  it("holds on every tab, in every state it can reach without a server", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ nodes: [], edges: [], truncated: false }),
      ),
    );
    for (const entry of [
      "/library",
      "/library?tab=documents",
      "/library?entity=nothing-here",
      "/library?tab=graph",
      "/library?tab=reference",
    ]) {
      const { unmount } = renderAt(entry);
      if (entry.includes("reference")) {
        await screen.findByRole("table", { name: "Ontology classes" });
      }
      await waitFor(() => {
        const text = visibleText();
        for (const [what, pattern] of INTERNALS) {
          expect(pattern.test(text), `${entry} leaked ${what}`).toBe(false);
        }
      });
      unmount();
    }
  });
});
