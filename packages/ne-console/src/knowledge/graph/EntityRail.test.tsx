// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useLayoutEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import type { GraphEntityFacetsWire, GraphEntityListWire } from "../../api/types";
import type { PrincipalState } from "../../auth/PrincipalContext";
import { EntityRail } from "./EntityRail";
import { EMPTY_ENTITY_RAIL_FILTERS, type EntityRailFilters } from "./entityRailState";

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

function entity(key: string, displayName: string) {
  return {
    key,
    entity_class: "Account",
    display_name: displayName,
    source_system: "salesforce",
    assurance: "source",
  };
}

const ZULU = entity("entity-zulu", "Zulu Holdings");
const ALPHA = entity("entity-alpha", "Alpha Contact");
const BETA = entity("entity-beta", "Beta Company");

/** A rail client stub. `graphFacets` is served by default so the Class/Source
 * vocabulary never has to be re-stated in a test about row ownership. */
function railClient(overrides: Partial<ApiClient>): ApiClient {
  return {
    graphFacets: vi.fn().mockResolvedValue(FACETS_EMPTY),
    ...overrides,
  } as unknown as ApiClient;
}

const FACETS_EMPTY: GraphEntityFacetsWire = {
  entity_classes: [],
  source_systems: [],
  truncated: false,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function synchronousDeferred<T>() {
  let onResolve: ((value: T) => void) | null = null;
  const promise = {
    then(resolve: (value: T) => void) {
      onResolve = resolve;
      return { catch: () => promise };
    },
    catch() {
      return promise;
    },
  } as unknown as Promise<T>;
  return {
    promise,
    resolve(value: T) {
      onResolve?.(value);
    },
  };
}

function ResolveBeforePassiveCleanup({ resolve }: { resolve: () => void }) {
  useLayoutEffect(resolve, [resolve]);
  return null;
}

async function runDebounce() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300);
  });
}

/** The visible option labels of one named filter select. */
function optionLabels(name: string): string[] {
  const select = screen.getByLabelText(name) as HTMLSelectElement;
  return [...select.options].map((option) => option.textContent ?? "");
}

function facets(overrides: Partial<GraphEntityFacetsWire> = {}): GraphEntityFacetsWire {
  return { ...FACETS_EMPTY, ...overrides };
}

function Harness({
  initialFilters = EMPTY_ENTITY_RAIL_FILTERS,
  selectedEntityKey = null,
  onFirstVisibleEntity = () => {},
  onSelectEntity = () => {},
}: {
  initialFilters?: EntityRailFilters;
  selectedEntityKey?: string | null;
  onFirstVisibleEntity?: (key: string) => void;
  onSelectEntity?: (key: string) => void;
}) {
  const [filters, setFilters] = useState(initialFilters);
  return (
    <EntityRail
      filters={filters}
      selectedEntityKey={selectedEntityKey}
      onFiltersChange={setFilters}
      onFirstVisibleEntity={onFirstVisibleEntity}
      onSelectEntity={onSelectEntity}
    />
  );
}

function ServedWindowLifecycleHarness({ onFirst }: { onFirst: (key: string) => void }) {
  const [selectedEntityKey, setSelectedEntityKey] = useState<string | null>(null);
  return (
    <>
      <button type="button" onClick={() => setSelectedEntityKey(null)}>
        Return to bare route
      </button>
      <output aria-label="Selected entity">{selectedEntityKey ?? "bare"}</output>
      <EntityRail
        filters={EMPTY_ENTITY_RAIL_FILTERS}
        selectedEntityKey={selectedEntityKey}
        onFiltersChange={() => {}}
        onFirstVisibleEntity={(entityKey) => {
          onFirst(entityKey);
          setSelectedEntityKey(entityKey);
        }}
        onSelectEntity={setSelectedEntityKey}
      />
    </>
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  stub.principal = { status: "ok", principal: { id: "alice", groups: [] } };
  stub.client = {
    graphEntities: vi.fn(),
    graphFacets: vi.fn().mockResolvedValue({
      entity_classes: [],
      source_systems: [],
      truncated: false,
    }),
  };
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EntityRail", () => {
  // --- the server-derived Class / Source vocabulary ------------------------
  //
  // The shipped defect: Class listed the compile-time ne-schema registry
  // (Account/Document/Order/Person/Ticket) against a live graph of Customer/
  // Document/AutoLoan/ServicingNote — four dead options, three unfilterable
  // classes — and Source was a free-text box. Both halves are pinned here with
  // the same three arms the server battery uses.

  it("drives Class and Source from the served facets, never from a built-in list", async () => {
    vi.mocked((stub.client as ApiClient).graphFacets).mockResolvedValue(
      facets({
        entity_classes: ["AutoLoan", "Customer", "Document"],
        source_systems: ["m365-sharepoint"],
      }),
    );
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [ZULU],
      next_cursor: null,
    });
    render(<Harness />);
    await runDebounce();

    // (1) PRESENT in the served slice -> offered, both halves.
    expect(optionLabels("Class")).toEqual(["Any class", "AutoLoan", "Customer", "Document"]);
    expect(optionLabels("Source")).toEqual(["Any source", "m365-sharepoint"]);
    expect((stub.client as ApiClient).graphFacets).toHaveBeenCalledTimes(1);

    // (2) In the compile-time registry, but NOT served -> never offered. This
    // is the arm a "fix" that re-lists everything would fail.
    for (const registryClass of ["Account", "Person", "Ticket", "Order"]) {
      expect(optionLabels("Class")).not.toContain(registryClass);
    }

    // Both offered values are live filters, not decoration.
    fireEvent.change(screen.getByLabelText("Class"), { target: { value: "AutoLoan" } });
    await runDebounce();
    expect((stub.client as ApiClient).graphEntities).toHaveBeenLastCalledWith(
      { entity_class: "AutoLoan" },
      expect.any(AbortSignal),
    );
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "m365-sharepoint" } });
    await runDebounce();
    expect((stub.client as ApiClient).graphEntities).toHaveBeenLastCalledWith(
      { entity_class: "AutoLoan", source: "m365-sharepoint" },
      expect.any(AbortSignal),
    );
  });

  it("never carries one principal's vocabulary onto another principal", async () => {
    // (3) THE ACL ARM at the console layer: the served lists are per-principal,
    // so a change of owner refetches and the previous owner's classes/sources
    // must not survive the switch. A cache keyed on anything but the principal
    // would fail here.
    const clientA = railClient({
      graphEntities: vi.fn().mockResolvedValue({ entities: [], next_cursor: null }),
      graphFacets: vi
        .fn()
        .mockResolvedValue(
          facets({ entity_classes: ["Customer"], source_systems: ["m365-sharepoint"] }),
        ),
    });
    const clientB = railClient({
      graphEntities: vi.fn().mockResolvedValue({ entities: [], next_cursor: null }),
      graphFacets: vi
        .fn()
        .mockResolvedValue(facets({ entity_classes: ["Ticket"], source_systems: ["zendesk"] })),
    });
    stub.client = clientA;
    const view = render(<Harness />);
    await runDebounce();
    expect(optionLabels("Class")).toEqual(["Any class", "Customer"]);
    expect(optionLabels("Source")).toEqual(["Any source", "m365-sharepoint"]);

    stub.client = clientB;
    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    view.rerender(<Harness />);
    await runDebounce();
    expect(clientB.graphFacets).toHaveBeenCalledTimes(1);
    expect(optionLabels("Class")).toEqual(["Any class", "Ticket"]);
    expect(optionLabels("Source")).toEqual(["Any source", "zendesk"]);
    expect(optionLabels("Class")).not.toContain("Customer");
    expect(optionLabels("Source")).not.toContain("m365-sharepoint");
  });

  it("shows an em dash and a factual note when nothing is visible, never an invented list", async () => {
    vi.mocked((stub.client as ApiClient).graphFacets).mockResolvedValue(FACETS_EMPTY);
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [],
      next_cursor: null,
    });
    render(<Harness />);
    await runDebounce();

    expect(optionLabels("Class")).toEqual(["\u2014"]);
    expect(optionLabels("Source")).toEqual(["\u2014"]);
    expect(screen.getByLabelText("Class")).toBeDisabled();
    expect(screen.getByLabelText("Source")).toBeDisabled();
    expect(
      screen.getByText(
        "Class and Source are empty \u2014 no entities are visible to you in this graph.",
      ),
    ).toBeInTheDocument();
    const form = screen.getByRole("search");
    for (const registryClass of ["Account", "Person", "Document", "Ticket", "Order"]) {
      expect(form.textContent ?? "").not.toContain(registryClass);
    }
  });

  it("treats a refused vocabulary as unavailable, not as an empty graph", async () => {
    vi.mocked((stub.client as ApiClient).graphFacets).mockRejectedValue(
      new ApiError("refused", 403, "forbidden"),
    );
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [ZULU],
      next_cursor: null,
    });
    render(<Harness />);
    await runDebounce();

    expect(optionLabels("Class")).toEqual(["\u2014"]);
    expect(screen.getByLabelText("Class")).toBeDisabled();
    expect(
      screen.getByText(
        "Class and Source are unavailable \u2014 the server didn\u2019t return the values in this graph.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no entities are visible to you/)).toBeNull();
  });

  it("says so when the server capped its facet scan", async () => {
    vi.mocked((stub.client as ApiClient).graphFacets).mockResolvedValue(
      facets({ entity_classes: ["Customer"], source_systems: ["m365"], truncated: true }),
    );
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [],
      next_cursor: null,
    });
    render(<Harness />);
    await runDebounce();

    expect(
      screen.getByText(/the server capped its scan, so there may be more/),
    ).toBeInTheDocument();
    expect(optionLabels("Class")).toEqual(["Any class", "Customer"]);
  });

  it("keeps a route-supplied filter representable even when the server did not serve it", async () => {
    vi.mocked((stub.client as ApiClient).graphFacets).mockResolvedValue(
      facets({ entity_classes: ["Customer"], source_systems: ["m365-sharepoint"] }),
    );
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [],
      next_cursor: null,
    });
    render(
      <Harness
        initialFilters={{
          ...EMPTY_ENTITY_RAIL_FILTERS,
          entityClass: "Ticket",
          source: "zendesk",
        }}
      />,
    );
    await runDebounce();

    // The deep link's own filter is echoed back so the control cannot silently
    // display a different filter than the one being applied. It is the only
    // value not served by the server, and it came from the caller.
    expect(optionLabels("Class")).toEqual(["Any class", "Customer", "Ticket"]);
    expect((screen.getByLabelText("Class") as HTMLSelectElement).value).toBe("Ticket");
    expect(optionLabels("Source")).toEqual(["Any source", "m365-sharepoint", "zendesk"]);
  });

  it("sends search and filters to the server, preserves served rows, and defaults from its first row", async () => {
    const onFirst = vi.fn();
    const onSelect = vi.fn();
    const page: GraphEntityListWire = { entities: [ZULU, ALPHA], next_cursor: null };
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue(page);
    render(
      <Harness
        initialFilters={{
          query: "Alpha",
          entityClass: "Account",
          source: "salesforce",
          assurance: "source",
        }}
        onFirstVisibleEntity={onFirst}
        onSelectEntity={onSelect}
      />,
    );

    await runDebounce();
    expect((stub.client as ApiClient).graphEntities).toHaveBeenCalledWith(
      {
        q: "Alpha",
        entity_class: "Account",
        source: "salesforce",
        assurance: "source",
      },
      expect.any(AbortSignal),
    );
    const list = screen.getByRole("listbox", { name: "Visible entities" });
    const rows = within(list).getAllByRole("option");
    // CR-B9: no raw entity key in the row body — nine keys of mono noise per
    // screenful was the shipped rail's single biggest legibility cost. The key
    // survives on the row's own `title` and in the record pane.
    expect(rows.map((row) => row.textContent)).toEqual([
      "Zulu HoldingsAccount · salesforce",
      "Alpha ContactAccount · salesforce",
    ]);
    expect(rows.map((row) => row.getAttribute("title"))).toEqual(["entity-zulu", "entity-alpha"]);
    expect(onFirst).toHaveBeenCalledWith("entity-zulu");
    expect(onFirst).toHaveBeenCalledTimes(1);
    fireEvent.click(within(list).getByRole("option", { name: /Alpha Contact/ }));
    expect(onSelect).toHaveBeenCalledWith("entity-alpha");
  });

  it("reopens one served default window after selection returns to bare without refetching", async () => {
    const onFirst = vi.fn();
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [ZULU, ALPHA],
      next_cursor: null,
    });
    render(<ServedWindowLifecycleHarness onFirst={onFirst} />);

    await runDebounce();
    expect(onFirst).toHaveBeenCalledTimes(1);
    expect(onFirst).toHaveBeenLastCalledWith("entity-zulu");
    expect(screen.getByLabelText("Selected entity")).toHaveTextContent("entity-zulu");
    expect((stub.client as ApiClient).graphEntities).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Return to bare route" }));
    await act(async () => Promise.resolve());
    expect(onFirst).toHaveBeenCalledTimes(2);
    expect(onFirst).toHaveBeenLastCalledWith("entity-zulu");
    expect(screen.getByLabelText("Selected entity")).toHaveTextContent("entity-zulu");
    expect((stub.client as ApiClient).graphEntities).toHaveBeenCalledTimes(1);
  });

  it("appends the opaque next page only inside the same server filter window", async () => {
    vi.mocked((stub.client as ApiClient).graphEntities)
      .mockResolvedValueOnce({ entities: [ZULU], next_cursor: "opaque/+ cursor" })
      .mockResolvedValueOnce({ entities: [ALPHA], next_cursor: null });
    render(<Harness />);
    await runDebounce();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Load more" })));
    screen.getByRole("option", { name: /Alpha Contact/ });

    expect((stub.client as ApiClient).graphEntities).toHaveBeenNthCalledWith(
      2,
      { cursor: "opaque/+ cursor" },
      expect.any(AbortSignal),
    );
    const list = screen.getByRole("listbox", { name: "Visible entities" });
    expect(
      within(list)
        .getAllByRole("option")
        .map((row) => row.textContent),
    ).toEqual(["Zulu HoldingsAccount · salesforce", "Alpha ContactAccount · salesforce"]);
  });

  it("rejects an abort-insensitive old-filter cursor response", async () => {
    const oldCursor = deferred<GraphEntityListWire>();
    const signals: AbortSignal[] = [];
    vi.mocked((stub.client as ApiClient).graphEntities)
      .mockImplementationOnce((_query, signal) => {
        signals.push(signal as AbortSignal);
        return Promise.resolve({ entities: [ZULU], next_cursor: "old-cursor" });
      })
      .mockImplementationOnce((_query, signal) => {
        signals.push(signal as AbortSignal);
        return oldCursor.promise;
      })
      .mockImplementationOnce((_query, signal) => {
        signals.push(signal as AbortSignal);
        return Promise.resolve({ entities: [BETA], next_cursor: null });
      });
    render(<Harness />);
    await runDebounce();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Load more" })));
    fireEvent.change(screen.getByRole("searchbox", { name: "Find an entity" }), {
      target: { value: "Beta" },
    });
    await act(async () => Promise.resolve());
    expect(screen.queryByRole("option", { name: /Zulu Holdings/ })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading visible entities…");
    await runDebounce();
    expect(screen.getByRole("option", { name: /Beta Company/ })).toBeInTheDocument();
    expect(signals[1]?.aborted).toBe(true);

    await act(async () => oldCursor.resolve({ entities: [ALPHA], next_cursor: null }));
    expect(screen.queryByRole("option", { name: /Alpha Contact/ })).toBeNull();
    expect(
      within(screen.getByRole("listbox", { name: "Visible entities" })).getAllByRole("option"),
    ).toHaveLength(1);
  });

  it("rejects a delayed response after both client and principal ownership change", async () => {
    const ownerA = deferred<GraphEntityListWire>();
    const signalA: AbortSignal[] = [];
    const clientA = railClient({
      graphEntities: vi.fn((_query, signal) => {
        signalA.push(signal as AbortSignal);
        return ownerA.promise;
      }),
    });
    const clientB = railClient({
      graphEntities: vi.fn().mockResolvedValue({ entities: [BETA], next_cursor: null }),
    });
    const onFirst = vi.fn();
    stub.client = clientA;
    const view = render(<Harness onFirstVisibleEntity={onFirst} />);
    await runDebounce();

    stub.client = clientB;
    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    view.rerender(<Harness onFirstVisibleEntity={onFirst} />);
    await runDebounce();
    expect(screen.getByRole("option", { name: /Beta Company/ })).toBeInTheDocument();
    expect(signalA[0]?.aborted).toBe(true);

    await act(async () => ownerA.resolve({ entities: [ALPHA], next_cursor: null }));
    expect(screen.queryByRole("option", { name: /Alpha Contact/ })).toBeNull();
    expect(onFirst).toHaveBeenCalledTimes(1);
    expect(onFirst).toHaveBeenCalledWith("entity-beta");
  });

  it.each(["client", "subject"] as const)(
    "rejects delayed rows when only the owner %s axis changes",
    async (axis) => {
      const ownerA = deferred<GraphEntityListWire>();
      const signalA: AbortSignal[] = [];
      const graphEntitiesA = vi
        .fn((_query, signal) => {
          signalA.push(signal as AbortSignal);
          return ownerA.promise;
        })
        .mockImplementationOnce((_query, signal) => {
          signalA.push(signal as AbortSignal);
          return ownerA.promise;
        })
        .mockResolvedValue({ entities: [BETA], next_cursor: null });
      const clientA = railClient({ graphEntities: graphEntitiesA });
      const clientB = railClient({
        graphEntities: vi.fn().mockResolvedValue({ entities: [BETA], next_cursor: null }),
      });
      const onFirst = vi.fn();
      stub.client = clientA;
      stub.principal = { status: "ok", principal: { id: "alice", groups: [] } };
      const view = render(<Harness onFirstVisibleEntity={onFirst} />);
      await runDebounce();

      if (axis === "client") stub.client = clientB;
      else stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
      view.rerender(<Harness onFirstVisibleEntity={onFirst} />);
      await runDebounce();
      expect(screen.getByRole("option", { name: /Beta Company/ })).toBeInTheDocument();
      expect(signalA[0]?.aborted).toBe(true);

      await act(async () => ownerA.resolve({ entities: [ALPHA], next_cursor: null }));
      expect(screen.queryByRole("option", { name: /Alpha Contact/ })).toBeNull();
      expect(onFirst).toHaveBeenCalledTimes(1);
      expect(onFirst).toHaveBeenCalledWith("entity-beta");
    },
  );

  it("rejects an old owner resolved during a new principal/filter render before cleanup", async () => {
    const ownerA = synchronousDeferred<GraphEntityListWire>();
    const clientA = railClient({ graphEntities: vi.fn(() => ownerA.promise) });
    const clientB = railClient({
      graphEntities: vi.fn().mockResolvedValue({ entities: [BETA], next_cursor: null }),
    });
    const onFirst = vi.fn();
    stub.client = clientA;
    const view = render(
      <EntityRail
        filters={EMPTY_ENTITY_RAIL_FILTERS}
        selectedEntityKey={null}
        onFiltersChange={() => {}}
        onFirstVisibleEntity={onFirst}
        onSelectEntity={() => {}}
      />,
    );
    await runDebounce();

    stub.client = clientB;
    stub.principal = { status: "ok", principal: { id: "bob", groups: [] } };
    const nextFilters = { ...EMPTY_ENTITY_RAIL_FILTERS, query: "Beta" };
    const resolveOld = () => ownerA.resolve({ entities: [ALPHA], next_cursor: null });
    view.rerender(
      <>
        <EntityRail
          filters={nextFilters}
          selectedEntityKey={null}
          onFiltersChange={() => {}}
          onFirstVisibleEntity={onFirst}
          onSelectEntity={() => {}}
        />
        <ResolveBeforePassiveCleanup resolve={resolveOld} />
      </>,
    );

    expect(screen.queryByRole("option", { name: /Alpha Contact/ })).toBeNull();
    expect(onFirst).not.toHaveBeenCalled();
    await runDebounce();
    expect(screen.getByRole("option", { name: /Beta Company/ })).toBeInTheDocument();
    expect(onFirst).toHaveBeenCalledTimes(1);
    expect(onFirst).toHaveBeenCalledWith("entity-beta");
  });

  it("keeps genuine empty and filtered no-match copy distinct", async () => {
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [],
      next_cursor: null,
    });
    const view = render(<Harness />);
    await runDebounce();
    expect(screen.getByText("No visible entities are available.")).toBeInTheDocument();
    view.unmount();

    render(<Harness initialFilters={{ ...EMPTY_ENTITY_RAIL_FILTERS, query: "missing" }} />);
    await runDebounce();
    expect(screen.getByText("No visible entities match these filters.")).toBeInTheDocument();
  });

  it.each([
    [403, "Your role doesn’t include graph entity discovery."],
    [404, "The knowledge graph isn’t enabled on this deployment."],
    [503, "The knowledge graph isn’t enabled on this deployment."],
    [500, "Couldn’t load visible entities."],
  ])("renders status %i as its own truthful terminal state", async (status, message) => {
    vi.mocked((stub.client as ApiClient).graphEntities).mockRejectedValue(
      new ApiError("failed", status),
    );
    render(<Harness />);
    await runDebounce();
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" }) !== null).toBe(status === 500);
  });

  it("uses roving arrow focus on selectable buttons without changing served order", async () => {
    const onSelect = vi.fn();
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [ZULU, ALPHA],
      next_cursor: null,
    });
    render(<Harness onSelectEntity={onSelect} />);
    await runDebounce();
    const options = within(screen.getByRole("listbox", { name: "Visible entities" })).getAllByRole(
      "option",
    );
    options[0]?.focus();
    fireEvent.keyDown(options[0] as HTMLElement, { key: "ArrowDown" });
    expect(options[1]).toHaveFocus();
    expect(options[0]).toHaveAttribute("tabindex", "-1");
    expect(options[1]).toHaveAttribute("tabindex", "0");
    expect(options[1]?.tagName).toBe("BUTTON");
    fireEvent.click(options[1] as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith("entity-alpha");
  });

  it("gives a non-served deep-link selection one deterministic keyboard entry point", async () => {
    vi.mocked((stub.client as ApiClient).graphEntities).mockResolvedValue({
      entities: [ZULU, ALPHA],
      next_cursor: null,
    });
    render(<Harness selectedEntityKey="entity-outside-window" />);
    await runDebounce();
    const options = within(screen.getByRole("listbox", { name: "Visible entities" })).getAllByRole(
      "option",
    );
    expect(options[0]).toHaveAttribute("tabindex", "0");
    expect(options[1]).toHaveAttribute("tabindex", "-1");
  });
});
