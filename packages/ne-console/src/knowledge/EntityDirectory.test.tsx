// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — Library › Entities, with nothing open: the way into the corpus.
//
// The assertions that matter are the ones about what this screen must NOT do.
// It must not run a template that takes an argument (the console would be
// choosing the argument, and a subset would be presented as the whole). It must
// not link a row whose citation is not an entity urn (the key would be a guess).
// It must not manufacture a browse when the deployment registers none. And a
// broken read must not be dressed as a role boundary.
//
// Every ordering and identity assertion reads ROW IDENTITY, never a count.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { SubgraphWire, VerifiedInvokeWire, VerifiedTemplatesWire } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { LibraryPage } from "./LibraryPage";

/** Three zero-argument templates and one that takes an argument — the shape the
 * Acme deployment actually registers (accounts/orders), plus a steward-shaped
 * one (`tickets-triage`) whose SERVED order is identifier-first WITH a declared
 * label — the case that proves the display rule reorders rather than echoes. */
const CATALOG: VerifiedTemplatesWire = {
  enabled: true,
  registry_hash: "reg-hash",
  templates: [
    {
      template_id: "orders-directory",
      version: 1,
      title: "Every order visible to the caller, with its account and status.",
      tool_name: "read.kg.orders-directory",
      args_schema: { type: "object", properties: {} },
      columns: ["order", "account", "status"],
      citation_var: "order",
      // No human-readable column exists on this template: the DEGRADE case.
      label_var: null,
      max_rows: null,
    },
    {
      template_id: "accounts-by-name",
      version: 2,
      title: "Accounts whose name matches a query string.",
      tool_name: "read.kg.accounts-by-name",
      args_schema: {
        type: "object",
        required: ["q_name"],
        properties: { q_name: { type: "string" } },
      },
      columns: ["account", "name"],
      citation_var: "account",
      label_var: null,
      max_rows: null,
    },
    {
      template_id: "accounts-directory",
      version: 1,
      title: "Every account visible to the caller, with its name.",
      tool_name: "read.kg.accounts-directory",
      args_schema: { type: "object", properties: {} },
      columns: ["name", "account"],
      citation_var: "account",
      label_var: "name",
      max_rows: null,
    },
    {
      template_id: "tickets-triage",
      version: 1,
      title: "Open tickets, with their subject.",
      tool_name: "read.kg.tickets-triage",
      args_schema: { type: "object", properties: {} },
      // Identifier-FIRST served order + a declared label: display must lead
      // with `subject` and demote `ticket` to last — not echo this order.
      columns: ["ticket", "priority", "subject"],
      citation_var: "ticket",
      label_var: "subject",
      max_rows: null,
    },
  ],
};

const ACCOUNTS: VerifiedInvokeWire = {
  answer: {
    template_id: "accounts-directory",
    template_version: 1,
    args: {},
    columns: ["name", "account"],
    // Served deliberately NOT alphabetically: the table's order is the server's.
    rows: [
      ["Harbor Metals LLC", "urn:ne:entity:k-harbor"],
      ["Cascade Ventures LLC", "urn:ne:entity:k-cascade"],
      ["Silverline Freight Inc", "urn:ne:entity:k-silver"],
    ],
    citations: ["urn:ne:entity:k-harbor", "urn:ne:entity:k-cascade", "urn:ne:entity:k-silver"],
    truncated: false,
    row_limit: 500,
    returned_rows: 3,
    total_rows: 3,
    truncation_reason: null,
    config_hash: "cfg",
    ontology_version: "onto-1",
    executed_at: "2026-08-03T09:00:00Z",
  },
  replay_match: true,
};

const TICKETS: VerifiedInvokeWire = {
  answer: {
    ...ACCOUNTS.answer,
    template_id: "tickets-triage",
    columns: ["ticket", "priority", "subject"],
    rows: [["urn:ne:entity:k-tic-9", "high", "Loading dock camera offline"]],
    citations: ["urn:ne:entity:k-tic-9"],
    returned_rows: 1,
    total_rows: 1,
  },
  replay_match: true,
};

const ORDERS: VerifiedInvokeWire = {
  answer: {
    ...ACCOUNTS.answer,
    template_id: "orders-directory",
    columns: ["order", "account", "status"],
    rows: [["urn:ne:entity:k-ord-1", "urn:ne:entity:k-harbor", "placed"]],
    citations: ["urn:ne:entity:k-ord-1"],
    returned_rows: 1,
    total_rows: 1,
  },
  replay_match: true,
};

const HARBOR: SubgraphWire = {
  nodes: [
    {
      key: "k-harbor",
      entity_class: "Account",
      display_name: "Harbor Metals LLC",
      source_system: "acme-fixture",
      source_ref: "acme/accounts:acct-0008:acct-0008",
      properties: { region: "EU" },
      assurance: "source",
    },
  ],
  edges: [],
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

/** One request recorder per test, so "which template did it run" is an
 * assertion about a real request body rather than about what is on screen. */
function stubDirectory(catalog: VerifiedTemplatesWire = CATALOG) {
  const invoked: Array<Record<string, unknown>> = [];
  server.use(
    http.get("*/api/v1/verified/templates", () => HttpResponse.json(catalog)),
    http.post("*/api/v1/verified/invoke", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      invoked.push(body);
      return HttpResponse.json(
        body.template_id === "orders-directory"
          ? ORDERS
          : body.template_id === "tickets-triage"
            ? TICKETS
            : ACCOUNTS,
      );
    }),
    http.get("*/api/v1/graph/neighborhood", () => HttpResponse.json(HARBOR)),
  );
  return invoked;
}

/** The directory picker — queried by ROLE, because the table beside it carries
 * the same accessible name and a label query cannot tell them apart. */
function picker(): HTMLElement {
  return screen.getByRole("combobox", { name: "Verified query" });
}

/** The directory table's rows, as the cells that identify each one — awaited
 * past the skeleton, so an assertion is never made against a loading table. */
async function directoryRowsOnScreen(): Promise<string[][]> {
  const table = await screen.findByRole("table", { name: "Directory" });
  await waitFor(() => expect(table.querySelector(".ne-tr--skeleton")).toBeNull());
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => (cell.textContent ?? "").trim()),
    );
}

describe("Entities — the way in", () => {
  it("runs a registered zero-argument template and shows the rows the server returned", async () => {
    const invoked = stubDirectory();
    renderAt("/library");

    // Asserted POSITIONALLY: the label column leads, while the immutable key is
    // secondary without making its machine value the row's primary content.
    expect(await directoryRowsOnScreen()).toEqual([
      ["Harbor Metals LLC", "k-harbor"],
      ["Cascade Ventures LLC", "k-cascade"],
      ["Silverline Freight Inc", "k-silver"],
    ]);
    // The body carries EXACTLY the governed keys and NO invented argument.
    expect(invoked).toEqual([{ template_id: "accounts-directory", version: 1, args: {} }]);
    expect(screen.getByText("3 rows")).toBeInTheDocument();
  });

  it("leads with the declared label column and demotes the identifier to an exact-key affordance", async () => {
    // The decisive case: tickets-triage is SERVED identifier-first
    // (["ticket", "priority", "subject"]) and declares label_var="subject".
    // Today's broken layout — echoing the served order — fails this test; a
    // layout that lost the identifier would fail the tooltip assertion too.
    stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();

    await userEvent.selectOptions(picker(), "Open tickets, with their subject.");
    await waitFor(async () =>
      expect(await directoryRowsOnScreen()).toEqual([
        ["Loading dock camera offline", "high", "k-tic-9"],
      ]),
    );
    const table = screen.getByRole("table", { name: "Directory" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((h) => (h.textContent ?? "").trim()),
    ).toEqual(["subject", "priority", "ticket"]);
    expect(within(table).getByText("k-tic-9")).toHaveAttribute(
      "title",
      "Citation: urn:ne:entity:k-tic-9",
    );
  });

  it("keeps duplicate labels distinguishable with visible copyable keys and an exact keyboard deep link", async () => {
    const entityKey = "5eebc893-2060-5265-8dda-40c3e2fb23c1";
    const secondKey = "6f1b751f-b43f-5d11-9813-7a3a20a04f88";
    let asked: string | null = null;
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({
          ...ACCOUNTS,
          answer: {
            ...ACCOUNTS.answer,
            rows: [
              ["Meridian Horizon Health", `urn:ne:entity:${entityKey}`],
              ["Meridian Horizon Health", `urn:ne:entity:${secondKey}`],
            ],
            citations: [`urn:ne:entity:${entityKey}`, `urn:ne:entity:${secondKey}`],
            returned_rows: 2,
            total_rows: 2,
          },
        }),
      ),
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        asked = new URL(request.url).searchParams.get("entity");
        return HttpResponse.json(HARBOR);
      }),
    );
    renderAt("/library");

    const table = await screen.findByRole("table", { name: "Directory" });
    await waitFor(() => expect(table.querySelector(".ne-tr--skeleton")).toBeNull());
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(`Meridian Horizon Health${entityKey}`);
    expect(rows[1]).toHaveTextContent(`Meridian Horizon Health${secondKey}`);
    const visibleKey = within(rows[0]!).getByText(entityKey);
    expect(visibleKey.tagName).toBe("CODE");
    expect(visibleKey).toBeVisible();
    expect(visibleKey).toHaveAttribute("title", `Citation: urn:ne:entity:${entityKey}`);

    rows[0]!.focus();
    expect(rows[0]).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(asked).toBe(entityKey));
  });

  it("says what the picker's entries are — authored queries, not object types", async () => {
    stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();
    expect(screen.getByText(/Verified queries authored for this deployment/)).toBeInTheDocument();
  });

  it("never offers a template that takes an argument", async () => {
    stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();

    const options = within(picker()).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Every account visible to the caller, with its name.",
      "Every order visible to the caller, with its account and status.",
      "Open tickets, with their subject.",
    ]);
  });

  it("opens the record of the row activated, through that row's own citation", async () => {
    let asked: string | null = null;
    stubDirectory();
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        asked = new URL(request.url).searchParams.get("entity");
        return HttpResponse.json(HARBOR);
      }),
    );
    renderAt("/library");
    await directoryRowsOnScreen();
    const table = screen.getByRole("table", { name: "Directory" });
    const harbor = within(table)
      .getAllByRole("row")
      .find((row) => (row.textContent ?? "").includes("Harbor Metals LLC"))!;
    await userEvent.click(harbor);

    expect(await screen.findByRole("heading", { name: "Harbor Metals LLC" })).toBeInTheDocument();
    await waitFor(() => expect(asked).toBe("k-harbor"));
  });

  it("switches to another registered directory and runs THAT one", async () => {
    const invoked = stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();

    await userEvent.selectOptions(
      picker(),
      "Every order visible to the caller, with its account and status.",
    );
    await waitFor(() =>
      expect(invoked.at(-1)).toEqual({ template_id: "orders-directory", version: 1, args: {} }),
    );
    // The columns are the NEW template's own, not the previous one's — and this
    // template declares NO label (label_var null), so the served order stands
    // VERBATIM: identifier-led, no crash, and no label invented from another
    // column. This is the degrade case, asserted positionally.
    const table = await screen.findByRole("table", { name: "Directory" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((h) => (h.textContent ?? "").trim()),
    ).toEqual(["order", "account", "status"]);
  });

  it("filters the rows the server returned, and says so in the count", async () => {
    stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();

    await userEvent.type(screen.getByLabelText("Filter these rows…"), "Cascade");
    expect(await directoryRowsOnScreen()).toEqual([["Cascade Ventures LLC", "k-cascade"]]);
    expect(screen.getByText("1 of 3 rows")).toBeInTheDocument();
  });

  it("distinguishes a filter that matched nothing from a directory that IS empty", async () => {
    stubDirectory();
    renderAt("/library");
    await directoryRowsOnScreen();

    await userEvent.type(screen.getByLabelText("Filter these rows…"), "zzzz");
    expect(await screen.findByText("No rows here match that filter.")).toBeInTheDocument();
    expect(screen.queryByText("This directory returned no rows.")).toBeNull();
  });

  it("renders the SERVER's replay verdict and computes no part of it", async () => {
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({ ...ACCOUNTS, replay_match: false }),
      ),
    );
    renderAt("/library");
    await directoryRowsOnScreen();
    expect(screen.getByText("Re-execution returned a different result.")).toBeInTheDocument();
    expect(screen.queryByText("Re-executed and reproduced.")).toBeNull();
  });

  it("states the server's cap beside the rows it shortened", async () => {
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({
          ...ACCOUNTS,
          answer: {
            ...ACCOUNTS.answer,
            truncated: true,
            returned_rows: 3,
            total_rows: 120,
            truncation_reason: "rows",
          },
        }),
      ),
    );
    renderAt("/library");
    await directoryRowsOnScreen();
    expect(screen.getByText("Showing 3 of 120 rows.")).toBeInTheDocument();
  });
});

describe("Entities — the directory does not invent one", () => {
  it("falls back to the spec's idle line when nothing runnable is registered", async () => {
    stubDirectory({
      ...CATALOG,
      templates: CATALOG.templates.filter((t) => t.template_id === "accounts-by-name"),
    });
    renderAt("/library");
    expect(
      await screen.findByText(
        "No verified query on this deployment can browse without an input yet — queries are " +
          "authored per engagement. Open an entity from a citation in Agents or Search.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Directory" })).toBeNull();
  });

  it("names the deployment fact when verified answers are off here", async () => {
    stubDirectory({ enabled: false, registry_hash: null, templates: [] });
    renderAt("/library");
    expect(
      await screen.findByText("Verified answers are off on this deployment."),
    ).toBeInTheDocument();
  });

  it("leaves a row unlinked when its citation is not an entity urn", async () => {
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({
          ...ACCOUNTS,
          answer: {
            ...ACCOUNTS.answer,
            rows: [["Harbor Metals LLC", "https://crm.example/acct/7"]],
            citations: ["https://crm.example/acct/7"],
            returned_rows: 1,
            total_rows: 1,
          },
        }),
      ),
    );
    renderAt("/library");
    await directoryRowsOnScreen();
    const table = screen.getByRole("table", { name: "Directory" });
    const row = within(table).getAllByRole("row")[1]!;
    // The served value is rendered as it arrived — not shortened to a key that
    // was never in it.
    expect(row.textContent).toContain("https://crm.example/acct/7");
    await userEvent.click(row);
    // Activating it opens NOTHING: there is no key to open, and one is not
    // guessed from the cell beside it.
    expect(screen.queryByRole("heading", { name: "Harbor Metals LLC" })).toBeNull();
    expect(await screen.findByRole("table", { name: "Directory" })).toBeInTheDocument();
  });
});

describe("Entities — a refusal is a 403 and nothing else", () => {
  it("renders the server's refusal when the server refuses the run", async () => {
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({ detail: "tool-denied" }, { status: 403 }),
      ),
    );
    renderAt("/library");
    expect(
      await screen.findByText(/Your role doesn.t include verified answers/),
    ).toBeInTheDocument();
  });

  it("does NOT call a broken run a role boundary", async () => {
    stubDirectory();
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt("/library");
    expect(await screen.findByText("Couldn’t load the directory.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("does NOT call a broken catalog read a role boundary", async () => {
    server.use(
      http.get("*/api/v1/verified/templates", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt("/library");
    expect(await screen.findByText("Couldn’t load the directory.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });
});
