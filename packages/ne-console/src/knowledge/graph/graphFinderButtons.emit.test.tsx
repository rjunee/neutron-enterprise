// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE MARKUP HALF OF THE FINDER-BUTTON CASCADE GUARD.
//
// Owner-reported defect, 2026-08-26: on the desktop-width (permanent-sidebar)
// layout, the "Find entities" and "Close finder" buttons render but do
// nothing. Root cause is a CSS cascade fight, not React: `.ne-btn { display:
// inline-flex }` (styles/table.css) and `.ne-workbar__findbtn`/
// `.ne-graphwork__finder-close { display: none }` (this component's own
// graphWorkspace.css) tie on specificity, so whichever rule the BUNDLER
// happens to place later in the shipped stylesheet wins — and it was
// `.ne-btn`, re-showing both buttons at every width including desktop, where
// they are meaningless (the sidebar is already permanently open there).
//
// jsdom does no layout and cannot resolve cascade/specificity the way a real
// browser does, so no vitest assertion here can see the fight. This file only
// emits the REAL DOM React renders for the three states that matter — so the
// browser half of this guard (scripts/graph-finder-buttons-cascade-check.mjs)
// measures what GraphWorkspace actually produces, not a hand-written
// imitation that can drift and quietly stop testing anything.
//
// It writes nothing during an ordinary `vitest run`: gated on
// NE_GRAPH_FINDER_EMIT, which only the check script sets.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { GraphNodeWire, SubgraphWire } from "../../api/types";
import type { PrincipalState } from "../../auth/PrincipalContext";
import { GraphWorkspace, type GraphWorkspaceProps } from "./GraphWorkspace";
import type { GraphRouteState } from "./graphRouteState";

const outDir = process.env.NE_GRAPH_FINDER_EMIT;

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
  edges: [],
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

function props(): GraphWorkspaceProps {
  return {
    route: ROUTE,
    currentSearch: new URLSearchParams({
      entity: ROUTE.entity ?? "",
      mode: ROUTE.mode,
      depth: String(ROUTE.depth),
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

function client(): ApiClient {
  return {
    graphEntities: vi.fn().mockResolvedValue({
      entities: [
        { key: "acct/a", entity_class: "Account", display_name: "Acme Corp", assurance: "source" },
        { key: "acct/b", entity_class: "Account", display_name: "Related", assurance: "source" },
      ],
      next_cursor: null,
    }),
    graphNeighborhood: vi.fn().mockResolvedValue(SUBGRAPH),
    graphQuery: vi.fn().mockResolvedValue(SUBGRAPH),
  } as unknown as ApiClient;
}

/** matches the (max-width: 64rem) query GraphWorkspace's useNarrowLayout uses. */
function stubMatchMedia(narrow: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
      matches: narrow,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
}

/** The rendered `.ne-graphwork`, as the browser harness will see it. */
function emit(arm: string, container: HTMLElement): void {
  const root = container.querySelector(".ne-graphwork");
  expect(root, `${arm}: no .ne-graphwork rendered`).not.toBeNull();
  mkdirSync(outDir!, { recursive: true });
  writeFileSync(join(outDir!, `${arm}.html`), root!.outerHTML, "utf8");
}

function renderWorkspace() {
  return render(
    <MemoryRouter>
      <GraphWorkspace {...props()} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  stub.principal = { status: "ok", principal: { id: "alice", groups: [] } };
  stub.client = client();
});

describe.runIf(outDir)("graph finder button fixtures", () => {
  // Desktop: the permanent 280px sidebar. `narrow` is false the whole time,
  // matching useNarrowLayout's own matchMedia query never matching.
  it("emits the desktop arm", async () => {
    stubMatchMedia(false);
    const { container } = renderWorkspace();
    expect(await screen.findByRole("region", { name: "Graph viewport" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find entities" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    emit("desktop", container);
  });

  // Narrow, sheet not yet opened: the toggle is the only reachable control.
  it("emits the narrow-closed arm", async () => {
    stubMatchMedia(true);
    const { container } = renderWorkspace();
    expect(await screen.findByRole("region", { name: "Graph viewport" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find entities" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("dialog", { name: "Entity finder sheet" })).toBeNull();
    emit("narrow-closed", container);
  });

  // Narrow, sheet opened via a REAL click through GraphWorkspace's own
  // onClick/state wiring — proving the toggle is still functional, not just
  // present, before the browser half checks the sheet actually renders
  // visible.
  it("emits the narrow-open arm", async () => {
    stubMatchMedia(true);
    const { container } = renderWorkspace();
    const toggle = await screen.findByRole("button", { name: "Find entities" });
    await userEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: "Entity finder sheet" })).toBeInTheDocument(),
    );
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    emit("narrow-open", container);
  });
});
