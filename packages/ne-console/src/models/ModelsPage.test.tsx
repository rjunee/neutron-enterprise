// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 — MODELS, as rendered on a deployment served by the posture read alone.
//
// The measured starting point was a heading over a paragraph. So the assertions
// below are about the things whose absence was the defect — a real table with
// the stated default sort, sortable headers, a filter that narrows the ROWS
// (asserted by identity, because a count assertion passes for the wrong rows), a
// live count and an export — and about the things whose PRESENCE would be the
// defect: a fabricated binding column, a zero counted out of an unread source, a
// dollar figure on a page that reads no prices, a console-authored refusal
// standing in for the server's, and a role-boundary sentence over a read that
// merely failed.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { ModelLaneView, PostureView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { POSTURE } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { ModelsPage } from "./ModelsPage";

/** Four lanes across two lane names, SERVED IN AN ORDER THAT IS NEITHER the
 * default sort nor its reverse — so a default sort that does nothing is visible
 * on the first assertion, and so the Model tiebreak inside a lane is real. */
const LANES: ModelLaneView[] = [
  {
    model_id: "qwen3-32b",
    provider: "vllm",
    lane: "private-vllm",
    configured_model: "Qwen/Qwen3-32B",
    context_window: 32768,
  },
  {
    model_id: "mistral-large",
    provider: "mistral",
    lane: "frontier-api",
    configured_model: "mistral-large-latest",
    context_window: 128000,
  },
  {
    model_id: "llama-3-70b",
    provider: "vllm",
    lane: "private-vllm",
    configured_model: "meta-llama/Llama-3-70B",
    context_window: 8192,
  },
  {
    model_id: "claude-fable-5",
    provider: "anthropic",
    lane: "frontier-api",
    configured_model: "claude-fable-5",
    context_window: 1000000,
  },
];

function servePosture(over: Partial<PostureView> = {}) {
  server.use(
    http.get("*/api/v1/posture", () =>
      HttpResponse.json({ ...POSTURE, model_lanes: LANES, ...over }),
    ),
  );
}

function setup(path = "/models") {
  server.use(
    http.get("*/api/v1/models/lifecycle", () =>
      HttpResponse.json({ registry_hash: "a".repeat(64), transport_available: true, targets: [] }),
    ),
  );
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <ModelsPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The rendered rows, by the identity in their first cell. */
function models(): string[] {
  const table = screen.getByRole("table", { name: "Model policy" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

async function loaded() {
  await waitFor(() => expect(models()).toHaveLength(4));
}

/** Everything the screen is currently announcing, empties dropped. */
function announcements(): string[] {
  return screen
    .queryAllByRole("status")
    .map((node) => node.textContent ?? "")
    .filter((text) => text !== "");
}

describe("§10 — the lanes table", () => {
  it("renders a REAL table of the served lanes, sorted Lane A–Z then Model", async () => {
    servePosture();
    setup();
    await loaded();
    // BY IDENTITY, in order: both lane groups, and inside each the models A–Z.
    expect(models()).toEqual(["claude-fable-5", "mistral-large", "llama-3-70b", "qwen3-32b"]);
  });

  it("names the model EVERY lane resolves to, frontier rows included", async () => {
    // The defect: Model / Provider / Lane rendered `frontier-default` and
    // `frontier-best` as two aliases over one provider and one lane, so an
    // operator could not answer "what is frontier-default?" from this screen at
    // all — and the serving panel never covers a frontier lane, because ne-api
    // may not probe a provider host. Deleting the Configured model column, or
    // populating it for private lanes only, goes RED here.
    servePosture();
    setup();
    await loaded();
    const table = screen.getByRole("table", { name: "Model policy" });
    const cells = (alias: string) => {
      const row = within(table)
        .getAllByRole("row")
        .slice(1)
        .find((node) => within(node).getAllByRole("cell")[0]?.textContent === alias);
      return (row ? within(row).getAllByRole("cell") : []).map((c) => c.textContent ?? "");
    };
    // A FRONTIER row: alias, provider, lane, then the model it resolves to.
    expect(cells("mistral-large")).toEqual([
      "mistral-large",
      "mistral",
      "frontier-api",
      "mistral-large-latest",
      "128,000",
    ]);
    // A PRIVATE row answers the same two questions, in the same two columns.
    expect(cells("qwen3-32b")).toEqual([
      "qwen3-32b",
      "vllm",
      "private-vllm",
      "Qwen/Qwen3-32B",
      "32,768",
    ]);
  });

  it("labels those two columns as CONFIGURATION, never as an observation", async () => {
    // The honesty pairing: the lane is finally legible, and the words that make
    // it legible must not borrow the serving panel's measured voice ("Checked
    // in under a minute · 76 ms"). Renaming these headers to a bare "Model" /
    // "Window" would let a config mapping read as evidence the lane is up.
    servePosture();
    setup();
    await loaded();
    expect(screen.getByRole("columnheader", { name: "Configured model" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Declared window" })).toBeInTheDocument();
    // And the table states no freshness of its own: nothing in it was checked.
    const table = screen.getByRole("table", { name: "Model policy" });
    expect(table.textContent).not.toMatch(/Checked|ms\b|ago/);
  });

  it("states that default sort on the Lane header, and marks no other header sorted", async () => {
    servePosture();
    setup();
    await loaded();
    expect(screen.getByRole("columnheader", { name: "Lane" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    for (const name of ["Model", "Provider"]) {
      expect(screen.getByRole("columnheader", { name })).not.toHaveAttribute("aria-sort");
    }
  });

  it("sorts by a clicked header, and reverses on the second click", async () => {
    servePosture();
    const user = setup();
    await loaded();
    await user.click(screen.getByRole("button", { name: "Model" }));
    expect(models()).toEqual(["claude-fable-5", "llama-3-70b", "mistral-large", "qwen3-32b"]);
    expect(screen.getByRole("columnheader", { name: "Model" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await user.click(screen.getByRole("button", { name: "Model" }));
    expect(models()).toEqual(["qwen3-32b", "mistral-large", "llama-3-70b", "claude-fable-5"]);
    expect(screen.getByRole("columnheader", { name: "Model" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("the lane filter narrows the RENDERED ROWS, and the count follows them", async () => {
    servePosture();
    const user = setup();
    await loaded();
    expect(screen.getByText("4 models")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Lane"), "private-vllm");
    // Identity, not cardinality: a count of two passes for the wrong two rows.
    expect(models()).toEqual(["llama-3-70b", "qwen3-32b"]);
    expect(screen.getByText("2 models")).toBeInTheDocument();
    // …and the active filter is dismissible, so the reason the list is short is
    // visible beside the list.
    await user.click(screen.getByRole("button", { name: "Clear Lane: private-vllm" }));
    expect(models()).toEqual(["claude-fable-5", "mistral-large", "llama-3-70b", "qwen3-32b"]);
  });

  it("the search-within narrows the RENDERED ROWS across model, provider and lane", async () => {
    servePosture();
    const user = setup();
    await loaded();
    await user.type(screen.getByLabelText("Filter models…"), "vllm");
    // Matches the two lanes whose PROVIDER is vllm — not a model-name match.
    expect(models()).toEqual(["llama-3-70b", "qwen3-32b"]);
    expect(screen.getByText("2 models")).toBeInTheDocument();
  });

  it("offers only the lanes the server sent, plus the all-lanes default", async () => {
    servePosture();
    setup();
    await loaded();
    const options = within(screen.getByLabelText("Lane"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["All lanes", "frontier-api", "private-vllm"]);
  });

  it("renders the spec's empty line when the deployment routes to no model", async () => {
    servePosture({ model_lanes: [] });
    setup();
    expect(await screen.findByText("No models configured.")).toBeInTheDocument();
    expect(screen.getByText("0 models")).toBeInTheDocument();
  });

  it("carries NO binding, task or status column while the policy router is absent", async () => {
    servePosture();
    setup();
    await loaded();
    const headers = screen
      .getAllByRole("columnheader")
      .map((header) => header.textContent?.replace(/[▲▼]/g, "") ?? "");
    // ABSENT, not blank: a column of em dashes would state that every lane on
    // this deployment lacks a binding.
    expect(headers).toEqual(["Model", "Provider", "Lane", "Configured model", "Declared window"]);
  });
});

describe("§10 — the network row", () => {
  it("renders the served air-gap state", async () => {
    servePosture({ air_gap: true });
    setup();
    await loaded();
    expect(document.querySelector(".ne-models__networkvalue")?.textContent).toBe("On");
  });

  it("renders OFF when the server says the gap is open — no default, no guess", async () => {
    servePosture({ air_gap: false, egress_allowlist: ["api.anthropic.com"] });
    setup();
    await loaded();
    expect(document.querySelector(".ne-models__networkvalue")?.textContent).toBe("Off");
  });

  it("offers the air-gap write (NE-AIRGAP-TOGGLE ships its endpoint); the B3 model-policy writes stay absent", async () => {
    servePosture();
    setup();
    await loaded();
    // The governed air-gap change is served by POST /v1/network/air-gap now, so
    // its control renders. Its four outcomes are ModelsPage.airgap.test.tsx.
    expect(screen.getByRole("button", { name: /request change/i })).toBeInTheDocument();
    // The model-policy write endpoints still do not exist on this deployment:
    // their controls stay ABSENT — not disabled (§10).
    expect(screen.queryByRole("button", { name: /allow a model/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /edit binding/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /remove from policy/i })).toBeNull();
  });
});

describe("§10 — the states are the server's, not this console's", () => {
  it("renders the SERVER's refusal, with no table and no counted zero", async () => {
    server.use(
      http.get("*/api/v1/posture", () =>
        HttpResponse.json({ detail: "requires an ops key" }, { status: 403 }),
      ),
    );
    setup();
    expect(
      await screen.findByText(
        "Your role doesn’t include the model policy. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    // A count of zero over a read this key was never shown is a fabricated zero.
    expect(screen.queryByText("0 models")).toBeNull();
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
    // The same refusal is what the network row read, and it says so.
    expect(screen.getByText("Not visible to your role")).toBeInTheDocument();
  });

  it("a FAILED read is an outage, never a role boundary", async () => {
    server.use(http.get("*/api/v1/posture", () => HttpResponse.error()));
    setup();
    expect(await screen.findByText("Couldn’t load models.")).toBeInTheDocument();
    // THE TRAP: "not visible to your role" over a read that merely failed is a
    // false statement about authorization.
    expect(screen.queryByText("Not visible to your role")).toBeNull();
    expect(screen.getByText("Couldn’t load")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("Retry re-reads, and the rows arrive when the server answers", async () => {
    let attempt = 0;
    server.use(
      http.get("*/api/v1/posture", () => {
        attempt += 1;
        if (attempt === 1) return HttpResponse.error();
        return HttpResponse.json({ ...POSTURE, model_lanes: LANES });
      }),
    );
    const user = setup();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await loaded();
    expect(models()).toEqual(["claude-fable-5", "mistral-large", "llama-3-70b", "qwen3-32b"]);
  });

  it("announces each load in its own geometry, and announces nothing else", async () => {
    servePosture();
    setup();
    // Skeleton rows in the table plus the network row's own bar — two reads in
    // flight, two announcements, no spinner paragraph.
    expect(announcements()).toEqual([
      "Loading inference targets…",
      "Loading network state",
      "Loading models",
    ]);
    // AND NOT A COUNT: "0 models" over a window still in flight is a zero
    // counted out of a read that has not answered.
    expect(screen.queryByText("0 models")).toBeNull();
    await loaded();
    // The load announcements go; the live count is what remains.
    expect(announcements()).toEqual(["4 models"]);
  });
});

describe("§10 — the Usage tab", () => {
  it("says what is not available, in one line, with no preview and no rows", async () => {
    servePosture();
    const user = setup();
    await loaded();
    await user.click(screen.getByRole("tab", { name: "Usage" }));
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveTextContent("Usage reporting isn’t available on this deployment yet.");
    expect(within(panel).queryByRole("table")).toBeNull();
    // No invented rows, no invented money.
    expect(panel.textContent ?? "").not.toMatch(/\$/);
    expect(panel.textContent ?? "").not.toMatch(/\d/);
  });

  it("keeps the tab in the address, so a link opens the tab it names", async () => {
    servePosture();
    setup("/models?tab=usage");
    expect(screen.getByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Policy" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tabpanel")).toHaveTextContent(
      "Usage reporting isn’t available on this deployment yet.",
    );
  });

  it("moves between tabs from the keyboard, in the order they are rendered", async () => {
    servePosture();
    const user = setup();
    await loaded();
    await user.click(screen.getByRole("tab", { name: "Policy" }));
    // Policy -> Routing -> Usage, and the wrap back to Policy: asserting the
    // whole cycle rather than one hop, so adding or reordering a tab cannot
    // leave this passing on a stale arrangement (NE-MODEL-ROUTING added
    // Routing between the two).
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Routing" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Policy" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("§10 — nothing on this screen leaks how it is built", () => {
  it("renders no path, endpoint, verb, status code or ticket id", async () => {
    servePosture();
    setup();
    await loaded();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\/v1\//);
    expect(text).not.toMatch(/NE-\d/);
    expect(text).not.toMatch(/\.(py|tsx?)\b/);
    expect(text).not.toMatch(/\b(GET|POST|PATCH) /);
    expect(text).not.toMatch(/HTTP \d/);
    // No dollar figure anywhere: this screen reads no price on this deployment.
    expect(text).not.toMatch(/\$/);
  });
});
