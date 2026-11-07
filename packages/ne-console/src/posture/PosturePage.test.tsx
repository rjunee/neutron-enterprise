// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §14 — POSTURE, recomposed (CR-B8, screens/posture.html). The measured
// starting point (§14 v1) was twelve card panels listing the source files that
// implement each control — recomposed once already into a sortable table, and
// now recomposed again into the annunciator this screen was always meant to be:
// a claim strip answering the whole question in zero clicks, and twelve tiles
// (id · name · state) that read calm at rest. The sortable table and its
// per-column headers leave with it (NOTES.md: "the sortable detail lives one
// click away in the pane") — the filter and the live count survive.
//
// THE VOCABULARY IS THE CRUX (NOTES.md, "the prior defect, designed around"):
// a role-scoped refusal once drew three fault-coloured blocks on a reader's
// home screen for a boundary that was working exactly as configured. The
// assertions below are the regression lock for that: FAILED is the only fact
// that earns the fault colour (and earns it twice — word and tile edge);
// MANUAL is gated-neutral, never a lesser shade of FAILED; a refusal renders
// gated and alone; OPEN egress renders neutral, with a fault-coloured control
// in the SAME render proving the distinction is real.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConfigHashSpanView, PostureConstraintView, PostureView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { POSTURE } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { PosturePage } from "./PosturePage";

// ===========================================================================
// Fixtures
// ===========================================================================

/** SERVED IN AN ORDER THAT IS NEITHER the grid's own order nor its reverse, so
 * an id-ascending arrangement that does nothing is visible on the first
 * assertion. C10 leads deliberately: a lexicographic comparator puts it before
 * C2, and the grid's reading order is C1…C12. */
const CONTROLS: PostureConstraintView[] = [
  {
    id: "C10",
    short: "Audit integrity",
    enforcement: "Every audit record is chained to the one before it",
    attested: true,
    mode: "static",
    evidence: ["packages/ne-api/tests/test_c10_guardian.py"],
  },
  {
    id: "C2",
    short: "Sealed egress",
    enforcement: "Outbound network blocked and verified at build",
    attested: true,
    mode: "static",
    evidence: ["packages/ne-gateway/tests/test_c2_guardian.py"],
  },
  {
    id: "C7",
    short: "Trimmed knowledge",
    enforcement: "Results are filtered to the user's permissions before they are returned",
    attested: false,
    mode: "manual",
    evidence: [],
  },
  {
    id: "C1",
    short: "Self-hosted",
    enforcement: "No code path can skip the policy check",
    attested: true,
    mode: "static",
    evidence: ["packages/ne-core/tests/test_c1_guardian.py"],
  },
];

/** Three spans, served oldest-first — the opposite of §14's "sorted From desc". */
const HISTORY: ConfigHashSpanView[] = [
  { hash: "a".repeat(64), from: "2026-05-01T00:00:00Z", to: "2026-06-01T00:00:00Z" },
  { hash: "b".repeat(64), from: "2026-06-01T00:00:00Z", to: "2026-07-06T00:00:00Z" },
  { hash: "c".repeat(64), from: "2026-07-06T00:00:00Z", to: null },
];

function servePosture(over: Partial<PostureView> = {}) {
  server.use(
    http.get("*/api/v1/posture", () =>
      HttpResponse.json({
        ...POSTURE,
        constraints: CONTROLS,
        config_hash: "c".repeat(64),
        config_hash_history: HISTORY,
        ...over,
      }),
    ),
  );
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/posture"]}>
        <PosturePage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

// ===========================================================================
// Row-identity readers. A count assertion passes for the wrong rows.
// ===========================================================================

function listbox(): HTMLElement {
  return screen.getByRole("listbox", { name: "Constraint attestations" });
}

function tiles(): HTMLElement[] {
  return within(listbox()).getAllByRole("option");
}

function tileIds(): string[] {
  return tiles().map((tile) => tile.querySelector(".ne-ann__id")?.textContent ?? "");
}

function tileNames(): string[] {
  return tiles().map((tile) => tile.querySelector(".ne-ann__name")?.textContent ?? "");
}

/** The state WORD each tile announces (glyph excluded — the glyph is
 * `aria-hidden` decoration, not part of the word). */
function tileStateWords(): string[] {
  return tiles().map((tile) => {
    const state = tile.querySelector(".ne-ann__state");
    const glyph = state?.querySelector("[aria-hidden]");
    const full = state?.textContent ?? "";
    const glyphText = glyph?.textContent ?? "";
    return full.replace(glyphText, "").trim();
  });
}

function recordPane(): HTMLElement | null {
  return document.querySelector(".ne-record");
}

function cells(table: HTMLElement, index: number): string[] {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[index]?.textContent ?? "");
}

/** The configuration history's rows, by the served instant each one starts at. */
function historyFrom(): string[] {
  return within(screen.getByRole("table", { name: "Configuration history" }))
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.querySelector("time")?.getAttribute("datetime") ?? "");
}

async function loaded() {
  await waitFor(() => expect(tileIds()).toHaveLength(CONTROLS.length));
}

// ===========================================================================
// 0 — The claim strip
// ===========================================================================

describe("§14.0 — the claim strip", () => {
  it("answers Controls/Egress/Configuration in one row, before any table", async () => {
    servePosture({ air_gap: true, egress_allowlist: [] });
    setup();
    await loaded();

    const controls = screen.getByLabelText("Controls");
    expect(within(controls).getByText(/3 of 4 attested/)).toBeInTheDocument();
    expect(within(controls).getByText("Passed")).toBeInTheDocument();
    expect(within(controls).getByText("Build-time guardians · this build")).toBeInTheDocument();

    const egress = screen.getByLabelText("Egress boundary");
    expect(within(egress).getByText(/Sealed/)).toBeInTheDocument();
    expect(
      within(egress).getByText(/Air gap engaged — frontier egress is disabled\./),
    ).toBeInTheDocument();

    const configuration = screen.getByLabelText("Configuration");
    expect(within(configuration).getByText("cccccccc")).toBeInTheDocument();
    expect(within(configuration).getByText(/In force since/)).toBeInTheDocument();
  });

  it("turns the Controls fact to 'N of M · K failed' when a guardian is red", async () => {
    servePosture({
      constraints: [
        ...CONTROLS,
        {
          id: "C4",
          short: "License gate",
          enforcement: "License gate enforced in CI",
          attested: false,
          mode: "static",
          evidence: [],
        },
      ],
    });
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(5));

    const controls = screen.getByLabelText("Controls");
    // Three static+attested of five served (one manual, one static-failed).
    expect(within(controls).getByText(/3 of 5 attested/)).toBeInTheDocument();
    expect(within(controls).getByText(/1 failed/)).toBeInTheDocument();
    expect(within(controls).queryByText("Passed")).toBeNull();
  });

  it("never appears while the read is loading or refused", async () => {
    server.use(http.get("*/api/v1/posture", () => new Promise(() => {})));
    setup();

    expect(await screen.findByText("Loading build-time attestations")).toBeInTheDocument();
    expect(screen.queryByLabelText("Egress boundary")).toBeNull();
    expect(screen.queryByLabelText("Configuration")).toBeNull();
  });
});

// ===========================================================================
// 1 — Controls: the annunciator
// ===========================================================================

describe("§14.1 — the annunciator", () => {
  it("renders twelve id/name/state tiles in id-ascending reading order", async () => {
    servePosture();
    setup();
    await loaded();

    // Served C10, C2, C7, C1 — rendered id-ascending, which is neither the
    // served order nor a lexicographic one (C10 would lead lexicographically).
    expect(tileIds()).toEqual(["C1", "C2", "C7", "C10"]);
    expect(tileNames()).toEqual([
      "Self-hosted",
      "Sealed egress",
      "Trimmed knowledge",
      "Audit integrity",
    ]);
  });

  it("draws exactly the three named states, with FAILED the tile's own edge mark", async () => {
    servePosture();
    setup();
    await loaded();

    expect(tileStateWords()).toEqual(["Passed", "Passed", "Manual", "Passed"]);
    const byId = Object.fromEntries(tiles().map((tile) => [tile.id, tile]));
    // C7 is served manual/unattested — MANUAL, not FAILED: unverified is not
    // broken, and it must not carry the fault-coloured edge.
    expect(byId["C7"]).toHaveClass("ne-ann");
    expect(byId["C7"]).not.toHaveClass("ne-ann--fail");
  });

  it("no header row, no sort control — sorting left with the table it replaced", async () => {
    servePosture();
    setup();
    await loaded();

    // The annunciator itself carries no columnheader — the Configuration
    // history table (a real, sortable table) legitimately does, so the check
    // is scoped to the tile grid rather than the whole page.
    expect(within(listbox()).queryByRole("columnheader")).toBeNull();
    expect(within(listbox()).queryByRole("button", { name: "Control" })).toBeNull();
  });

  it("counts build guardians without implying current runtime health", async () => {
    servePosture();
    const user = setup();
    await loaded();

    // Four served, three static guardian passes — the manual one is counted as
    // an attestation and not as a build-time pass.
    expect(
      screen.getByText("4 constraint attestations · 3 build-time guardians passed"),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/controls\s*[·/]\s*\d+ verified/i);

    await user.type(screen.getByRole("searchbox", { name: "Filter controls…" }), "egress");
    expect(
      screen.getByText("1 constraint attestation · 1 build-time guardian passed"),
    ).toBeInTheDocument();
  });

  it("the search-within narrows the rendered TILES, and Esc restores them", async () => {
    servePosture();
    const user = setup();
    await loaded();

    const box = screen.getByRole("searchbox", { name: "Filter controls…" });
    await user.type(box, "chained");
    // BY IDENTITY: the one control whose enforcement mentions it, not merely
    // "one tile".
    expect(tileIds()).toEqual(["C10"]);

    // A filter over the state word is a filter over the rendered word, not
    // over a private flag.
    await user.clear(box);
    await user.type(box, "manual");
    expect(tileIds()).toEqual(["C7"]);

    await user.type(box, "{Escape}");
    expect(tileIds()).toEqual(["C1", "C2", "C7", "C10"]);
  });

  it("a tile click fills the docked pane with evidence class, build result, mode, enforcement", async () => {
    servePosture();
    const user = setup();
    await loaded();

    await user.click(screen.getByText("Trimmed knowledge"));
    const pane = recordPane();
    expect(pane).not.toBeNull();
    expect(pane).toHaveAttribute("aria-label", "C7 Trimmed knowledge");

    expect(
      within(pane as HTMLElement)
        .getAllByRole("term")
        .map((dt) => dt.textContent),
    ).toEqual(["Evidence class", "Build result", "Mode", "Enforcement"]);
    expect(within(pane as HTMLElement).getByText("manual")).toBeInTheDocument();
    expect(
      within(pane as HTMLElement).getByText(
        "Results are filtered to the user's permissions before they are returned",
      ),
    ).toBeInTheDocument();
    // The open tile carries aria-selected, and only it.
    const open = tiles().find((tile) => tile.id === "C7") as HTMLElement;
    expect(open).toHaveAttribute("aria-selected", "true");
    expect(
      tiles()
        .filter((tile) => tile !== open)
        .every((tile) => tile.getAttribute("aria-selected") === "false"),
    ).toBe(true);

    // Esc closes it (§1.4) without leaving the grid.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(recordPane()).toBeNull());
    expect(tileIds()).toEqual(["C1", "C2", "C7", "C10"]);
  });

  it("the split collapses to one column while nothing is selected", async () => {
    servePosture();
    setup();
    await loaded();

    expect(listbox().closest(".ne-split")).toHaveClass("ne-split--nopane");
  });

  it("renders the empty state when the server reports no controls", async () => {
    servePosture({ constraints: [] });
    setup();

    expect(await screen.findByText("No controls reported.")).toBeInTheDocument();
    expect(screen.queryByRole("listbox", { name: "Constraint attestations" })).toBeNull();
  });

  it("says the FILTER emptied the list, not that the server reported nothing", async () => {
    servePosture();
    const user = setup();
    await loaded();

    await user.type(screen.getByRole("searchbox", { name: "Filter controls…" }), "zzzz");
    expect(screen.getByText("No controls match this filter.")).toBeInTheDocument();
    expect(screen.queryByText("No controls reported.")).toBeNull();
  });
});

// ===========================================================================
// State gallery — the vocabulary regression lock (NOTES.md "the prior defect")
// ===========================================================================

describe("§14 — state gallery: fault, manual and gated are never the same colour", () => {
  it("FAILED is the page's only red, MANUAL is neutral, and a refusal is gated — never on the same colour, in one pass", async () => {
    // 1 — a mixed grid: one FAILED (C4), one MANUAL (C7), two PASSED (C10, C2).
    servePosture({
      constraints: [
        ...CONTROLS.map((row) => ({ ...row, attested: true, mode: "static" })).slice(0, 2),
        { ...CONTROLS[2]!, attested: false, mode: "manual" }, // C7 — MANUAL
        {
          id: "C4",
          short: "License gate",
          enforcement: "License gate enforced in CI",
          attested: false,
          mode: "static",
          evidence: [],
        }, // FAILED
      ],
    });
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(4));

    const byId = Object.fromEntries(tiles().map((tile) => [tile.id, tile]));
    expect(byId["C4"]).toHaveClass("ne-ann--fail");
    expect(byId["C4"]?.querySelector(".ne-ann__state")).toHaveClass("ne-ann__state--fail");
    expect(byId["C7"]).not.toHaveClass("ne-ann--fail");
    expect(byId["C7"]?.querySelector(".ne-ann__state")).toHaveClass("ne-ann__state--manual");
    expect(byId["C10"]).not.toHaveClass("ne-ann--fail");
    expect(byId["C10"]?.querySelector(".ne-ann__state")).toHaveClass("ne-ann__state--pass");
    cleanup();

    // 2 — the SAME boundary, refused: gated, alone, never red.
    server.use(
      http.get("*/api/v1/posture", () =>
        HttpResponse.json({ detail: "not visible to this key" }, { status: 403 }),
      ),
    );
    setup();
    expect(
      await screen.findByText(
        "Your role doesn’t include posture. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("GATED")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).toBeNull();
    expect(document.querySelector(".ne-ann--fail")).toBeNull();
  });
});

describe("§14 — refused: strip, tiles and count all absent", () => {
  it("renders the SERVER's refusal on a 403 with no strip, no tiles, no count", async () => {
    let asked = 0;
    server.use(
      http.get("*/api/v1/posture", () => {
        asked += 1;
        return HttpResponse.json({ detail: "not visible to this key" }, { status: 403 });
      }),
    );
    setup();

    expect(
      await screen.findByText(
        "Your role doesn’t include posture. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    // A role boundary is drawn neutral, never as a fault (§2.3).
    expect(screen.getByText("GATED")).toBeInTheDocument();
    // The claim strip is gone entirely.
    expect(screen.queryByLabelText("Controls")).toBeNull();
    expect(screen.queryByLabelText("Egress boundary")).toBeNull();
    expect(screen.queryByLabelText("Configuration")).toBeNull();
    // No tiles, no listbox, no pane.
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("option")).toBeNull();
    expect(recordPane()).toBeNull();
    // No count, no table.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(/constraint attestations ·/)).toBeNull();
    // The title row survives, so the URL still resolves to a page (C7).
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Posture");
    // THIN CLIENT (C7): the refusal on screen is the SERVER's, so the console
    // must have asked. A page that renders this block without a request has
    // decided a permission for itself.
    expect(asked).toBe(1);
  });
});

describe("§14 — OPEN egress is neutral, with a fault-colour control in the same render", () => {
  it("OPEN carries no colour-modifier class; a FAILED tile in the same read does", async () => {
    servePosture({
      air_gap: false,
      egress_allowlist: ["api.anthropic.com"],
      constraints: [
        ...CONTROLS,
        {
          id: "C4",
          short: "License gate",
          enforcement: "License gate enforced in CI",
          attested: false,
          mode: "static",
          evidence: [],
        },
      ],
    });
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(5));

    const egress = screen.getByLabelText("Egress boundary");
    // NB: `.ne-claim__value > *` (a class compound + universal selector) does
    // not match under jsdom's nwsapi — a `> span` combinator does, so the tag
    // is named explicitly rather than relying on the universal selector.
    const value = egress.querySelector(".ne-claim__value > span") as HTMLElement;
    expect(value).toHaveClass("ne-egressvalue");
    // The fault-colour control, in the SAME render: OPEN carries no modifier
    // at all, while the served FAILED tile carries the fault edge. If OPEN
    // ever grew a colour class this assertion (an exact class list) breaks.
    expect(value.className).toBe("ne-egressvalue");
    expect(value.textContent).toContain("Open");

    const byId = Object.fromEntries(tiles().map((tile) => [tile.id, tile]));
    expect(byId["C4"]).toHaveClass("ne-ann--fail");
  });
});

// ===========================================================================
// 2 — Network
// ===========================================================================

describe("§14.2 — network", () => {
  it("labels network facts as a dated direct read, never a liveness probe", async () => {
    servePosture({ air_gap: true, egress_allowlist: [] });
    setup();
    await loaded();

    const heading = screen.getByRole("heading", { name: "Runtime reading · Network" });
    const section = heading.closest("section") as HTMLElement;
    expect(within(section).getByText("DIRECT RUNTIME READ.")).toBeVisible();
    expect(within(section).getByText(/not an endpoint liveness probe/)).toBeVisible();
    expect(section.querySelector("time")).toHaveAttribute("datetime", POSTURE.network_observed_at);
  });

  it("calls freshness unknown when an older server supplies no observation time", async () => {
    servePosture({ network_observed_at: undefined });
    setup();
    await loaded();

    const network = screen
      .getByRole("heading", { name: "Runtime reading · Network" })
      .closest("section") as HTMLElement;
    expect(
      within(network).getByText("Observation time unavailable; freshness unknown."),
    ).toBeVisible();
    expect(within(network).queryByText("just now")).toBeNull();
  });

  it("states the air gap and a factual sentence for an empty allowlist", async () => {
    servePosture({ air_gap: true, egress_allowlist: [] });
    setup();
    await loaded();

    const row = screen.getByText("Air gap").parentElement as HTMLElement;
    expect(within(row).getByText("On")).toBeInTheDocument();
    expect(screen.getByText("None — no external host is sanctioned.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Allowed hosts" })).toBeNull();
  });

  it("renders a non-empty allowlist as a one-column table, by host identity", async () => {
    servePosture({
      air_gap: false,
      egress_allowlist: ["upload.example.com", "api.anthropic.com"],
    });
    setup();
    await loaded();

    const row = screen.getByText("Air gap").parentElement as HTMLElement;
    expect(within(row).getByText("Off")).toBeInTheDocument();

    const hosts = screen.getByRole("table", { name: "Allowed hosts" });
    expect(cells(hosts, 0)).toEqual(["api.anthropic.com", "upload.example.com"]);
    expect(screen.queryByText("None — no external host is sanctioned.")).toBeNull();
  });

  it("sends model routing to Models — the lanes table has left this page", async () => {
    servePosture();
    setup();
    await loaded();

    const link = screen.getByRole("link", { name: "Model routing is on Models ›" });
    expect(link).toHaveAttribute("href", "/models");
    // The served lanes are NOT rendered here any more (§14).
    expect(screen.queryByText("qwen3-32b")).toBeNull();
    expect(screen.queryByText("claude-fable-5")).toBeNull();
  });

  it("keeps the card head complete and keyboard ordered at the expanded width", async () => {
    servePosture({
      constraints: Array.from({ length: 12 }, (_, index) => ({
        ...CONTROLS[index % CONTROLS.length]!,
        id: `C${index + 1}`,
      })),
      air_gap: false,
      egress_allowlist: ["api.anthropic.com", "upload.example.com"],
    });
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(12));

    const headings = screen.getAllByRole("heading", { level: 2 });
    expect(headings.map((heading) => heading.textContent)).toEqual([
      "Build-time attestations",
      "Runtime reading · Network",
      "Runtime evidence · Configuration",
    ]);

    const heading = screen.getByRole("heading", { name: "Runtime reading · Network" });
    const section = heading.closest("section") as HTMLElement;
    expect(section).toHaveAttribute("aria-labelledby", "ne-posture-network");
    const cardhead = heading.parentElement as HTMLElement;
    const models = within(cardhead).getByRole("link", { name: "Model routing is on Models ›" });
    models.focus();
    expect(models).toHaveFocus();

    expect(section.querySelector("dl")).toHaveClass("ne-kv");
    expect(Array.from(section.querySelectorAll("dt"), (term) => term.textContent)).toEqual([
      "Air gap",
      "Allowed hosts",
    ]);
    expect(within(section).getByText("DIRECT RUNTIME READ.")).toBeVisible();
    expect(within(section).getByText(/not an endpoint liveness probe/)).toBeVisible();
    expect(section.querySelector("time")).toHaveAttribute("datetime", POSTURE.network_observed_at);
    expect(within(section).getByText("Off")).toBeVisible();
    expect(within(section).getByText("api.anthropic.com")).toBeVisible();
    expect(within(section).getByText("upload.example.com")).toBeVisible();
  });
});

// ===========================================================================
// 3 — Configuration
// ===========================================================================

describe("§14.3 — configuration", () => {
  it("plates the hash in force, truncated, with the date it came into force", async () => {
    servePosture();
    setup();
    await loaded();

    const plate = document.querySelector(".ne-plate") as HTMLElement;
    // Eight characters on the face, the full sixty-four in `title` (§2.7).
    expect(within(plate).getByText("cccccccc")).toHaveAttribute("title", "c".repeat(64));
    expect(within(plate).getByText(/^In force since/)).toBeInTheDocument();
    // The in-force instant is served, not derived: it is the `from` of the span
    // whose `to` is null.
    const since = within(plate)
      .getByText(/^In force since/)
      .querySelector("time");
    expect(since).toHaveAttribute("datetime", "2026-07-06T00:00:00Z");
  });

  it("never spells the 64-character hash out on the page", async () => {
    servePosture();
    setup();
    await loaded();

    expect(document.body.textContent).not.toContain("c".repeat(64));
    expect(document.body.textContent).not.toContain("a".repeat(64));
  });

  it("renders the history sorted From desc, with '—' for the span in force", async () => {
    servePosture();
    setup();
    await loaded();

    // Served oldest-first; rendered newest-first, by row identity.
    expect(historyFrom()).toEqual([
      "2026-07-06T00:00:00Z",
      "2026-06-01T00:00:00Z",
      "2026-05-01T00:00:00Z",
    ]);
    const history = screen.getByRole("table", { name: "Configuration history" });
    expect(cells(history, 2)[0]).toBe("—");
  });

  it("toggles the From sort, and every history column is sortable", async () => {
    servePosture();
    const user = setup();
    await loaded();

    const history = screen.getByRole("table", { name: "Configuration history" });
    expect(
      within(history)
        .getAllByRole("columnheader")
        .map((th) => (th.textContent ?? "").replace(/[▲▼]/g, "")),
    ).toEqual(["Configuration", "From", "To"]);
    expect(within(history).getAllByRole("columnheader")[1]).toHaveAttribute(
      "aria-sort",
      "descending",
    );

    await user.click(within(history).getByRole("button", { name: "From" }));
    expect(historyFrom()).toEqual([
      "2026-05-01T00:00:00Z",
      "2026-06-01T00:00:00Z",
      "2026-07-06T00:00:00Z",
    ]);
  });

  it("carries §14's one permitted sentence of explanation, verbatim", async () => {
    servePosture();
    setup();
    await loaded();

    expect(
      screen.getByText(
        "Every audit record carries the configuration in force when it was written.",
      ),
    ).toBeInTheDocument();
  });
});

// ===========================================================================
// The page: order, states, and what may never appear
// ===========================================================================

describe("§14 — the page", () => {
  it("visually and semantically separates build-time attestations from runtime evidence", async () => {
    servePosture();
    setup();
    await loaded();

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Posture");
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "Build-time attestations",
      "Runtime reading · Network",
      "Runtime evidence · Configuration",
    ]);
    const buildSection = screen
      .getByRole("heading", { name: "Build-time attestations" })
      .closest("section") as HTMLElement;
    const runtimeSection = screen
      .getByRole("heading", { name: "Runtime reading · Network" })
      .closest("section") as HTMLElement;
    const configurationSection = screen
      .getByRole("heading", { name: "Runtime evidence · Configuration" })
      .closest("section") as HTMLElement;
    expect(within(buildSection).getByText("BUILD-TIME evidence.")).toBeVisible();
    expect(within(buildSection).getByText(/not a live runtime health check/)).toBeVisible();
    expect(within(runtimeSection).getByText("DIRECT RUNTIME READ.")).toBeVisible();
    expect(within(configurationSection).getByText("AUDIT-BACKED RUNTIME READ.")).toBeVisible();
  });

  it("shows server provenance and labels all twelve tiles PASSED", async () => {
    server.use(http.get("*/api/v1/posture", () => HttpResponse.json(POSTURE)));
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(12));

    expect(screen.getByText(POSTURE.attestation_source)).toBeVisible();
    expect(tileStateWords()).toEqual(Array(12).fill("Passed"));
    expect(
      screen.getByText("12 constraint attestations · 12 build-time guardians passed"),
    ).toBeVisible();
    expect(document.body.textContent).not.toContain("12/12 verified");
    expect(document.body.textContent).not.toContain("12 controls · 12 verified");
  });

  it("announces the load in the grid's own geometry, and counts nothing yet", async () => {
    // A read that never resolves: the loading state is the state under test.
    server.use(http.get("*/api/v1/posture", () => new Promise(() => {})));
    setup();

    expect(await screen.findByText("Loading build-time attestations")).toBeInTheDocument();
    // NO fabricated zero over a window that has not arrived…
    expect(screen.queryByText(/constraint attestations ·/)).toBeNull();
    // …and no claim about this key's role while the read is still in flight.
    expect(screen.queryByText(/Your role doesn/)).toBeNull();
  });

  it("renders an unreachable read as an ERROR with Retry — never as a role boundary", async () => {
    let attempts = 0;
    server.use(
      http.get("*/api/v1/posture", () => {
        attempts += 1;
        if (attempts === 1) return HttpResponse.json({ detail: "boom" }, { status: 500 });
        return HttpResponse.json({ ...POSTURE, constraints: CONTROLS });
      }),
    );
    const user = setup();

    expect(await screen.findByText("Couldn’t load posture.")).toBeInTheDocument();
    // THE BUG THIS TEST EXISTS FOR: a failed read is not a refused one, and
    // stating an authorization boundary over it is a false claim about
    // authorization made by the console about the server.
    expect(screen.queryByText(/Your role doesn/)).toBeNull();
    expect(screen.queryByText("GATED")).toBeNull();
    // Runtime evidence is absent on failure; unavailable data cannot inherit a
    // successful AIR_GAP/config appearance.
    expect(screen.queryByRole("heading", { name: "Runtime reading · Network" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Runtime evidence · Configuration" })).toBeNull();
    expect(screen.queryByText("Air gap")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await loaded();
    expect(screen.queryByText("Couldn’t load posture.")).toBeNull();
    expect(screen.getByRole("heading", { name: "Runtime reading · Network" })).toBeVisible();
  });

  it("shows attestation provenance but not every per-control evidence path", async () => {
    // The unchanged fixture sends both provenance and a distinct evidence path
    // for each constraint. The source is visible; the twelve evidence paths are
    // not duplicated onto the tiles or the pane.
    server.use(http.get("*/api/v1/posture", () => HttpResponse.json(POSTURE)));
    setup();
    await waitFor(() => expect(tileIds()).toHaveLength(12));

    expect(screen.getByText(POSTURE.attestation_source)).toBeVisible();
    for (const constraint of POSTURE.constraints) {
      for (const path of constraint.evidence) {
        expect(document.body.textContent).not.toContain(path);
      }
    }
    expect(tileIds()).toHaveLength(12);
  });
});

// ===========================================================================
// Runtime freshness — the observation stamp, not browser fetch time, owns age
// ===========================================================================

describe("§14 — runtime freshness", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-18T20:02:30Z"));
  });

  afterEach(() => {
    // Clear PosturePage's polling/tick intervals before restoring real timers.
    cleanup();
    vi.useRealTimers();
  });

  it("ages a cached success from the served stamp instead of re-dating it current", async () => {
    let asked = 0;
    server.use(
      http.get("*/api/v1/posture", () => {
        asked += 1;
        return HttpResponse.json({
          ...POSTURE,
          constraints: CONTROLS,
          network_observed_at: "2026-08-18T20:00:00Z",
          configuration_observed_at: "2026-08-18T20:01:00Z",
        });
      }),
    );
    setup();
    await loaded();

    const network = screen
      .getByRole("heading", { name: "Runtime reading · Network" })
      .closest("section") as HTMLElement;
    expect(within(network).getByText("2 min ago")).toBeVisible();

    // The 30 s poll returns the SAME timestamp (a downstream/cache replay).
    // The five-second display clock must make it older, never "just now".
    await act(async () => {
      await vi.advanceTimersByTimeAsync(35_000);
    });
    await waitFor(() => expect(asked).toBeGreaterThanOrEqual(2));
    expect(within(network).getByText("3 min ago")).toBeVisible();
    expect(within(network).queryByText("just now")).toBeNull();
  });

  it("withdraws a prior runtime success when the scheduled re-read fails", async () => {
    let asked = 0;
    server.use(
      http.get("*/api/v1/posture", () => {
        asked += 1;
        if (asked === 1) return HttpResponse.json({ ...POSTURE, constraints: CONTROLS });
        return HttpResponse.json({ detail: "runtime read unavailable" }, { status: 500 });
      }),
    );
    setup();
    await loaded();
    expect(screen.getByRole("heading", { name: "Runtime reading · Network" })).toBeVisible();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(await screen.findByText("Couldn’t load posture.")).toBeVisible();
    expect(asked).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("heading", { name: "Runtime reading · Network" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Runtime evidence · Configuration" })).toBeNull();
    expect(screen.queryByText("Air gap")).toBeNull();
  });
});
