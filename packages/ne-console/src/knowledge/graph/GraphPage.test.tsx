// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef } from "react";
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { PrincipalOwner, PrincipalState } from "../../auth/PrincipalContext";
import type { GraphWorkspaceProps } from "./GraphWorkspace";
import { GraphPage } from "./GraphPage";

const workspaceStub = vi.hoisted(() => ({
  reconcileReadyWindow: false,
  graphEntities: vi.fn(),
  lastProps: null as GraphWorkspaceProps | null,
  renderedSearches: [] as string[],
}));

vi.mock("./GraphWorkspace", () => ({
  GraphWorkspace: (props: GraphWorkspaceProps) => {
    workspaceStub.lastProps = props;
    workspaceStub.renderedSearches.push(props.currentSearch);
    const navigate = useNavigate();
    const consumed = useRef(false);
    const { onFirstVisibleEntity, route } = props;
    const mountedRoute = useRef(route);
    const mountedSearch = useRef(props.currentSearch);
    useEffect(() => {
      workspaceStub.graphEntities(mountedRoute.current, mountedSearch.current);
    }, []);
    useEffect(() => {
      if (!workspaceStub.reconcileReadyWindow) return;
      if (route.entity !== null) {
        consumed.current = false;
      } else if (!consumed.current) {
        consumed.current = true;
        onFirstVisibleEntity("served /?#");
      }
    }, [onFirstVisibleEntity, route.entity]);
    return (
      <div>
        <output aria-label="Served workspace bytes">Graph, rail, record and drawer bytes</output>
        <output aria-label="Workspace route">{JSON.stringify(props.route)}</output>
        <output aria-label="Opaque href">{props.entityHref("opaque /?#", "record")}</output>
        <button type="button" onClick={() => props.onFirstVisibleEntity("served /?#")}>
          Default served entity
        </button>
        <button type="button" onClick={() => props.onSelectEntity("chosen /?#")}>
          Select entity
        </button>
        <button type="button" onClick={() => props.onCitationFocusEntity("cited /?#")}>
          Resolve citation
        </button>
        <button type="button" onClick={() => navigate("/knowledge/graph?utm=keep")}>
          Return to bare route
        </button>
        <button
          type="button"
          onClick={() =>
            props.onFiltersChange({
              query: "new query",
              entityClass: "Person",
              source: "workday",
              assurance: "confirmed",
            })
          }
        >
          Change filters
        </button>
      </div>
    );
  },
}));

function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <output aria-label="Location">
      {navigationType}:{location.pathname}
      {location.search}
    </output>
  );
}

const CLIENT_A = {} as ApiClient;
const CLIENT_B = {} as ApiClient;

function principal(status: PrincipalState["status"], id: string | null): PrincipalState {
  return {
    status,
    principal: status === "ok" && id !== null ? { id, groups: [] } : null,
  };
}

function owner(
  generation: string,
  client: ApiClient,
  status: PrincipalState["status"],
  id: string | null,
): PrincipalOwner {
  return { client, generation, principal: principal(status, id) };
}

const OWNER_A = owner("client-a:alice", CLIENT_A, "ok", "alice");

function renderPage(initialEntry: string, initialOwner = OWNER_A) {
  const page = (currentOwner: PrincipalOwner) => (
    <MemoryRouter initialEntries={[initialEntry]}>
      <GraphPage owner={currentOwner} />
      <LocationProbe />
    </MemoryRouter>
  );
  const view = render(page(initialOwner));
  return {
    ...view,
    rerenderOwner(currentOwner: PrincipalOwner) {
      view.rerender(page(currentOwner));
    },
  };
}

beforeEach(() => {
  workspaceStub.reconcileReadyWindow = false;
  workspaceStub.graphEntities.mockReset();
  workspaceStub.lastProps = null;
  workspaceStub.renderedSearches = [];
});

describe("GraphPage", () => {
  it("preserves an initial deep link while its first principal generation settles", async () => {
    const initial =
      "/knowledge/graph?entity=acct%2Fa&mode=record&depth=4&anchor=one&citation=two&q=risk&class=Account&source=crm&assurance=source&cursor=served&keep=one&keep=two";
    const view = renderPage(initial, owner("client-a:loading", CLIENT_A, "loading", null));
    expect(screen.queryByLabelText("Served workspace bytes")).toBeNull();
    expect(screen.getByLabelText("Location")).toHaveTextContent(initial);

    view.rerenderOwner(OWNER_A);
    expect(await screen.findByLabelText("Served workspace bytes")).toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toHaveTextContent(initial);
    expect(JSON.parse(screen.getByLabelText("Workspace route").textContent ?? "null")).toEqual({
      entity: "acct/a",
      mode: "record",
      depth: 4,
      anchors: ["one"],
      citations: ["two"],
      query: "risk",
      entityClass: "Account",
      source: "crm",
      assurance: "source",
    });
  });

  it.each([
    ["same client, different subject", owner("client-a:bob", CLIENT_A, "ok", "bob")],
    ["same subject, different client", owner("client-b:alice", CLIENT_B, "ok", "alice")],
  ])("sanitizes the whole Graph-owned route for %s", async (_label, nextOwner) => {
    const view = renderPage(
      "/knowledge/graph?keep=one&entity=acct%2Fa&mode=record&keep=two&depth=4&anchor=first&citation=ref&q=risk&class=Account&source=crm&assurance=source&cursor=served&z=last",
    );
    expect(screen.getByLabelText("Served workspace bytes")).toBeInTheDocument();

    view.rerenderOwner(nextOwner);
    await waitFor(() => {
      const location = screen.getByLabelText("Location").textContent ?? "";
      expect(new URLSearchParams(location.split("?")[1])).toEqual(
        new URLSearchParams("keep=one&keep=two&z=last"),
      );
    });
    const entries = [
      ...new URLSearchParams(
        (screen.getByLabelText("Location").textContent ?? "").split("?")[1],
      ).entries(),
    ];
    expect(entries).toEqual([
      ["keep", "one"],
      ["keep", "two"],
      ["z", "last"],
    ]);
    expect(await screen.findByLabelText("Served workspace bytes")).toBeInTheDocument();
    expect(JSON.parse(screen.getByLabelText("Workspace route").textContent ?? "null")).toEqual({
      entity: null,
      mode: "graph",
      depth: 1,
      anchors: [],
      citations: [],
      query: "",
      entityClass: "",
      source: "",
      assurance: null,
    });
  });

  it("clears the exact Run-evidence tuple before a replacement principal can render or read", async () => {
    const priorAgent = "agent/prior-private";
    const priorRun = "run?prior-private";
    const priorEvidence = "evidence#prior-private";
    const view = renderPage(
      `/knowledge/graph?keep=one&mode=graph&agent=${encodeURIComponent(priorAgent)}&run=${encodeURIComponent(priorRun)}&evidence=${encodeURIComponent(priorEvidence)}&keep=two`,
    );
    expect(screen.getByLabelText("Served workspace bytes")).toBeInTheDocument();
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);

    workspaceStub.renderedSearches = [];
    view.rerenderOwner(owner("client-a:bob", CLIENT_A, "ok", "bob"));

    expect(document.body.textContent).not.toContain(priorAgent);
    expect(document.body.textContent).not.toContain(priorRun);
    expect(document.body.textContent).not.toContain(priorEvidence);

    await waitFor(() => {
      const location = screen.getByLabelText("Location").textContent ?? "";
      expect(new URLSearchParams(location.split("?")[1])).toEqual(
        new URLSearchParams("keep=one&keep=two"),
      );
    });
    expect(await screen.findByLabelText("Served workspace bytes")).toBeInTheDocument();
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(2);
    const clearedRoute = {
      entity: null,
      mode: "graph",
      depth: 1,
      anchors: [],
      citations: [],
      query: "",
      entityClass: "",
      source: "",
      assurance: null,
    };
    expect(workspaceStub.graphEntities.mock.calls[1]?.[0]).toEqual(clearedRoute);
    expect(JSON.parse(screen.getByLabelText("Workspace route").textContent ?? "null")).toEqual(
      clearedRoute,
    );
    expect(screen.getByLabelText("Location")).not.toHaveTextContent(priorAgent);
    expect(screen.getByLabelText("Location")).not.toHaveTextContent(priorRun);
    expect(screen.getByLabelText("Location")).not.toHaveTextContent(priorEvidence);

    // Ordering, not eventual sanitation: the replacement principal's workspace was never
    // rendered with, and never mounted its read on, a search still carrying the tuple. A
    // regression that hands it over for a single render/effect cycle and only then clears
    // the URL fails here even though every assertion above still holds.
    const sanitized = new URLSearchParams("keep=one&keep=two");
    expect(workspaceStub.renderedSearches.length).toBeGreaterThan(0);
    for (const search of workspaceStub.renderedSearches) {
      expect(search).not.toContain("prior-private");
      expect(new URLSearchParams(search)).toEqual(sanitized);
    }
    expect(new URLSearchParams(workspaceStub.graphEntities.mock.calls[1]?.[1] ?? "")).toEqual(
      sanitized,
    );
  });

  it("withholds old bytes and new-client reads while unsettled, and makes stale callbacks inert", async () => {
    const view = renderPage(
      "/knowledge/graph?entity=acct%2Fa&mode=record&anchor=one&citation=two&q=risk&keep=one&keep=two",
    );
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);
    const stale = workspaceStub.lastProps;
    expect(stale).not.toBeNull();

    view.rerenderOwner(owner("client-b:loading", CLIENT_B, "loading", null));
    expect(screen.queryByLabelText("Served workspace bytes")).toBeNull();
    await waitFor(() =>
      expect(screen.getByLabelText("Location")).toHaveTextContent(
        "REPLACE:/knowledge/graph?keep=one&keep=two",
      ),
    );
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);

    act(() => {
      stale?.onFirstVisibleEntity("stale-default");
      stale?.onCitationFocusEntity("stale-citation");
      stale?.onSelectEntity("stale-selection");
      stale?.onFiltersChange({
        query: "stale-query",
        entityClass: "StaleClass",
        source: "stale-source",
        assurance: "confirmed",
      });
      stale?.onDepthChange(4);
    });
    expect(screen.getByLabelText("Location")).toHaveTextContent(
      "REPLACE:/knowledge/graph?keep=one&keep=two",
    );
    expect(screen.queryByLabelText("Served workspace bytes")).toBeNull();
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);

    view.rerenderOwner(owner("client-b:bob", CLIENT_B, "ok", "bob"));
    expect(await screen.findByLabelText("Served workspace bytes")).toBeInTheDocument();
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Location")).toHaveTextContent(
      "REPLACE:/knowledge/graph?keep=one&keep=two",
    );

    act(() => {
      stale?.onSelectEntity("stale-after-replacement-ready");
    });
    expect(screen.getByLabelText("Location")).toHaveTextContent(
      "REPLACE:/knowledge/graph?keep=one&keep=two",
    );
  });

  it("replaces the bare route with the first served entity and preserves unrelated state", async () => {
    renderPage("/knowledge/graph?utm=keep");
    await userEvent.click(screen.getByRole("button", { name: "Default served entity" }));
    const location = screen.getByLabelText("Location").textContent ?? "";
    expect(location).toContain("REPLACE:/knowledge/graph?");
    const params = new URLSearchParams(location.split("?")[1]);
    expect(params.get("entity")).toBe("served /?#");
    expect(params.get("utm")).toBe("keep");
    expect(params.get("mode")).toBe("graph");
  });

  it("replaces a same-mounted selected-to-bare route from the ready served window", async () => {
    workspaceStub.reconcileReadyWindow = true;
    renderPage("/knowledge/graph?entity=chosen%2Fone&utm=keep");
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Return to bare route" }));
    const location = screen.getByLabelText("Location").textContent ?? "";
    expect(location).toContain("REPLACE:/knowledge/graph?");
    const params = new URLSearchParams(location.split("?")[1]);
    expect(params.get("entity")).toBe("served /?#");
    expect(params.get("utm")).toBe("keep");
    expect(params.get("mode")).toBe("graph");
    expect(workspaceStub.graphEntities).toHaveBeenCalledTimes(1);
  });

  it("clears old anchors on deliberate selection while canonical hrefs retain context", async () => {
    renderPage(
      "/knowledge/graph?anchor=root%2Fone&anchor=%20root%20&anchor=root%2Fone&citation=first%2Fref&citation=second%20ref&q=acme&class=Account&source=crm&assurance=source&cursor=old&extra=keep",
    );
    const href = new URL(screen.getByLabelText("Opaque href").textContent ?? "", "https://x");
    expect(href.searchParams.getAll("anchor")).toEqual(["root/one", " root ", "root/one"]);
    expect(href.searchParams.get("entity")).toBe("opaque /?#");
    expect(href.searchParams.get("mode")).toBe("record");
    await userEvent.click(screen.getByRole("button", { name: "Select entity" }));
    const search = (screen.getByLabelText("Location").textContent ?? "").split("?")[1];
    const params = new URLSearchParams(search);
    expect(params.get("entity")).toBe("chosen /?#");
    expect(params.getAll("anchor")).toEqual([]);
    expect(params.getAll("citation")).toEqual(["first/ref", "second ref"]);
    expect(params.get("q")).toBe("acme");
    expect(params.get("class")).toBe("Account");
    expect(params.get("source")).toBe("crm");
    expect(params.get("assurance")).toBe("source");
    expect(params.get("extra")).toBe("keep");
    expect(params.has("cursor")).toBe(false);
  });

  it.each(["anchor=%20opaque%20&anchor=%20opaque%20", "citation=source%2Fone"])(
    "does not install a discovery default over existing graph context: %s",
    async (context) => {
      renderPage(`/knowledge/graph?${context}&extra=keep`);
      await userEvent.click(screen.getByRole("button", { name: "Default served entity" }));
      const location = screen.getByLabelText("Location").textContent ?? "";
      expect(location).toContain(`POP:/knowledge/graph?${context}&extra=keep`);
      expect(new URLSearchParams(location.split("?")[1]).has("entity")).toBe(false);
    },
  );

  it("replaces citation-only context with its exact served entity without losing the citation", async () => {
    renderPage("/knowledge/graph?citation=source%2Fone&citation=source%2Fone&q=risk&extra=keep");
    await userEvent.click(screen.getByRole("button", { name: "Resolve citation" }));

    const location = screen.getByLabelText("Location").textContent ?? "";
    expect(location).toContain("REPLACE:/knowledge/graph?");
    const params = new URLSearchParams(location.split("?")[1]);
    expect(params.get("entity")).toBe("cited /?#");
    expect(params.get("mode")).toBe("graph");
    expect(params.getAll("citation")).toEqual(["source/one", "source/one"]);
    expect(params.get("q")).toBe("risk");
    expect(params.get("extra")).toBe("keep");
  });

  it("writes all discovery filters to the URL without retaining a bound cursor", async () => {
    renderPage("/knowledge/graph?entity=acct%2Fa&cursor=old&extra=keep");
    await userEvent.click(screen.getByRole("button", { name: "Change filters" }));
    const search = (screen.getByLabelText("Location").textContent ?? "").split("?")[1];
    const params = new URLSearchParams(search);
    expect(params.get("q")).toBe("new query");
    expect(params.get("class")).toBe("Person");
    expect(params.get("source")).toBe("workday");
    expect(params.get("assurance")).toBe("confirmed");
    expect(params.get("entity")).toBe("acct/a");
    expect(params.get("extra")).toBe("keep");
    expect(params.has("cursor")).toBe(false);
  });
});
