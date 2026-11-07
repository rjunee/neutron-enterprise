// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../../api/client";
import type {
  AgentEvidenceEnvelopeWire,
  AgentEvidenceGraphEntityWire,
  AgentEvidenceGraphHopWire,
  GraphNodeWire,
  SubgraphWire,
} from "../../api/types";
import type { PrincipalState } from "../../auth/PrincipalContext";
import type { GraphRouteState } from "./graphRouteState";
import { GraphWorkspace, type GraphWorkspaceProps } from "./GraphWorkspace";

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

function node(key: string, name: string, entityClass = "Account"): GraphNodeWire {
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

function subgraph(name: string, truncated = false): SubgraphWire {
  return {
    nodes: [node("acct/a", name), node("acct/b", "Related")],
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
    truncated,
  };
}

function evidenceEnvelope(
  overrides: Partial<AgentEvidenceEnvelopeWire> = {},
): AgentEvidenceEnvelopeWire {
  return {
    schema_version: 1,
    agent_id: "agent/one",
    run_id: "run/one",
    run_status: "completed",
    mapping_state: "complete",
    mapping_message: "Claim mapping complete",
    output: {
      verdict: "grounded",
      state: "available",
      message: "Answer available",
      display: "Answer",
    },
    claims: [],
    evidence: [
      {
        evidence_id: "evidence/one",
        state: "available",
        message: "Evidence available",
        assurance: "direct",
        locator: { entity_id: "entity/served" },
      },
    ],
    governance: {
      status: "complete",
      delegated_user_label: null,
      agent_service_identity: null,
      connector_credential_owner_label: null,
      effective_scopes: [],
      policy_environment: null,
      tool_tier: null,
      tool_decision: null,
      unavailable_reasons: [],
    },
    ...overrides,
  };
}

function graphEntity(entityKey: string): AgentEvidenceGraphEntityWire {
  return {
    entity_class: "Account",
    entity_key: entityKey,
    display: entityKey,
    assurance: "source",
    source_ref: `crm:${entityKey}`,
  };
}

function graphHop(subject: string, obj: string): AgentEvidenceGraphHopWire {
  return {
    subject: graphEntity(subject),
    edge_kind: "RELATED_TO",
    obj: graphEntity(obj),
    source_system: "crm",
    source_ref: `crm:relation:${subject}:${obj}`,
    assurance: "source",
    review_state: null,
  };
}

function graphPathEvidenceEnvelope(): AgentEvidenceEnvelopeWire {
  const graphPath: AgentEvidenceGraphHopWire[] = [
    {
      subject: {
        entity_class: "Account",
        entity_key: "entity/a",
        display: "First path endpoint",
        assurance: "source",
        source_ref: "crm:account:a",
      },
      edge_kind: "RELATED_TO",
      obj: {
        entity_class: "Account",
        entity_key: "entity/b",
        display: "Middle path endpoint",
        assurance: "source",
        source_ref: "crm:account:b",
      },
      source_system: "crm",
      source_ref: "crm:relation:a-b",
      assurance: "source",
      review_state: null,
    },
    {
      subject: {
        entity_class: "Account",
        entity_key: "entity/b",
        display: "Middle path endpoint",
        assurance: "source",
        source_ref: "crm:account:b",
      },
      edge_kind: "RELATED_TO",
      obj: {
        entity_class: "Account",
        entity_key: "entity/c",
        display: "Last path endpoint",
        assurance: "source",
        source_ref: "crm:account:c",
      },
      source_system: "crm",
      source_ref: "crm:relation:b-c",
      assurance: "source",
      review_state: null,
    },
  ];
  return evidenceEnvelope({
    evidence: [
      {
        ...evidenceEnvelope().evidence[0]!,
        assurance: "inferred",
        locator: { entity_id: null, graph_path: graphPath },
      },
    ],
  });
}

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

const href = (key: string, mode: "graph" | "record") =>
  `/knowledge/graph?entity=${encodeURIComponent(key)}&mode=${mode}`;

function props(route: GraphRouteState = ROUTE): GraphWorkspaceProps {
  return {
    route,
    currentSearch: new URLSearchParams({
      entity: route.entity ?? "",
      mode: route.mode,
      depth: String(route.depth),
    }).toString(),
    entityHref: href,
    onFiltersChange: vi.fn(),
    onSelectEntity: vi.fn(),
    onFirstVisibleEntity: vi.fn(),
    onCitationFocusEntity: vi.fn(),
    onDepthChange: vi.fn(),
  };
}

function renderWorkspace(route: GraphRouteState = ROUTE) {
  return render(
    <MemoryRouter>
      <GraphWorkspace {...props(route)} />
    </MemoryRouter>,
  );
}

function graphClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    graphEntities: vi.fn().mockResolvedValue({ entities: [], next_cursor: null }),
    graphFacets: vi.fn().mockResolvedValue({
      entity_classes: [],
      source_systems: [],
      truncated: false,
    }),
    graphNeighborhood: vi.fn().mockResolvedValue(subgraph("Anchor")),
    graphQuery: vi.fn().mockResolvedValue(subgraph("Citation")),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  stub.principal = { status: "ok", principal: { id: "alice", groups: [] } };
  stub.client = graphClient();
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
});

describe("GraphWorkspace", () => {
  it("names in the canvas legend exactly the classes on the canvas, not a built-in list", async () => {
    // The legend carried the same hardcoded ne-schema registry as the dead
    // Class dropdown (Account/Person/Document/Ticket/Order). It now labels what
    // is actually drawn — derived from the ACL-trimmed subgraph in hand, so it
    // can never name a class this viewer cannot see.
    const graphNeighborhood = vi.fn().mockResolvedValue({
      nodes: [
        node("cus/1", "Ada Lovelace", "Customer"),
        node("doc/1", "Servicing policy", "Document"),
        node("cus/2", "Grace Hopper", "Customer"),
      ],
      edges: [],
      truncated: false,
    });
    stub.client = graphClient({ graphNeighborhood });
    renderWorkspace({ ...ROUTE, entity: "cus/1" });

    const legend = await screen.findByLabelText("Entity classes");
    expect([...legend.children].map((item) => item.textContent)).toEqual([
      "Customer",
      "Document",
      "Unconfirmed",
    ]);
    for (const registryClass of ["Account", "Person", "Ticket", "Order"]) {
      expect(legend.textContent ?? "").not.toContain(registryClass);
    }
  });

  it("hydrates one exact server-authorized Run evidence tuple and focuses its opaque entity", async () => {
    const getAgentRunEvidence = vi.fn().mockResolvedValue(evidenceEnvelope());
    const graphNeighborhood = vi.fn().mockResolvedValue({
      nodes: [node("entity/served", "Served evidence entity")],
      edges: [],
      truncated: false,
    });
    stub.client = graphClient({ getAgentRunEvidence, graphNeighborhood });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Served evidence entity · Account" }),
    ).toHaveAttribute("data-lit", "true");
    expect(getAgentRunEvidence).toHaveBeenCalledWith("agent/one", "run/one");
    expect(graphNeighborhood).toHaveBeenCalledWith("entity/served", { hops: 3 });
  });

  it("hydrates an exact canonical Graph-path receipt without inventing one focus entity", async () => {
    const getAgentRunEvidence = vi.fn().mockResolvedValue(graphPathEvidenceEnvelope());
    const graphQuery = vi.fn().mockResolvedValue({
      nodes: [
        node("entity/a", "First path endpoint"),
        node("entity/b", "Middle path endpoint"),
        node("entity/c", "Last path endpoint"),
      ],
      edges: [],
      truncated: false,
    });
    const graphNeighborhood = vi.fn().mockResolvedValue({
      nodes: [node("entity/a", "First path endpoint")],
      edges: [],
      truncated: false,
    });
    stub.client = graphClient({ getAgentRunEvidence, graphQuery, graphNeighborhood });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(
      await screen.findByRole("button", { name: "First path endpoint · Account" }),
    ).toHaveAttribute("data-lit", "true");
    expect(screen.getByRole("button", { name: "Middle path endpoint · Account" })).toHaveAttribute(
      "data-lit",
      "true",
    );
    expect(screen.getByRole("button", { name: "Last path endpoint · Account" })).toHaveAttribute(
      "data-lit",
      "true",
    );
    expect(graphQuery).toHaveBeenCalledWith({
      anchor_keys: ["entity/a", "entity/b", "entity/c"],
      anchor_citations: [],
      hops: 2,
    });
    expect(graphNeighborhood).not.toHaveBeenCalled();
  });

  it("refuses a malformed or disconnected evidence path before Graph I/O", async () => {
    const malformed = graphPathEvidenceEnvelope();
    malformed.evidence[0]!.locator = {
      entity_id: null,
      graph_path: [graphHop("entity/a", "entity/b"), graphHop("entity/other", "entity/c")],
    };
    const graphQuery = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    const graphNeighborhood = vi.fn();
    stub.client = graphClient({
      getAgentRunEvidence: vi.fn().mockResolvedValue(malformed),
      graphQuery,
      graphNeighborhood,
    });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(
      await screen.findByText("This governed evidence link is unavailable."),
    ).toBeInTheDocument();
    expect(graphQuery).not.toHaveBeenCalled();
    expect(graphNeighborhood).not.toHaveBeenCalled();
  });

  it("refuses a path above the 32-hop wire bound before Graph I/O", async () => {
    const oversized = graphPathEvidenceEnvelope();
    oversized.evidence[0]!.locator = {
      entity_id: null,
      graph_path: Array.from({ length: 33 }, (_, index) =>
        graphHop(`entity/${index}`, `entity/${index + 1}`),
      ),
    };
    const graphQuery = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    const graphNeighborhood = vi.fn();
    stub.client = graphClient({
      getAgentRunEvidence: vi.fn().mockResolvedValue(oversized),
      graphQuery,
      graphNeighborhood,
    });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(
      await screen.findByText("This governed evidence link is unavailable."),
    ).toBeInTheDocument();
    expect(graphQuery).not.toHaveBeenCalled();
    expect(graphNeighborhood).not.toHaveBeenCalled();
  });

  it("rejects a malformed tuple before either evidence or Graph I/O", async () => {
    const getAgentRunEvidence = vi.fn();
    const graphEntities = vi.fn();
    const graphNeighborhood = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    const graphQuery = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    stub.client = graphClient({
      getAgentRunEvidence,
      graphEntities,
      graphNeighborhood,
      graphQuery,
    });
    renderWorkspace({ ...ROUTE, entity: null, evidenceFocus: { state: "invalid" } });

    expect(screen.getByText("This governed evidence link is unavailable.")).toBeInTheDocument();
    await act(async () => new Promise((resolvePromise) => setTimeout(resolvePromise, 300)));
    expect(getAgentRunEvidence).not.toHaveBeenCalled();
    expect(graphEntities).not.toHaveBeenCalled();
    expect(graphNeighborhood).not.toHaveBeenCalled();
    expect(graphQuery).not.toHaveBeenCalled();
  });

  it.each([
    ["foreign Run", evidenceEnvelope({ run_id: "run/other" })],
    [
      "unavailable evidence",
      evidenceEnvelope({
        evidence: [
          {
            evidence_id: "evidence/one",
            state: "withheld",
            message: "Unavailable",
            assurance: "unavailable",
            locator: { entity_id: "entity/must-not-leak" },
          },
        ],
      }),
    ],
    ["missing evidence", evidenceEnvelope({ evidence: [] })],
    [
      "ambiguous evidence",
      evidenceEnvelope({
        evidence: [...evidenceEnvelope().evidence, ...evidenceEnvelope().evidence],
      }),
    ],
  ] as const)("uses the same unavailable shell for %s", async (_case, response) => {
    const graphEntities = vi.fn().mockResolvedValue({ entities: [], next_cursor: null });
    const graphNeighborhood = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    const graphQuery = vi.fn().mockResolvedValue({ nodes: [], edges: [], truncated: false });
    stub.client = graphClient({
      getAgentRunEvidence: vi.fn().mockResolvedValue(response),
      graphEntities,
      graphNeighborhood,
      graphQuery,
    });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(
      await screen.findByText("This governed evidence link is unavailable."),
    ).toBeInTheDocument();
    expect(graphEntities).not.toHaveBeenCalled();
    expect(graphNeighborhood).not.toHaveBeenCalled();
    expect(graphQuery).not.toHaveBeenCalled();
  });

  it("uses the same unavailable shell for a refused evidence read", async () => {
    const graphEntities = vi.fn();
    const graphNeighborhood = vi.fn();
    stub.client = graphClient({
      getAgentRunEvidence: vi.fn().mockRejectedValue(new ApiError("not found", 404)),
      graphEntities,
      graphNeighborhood,
    });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      evidenceFocus: {
        state: "ready",
        agentId: "agent/one",
        runId: "run/one",
        evidenceId: "evidence/one",
      },
    });

    expect(
      await screen.findByText("This governed evidence link is unavailable."),
    ).toBeInTheDocument();
    expect(graphEntities).not.toHaveBeenCalled();
    expect(graphNeighborhood).not.toHaveBeenCalled();
  });

  it("renders the exact neighborhood counts/truncation and opens an independent one-hop drawer", async () => {
    const neighborhood = vi
      .fn()
      .mockImplementation((entity: string, options: { hops?: number }) =>
        Promise.resolve(
          entity === "acct/a" && options.hops === 3
            ? subgraph("Drawn graph", true)
            : { nodes: [node(entity, "Independent record")], edges: [], truncated: false },
        ),
      );
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    renderWorkspace();

    expect(await screen.findByText("2 nodes · 1 edges")).toBeInTheDocument();
    expect(screen.getByText("This graph was capped by the server.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(screen.getByRole("button", { name: "Related · Account" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Drawn graph · Account" }));
    // The docked pane is an <aside>, not a modal — it is the floor's third
    // column, never a route change (screens/graph.html §record pane).
    expect(
      await screen.findByRole("complementary", { name: "Independent record" }),
    ).toBeInTheDocument();
    expect(neighborhood).toHaveBeenNthCalledWith(1, "acct/a", { hops: 3 });
    expect(neighborhood).toHaveBeenNthCalledWith(2, "acct/a", { hops: 1 });
  });

  it("uses the citation query with ordered citations and an honest depth cap of two", async () => {
    const graphQuery = vi.fn().mockResolvedValue(subgraph("Citation graph"));
    const neighborhood = vi.fn();
    stub.client = graphClient({ graphQuery, graphNeighborhood: neighborhood });
    renderWorkspace({
      ...ROUTE,
      entity: null,
      citations: ["first/ref", " second ref "],
      depth: 4,
    });

    expect(
      await screen.findByRole("button", { name: "Citation graph · Account" }),
    ).toBeInTheDocument();
    expect(graphQuery).toHaveBeenCalledWith({
      anchor_keys: [],
      anchor_citations: ["first/ref", " second ref "],
      hops: 2,
    });
    expect(neighborhood).not.toHaveBeenCalled();
    const depth = screen.getByRole("combobox", { name: "Graph depth" });
    expect(depth).toHaveValue("2");
    expect(within(depth).getAllByRole("option")).toHaveLength(2);
  });

  it("focuses one exact case-sensitive citation match only after a complete graph response", async () => {
    const exact = node("acct/exact", "Exact citation entity");
    exact.source_ref = "Source/Exact";
    const other = node("acct/other", "Other entity");
    other.source_ref = "source/exact";
    const onCitationFocusEntity = vi.fn();
    stub.client = graphClient({
      graphQuery: vi.fn().mockResolvedValue({ nodes: [exact, other], edges: [], truncated: false }),
    });

    render(
      <MemoryRouter>
        <GraphWorkspace
          {...props({ ...ROUTE, entity: null, citations: ["Source/Exact"] })}
          onCitationFocusEntity={onCitationFocusEntity}
        />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: "Exact citation entity · Account" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(onCitationFocusEntity).toHaveBeenCalledWith("acct/exact"));
    expect(onCitationFocusEntity).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "ambiguous",
      {
        nodes: [
          { ...node("acct/a", "First"), source_ref: "source/ref" },
          { ...node("acct/b", "Second"), source_ref: "source/ref" },
        ],
        edges: [],
        truncated: false,
      },
    ],
    [
      "case-mismatched",
      {
        nodes: [{ ...node("acct/a", "Wrong case"), source_ref: "SOURCE/REF" }],
        edges: [],
        truncated: false,
      },
    ],
    [
      "truncated",
      {
        nodes: [{ ...node("acct/a", "Incomplete match"), source_ref: "source/ref" }],
        edges: [],
        truncated: true,
      },
    ],
  ] as const)("does not guess a citation focus from a %s result", async (_case, result) => {
    const onCitationFocusEntity = vi.fn();
    stub.client = graphClient({ graphQuery: vi.fn().mockResolvedValue(result) });
    render(
      <MemoryRouter>
        <GraphWorkspace
          {...props({ ...ROUTE, entity: null, citations: ["source/ref"] })}
          onCitationFocusEntity={onCitationFocusEntity}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByText(`${result.nodes.length} nodes · 0 edges`)).toBeInTheDocument();
    expect(onCitationFocusEntity).not.toHaveBeenCalled();
  });

  it("re-reads a focused citation entity as a neighborhood while retaining route context", async () => {
    const graphQuery = vi.fn().mockResolvedValue(subgraph("Wrong citation query"));
    const graphNeighborhood = vi.fn().mockResolvedValue(subgraph("Focused neighborhood"));
    const onCitationFocusEntity = vi.fn();
    stub.client = graphClient({ graphQuery, graphNeighborhood });
    const route = { ...ROUTE, entity: "acct/focused", citations: ["crm/acct/a"], depth: 4 };
    render(
      <MemoryRouter>
        <GraphWorkspace {...props(route)} onCitationFocusEntity={onCitationFocusEntity} />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: "Focused neighborhood · Account" }),
    ).toBeInTheDocument();
    expect(graphNeighborhood).toHaveBeenCalledWith("acct/focused", { hops: 4 });
    expect(graphQuery).not.toHaveBeenCalled();
    expect(onCitationFocusEntity).not.toHaveBeenCalled();
  });

  it("uses one exact opaque anchor as a four-hop neighborhood and lights that anchor", async () => {
    const anchor = " anchor /?# ";
    const neighborhood = vi.fn().mockResolvedValue({
      nodes: [node(anchor, "Opaque anchor")],
      edges: [],
      truncated: false,
    });
    const graphQuery = vi.fn();
    stub.client = graphClient({ graphNeighborhood: neighborhood, graphQuery });
    renderWorkspace({ ...ROUTE, entity: "unrelated selection", anchors: [anchor], depth: 4 });

    const anchorNode = await screen.findByRole("button", { name: "Opaque anchor · Account" });
    expect(anchorNode).toHaveAttribute("data-lit", "true");
    expect(neighborhood).toHaveBeenCalledWith(anchor, { hops: 4 });
    expect(graphQuery).not.toHaveBeenCalled();
    expect(
      within(screen.getByRole("combobox", { name: "Graph depth" })).getAllByRole("option"),
    ).toHaveLength(4);
  });

  it("preserves ordered duplicate anchors and citations in the capped graph query", async () => {
    const graphQuery = vi.fn().mockResolvedValue(subgraph("Repeated anchors"));
    const neighborhood = vi.fn();
    stub.client = graphClient({ graphQuery, graphNeighborhood: neighborhood });
    renderWorkspace({
      ...ROUTE,
      entity: "selection-not-an-anchor",
      anchors: ["root/one", " root two ", "root/one"],
      citations: ["source/ref", " source ref ", "source/ref"],
      depth: 4,
    });

    expect(
      await screen.findByRole("button", { name: "Repeated anchors · Account" }),
    ).toBeInTheDocument();
    expect(graphQuery).toHaveBeenCalledWith({
      anchor_keys: ["root/one", " root two ", "root/one"],
      anchor_citations: ["source/ref", " source ref ", "source/ref"],
      hops: 2,
    });
    expect(neighborhood).not.toHaveBeenCalled();
  });

  it("binds late graph-query results to the complete ordered anchor request", async () => {
    let resolveOld!: (value: SubgraphWire) => void;
    const old = new Promise<SubgraphWire>((resolvePromise) => {
      resolveOld = resolvePromise;
    });
    const graphQuery = vi
      .fn()
      .mockReturnValueOnce(old)
      .mockResolvedValueOnce(subgraph("Current anchor order"));
    stub.client = graphClient({ graphQuery });
    const rendered = renderWorkspace({ ...ROUTE, entity: null, anchors: ["one", "two"] });
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props({ ...ROUTE, entity: null, anchors: ["two", "one"] })} />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("button", { name: "Current anchor order · Account" }),
    ).toBeInTheDocument();
    await act(async () => resolveOld(subgraph("Stale anchor order")));
    expect(screen.queryByText("Stale anchor order · Account")).toBeNull();
    expect(graphQuery).toHaveBeenNthCalledWith(1, {
      anchor_keys: ["one", "two"],
      anchor_citations: [],
      hops: 2,
    });
    expect(graphQuery).toHaveBeenNthCalledWith(2, {
      anchor_keys: ["two", "one"],
      anchor_citations: [],
      hops: 2,
    });
  });

  it("treats whitespace as an opaque anchor and only empties truly unanchored state", async () => {
    const neighborhood = vi.fn().mockResolvedValue({
      nodes: [node(" ", "Whitespace anchor")],
      edges: [],
      truncated: false,
    });
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    const rendered = renderWorkspace({ ...ROUTE, entity: null, anchors: [" "] });
    expect(
      await screen.findByRole("button", { name: "Whitespace anchor · Account" }),
    ).toHaveAttribute("data-lit", "true");
    expect(neighborhood).toHaveBeenCalledWith(" ", { hops: 3 });

    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props({ ...ROUTE, entity: null, anchors: [], citations: [] })} />
      </MemoryRouter>,
    );
    expect(
      screen.getByText("Choose a visible entity or follow a citation to explore the graph."),
    ).toBeInTheDocument();
  });

  it.each([
    ["anchor", { anchors: ["opaque / anchor"], citations: [] }],
    ["citation", { anchors: [], citations: ["opaque / citation"] }],
  ] as const)(
    "reuses one served discovery window when %s context becomes truly bare",
    async (_context, blocked) => {
      const graphEntities = vi.fn().mockResolvedValue({
        entities: [
          {
            key: "acct/served",
            entity_class: "Account",
            display_name: "Served account",
            source_system: "crm",
            assurance: "source",
          },
        ],
        next_cursor: null,
      });
      const onFirstVisibleEntity = vi.fn();
      stub.client = graphClient({ graphEntities });
      const blockedRoute = {
        ...ROUTE,
        entity: null,
        anchors: blocked.anchors,
        citations: blocked.citations,
      };
      const rendered = render(
        <MemoryRouter>
          <GraphWorkspace {...props(blockedRoute)} onFirstVisibleEntity={onFirstVisibleEntity} />
        </MemoryRouter>,
      );

      await waitFor(() => expect(graphEntities).toHaveBeenCalledTimes(1));
      await waitFor(() =>
        expect(screen.getByRole("option", { name: /Served account/ })).toBeInTheDocument(),
      );
      expect(onFirstVisibleEntity).not.toHaveBeenCalled();

      const bareRoute = { ...blockedRoute, anchors: [], citations: [] };
      rendered.rerender(
        <MemoryRouter>
          <GraphWorkspace {...props(bareRoute)} onFirstVisibleEntity={onFirstVisibleEntity} />
        </MemoryRouter>,
      );
      await waitFor(() => expect(onFirstVisibleEntity).toHaveBeenCalledWith("acct/served"));
      expect(onFirstVisibleEntity).toHaveBeenCalledTimes(1);
      expect(graphEntities).toHaveBeenCalledTimes(1);
    },
  );

  it("hides an already-served graph when the same principal/key moves to a new client", async () => {
    let resolveSecond!: (value: SubgraphWire) => void;
    const second = new Promise<SubgraphWire>((resolvePromise) => {
      resolveSecond = resolvePromise;
    });
    const firstClient = graphClient({
      graphNeighborhood: vi.fn().mockResolvedValue(subgraph("First principal")),
    });
    const secondClient = graphClient({ graphNeighborhood: vi.fn().mockReturnValue(second) });
    stub.client = firstClient;
    const rendered = renderWorkspace();
    expect(
      await screen.findByRole("button", { name: "First principal · Account" }),
    ).toBeInTheDocument();

    stub.client = secondClient;
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("First principal · Account")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await act(async () => resolveSecond(subgraph("Second principal")));
    expect(
      await screen.findByRole("button", { name: "Second principal · Account" }),
    ).toBeInTheDocument();
  });

  it("invalidates graph and drawer bytes when the same client/key moves to a new principal", async () => {
    let resolveBob!: (value: SubgraphWire) => void;
    const bobGraph = new Promise<SubgraphWire>((resolvePromise) => {
      resolveBob = resolvePromise;
    });
    const neighborhood = vi
      .fn()
      .mockResolvedValueOnce(subgraph("Alice graph"))
      .mockResolvedValueOnce({
        nodes: [node("acct/a", "Alice drawer")],
        edges: [],
        truncated: false,
      })
      .mockReturnValueOnce(bobGraph);
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    const rendered = renderWorkspace();
    await userEvent.click(await screen.findByRole("button", { name: "Alice graph · Account" }));
    expect(await screen.findByRole("complementary", { name: "Alice drawer" })).toBeInTheDocument();

    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Alice graph · Account")).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Alice drawer" })).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(neighborhood).toHaveBeenCalledTimes(3);

    await act(async () => resolveBob(subgraph("Bob graph")));
    expect(await screen.findByRole("button", { name: "Bob graph · Account" })).toBeInTheDocument();

    stub.principal = { status: "loading", principal: null };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Bob graph · Account")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    stub.principal = { status: "error", principal: null };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Bob graph · Account")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load the graph.");

    stub.principal = { status: "ok", principal: null };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Bob graph · Account")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load the graph.");
  });

  it("rejects an old-principal completion resolved before layout cleanup", async () => {
    let resolveAlice!: (value: SubgraphWire) => void;
    let resolveBob!: (value: SubgraphWire) => void;
    const aliceGraph = new Promise<SubgraphWire>((resolvePromise) => {
      resolveAlice = resolvePromise;
    });
    const bobGraph = new Promise<SubgraphWire>((resolvePromise) => {
      resolveBob = resolvePromise;
    });
    const neighborhood = vi.fn().mockReturnValueOnce(aliceGraph).mockReturnValueOnce(bobGraph);
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    const rendered = renderWorkspace();

    resolveAlice(subgraph("Alice stale graph"));
    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props()} />
      </MemoryRouter>,
    );
    await act(async () => Promise.resolve());
    expect(screen.queryByText("Alice stale graph · Account")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    await act(async () => resolveBob(subgraph("Bob current graph")));
    expect(
      await screen.findByRole("button", { name: "Bob current graph · Account" }),
    ).toBeInTheDocument();
  });

  it("does not let a late graph response from the previous entity replace the current graph", async () => {
    let resolveOld!: (value: SubgraphWire) => void;
    const old = new Promise<SubgraphWire>((resolvePromise) => {
      resolveOld = resolvePromise;
    });
    const neighborhood = vi
      .fn()
      .mockImplementation((entity: string) =>
        entity === "old" ? old : Promise.resolve(subgraph("Current graph")),
      );
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    const rendered = renderWorkspace({ ...ROUTE, entity: "old" });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props({ ...ROUTE, entity: "current" })} />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("button", { name: "Current graph · Account" }),
    ).toBeInTheDocument();
    await act(async () => resolveOld(subgraph("Stale graph")));
    expect(screen.queryByText("Stale graph · Account")).toBeNull();
    expect(screen.getByRole("button", { name: "Current graph · Account" })).toBeInTheDocument();
  });

  it("keeps a ready entity finder independent from a refused graph read", async () => {
    stub.client = graphClient({
      graphEntities: vi.fn().mockResolvedValue({
        entities: [
          {
            key: "acct/visible",
            entity_class: "Account",
            display_name: "Visible account",
            source_system: "crm",
            assurance: "source",
          },
        ],
        next_cursor: null,
      }),
      graphNeighborhood: vi.fn().mockRejectedValue(new ApiError("refused", 403)),
    });
    renderWorkspace();
    expect(
      await screen.findByText(/Your role doesn.t include the knowledge graph/i),
    ).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /Visible account/ })).toBeInTheDocument();
  });

  it("renders Record mode through the independent one-hop record read", async () => {
    const neighborhood = vi.fn().mockResolvedValue({
      nodes: [node("acct/a", "Full record")],
      edges: [],
      truncated: false,
    });
    const graphQuery = vi.fn();
    stub.client = graphClient({ graphNeighborhood: neighborhood, graphQuery });
    renderWorkspace({ ...ROUTE, mode: "record", citations: ["kept citation"] });
    expect(await screen.findByRole("heading", { name: "Full record" })).toBeInTheDocument();
    expect(neighborhood).toHaveBeenCalledWith("acct/a", { hops: 1 });
    expect(graphQuery).not.toHaveBeenCalled();
  });

  it("remounts a same-key record read when the principal changes", async () => {
    let resolveBob!: (value: SubgraphWire) => void;
    const bobRecord = new Promise<SubgraphWire>((resolvePromise) => {
      resolveBob = resolvePromise;
    });
    const neighborhood = vi
      .fn()
      .mockResolvedValueOnce({
        nodes: [node("acct/a", "Alice record")],
        edges: [],
        truncated: false,
      })
      .mockReturnValueOnce(bobRecord);
    stub.client = graphClient({ graphNeighborhood: neighborhood });
    const route = { ...ROUTE, mode: "record" as const };
    const rendered = renderWorkspace(route);
    expect(await screen.findByRole("heading", { name: "Alice record" })).toBeInTheDocument();

    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    rendered.rerender(
      <MemoryRouter>
        <GraphWorkspace {...props(route)} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("heading", { name: "Alice record" })).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await act(async () =>
      resolveBob({
        nodes: [node("acct/a", "Bob record")],
        edges: [],
        truncated: false,
      }),
    );
    expect(await screen.findByRole("heading", { name: "Bob record" })).toBeInTheDocument();
  });

  it("traps a narrow finder sheet, inerts background controls, and returns focus", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    );
    renderWorkspace({ ...ROUTE, entity: null, citations: [] });
    const toggle = screen.getByRole("button", { name: "Find entities" });
    expect(screen.queryByRole("dialog", { name: "Entity finder sheet" })).toBeNull();
    await userEvent.click(toggle);
    const sheet = screen.getByRole("dialog", { name: "Entity finder sheet" });
    await waitFor(() =>
      expect(within(sheet).getByRole("searchbox", { name: "Find an entity" })).toHaveFocus(),
    );
    expect(screen.getByRole("navigation", { name: "Graph display" })).toHaveAttribute("inert");
    expect(screen.getByRole("region", { name: "Graph result" })).toHaveAttribute("inert");
    const close = within(sheet).getByRole("button", { name: "Close finder" });
    const assurance = within(sheet).getByRole("combobox", { name: "Assurance" });
    close.focus();
    await userEvent.tab({ shift: true });
    expect(assurance).toHaveFocus();
    await userEvent.tab();
    expect(close).toHaveFocus();
    assurance.focus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Entity finder sheet" })).toBeNull();
    await waitFor(() => expect(toggle).toHaveFocus());
  });

  it("declares the three-column and narrow sheet layout without motion", () => {
    const css = readFileSync(resolve(__dirname, "graphWorkspace.css"), "utf8");
    // The floor is rail · canvas by default and gains the record column only
    // while a node is selected (screens/graph.html §floor).
    expect(css).toMatch(
      /\.ne-graphwork__floor\s*{[^}]*grid-template-columns:\s*280px minmax\(0, 1fr\)/is,
    );
    expect(css).toMatch(
      /\.ne-graphwork__floor\[data-pane-open="true"\]\s*{[^}]*grid-template-columns:\s*280px minmax\(0, 1fr\) var\(--detail-width\)/is,
    );
    // ≤64rem/1024px: the rail folds into a focus-trapped sheet; the floor
    // itself collapses to a single track regardless of the pane.
    expect(css).toMatch(
      /@media \(max-width: 64rem\)[\s\S]*\.ne-graphwork__finder\s*{[^}]*position:\s*fixed/is,
    );
    expect(css).toMatch(
      /@media \(max-width: 64rem\)[\s\S]*\.ne-graphwork__floor,\s*\n\s*\.ne-graphwork__floor\[data-pane-open="true"\]\s*{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/is,
    );
    expect(css).not.toMatch(/animation:|transition:/);
  });

  it("does not introduce a second main landmark inside the application shell", () => {
    render(
      <MemoryRouter>
        <main aria-label="Application shell">
          <GraphWorkspace {...props({ ...ROUTE, entity: null, citations: [] })} />
        </main>
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Knowledge graph explorer" })).toBeInTheDocument();
  });
});
