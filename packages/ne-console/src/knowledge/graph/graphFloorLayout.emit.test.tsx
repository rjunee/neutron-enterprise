// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE MARKUP HALF OF THE GRAPH-FLOOR LAYOUT GUARD.
//
// jsdom does no layout: getBoundingClientRect() is all zeros, so a canvas that
// collapses to height 0 is indistinguishable from a correct one in vitest. The
// measurement therefore happens in a real browser, driven by
// scripts/graph-canvas-layout-check.mjs. This file exists so that what the
// browser measures is the DOM React ACTUALLY renders for every arm of
// .ne-graphwork__main — not a hand-written imitation that can drift from the
// component and quietly stop testing it.
//
// It writes nothing during an ordinary `vitest run`: the whole suite is gated
// on NE_GRAPH_LAYOUT_EMIT, which only the check script sets.

import { render, screen, waitFor } from "@testing-library/react";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../../api/client";
import type { GraphNodeWire, SubgraphWire } from "../../api/types";
import type { PrincipalState } from "../../auth/PrincipalContext";
import { toChamberGraph } from "../chamberSim";
import { GraphChamber } from "../GraphChamber";
import { GraphWorkspace, type GraphWorkspaceProps } from "./GraphWorkspace";
import type { GraphRouteState } from "./graphRouteState";

const outDir = process.env.NE_GRAPH_LAYOUT_EMIT;

const stub = vi.hoisted(() => ({
  client: {} as unknown,
  principal: { status: "ok", principal: { id: "alice", groups: [] } } as unknown,
}));

vi.mock("../../auth/useApiClient", () => ({
  useApiClient: () => stub.client as ApiClient,
}));

vi.mock("../../auth/PrincipalContext", () => ({
  usePrincipal: () => stub.principal as PrincipalState,
}));

function node(key: string, name: string): GraphNodeWire {
  return {
    key,
    entity_class: "Account",
    display_name: name,
    source_system: "crm",
    source_ref: `crm/${key}`,
    properties: { owner: "Dana Ops" },
    assurance: "source",
  };
}

const SUBGRAPH: SubgraphWire = {
  nodes: [node("acct/a", "Acme Corp"), node("acct/b", "Related")],
  edges: [
    {
      key: "edge/1",
      edge_kind: "OWNS",
      from_key: "acct/a",
      to_key: "acct/b",
      source_system: "crm",
      source_ref: "crm/edge/1",
      assurance: "confirmed",
    },
  ],
  truncated: false,
};

const ROUTE: GraphRouteState = {
  entity: "acct/a",
  mode: "graph",
  depth: 3,
  anchors: [],
  citations: [],
  query: "",
  entityClass: "",
  source: "",
  assurance: null,
};

function props(route: GraphRouteState): GraphWorkspaceProps {
  return {
    route,
    currentSearch: new URLSearchParams({
      entity: route.entity ?? "",
      mode: route.mode,
      depth: String(route.depth),
    }).toString(),
    entityHref: (key: string, mode: "graph" | "record") =>
      `/knowledge/graph?entity=${encodeURIComponent(key)}&mode=${mode}`,
    onFiltersChange: vi.fn(),
    onSelectEntity: vi.fn(),
    onFirstVisibleEntity: vi.fn(),
    onCitationFocusEntity: vi.fn(),
    onDepthChange: vi.fn(),
  };
}

function client(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    graphEntities: vi.fn().mockResolvedValue({
      entities: [
        { key: "acct/a", entity_class: "Account", display_name: "Acme Corp", assurance: "source" },
        { key: "acct/b", entity_class: "Account", display_name: "Related", assurance: "source" },
      ],
      next_cursor: null,
    }),
    graphFacets: vi.fn().mockResolvedValue({
      entity_classes: [],
      source_systems: [],
      truncated: false,
    }),
    graphNeighborhood: vi.fn().mockResolvedValue(SUBGRAPH),
    graphQuery: vi.fn().mockResolvedValue(SUBGRAPH),
    ...overrides,
  } as unknown as ApiClient;
}

/** The rendered subtree, as the browser harness will see it. */
function emit(arm: string, container: HTMLElement, selector = ".ne-graphwork"): void {
  const root = container.querySelector(selector);
  expect(root, `${arm}: no ${selector} rendered`).not.toBeNull();
  mkdirSync(outDir!, { recursive: true });
  writeFileSync(join(outDir!, `${arm}.html`), root!.outerHTML, "utf8");
}

function renderWorkspace(route: GraphRouteState) {
  return render(
    <MemoryRouter>
      <GraphWorkspace {...props(route)} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  stub.principal = { status: "ok", principal: { id: "alice", groups: [] } };
  stub.client = client();
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
});

describe.runIf(outDir)("graph floor layout fixtures", () => {
  it("emits the canvas arm", async () => {
    const { container } = renderWorkspace(ROUTE);
    expect(await screen.findByRole("region", { name: "Graph viewport" })).toBeInTheDocument();
    emit("canvas", container);
  });

  it("emits the loading arm", async () => {
    stub.client = client({
      graphNeighborhood: vi.fn().mockReturnValue(new Promise(() => {})),
    });
    const { container } = renderWorkspace(ROUTE);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    emit("loading", container);
  });

  it("emits the empty arm", async () => {
    const { container } = renderWorkspace({ ...ROUTE, entity: null });
    expect(
      await screen.findByText("Choose a visible entity or follow a citation to explore the graph."),
    ).toBeInTheDocument();
    emit("empty", container);
  });

  it("emits the refused arm", async () => {
    stub.client = client({
      graphNeighborhood: vi.fn().mockRejectedValue(new ApiError("no", 403)),
    });
    const { container } = renderWorkspace(ROUTE);
    expect(await screen.findByText(/Your role doesn/)).toBeInTheDocument();
    emit("refused", container);
  });

  it("emits the error arm", async () => {
    stub.client = client({
      graphNeighborhood: vi.fn().mockRejectedValue(new ApiError("boom", 500)),
    });
    const { container } = renderWorkspace(ROUTE);
    expect(await screen.findByText(/Couldn.t load the graph\./)).toBeInTheDocument();
    emit("error", container);
  });

  it("emits the record arm", async () => {
    const { container } = renderWorkspace({ ...ROUTE, mode: "record" });
    await waitFor(() => expect(container.querySelector(".ne-graph-record")).not.toBeNull());
    emit("record", container);
  });

  // GraphChamber's OTHER host. `.ne-graphwork .ne-chamber` is deliberately
  // scoped away from it; the browser check measures this fixture to prove the
  // legacy tab still gets library.css's own clamped chamber height.
  it("emits the legacy Library > Graph host", () => {
    const { container } = render(
      <div className="ne-library__tab">
        <GraphChamber
          graph={toChamberGraph(SUBGRAPH)}
          view={{ selectedKey: null, litKeys: new Set<string>(), highlightEdgeKeys: new Set() }}
          onSelectNode={vi.fn()}
        />
      </div>,
    );
    emit("legacy", container, ".ne-library__tab");
  });
});
