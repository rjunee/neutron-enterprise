// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";
import type { VerifiedInvokeWire, VerifiedTemplateWire, VerifiedTemplatesWire } from "../api/types";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { VerifiedQueriesPage } from "./VerifiedQueriesPage";

const COUNTY_QUERY: VerifiedTemplateWire = {
  template_id: "customers-in-counties",
  version: 3,
  title: "Customers in one validated pilot county.",
  tool_name: "read.kg.customers-in-counties",
  args_schema: {
    type: "object",
    additionalProperties: false,
    required: ["county"],
    properties: {
      county: { title: "County", type: "string", pattern: "[A-Za-z ]{1,64}", maxLength: 64 },
      note: { title: "Note", type: "string", maxLength: 64 },
    },
  },
  columns: ["customer", "name"],
  citation_var: "customer",
  label_var: "name",
  max_rows: 100,
};

const ID_NEEDLE_QUERY: VerifiedTemplateWire = {
  ...COUNTY_QUERY,
  template_id: "needle-only-in-technical-id",
  version: 1,
  title: "Every visible account.",
  args_schema: { type: "object", additionalProperties: false, properties: {} },
};

const CATALOG: VerifiedTemplatesWire = {
  enabled: true,
  registry_hash: "r".repeat(64),
  templates: [COUNTY_QUERY, ID_NEEDLE_QUERY],
};

const RESULT: VerifiedInvokeWire = {
  replay_match: true,
  answer: {
    template_id: COUNTY_QUERY.template_id,
    template_version: COUNTY_QUERY.version,
    args: { county: "Richmond" },
    columns: ["customer", "name"],
    rows: [["urn:ne:entity:customer-1", "Avery Stone"]],
    citations: ["urn:ne:entity:customer-1"],
    truncated: false,
    row_limit: 100,
    returned_rows: 1,
    total_rows: 1,
    truncation_reason: null,
    config_hash: "c".repeat(64),
    ontology_version: "meridian-v1",
    executed_at: "2026-08-20T12:00:00Z",
  },
};

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="Current location">{`${location.pathname}${location.search}`}</output>;
}

function renderPage(entry = "/verified-queries") {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={[entry]}>
        <PrincipalProvider>
          <main>
            <VerifiedQueriesPage />
          </main>
          <LocationProbe />
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
}

function catalogHandler(catalog = CATALOG) {
  server.use(http.get("*/api/v1/verified/templates", () => HttpResponse.json(catalog)));
}

describe("Verified Queries page interior", () => {
  it("titles the screen and keeps Create query truthfully disabled", async () => {
    catalogHandler();
    renderPage();
    expect(screen.getByRole("heading", { name: "Verified Queries", level: 1 })).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Create query" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(
      "Authoring is not enabled: templates enter this catalog only through steward review.",
    );
    // The two-store signpost states WHAT this page reads (the Tier-3 verified
    // store) and where the operational graph lives instead.
    expect(screen.getByText(/Tier-3 verified store/)).toBeInTheDocument();
    expect(
      await screen.findByRole("complementary", { name: "Verified query catalog" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Verified query workspace" })).toBeInTheDocument();
  });

  it("selects from the URL, never auto-runs, and posts exact id@version with optional blanks omitted", async () => {
    catalogHandler();
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(RESULT);
      }),
    );
    renderPage("/verified-queries?template=customers-in-counties");
    expect(
      await screen.findByRole("heading", { name: "Customers in counties", level: 2 }),
    ).toBeInTheDocument();
    // The full served description stays in the detail pane, demoted from the title.
    expect(screen.getByText(COUNTY_QUERY.title)).toBeInTheDocument();
    expect(bodies).toHaveLength(0);
    await userEvent.type(screen.getByLabelText(/County/), "Richmond");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    await screen.findByRole("heading", { name: "Verified result" });
    expect(bodies).toEqual([
      { template_id: "customers-in-counties", version: 3, args: { county: "Richmond" } },
    ]);
    expect(screen.getByText("Server replay matched this result.")).toBeInTheDocument();
  });

  it("lists short labels, searches labels + descriptions, and selection writes route state", async () => {
    catalogHandler();
    renderPage();
    // The catalog column shows the id-derived short label, never the paragraph
    // description the server serves as `title`.
    const countyButton = await screen.findByRole("button", { name: /Customers in counties/ });
    expect(countyButton).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Customers in one validated pilot county/ }),
    ).not.toBeInTheDocument();
    const search = screen.getByRole("searchbox", { name: "Search query names and descriptions…" });
    await userEvent.type(search, "pilot county");
    expect(
      screen.queryByRole("button", { name: /Needle only in technical id/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Customers in counties/ })).toBeInTheDocument();
    await userEvent.clear(search);
    await userEvent.click(screen.getByRole("button", { name: /Needle only in technical id/ }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/verified-queries?template=needle-only-in-technical-id",
    );
  });

  it("retries the immutable failed request even if the form is edited afterward", async () => {
    catalogHandler();
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        bodies.push(await request.json());
        return bodies.length === 1
          ? HttpResponse.json({ detail: "temporary read failure" }, { status: 500 })
          : HttpResponse.json(RESULT);
      }),
    );
    renderPage("/verified-queries?template=customers-in-counties");
    const county = await screen.findByLabelText(/County/);
    await userEvent.type(county, "Richmond");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    expect(await screen.findByText("temporary read failure")).toBeInTheDocument();
    await userEvent.clear(county);
    await userEvent.type(county, "Columbia");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("heading", { name: "Verified result" });
    expect(bodies).toEqual([
      { template_id: "customers-in-counties", version: 3, args: { county: "Richmond" } },
      { template_id: "customers-in-counties", version: 3, args: { county: "Richmond" } },
    ]);
  });

  it("keeps disabled, refused, and no-match catalog states distinct", async () => {
    catalogHandler({ enabled: false, registry_hash: null, templates: [] });
    const view = renderPage();
    expect(
      await screen.findByText("Verified queries are not enabled in this deployment."),
    ).toBeInTheDocument();
    view.unmount();

    server.use(
      http.get("*/api/v1/verified/templates", () => new HttpResponse(null, { status: 403 })),
    );
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/role doesn.t include verified queries/i);
  });

  // 🔴 STATION 3 OF THE GUIDED WALKTHROUGH LANDS HERE. `demo/stations.ts` scripts
  // "Open Verified Queries, choose the account directory, and run it", so this
  // provenance block is on the buyer's scripted path — and it painted a full
  // 64-character digest and a raw `2026-08-20T12:00:00Z`. §2.7 (primitives/HashChip)
  // is a written rule the other 31 files follow, and this was the ONLY bare-ISO
  // timestamp render left in the console.
  it("shows provenance a reader can use, not a 64-character digest and a wire instant", async () => {
    catalogHandler();
    server.use(http.post("*/api/v1/verified/invoke", () => HttpResponse.json(RESULT)));
    renderPage("/verified-queries?template=customers-in-counties");
    await userEvent.type(await screen.findByLabelText(/County/), "Richmond");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));

    // Eight characters on screen, the full digest still reachable in `title`.
    const chip = await screen.findByText("cccccccc");
    expect(chip).toHaveAttribute("title", "c".repeat(64));
    expect(screen.queryByText("c".repeat(64))).toBeNull();
    // And a copy affordance that says WHICH hash.
    expect(screen.getByRole("button", { name: "Copy configuration hash" })).toBeInTheDocument();

    // The instant reads as a formatted time, with the exact wire value kept in
    // `dateTime`. Asserted by SHAPE so the runner's timezone cannot decide the test.
    expect(screen.queryByText("2026-08-20T12:00:00Z")).toBeNull();
    const executed = screen.getByText(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
    expect(executed).toHaveAttribute("dateTime", "2026-08-20T12:00:00Z");
  });

  // §2.7 (primitives/HashChip) again, on the catalog's own provenance block —
  // and the absent case must keep saying "Unavailable" in words rather than
  // becoming a chip over nothing.
  it("chips the registry digest and keeps a missing one as a word, not an empty chip", async () => {
    catalogHandler();
    const view = renderPage("/verified-queries?template=customers-in-counties");
    await userEvent.click(await screen.findByText("Technical provenance"));
    const present = screen.getByText("Registry SHA-256").nextElementSibling as HTMLElement;
    expect(present).not.toHaveTextContent("r".repeat(64));
    expect(within(present).getByText("rrrrrrrr")).toHaveAttribute("title", "r".repeat(64));
    expect(
      within(present).getByRole("button", { name: "Copy registry SHA-256" }),
    ).toBeInTheDocument();
    view.unmount();

    catalogHandler({ ...CATALOG, registry_hash: null });
    renderPage("/verified-queries?template=customers-in-counties");
    await userEvent.click(await screen.findByText("Technical provenance"));
    const absent = screen.getByText("Registry SHA-256").nextElementSibling as HTMLElement;
    expect(absent).toHaveTextContent("Unavailable");
    expect(within(absent).queryByRole("button")).toBeNull();
  });

  it("announces an invocation refusal as a terminal alert", async () => {
    catalogHandler();
    server.use(
      http.post("*/api/v1/verified/invoke", () => new HttpResponse(null, { status: 403 })),
    );
    renderPage("/verified-queries?template=customers-in-counties");
    await userEvent.type(await screen.findByLabelText(/County/), "Richmond");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/role doesn.t include this verified query/i);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});
