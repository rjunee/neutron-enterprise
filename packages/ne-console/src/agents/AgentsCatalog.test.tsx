// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The catalog's four claims, each of which the old table got wrong:
//   1. an empty grant renders as NOTHING, never "0 skills";
//   2. the two groups PARTITION the served page and keep its order;
//   3. a deterministic card's plate is the WORKFLOW's identity, not the
//      Agent's config hash standing in for one;
//   4. an unavailable Agent shows the SERVER's reasons, verbatim.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { AgentsCatalog } from "./AgentsCatalog";
import type { AgentPage, AgentsInteriorClient, AgentSummary } from "./types";

const agentic: AgentSummary = {
  agent_id: "agent-meridian",
  version: 4,
  label: "Meridian Analyst",
  description: "Answers Meridian servicing questions with governed knowledge.",
  owner_label: "Meridian Servicing Operations",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "agent.meridian-analyst.v2",
  tool_grants: ["lookup", "search", "draft", "cite"],
  knowledge_sources: [{ source_id: "meridian-servicing", tiers: ["t1", "t2"] }],
  skills: [{ skill_id: "meridian-hardship-draft", version: 1, content_hash: "e".repeat(64) }],
  budgets: {
    max_steps: 12,
    max_tool_calls: 10,
    max_input_tokens: 24_000,
    max_output_tokens: 4_000,
  },
  enforced_caps: {
    max_tool_calls: 10,
    max_model_requests: 11,
    wall_clock_seconds: 120,
    max_total_tokens: 28_000,
    max_parked_writes: 1,
  },
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: "9dc85b".padEnd(64, "0"),
};

const deterministic: AgentSummary = {
  ...agentic,
  agent_id: "agent-hardship",
  version: 5,
  label: "Meridian Hardship Review",
  description: "Runs the fixed Meridian B1 disaster-hardship population review.",
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
  tool_grants: ["lookup", "search", "draft", "cite"],
  skills: [],
  workflow: {
    profile_id: "meridian-b1-disaster-hardship",
    version: 1,
    config_hash: "e90381".padEnd(64, "1"),
    max_subjects: 5,
    frozen_inputs: {
      counties: ["Alameda"],
      payment_window_start: "2026-01-01",
      payment_window_end: "2026-06-30",
    },
  },
  config_hash: "a1b2c3".padEnd(64, "2"),
};

function page(items: AgentSummary[], nextCursor: string | null = null): AgentPage {
  return { items, next_cursor: nextCursor };
}

function client(overrides: Partial<AgentsInteriorClient> = {}): AgentsInteriorClient {
  return {
    listAgents: vi.fn(async () => page([agentic, deterministic])),
    getAgent: vi.fn(async () => agentic),
    listAgentConversations: vi.fn(async () => ({ items: [], next_cursor: null })),
    getConversation: vi.fn(),
    listConversationTurns: vi.fn(async () => ({ items: [], next_cursor: null })),
    listConversationRuns: vi.fn(async () => ({ items: [], next_cursor: null })),
    listAgentRuns: vi.fn(async () => ({ items: [], next_cursor: null })),
    ...overrides,
  } as AgentsInteriorClient;
}

function mount(overrides: Partial<AgentsInteriorClient> = {}) {
  return render(
    <MemoryRouter>
      <AgentsCatalog client={client(overrides)} identityKey="principal-a" />
    </MemoryRouter>,
  );
}

/** The card an Agent's name links from, once the read has landed. */
async function cardFor(label: string): Promise<HTMLElement> {
  const card = (await screen.findByRole("link", { name: label })).closest("article");
  if (card === null) throw new Error(`no card for ${label}`);
  return card;
}

describe("Agents catalog", () => {
  it("an empty grant renders as nothing, not as zero", async () => {
    const bare: AgentSummary = {
      ...deterministic,
      agent_id: "agent-bare",
      label: "Bare Workflow",
      skills: [],
      tool_grants: [],
      knowledge_sources: [],
    };
    mount({ listAgents: vi.fn(async () => page([bare])) });
    const card = await cardFor("Bare Workflow");
    // The old table wrote "0 knowledge · 0 tools · 0 skills" and called it three
    // facts. A grant that was never made has no chip at all.
    expect(card).not.toHaveTextContent(/0\s*(skills?|tools?|knowledge)/i);
    expect(within(card).queryByText("Tools")).not.toBeInTheDocument();
    expect(within(card).queryByText("Skill")).not.toBeInTheDocument();
    expect(within(card).queryByText("Knowledge")).not.toBeInTheDocument();
    expect(card.textContent).not.toMatch(/\b0\b/);
  });

  it("the two groups partition by served strategy and preserve server order", async () => {
    const secondAgentic: AgentSummary = {
      ...agentic,
      agent_id: "agent-policy",
      label: "Policy Reviewer",
    };
    const secondFixed: AgentSummary = {
      ...deterministic,
      agent_id: "agent-sop",
      label: "Meridian Current SOP Review",
    };
    // Interleaved on the wire, and deliberately NOT alphabetical: the served
    // order is what the two groups must each preserve.
    mount({
      listAgents: vi.fn(async () => page([secondAgentic, secondFixed, agentic, deterministic])),
    });
    await screen.findByRole("link", { name: "Policy Reviewer" });

    const conversational = screen.getByRole("region", { name: "Conversational agents" });
    const fixed = screen.getByRole("region", { name: "Deterministic agents" });
    expect(conversational).toHaveTextContent("agentic — an open instruction, executed under");
    expect(fixed).toHaveTextContent("deterministic — the plan is frozen before the run starts");

    const names = (region: HTMLElement) =>
      within(region)
        .getAllByRole("heading", { level: 3 })
        .map((heading) => heading.textContent);
    // Partition: every Agent in exactly one group, by the SERVER's strategy.
    expect(names(conversational)).toEqual(["Policy Reviewer", "Meridian Analyst"]);
    expect(names(fixed)).toEqual(["Meridian Current SOP Review", "Meridian Hardship Review"]);
  });

  it("a deterministic card's plate is the served workflow identity", async () => {
    mount();
    const fixed = await cardFor("Meridian Hardship Review");
    expect(fixed).toHaveTextContent("meridian-b1-disaster-hardship");
    expect(fixed).toHaveTextContent("v1");
    // The WORKFLOW profile's hash, truncated — never the Agent definition's.
    expect(within(fixed).getByTitle(deterministic.workflow!.config_hash)).toHaveTextContent(
      "e90381",
    );
    expect(
      within(fixed).getByRole("button", { name: "Copy workflow profile hash" }),
    ).toBeInTheDocument();
    expect(fixed.textContent).not.toContain("a1b2c3");

    // workflow: null renders no plate, and nothing stands in for one.
    const conversational = await cardFor("Meridian Analyst");
    expect(conversational.querySelector(".ag-card__plate")).toBeNull();
    expect(conversational).not.toHaveTextContent("Workflow");
    expect(
      within(conversational).queryByRole("button", { name: "Copy workflow profile hash" }),
    ).not.toBeInTheDocument();
  });

  it("availability reasons are the server's words", async () => {
    const refused: AgentSummary = {
      ...deterministic,
      agent_id: "agent-down",
      label: "Grounded Reviewer",
      available: false,
      availability_reasons: ["knowledge-source-unavailable", "tool lookup is unhealthy"],
    };
    mount({ listAgents: vi.fn(async () => page([refused])) });
    const card = await cardFor("Grounded Reviewer");
    expect(card).toHaveTextContent(
      "Unavailable — knowledge-source-unavailable, tool lookup is unhealthy",
    );
    // No console-authored gloss on a governed decision the console did not make.
    expect(card).not.toHaveTextContent(/temporarily|try again|check back|currently offline/i);
  });

  it("says Unavailable alone when the server sent no reason", async () => {
    mount({
      listAgents: vi.fn(async () =>
        page([{ ...deterministic, available: false, availability_reasons: [] }]),
      ),
    });
    const card = await cardFor("Meridian Hardship Review");
    expect(card).toHaveTextContent("Unavailable");
    expect(card).not.toHaveTextContent("Unavailable —");
  });

  it("gives the lone conversational agent the lead card and the one primary action", async () => {
    mount();
    const lead = await cardFor("Meridian Analyst");
    expect(lead.className).toContain("ag-card--lead");
    expect(lead).toHaveTextContent("Agentic");
    expect(lead).toHaveTextContent("model-directed within caps");
    // THE CAPS ARE THE ENFORCED ONES, and they say so in words the transcript
    // band's own "N steps" count cannot be mistaken for. The declared budgets
    // (12 steps / 24,000 in / 4,000 out) are not printed here: 12 is enforced as
    // no count at all, and the two token budgets are enforced as one ceiling.
    expect(lead).toHaveTextContent("Caps · 10 tool calls · 11 model calls · 28,000 tokens");
    expect(lead).not.toHaveTextContent("steps");
    expect(lead).toHaveTextContent("Meridian Servicing Operations");
    expect(lead).toHaveTextContent("agent.meridian-analyst.v2");
    expect(within(lead).getByRole("link", { name: "Open workspace" }).className).toContain(
      "ne-btn--primary",
    );
    // Exactly one primary on the screen.
    expect(document.querySelectorAll(".ne-btn--primary")).toHaveLength(1);

    // Owner / model policy / caps are the lead's alone; a workflow card does
    // not repeat them.
    const fixed = await cardFor("Meridian Hardship Review");
    expect(fixed).not.toHaveTextContent("Owner");
    expect(fixed).not.toHaveTextContent("Model policy");
    expect(fixed).not.toHaveTextContent("Caps ·");
  });

  it("renders no group heading for a strategy the server sent no agents for", async () => {
    mount({ listAgents: vi.fn(async () => page([agentic])) });
    await screen.findByRole("link", { name: "Meridian Analyst" });
    expect(screen.getByRole("region", { name: "Conversational agents" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Deterministic agents" })).not.toBeInTheDocument();
    expect(screen.queryByText("Fixed workflows")).not.toBeInTheDocument();
  });

  it("counts the served page in the facts margin and links each agent by id", async () => {
    mount({
      listAgents: vi.fn(async () => page([agentic, { ...deterministic, available: false }])),
    });
    await screen.findByRole("link", { name: "Meridian Analyst" });
    expect(screen.getByText("2 agents · 1 available")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Meridian Analyst" })).toHaveAttribute(
      "href",
      "/agents/agent-meridian",
    );
  });

  it("states an empty catalog as a fact and offers no create affordance", async () => {
    mount({ listAgents: vi.fn(async () => page([])) });
    expect(
      await screen.findByText("No agents are registered in this deployment."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Agents are defined in the registry and served to this console; this screen creates nothing.",
      ),
    ).toBeInTheDocument();
    // This console does not author Agents, so it must not offer to.
    expect(screen.queryByRole("button", { name: /create|new agent/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /create|new agent/i })).not.toBeInTheDocument();
  });

  it("pages through opaque server cursors without inventing a total", async () => {
    const user = userEvent.setup();
    const listAgents = vi
      .fn<AgentsInteriorClient["listAgents"]>()
      .mockResolvedValueOnce(page([agentic], "opaque-next"))
      .mockResolvedValueOnce(page([{ ...agentic, agent_id: "agent-2", label: "Policy Reviewer" }]));
    mount({ listAgents });
    await screen.findByText("Meridian Analyst");
    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Policy Reviewer")).toBeInTheDocument();
    expect(listAgents).toHaveBeenLastCalledWith("opaque-next");
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("renders a sanitized 404 as neutral and unretryable, distinct from an error", async () => {
    const refused = Object.assign(new Error("no"), { status: 404 });
    const view = mount({ listAgents: vi.fn(async () => Promise.reject(refused)) });
    // ONE neutral presentation covers both "hidden from you" and "does not
    // exist": the server answers both with the same 404 so a refusal cannot
    // prove existence, and re-issuing the read cannot change the answer.
    expect(await screen.findByText("The Agents catalog isn’t available.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // No count beside the title: a failed read has no number to report.
    expect(screen.queryByText(/\d+ agents/)).not.toBeInTheDocument();
    view.unmount();

    mount({ listAgents: vi.fn(async () => Promise.reject(new Error("offline"))) });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load agents");
  });
});

/* --------------------------------------------------------------------------
 * THE COLLAPSED-TRACK DEFECT, on the catalog this time — the same failure the
 * transcript's step rows had, in a different container.
 *
 * WHAT SHIPPED. `.ag-cards` used `repeat(auto-fill, …)`, which KEEPS the tracks
 * it cannot fill. The CONVERSATIONAL group holds exactly one card, so at 1500px
 * that card was drawn into one third of an 1180px row with two empty tracks
 * beside it. At ~370px the card then: wrapped its title, CLIPPED its AVAILABLE
 * pill against its own `overflow: hidden` edge, wrapped its button label out of
 * a fixed-height box, and — because `.ne-mono` declared `overflow-wrap:
 * anywhere`, which lowers an element's min-content contribution to about one
 * character — fragmented the governed hash `9dc85b6a` into `9dc` / `85b` / `6a`
 * down three lines.
 *
 * WHY THESE ARE RULE ASSERTIONS. jsdom performs no layout, so a rendered
 * assertion cannot tell the broken stylesheet from the fixed one. The defect IS
 * the declaration. Each is paired with the DOM half it must not be "fixed" by:
 * no governed value may be shortened in the data to make it fit.
 * ------------------------------------------------------------------------ */

const CSS_DIR = resolve(fileURLToPath(import.meta.url), "..");
const AGENTS_SHEET = readFileSync(resolve(CSS_DIR, "agents.css"), "utf-8");
const SYSTEM_SHEET = readFileSync(resolve(CSS_DIR, "..", "styles", "system.css"), "utf-8");
const TABLE_SHEET = readFileSync(resolve(CSS_DIR, "..", "styles", "table.css"), "utf-8");

/** One rule's declarations, matched at the start of a line so a compound
 * selector cannot answer for the bare one. */
function ruleBody(sheet: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hit = new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, "m").exec(sheet);
  if (hit?.[1] === undefined) throw new Error(`no ${selector} rule`);
  return hit[1];
}

describe("the catalog card at a narrow track", () => {
  it("MUTATION `lone-card-takes-its-row`: the grid collapses tracks it cannot fill", () => {
    // `auto-fill` is the declaration that left two empty tracks beside the one
    // conversational card. Everything the card then did wrong followed from the
    // width it was left with.
    const body = ruleBody(AGENTS_SHEET, ".ag-cards");
    expect(body).toMatch(/grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(340px,\s*1fr\)\)/);
    expect(body).not.toMatch(/auto-fill/);
  });

  it("MUTATION `lead-card-spans`: the span is on the element that is in the grid", () => {
    // `.ag-card--lead` is on the <article> INSIDE the <li>. A `grid-column`
    // there addresses no grid, which is why the lead card silently never
    // spanned. The rule now sits on the <li>, and the component puts the class
    // there — both halves, because either alone is inert.
    expect(ruleBody(AGENTS_SHEET, ".ag-cards > li.ag-cards__lead")).toMatch(
      /grid-column:\s*1 \/ -1/,
    );
    expect(AGENTS_SHEET).not.toMatch(/^\.ag-card--lead\s*\{[^}]*grid-column/m);
  });

  it("MUTATION `identifier-never-fragments`: the data face gets a real minimum", () => {
    // `break-word` keeps the protection (a value that cannot fit its line still
    // breaks) without the collapse (`anywhere` zeroes the min-content
    // contribution, so the TRACK shrinks to one character first).
    const body = ruleBody(SYSTEM_SHEET, ".ne-mono");
    expect(body).toMatch(/overflow-wrap:\s*break-word/);
    expect(body).not.toMatch(/overflow-wrap:\s*anywhere/);
    expect(body).not.toMatch(/word-break/);
    // And the truncated-hash chip never breaks at all: eight characters plus a
    // copy button, in one piece, wherever it is drawn.
    expect(ruleBody(TABLE_SHEET, ".ne-hashchip")).toMatch(/white-space:\s*nowrap/);
  });

  it("MUTATION `nothing-clips-at-the-card-edge`: the state pill and the button hold", () => {
    // The card is `overflow: hidden`, so an over-wide row is not merely ugly —
    // it is CUT, and "AVAILABLE" reached the screen as "AVAILABL".
    for (const body of [ruleBody(TABLE_SHEET, ".ne-pill"), ruleBody(TABLE_SHEET, ".ne-btn")]) {
      expect(body).toMatch(/white-space:\s*nowrap/);
      expect(body).toMatch(/flex:\s*none/);
    }
    // …and the row they sit in wraps rather than overflowing the clip.
    expect(ruleBody(AGENTS_SHEET, ".ag-card__head")).toMatch(/flex-wrap:\s*wrap/);
  });
});

describe("the catalog card, in the DOM", () => {
  it("MUTATION `lead-card-spans`: the conversational card's grid item carries the span", async () => {
    mount();
    const card = await cardFor("Meridian Analyst");
    expect(card.closest("li")?.className).toBe("ag-cards__lead");
    // THE PAIRED HALF: a deterministic card is NOT promoted to a full row, so
    // the span means something. Three of them still share the grid.
    const fixed = await cardFor("Meridian Hardship Review");
    expect(fixed.closest("li")?.className).toBe("");
  });

  it("MUTATION `value-never-truncated-in-data`: the full hash is in the DOM", async () => {
    mount();
    const card = await cardFor("Meridian Analyst");
    const chip = card.querySelector(".ne-hashchip .ne-mono");
    if (chip === null) throw new Error("no hash chip on the card");
    // §2.7: eight characters are DISPLAYED, and the whole 64 is on the element.
    // Shortening the value itself — in the model, in the adapter, anywhere but
    // the render — is what this forbids.
    expect(chip.getAttribute("title")).toBe(agentic.config_hash);
    expect(agentic.config_hash).toHaveLength(64);
  });
});
