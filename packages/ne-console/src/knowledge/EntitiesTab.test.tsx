// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — Library › Entities.
//
// Every ordering and filtering assertion here reads ROW IDENTITY, never a row
// count: a count passes for the wrong four rows, and "the filter narrowed to
// four" is exactly the assertion that lets a wrong filter ship.
//
// The load-state assertions are the point of this file. A 403 must render the
// server's refusal; a 500 must NOT — telling someone their role excludes
// something, when what actually happened is that a read broke, is a false
// statement about authorization, and it is the defect this screen is built to
// not have.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { GraphNodeWire, SubgraphWire } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { LibraryPage } from "./LibraryPage";

const ACME: GraphNodeWire = {
  key: "acct-acme",
  entity_class: "Account",
  display_name: "Acme Corp",
  source_system: "crm",
  source_ref: "crm/acct-1",
  properties: { region: "EU", tier: "gold" },
  assurance: "source",
};

const NEIGHBORHOOD: SubgraphWire = {
  nodes: [
    ACME,
    {
      key: "person-dana",
      entity_class: "Person",
      display_name: "Dana Ops",
      source_system: "workday",
      source_ref: "hr/1",
      properties: {},
      assurance: "source",
    },
    {
      key: "person-sam",
      entity_class: "Person",
      display_name: "Sam Rep",
      source_system: "workday",
      source_ref: "hr/2",
      properties: {},
      assurance: "source",
    },
    {
      key: "doc-runbook",
      entity_class: "Document",
      display_name: "Ops Runbook",
      source_system: "sharepoint",
      source_ref: "sp/runbook",
      properties: {},
      assurance: "source",
    },
  ],
  // Served OUT of alphabetical order, so the default sort is a real claim.
  edges: [
    {
      key: "e-works",
      edge_kind: "WORKS_AT",
      from_key: "person-dana",
      to_key: "acct-acme",
      source_system: "workday",
      source_ref: "hr/emp-1",
      assurance: "source",
    },
    {
      key: "e-partner",
      edge_kind: "PARTNER",
      from_key: "person-sam",
      to_key: "acct-acme",
      source_system: "crm",
      source_ref: "llm:0123456789abcdef:sharepoint/deal-7",
      assurance: "extracted",
    },
    {
      key: "e-about",
      edge_kind: "ABOUT",
      from_key: "acct-acme",
      to_key: "doc-runbook",
      source_system: "sharepoint",
      source_ref: "sp/about-1",
      assurance: "source",
    },
    {
      key: "e-owns",
      edge_kind: "OWNS",
      from_key: "acct-acme",
      to_key: "person-sam",
      source_system: "crm",
      source_ref: "crm/owns-1",
      assurance: "source",
    },
  ],
  truncated: false,
};

function renderAt(entry: string) {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={[entry]}>
        <LibraryPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

function neighborhood(body: SubgraphWire) {
  return http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json(body));
}

/** The Relations table's rows, as the pair that identifies each one: the
 * relation and the entity at the other end. */
async function relationRowsOnScreen(): Promise<string[][]> {
  const table = await screen.findByRole("table", { name: "Relations" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole("cell")
        .slice(0, 3)
        // The direction cell carries a glyph for the eye and a word for the
        // screen reader; the word is the assertion.
        .map((cell) => (cell.textContent ?? "").replace(/[\u2190\u2192]/g, "").trim()),
    );
}

describe("Entities — the idle and not-found states", () => {
  // With nothing open the body is the directory (EntityDirectory.test.tsx owns
  // it). The shared fixture catalog registers only templates that take an
  // argument, so there is nothing runnable without inventing one — and THAT is
  // the case the spec's idle line was written for.
  it("falls back to the spec's idle line when the deployment has no browsable index", async () => {
    renderAt("/library");
    expect(
      await screen.findByText(
        "No verified query on this deployment can browse without an input yet — queries are " +
          "authored per engagement. Open an entity from a citation in Agents or Search.",
      ),
    ).toBeInTheDocument();
  });

  it("says where a key comes from when the graph holds none", async () => {
    server.use(neighborhood({ nodes: [], edges: [], truncated: false }));
    renderAt("/library?entity=nope");
    expect(
      await screen.findByText(
        "No entity with that key. Open one from a citation in Agents or Search.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Entities — the record", () => {
  it("reads the entity's own 1-hop neighborhood and renders its header and properties", async () => {
    let seen: URL | null = null;
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(NEIGHBORHOOD);
      }),
    );
    renderAt("/library?entity=acct-acme");

    expect(await screen.findByRole("heading", { name: "Acme Corp" })).toBeInTheDocument();
    expect(screen.getByText("Account")).toBeInTheDocument();
    await waitFor(() => expect(seen).not.toBeNull());
    expect(seen!.searchParams.get("entity")).toBe("acct-acme");
    expect(seen!.searchParams.get("hops")).toBe("1");

    const properties = screen.getByRole("table", { name: "Properties" });
    expect(within(properties).getByText("region")).toBeInTheDocument();
    expect(within(properties).getByText("EU")).toBeInTheDocument();
    expect(screen.queryByText(/This neighborhood was capped/)).toBeNull();
  });

  it("discloses when the server capped the entity's relation neighborhood", async () => {
    server.use(neighborhood({ ...NEIGHBORHOOD, truncated: true }));
    renderAt("/library?entity=acct-acme");

    expect(
      await screen.findByText(
        "This neighborhood was capped by the server. More visible relations may exist.",
      ),
    ).toHaveAttribute("role", "status");
    expect(await relationRowsOnScreen()).toHaveLength(4);
  });

  it("accepts the urn form of the same key, so a verified answer's citation lands", async () => {
    let seen: string | null = null;
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        seen = new URL(request.url).searchParams.get("entity");
        return HttpResponse.json(NEIGHBORHOOD);
      }),
    );
    renderAt("/library?entity=urn:ne:entity:acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });
    expect(seen).toBe("acct-acme");
  });

  it("flags a model-extracted entity as unconfirmed, and says what that means", async () => {
    server.use(
      neighborhood({
        ...NEIGHBORHOOD,
        nodes: [{ ...ACME, assurance: "extracted" }, ...NEIGHBORHOOD.nodes.slice(1)],
      }),
    );
    renderAt("/library?entity=acct-acme");
    const heading = await screen.findByRole("heading", { name: "Acme Corp" });
    const pill = within(heading.parentElement!).getByText("UNCONFIRMED");
    expect(pill.closest("[title]")).toHaveAttribute(
      "title",
      "Extracted by a model and not yet confirmed against a source",
    );
  });

  it("does not flag an entity the server vouched for", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });
    const header = screen.getByRole("heading", { name: "Acme Corp" }).parentElement!;
    expect(within(header).queryByText("UNCONFIRMED")).toBeNull();
  });
});

describe("Entities — the relations table", () => {
  it("defaults to Relation A–Z, whatever order the server sent", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    const rows = await relationRowsOnScreen();
    expect(rows.map((cells) => cells[0])).toEqual(["ABOUT", "OWNS", "PARTNER", "WORKS_AT"]);
    // …and the direction is read off each edge, not assumed.
    expect(rows.map((cells) => cells[1])).toEqual(["Outgoing", "Outgoing", "Incoming", "Incoming"]);
    expect(rows.map((cells) => cells[2])).toEqual([
      "Ops Runbook",
      "Sam Rep",
      "Sam Rep",
      "Dana Ops",
    ]);
  });

  it("sorts by Entity when that header is used, and marks the active column", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    await relationRowsOnScreen();
    const table = screen.getByRole("table", { name: "Relations" });
    await userEvent.click(within(table).getByRole("button", { name: /Entity/ }));

    expect(within(table).getByRole("columnheader", { name: /Entity/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    const rows = await relationRowsOnScreen();
    expect(rows.map((cells) => cells[2])).toEqual([
      "Dana Ops",
      "Ops Runbook",
      "Sam Rep",
      "Sam Rep",
    ]);
  });

  it("narrows to exactly the rows of the chosen class", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    await relationRowsOnScreen();

    await userEvent.selectOptions(screen.getByLabelText("Class"), "Document");
    expect(await relationRowsOnScreen()).toEqual([["ABOUT", "Outgoing", "Ops Runbook"]]);

    await userEvent.selectOptions(screen.getByLabelText("Class"), "Person");
    expect((await relationRowsOnScreen()).map((cells) => cells[0])).toEqual([
      "OWNS",
      "PARTNER",
      "WORKS_AT",
    ]);

    // The active filter is visible beside the table it shortened, and clearing
    // it restores every row.
    await userEvent.click(screen.getByRole("button", { name: "Clear Class: Person" }));
    expect((await relationRowsOnScreen()).map((cells) => cells[0])).toEqual([
      "ABOUT",
      "OWNS",
      "PARTNER",
      "WORKS_AT",
    ]);
  });

  it("flags the relation the model asserted, and leaves the vouched ones unmarked", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    const table = await screen.findByRole("table", { name: "Relations" });
    const rows = within(table).getAllByRole("row").slice(1);
    const marked = rows
      .filter((row) => within(row).queryByText("UNCONFIRMED") !== null)
      .map((row) => (within(row).getAllByRole("cell")[0]?.textContent ?? "").trim());
    expect(marked).toEqual(["PARTNER"]);
  });

  it("opens the asserting record's provenance, leading with the evidencing ref", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    const table = await screen.findByRole("table", { name: "Relations" });
    const partner = within(table)
      .getAllByRole("row")
      .find((row) => (row.textContent ?? "").includes("PARTNER"))!;
    await userEvent.click(partner);

    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("sharepoint/deal-7")).toHaveAttribute(
      "title",
      "llm:0123456789abcdef:sharepoint/deal-7",
    );
    expect(within(pane).getByText("Incoming")).toBeInTheDocument();
  });
});

describe("Entities — a refusal is a 403 and nothing else", () => {
  it("renders the SERVER's refusal when the server refuses", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderAt("/library?entity=acct-acme");
    expect(
      await screen.findByText(/Your role doesn.t include the knowledge graph/),
    ).toBeInTheDocument();
  });

  it("does NOT call a broken read a role boundary", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt("/library?entity=acct-acme");
    expect(await screen.findByText("Couldn’t load the entity record.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("names the deployment fact when the graph is switched off here", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "graph tier not enabled" }, { status: 404 }),
      ),
    );
    renderAt("/library?entity=acct-acme");
    expect(
      await screen.findByText("The knowledge graph isn't enabled on this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });
});

describe("Entities — paths to another entity", () => {
  it("asks the server for the paths and renders each hop chain in traversal order", async () => {
    let body: unknown = null;
    server.use(
      neighborhood(NEIGHBORHOOD),
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          paths: [
            {
              nodes: [NEIGHBORHOOD.nodes[0]!, NEIGHBORHOOD.nodes[1]!, NEIGHBORHOOD.nodes[2]!],
              edges: [NEIGHBORHOOD.edges[0]!, NEIGHBORHOOD.edges[3]!],
            },
          ],
          truncated: true,
        });
      }),
    );
    renderAt("/library?entity=acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });

    await userEvent.click(screen.getByRole("button", { name: "Paths to another entity…" }));
    await userEvent.type(screen.getByLabelText("Entity key"), "person-sam");
    await userEvent.click(screen.getByRole("button", { name: "Find paths" }));

    await waitFor(() => expect(body).toMatchObject({ from: "acct-acme", to: "person-sam" }));
    const chain = await screen.findByRole("listitem");
    expect(
      within(chain)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["Acme Corp", "Dana Ops", "Sam Rep"]);
    expect((chain.textContent ?? "").replace(/\s+/g, " ")).toContain("WORKS_AT");
    expect(
      screen.getByText(
        "These path results were capped by the server. More visible paths may exist.",
      ),
    ).toHaveAttribute("role", "status");
  });

  it("does not turn an empty visible result into an existence or connectivity claim", async () => {
    server.use(
      neighborhood(NEIGHBORHOOD),
      http.post("*/api/v1/graph/paths", () => HttpResponse.json({ paths: [], truncated: false })),
    );
    renderAt("/library?entity=acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });
    await userEvent.click(screen.getByRole("button", { name: "Paths to another entity…" }));
    await userEvent.type(screen.getByLabelText("Entity key"), "person-sam");
    await userEvent.click(screen.getByRole("button", { name: "Find paths" }));

    expect(
      await screen.findByText(
        "No visible path was returned. The destination may be unknown, outside your access, " +
          "disconnected, or beyond the served search window.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("No path between these entities.")).toBeNull();
    expect(screen.queryByText(/These path results were capped/)).toBeNull();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });

  it("keeps an empty result distinct from the server's independent truncation verdict", async () => {
    server.use(
      neighborhood(NEIGHBORHOOD),
      http.post("*/api/v1/graph/paths", () => HttpResponse.json({ paths: [], truncated: true })),
    );
    renderAt("/library?entity=acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });
    await userEvent.click(screen.getByRole("button", { name: "Paths to another entity…" }));
    await userEvent.type(screen.getByLabelText("Entity key"), "person-hidden");
    await userEvent.click(screen.getByRole("button", { name: "Find paths" }));

    expect(
      await screen.findByText(
        "No visible path was returned. The destination may be unknown, outside your access, " +
          "disconnected, or beyond the served search window.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "These path results were capped by the server. More visible paths may exist.",
      ),
    ).toHaveAttribute("role", "status");
  });
});

describe("Entities — leaving the record", () => {
  it("Open in graph anchors the Graph tab on this entity", async () => {
    server.use(neighborhood(NEIGHBORHOOD));
    renderAt("/library?entity=acct-acme");
    await screen.findByRole("heading", { name: "Acme Corp" });
    await userEvent.click(screen.getByRole("button", { name: "Open in graph" }));

    expect(await screen.findByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByLabelText("Anchor an entity…")).toHaveValue("acct-acme");
  });
});
