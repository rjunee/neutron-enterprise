// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 Dual-Key Compare — pane + control tests. The OIDC popup is mocked at the
// SPEC-designated src/api seam (acquireCompareKey); createCompareClient stays REAL
// so the compare bearer reaches the wire and MSW routes each key's response
// INDEPENDENTLY (C7 — the client never diffs privileged data). Assertions route
// through the DOM mirror (the "Compare nodes" list + ticker + placards), never
// canvas pixels (jsdom has no 2D context). setup.ts runs onUnhandledRequest:
// "error", so every governed call is explicitly mocked per test.

import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeConfigContext } from "../../AppBootstrap";
import { AuthStub } from "../../test/authStub";
import { server } from "../../test/mocks/server";
import { GRAPH_FIXTURE } from "../../test/mocks/graphFixtures";
import { VERIFIED_INVOKE_RESULT } from "../../test/mocks/ontologyFixtures";
import {
  COMPARE_TOKEN_B,
  GRAPH_FIXTURE_B_ONLY,
  GRAPH_FIXTURE_TRIMMED_B,
  PRIMARY_TOKEN,
  TEST_RUNTIME_CONFIG,
  graphQueryByBearer,
  makeCompareKeyStub,
  retrieveByBearer,
  verifiedInvokeByBearer,
} from "../../test/mocks/compareFixtures";
import type { RetrievalResult } from "../../api/types";
import { ComparePane } from "./ComparePane";
import { CompareControl } from "./CompareControl";
import { acquireCompareKey } from "../../api/compareAuth";
import type { CompareKey } from "../../api/compareAuth";

// Mock ONLY acquireCompareKey at the src/api seam; createCompareClient stays real
// (it delegates to the real createApiClient so MSW sees the real compare bearer).
vi.mock("../../api/compareAuth", async (importActual) => {
  const actual = await importActual<typeof import("../../api/compareAuth")>();
  return { ...actual, acquireCompareKey: vi.fn() };
});
const acquireMock = vi.mocked(acquireCompareKey);

const PLACARD =
  "Both views are exactly what the server sent each keyholder. The client never saw what was removed.";

const RETRIEVAL_FULL: RetrievalResult = {
  chunks: [],
  citations: [
    { chunk_id: "chunk-alpha", document_id: "doc-alpha", source_system: "sp", source_ref: "ref/a" },
    { chunk_id: "chunk-bravo", document_id: "doc-bravo", source_system: "sp", source_ref: "ref/b" },
  ],
  graph_paths: [],
  provisional_source_count: 0,
};
const RETRIEVAL_TRIMMED: RetrievalResult = {
  chunks: [],
  citations: [
    { chunk_id: "chunk-alpha", document_id: "doc-alpha", source_system: "sp", source_ref: "ref/a" },
  ],
  graph_paths: [],
  provisional_source_count: 0,
};
const INVOKE_TRIMMED = {
  ...VERIFIED_INVOKE_RESULT,
  answer: {
    ...VERIFIED_INVOKE_RESULT.answer,
    rows: [["urn:ne:entity:acct-acme", "Acme Corp"]],
    citations: ["urn:ne:entity:acct-acme"],
  },
};

function renderPane(overrides: Partial<React.ComponentProps<typeof ComparePane>> = {}) {
  const onExit = vi.fn();
  const compareKey = overrides.compareKey ?? makeCompareKeyStub(COMPARE_TOKEN_B);
  const props: React.ComponentProps<typeof ComparePane> = {
    compareKey,
    anchorKeys: ["acct-acme"],
    anchorCitations: [],
    chunkIds: [],
    verifiedRecord: null,
    onExit,
    ...overrides,
  };
  const utils = render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={TEST_RUNTIME_CONFIG}>
        <MemoryRouter>
          <ComparePane {...props} />
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
  return { ...utils, onExit, compareKey };
}

// The compare control and the pane it opens, wired the way a host screen wires
// them. §7 does not put this flow on Library — §6 puts it on Search — so the
// two tests that used to mount the Library page for its control now mount the
// control itself, which is the unit they were always about.
function CompareHost() {
  const [key, setKey] = useState<CompareKey | null>(null);
  if (key === null) return <CompareControl hasGraph onKeyAcquired={setKey} />;
  return (
    <ComparePane
      compareKey={key}
      anchorKeys={["acct-acme"]}
      anchorCitations={[]}
      chunkIds={[]}
      verifiedRecord={null}
      onExit={() => setKey(null)}
    />
  );
}

function renderHost() {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={TEST_RUNTIME_CONFIG}>
        <MemoryRouter>
          <CompareHost />
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

beforeEach(() => {
  acquireMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Dual-Key Compare — control opens the pane at the src/api seam", () => {
  it("COMPARE runs the second-persona auth and opens the pane", async () => {
    acquireMock.mockResolvedValue(makeCompareKeyStub(COMPARE_TOKEN_B));
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));

    renderHost();

    const compareBtn = await screen.findByRole("button", { name: "Dual-key compare" });
    await waitFor(() => expect(compareBtn).not.toBeDisabled());
    await userEvent.click(compareBtn);

    expect(acquireMock).toHaveBeenCalledTimes(1);
    expect(acquireMock).toHaveBeenCalledWith(TEST_RUNTIME_CONFIG);
    // The pane header shows the key-B persona label immediately.
    expect(await screen.findByText("beta@example.com")).toBeInTheDocument();
  });
});

describe("Dual-Key Compare — the pane", () => {
  it("runs the IDENTICAL graph query under EACH bearer", async () => {
    const captured: Array<{ auth: string | null; body: unknown }> = [];
    server.use(
      http.post("*/api/v1/graph/query", async ({ request }) => {
        const auth = request.headers.get("Authorization");
        captured.push({ auth, body: await request.json() });
        return HttpResponse.json(
          auth === `Bearer ${PRIMARY_TOKEN}` ? GRAPH_FIXTURE : GRAPH_FIXTURE_TRIMMED_B,
        );
      }),
    );
    renderPane();

    await waitFor(() => expect(captured).toHaveLength(2));
    const auths = captured.map((c) => c.auth).sort();
    expect(auths).toEqual([`Bearer ${COMPARE_TOKEN_B}`, `Bearer ${PRIMARY_TOKEN}`]);
    // Deep-equal query bodies — the SAME governed query under both keys.
    expect(captured[0]!.body).toEqual(captured[1]!.body);
    expect(captured[0]!.body).toEqual({ anchor_keys: ["acct-acme"], hops: 2 });
  });

  it("contracts to key B's nodes — a node absent under B implodes, retained stays", async () => {
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));
    renderPane();

    const list = await screen.findByRole("list", { name: "Compare nodes" });
    // doc-contract is in A (8) but not B (5): it implodes → imploded.
    await waitFor(
      () =>
        expect(list.querySelector('[data-entity-key="doc-contract"]')).toHaveAttribute(
          "data-compare-state",
          "imploded",
        ),
      { timeout: 2000 },
    );
    // acct-acme is in both keys → retained throughout.
    expect(list.querySelector('[data-entity-key="acct-acme"]')).toHaveAttribute(
      "data-compare-state",
      "retained",
    );
  });

  it("rolls the ticker down to key B's server-returned counts", async () => {
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));
    renderPane();
    // GRAPH_FIXTURE_TRIMMED_B has 5 nodes / 6 edges — the SERVER's B counts.
    await waitFor(() => expect(screen.getByText("5 NODES · 6 EDGES")).toBeInTheDocument(), {
      timeout: 2000,
    });
  });

  it("renders chunk and verified-answer row counts side-by-side under both keys", async () => {
    server.use(
      graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B),
      retrieveByBearer(RETRIEVAL_FULL, RETRIEVAL_TRIMMED),
      verifiedInvokeByBearer(VERIFIED_INVOKE_RESULT, INVOKE_TRIMMED),
    );
    renderPane({
      chunkIds: ["chunk-alpha", "chunk-bravo"],
      verifiedRecord: {
        template_id: "accounts-by-name",
        version: 2,
        args: { q_name: "Acme Corp" },
        row_count: 2,
      },
    });

    const chunkRow = await screen.findByText("CHUNKS");
    const chunkParent = chunkRow.closest(".ne-compare__stat-row")!;
    expect(within(chunkParent as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(chunkParent as HTMLElement).getByText("1")).toBeInTheDocument();

    const rowsRow = screen.getByText("VERIFIED ROWS");
    const rowsParent = rowsRow.closest(".ne-compare__stat-row")!;
    expect(within(rowsParent as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(rowsParent as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("renders honest unavailable lines when no session chunks / no recorded invoke exist", async () => {
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));
    renderPane(); // chunkIds [] and verifiedRecord null

    expect(
      await screen.findByText("No chunks in session — search and anchor first"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "No verified answer has been invoked this session — row-count compare unavailable",
      ),
    ).toBeInTheDocument();
  });

  it("renders the honest placard verbatim and no red-X element", async () => {
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));
    const { container } = renderPane();
    expect(await screen.findByText(PLACARD)).toBeInTheDocument();
    // No red-X denial mark anywhere — the removal is honest, not a rejection.
    expect(screen.queryByText("×")).not.toBeInTheDocument();
    expect(screen.queryByText("✕")).not.toBeInTheDocument();
    expect(container.querySelector(".ne-scram, [data-red-x]")).toBeNull();
  });

  it("KEY WITHDRAWN discards the second token and exits", async () => {
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));
    const compareKey = makeCompareKeyStub(COMPARE_TOKEN_B);
    const { onExit } = renderPane({ compareKey });

    const withdraw = await screen.findByRole("button", { name: "WITHDRAW KEY" });
    await userEvent.click(withdraw);

    expect(compareKey.withdraw).toHaveBeenCalledTimes(1);
    expect(compareKey.getAccessToken()).toBeUndefined();
    // The token was never persisted to any browser storage.
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain(COMPARE_TOKEN_B);
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(COMPARE_TOKEN_B);

    const stamp = await screen.findByText("KEY WITHDRAWN");
    expect(stamp).toBeInTheDocument();
    await userEvent.click(stamp);
    expect(onExit).toHaveBeenCalled();
  });

  it("collapses the contraction to instant final state under prefers-reduced-motion", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_B_ONLY));
    renderPane();

    const list = await screen.findByRole("list", { name: "Compare nodes" });
    // Final states are set synchronously on mount — never an intermediate
    // "imploding" frame.
    expect(list.querySelector('[data-entity-key="doc-contract"]')).toHaveAttribute(
      "data-compare-state",
      "imploded",
    );
    expect(list.querySelector('[data-entity-key="acct-acme"]')).toHaveAttribute(
      "data-compare-state",
      "retained",
    );
    expect(list.querySelector('[data-entity-key="acct-gamma"]')).toHaveAttribute(
      "data-compare-state",
      "b-only",
    );
    expect(list.querySelector('[data-compare-state="imploding"]')).toBeNull();
  });

  it("keeps the honest key-expired state when key B 401s on an auxiliary request", async () => {
    // Both graph queries succeed, but key B's token expires on the chunk
    // retrieve. The pane must NOT fall through to a false "active" comparison
    // with silently-missing B counts — it stays on the honest KEY B EXPIRED state.
    server.use(
      graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B),
      http.post("*/api/v1/brain/retrieve", ({ request }) => {
        const auth = request.headers.get("Authorization");
        if (auth === `Bearer ${PRIMARY_TOKEN}`) return HttpResponse.json(RETRIEVAL_FULL);
        return HttpResponse.json({ detail: "token expired" }, { status: 401 });
      }),
    );
    renderPane({ chunkIds: ["chunk-alpha", "chunk-bravo"] });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("KEY B EXPIRED");
    // No fabricated "active" comparison — the placard never rendered.
    expect(screen.queryByText(PLACARD)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Compare nodes" })).not.toBeInTheDocument();
  });

  it("renders a key-B 403 as an honest refusal, with no contraction", async () => {
    server.use(
      http.post("*/api/v1/graph/query", ({ request }) => {
        const auth = request.headers.get("Authorization");
        if (auth === `Bearer ${PRIMARY_TOKEN}`) return HttpResponse.json(GRAPH_FIXTURE);
        return HttpResponse.json({ detail: "principal not permitted" }, { status: 403 });
      }),
    );
    renderPane();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("key B: HTTP 403");
    expect(alert).toHaveTextContent("principal not permitted");
    // No fabricated trimmed view.
    expect(screen.queryByRole("list", { name: "Compare nodes" })).not.toBeInTheDocument();
  });
});

describe("Dual-Key Compare — popup failure", () => {
  it("renders an honest error and opens no pane when the popup fails", async () => {
    acquireMock.mockRejectedValue(new Error("popup blocked"));
    server.use(graphQueryByBearer(GRAPH_FIXTURE, GRAPH_FIXTURE_TRIMMED_B));

    renderHost();

    const compareBtn = await screen.findByRole("button", { name: "Dual-key compare" });
    await waitFor(() => expect(compareBtn).not.toBeDisabled());
    await userEvent.click(compareBtn);

    expect(await screen.findByText("popup blocked")).toBeInTheDocument();
    // No pane — the honest placard never rendered.
    expect(screen.queryByText(PLACARD)).not.toBeInTheDocument();
  });
});
