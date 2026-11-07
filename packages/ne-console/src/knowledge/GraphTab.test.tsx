// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — Library › Graph.
//
// Assertions route through the accessible DOM layer, never canvas pixels: jsdom
// has no 2D context, and a test that could only see the picture could not see
// anything at all.
//
// The hop control is the load-bearing case here. Two governed reads can draw a
// neighborhood and they cap hops differently; the select must offer what the
// route the current anchor uses will actually accept, because a control that
// offers 4 and gets 2 is a control that lies quietly.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { GraphNodeWire, SubgraphWire } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { GraphChamber } from "./GraphChamber";
import { LibraryPage } from "./LibraryPage";
import { toChamberGraph } from "./chamberSim";

function node(key: string, name: string, entityClass: string): GraphNodeWire {
  return {
    key,
    entity_class: entityClass,
    display_name: name,
    source_system: "crm",
    source_ref: `crm/${key}`,
    properties: {},
    assurance: "source",
  };
}

const SUB: SubgraphWire = {
  nodes: [
    node("acct-acme", "Acme Corp", "Account"),
    node("person-dana", "Dana Ops", "Person"),
    node("doc-runbook", "Ops Runbook", "Document"),
  ],
  edges: [
    {
      key: "e1",
      edge_kind: "WORKS_AT",
      from_key: "person-dana",
      to_key: "acct-acme",
      source_system: "workday",
      source_ref: "hr/1",
      assurance: "source",
    },
    {
      key: "e2",
      edge_kind: "ABOUT",
      from_key: "doc-runbook",
      to_key: "acct-acme",
      source_system: "sharepoint",
      source_ref: "sp/1",
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

describe("Graph — anchoring", () => {
  it("says what would draw something, and reads nothing until something is anchored", () => {
    renderAt("/library?tab=graph");
    expect(screen.getByText("Anchor an entity to draw its neighborhood.")).toBeInTheDocument();
  });

  it("an anchor link opens the Graph tab and draws exactly the nodes the server returned", async () => {
    server.use(http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json(SUB)));
    renderAt("/library?anchor=acct-acme");

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    const list = await screen.findByRole("list", { name: "Graph nodes" });
    expect(
      within(list)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Acme Corp · Account", "Dana Ops · Person", "Ops Runbook · Document"]);
    const anchor = within(list).getByRole("button", { name: "Acme Corp · Account" });
    expect(anchor).toHaveAttribute("data-lit", "true");
    expect(anchor).toHaveAccessibleDescription("Anchor node");
    expect(within(list).getByRole("button", { name: "Dana Ops · Person" })).not.toHaveAttribute(
      "data-lit",
    );
    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();
  });

  it("discloses a partial window beside the count, in the spec's words", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json({ ...SUB, truncated: true })),
    );
    renderAt("/library?anchor=acct-acme");
    expect(
      await screen.findByText("Showing the first 3 nodes — narrow the anchor to see the rest."),
    ).toBeInTheDocument();
  });

  it("does not disclose a partial window when the server did not report one", async () => {
    server.use(http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json(SUB)));
    renderAt("/library?anchor=acct-acme");
    await screen.findByRole("list", { name: "Graph nodes" });
    expect(screen.queryByText(/Showing the first/)).toBeNull();
  });

  it("also discloses a capped neighborhood in the selected entity record", async () => {
    let reads = 0;
    server.use(
      http.get("*/api/v1/graph/neighborhood", () => {
        reads += 1;
        return HttpResponse.json(reads === 1 ? SUB : { ...SUB, truncated: true });
      }),
    );
    renderAt("/library?anchor=acct-acme");
    const graphNodes = await screen.findByRole("list", { name: "Graph nodes" });
    await userEvent.click(within(graphNodes).getByRole("button", { name: "Acme Corp · Account" }));

    expect(
      await screen.findByText(
        "This neighborhood was capped by the server. More visible relations may exist.",
      ),
    ).toHaveAttribute("role", "status");
    expect(
      screen.queryByText("Showing the first 3 nodes — narrow the anchor to see the rest."),
    ).toBeNull();
  });
});

describe("Graph — the hop control offers what the route accepts", () => {
  it("offers four hops on a single-key anchor, and sends the chosen one", async () => {
    const seen: string[] = [];
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        seen.push(new URL(request.url).searchParams.get("hops") ?? "");
        return HttpResponse.json(SUB);
      }),
    );
    renderAt("/library?anchor=acct-acme");
    await screen.findByRole("list", { name: "Graph nodes" });

    const select = screen.getByLabelText("Hops");
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["1", "2", "3", "4"]);
    await userEvent.selectOptions(select, "3");
    await waitFor(() => expect(seen).toContain("3"));
  });

  it("offers only what the citation-anchored route accepts, and uses that route", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/graph/query", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(SUB);
      }),
    );
    renderAt("/library?citation=ref%2Fdoc-alpha");
    await screen.findByRole("list", { name: "Graph nodes" });

    const select = screen.getByLabelText("Hops");
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["1", "2"]);
    expect(body).toMatchObject({ anchor_citations: ["ref/doc-alpha"] });
  });
});

describe("Graph — an anchor that resolves to nothing fails LOUD, naming the anchor", () => {
  const EMPTY: SubgraphWire = { nodes: [], edges: [], truncated: false };

  it("a citation anchor names the ref that found nothing, and why it can", async () => {
    // The owner followed Search's "View in Library" here and read a generic
    // invitation over 0 nodes — a silent dead end dressed as a design
    // decision (SPEC §12 item 8's shape). The miss must say WHICH citation
    // and WHY a citation can miss.
    server.use(http.post("*/api/v1/graph/query", () => HttpResponse.json(EMPTY)));
    renderAt("/library?citation=acme%2Fdocs%2Fdoc-0169");
    expect(
      await screen.findByText(
        'No graph entity cites "acme/docs/doc-0169" as evidence. ' +
          "This citation points at a document, and only sources an entity was extracted from resolve here.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("No entity with that key.", { exact: false })).toBeNull();
  });

  it("a key anchor names the key that found nothing", async () => {
    server.use(http.post("*/api/v1/graph/query", () => HttpResponse.json(EMPTY)));
    renderAt("/library?anchor=acct-ghost&anchor=acct-ghost2");
    expect(
      await screen.findByText(
        'No entity with key "acct-ghost", "acct-ghost2". Open one from a citation in Agents or Search.',
      ),
    ).toBeInTheDocument();
  });

  it("control: an anchor that resolves draws, and no miss copy renders", async () => {
    server.use(http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json(SUB)));
    renderAt("/library?anchor=acct-acme");
    await screen.findByRole("list", { name: "Graph nodes" });
    expect(screen.queryByText(/No graph entity cites|No entity with key/)).toBeNull();
  });
});

describe("Graph — what a failed read means", () => {
  it("renders the SERVER's refusal on a 403", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderAt("/library?anchor=acct-acme");
    expect(
      await screen.findByText(/Your role doesn.t include the knowledge graph/),
    ).toBeInTheDocument();
  });

  it("calls a broken read broken, and never a role boundary", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt("/library?anchor=acct-acme");
    expect(await screen.findByText("Couldn’t load the graph.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });

  it("names the deployment fact when the graph tier is off here", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "graph tier not enabled" }, { status: 404 }),
      ),
    );
    renderAt("/library?anchor=acct-acme");
    expect(
      await screen.findByText("The knowledge graph isn't enabled on this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });
});

describe("Graph — a node opens the same entity record", () => {
  it("opens the record in the detail pane, read from that entity's own neighborhood", async () => {
    const asked: string[] = [];
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("entity") ?? "");
        return HttpResponse.json(SUB);
      }),
    );
    renderAt("/library?anchor=acct-acme");
    const list = await screen.findByRole("list", { name: "Graph nodes" });
    const selected = within(list).getByRole("button", { name: /Dana Ops/ });
    await userEvent.click(selected);

    const pane = await screen.findByRole("dialog");
    expect(selected).toHaveAttribute("aria-pressed", "true");
    expect(within(pane).getByRole("table", { name: "Relations" })).toBeInTheDocument();
    await waitFor(() => expect(asked).toContain("person-dana"));
  });
});

describe("Graph Chamber — focus and pointer identify independently", () => {
  it("retains keyboard focus A after pointer B moves and leaves", async () => {
    const graph = toChamberGraph(SUB);
    const { container } = render(
      <GraphChamber
        graph={graph}
        view={{ selectedKey: null, litKeys: new Set(), highlightEdgeKeys: new Set() }}
        onSelectNode={vi.fn()}
      />,
    );
    const list = screen.getByRole("list", { name: "Graph nodes" });
    const focusA = within(list).getByRole("button", { name: "Acme Corp · Account" });
    const pointerB = within(list).getByRole("button", { name: "Dana Ops · Person" });
    const canvas = container.querySelector("canvas");
    expect(canvas).not.toBeNull();

    fireEvent.focus(focusA);
    expect(focusA).toHaveAttribute("data-focused", "true");

    await waitFor(() => expect(graph.nodes[1]?.x).toEqual(expect.any(Number)));
    fireEvent.pointerMove(canvas!, {
      clientX: graph.nodes[1]!.x,
      clientY: graph.nodes[1]!.y,
    });
    expect(pointerB).toHaveAttribute("data-pointer", "true");
    expect(focusA).toHaveAttribute("data-focused", "true");

    fireEvent.pointerLeave(canvas!);
    expect(pointerB).not.toHaveAttribute("data-pointer");
    expect(focusA).toHaveAttribute("data-focused", "true");
  });
});
