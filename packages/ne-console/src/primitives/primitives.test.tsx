// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The design-system primitives (redesign §1.4). Nothing else in this console is
// allowed to invent a state word, a figure without a source, or a disclosure
// that hides its own contents — so these five laws are tested here, once:
//   L1  four parts or it does not render (LEGEND / VALUE / STATE / SOURCE)
//   L3  absent ≠ zero
//   L5  state is a glyph PLUS a word, never colour alone
//   §1.4 the drawer states its contents and count, and INSPECT force-opens it
//   L4  a refusal is a proof, not an error

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";
import { EmptyState } from "./EmptyState";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { InspectProvider } from "./InspectContext";
import { InspectSwitch } from "./InspectSwitch";
import { PrincipalLabel } from "./PrincipalLabel";
import { RefusalProof } from "./RefusalProof";
import { STATE_WORDS, StatePill } from "./StatePill";

describe("StatePill (L5)", () => {
  it("renders a glyph AND a word for every member of the closed set", () => {
    for (const word of STATE_WORDS) {
      const { unmount, container } = render(<StatePill word={word} />);
      // The word is spelled out — colour is redundant reinforcement only.
      expect(screen.getByLabelText(word)).toBeInTheDocument();
      const glyph = container.querySelector(".ne-pill__glyph");
      expect(glyph?.textContent).toMatch(/[●○◑⊘⚠⊟]/);
      unmount();
    }
  });

  it("spells the detail into the accessible name", () => {
    render(<StatePill word="BREAK" detail="seq 4021" />);
    expect(screen.getByLabelText("BREAK — seq 4021")).toBeInTheDocument();
  });

  // ---- The round-3 state-vocabulary divergence, and the half of it that did
  // NOT change. design/round3/screens/NOTES.md, "Named divergences from the
  // round-2 CSS baseline": PENDING renders in the neutral pill, because a queue
  // at rest should be quiet and amber is spent on the exceptional rows. Both
  // halves are asserted — a test that only pinned the remap would certify the
  // gap away by proving nothing about what amber is still for.
  //
  // The tone CLASS is only half the claim: "neutral" and "amber" are colours,
  // so the two rules that give those classes their colour are read out of the
  // stylesheet as well. Otherwise recolouring .ne-pill--idle to amber would
  // leave every assertion below green.

  const SYSTEM_CSS = readFileSync(
    resolve(fileURLToPath(import.meta.url), "..", "..", "styles", "system.css"),
    "utf-8",
  );

  /** The `color:` declaration inside the single `.ne-pill--<tone> { … }` rule. */
  function pillToneColour(tone: string): string {
    const block = new RegExp(`\\.ne-pill--${tone}\\s*\\{([^}]*)\\}`).exec(SYSTEM_CSS);
    if (block?.[1] === undefined) throw new Error(`.ne-pill--${tone} has no rule in system.css`);
    const colour = /(?:^|[;{\s])color:\s*([^;]+);/.exec(block[1]);
    if (colour?.[1] === undefined) throw new Error(`.ne-pill--${tone} sets no colour`);
    return colour[1].trim();
  }

  it("renders PENDING in the neutral tone, keeping the partial glyph", () => {
    const { container } = render(<StatePill word="PENDING" />);
    const pill = container.querySelector(".ne-pill");
    expect(pill).toHaveClass("ne-pill--idle");
    expect(pill).not.toHaveClass("ne-pill--warn");
    // The divergence is about tone only — the glyph vocabulary is unchanged.
    expect(container.querySelector(".ne-pill__glyph")?.textContent).toBe("◑");
  });

  it("control: OVERDUE and ESCALATED still carry the warn tone", () => {
    for (const word of ["OVERDUE", "ESCALATED"] as const) {
      const { container, unmount } = render(<StatePill word={word} />);
      const pill = container.querySelector(".ne-pill");
      expect(pill, word).toHaveClass("ne-pill--warn");
      expect(pill, word).not.toHaveClass("ne-pill--idle");
      unmount();
    }
  });

  it("the neutral tone draws in --text-dim and the warn tone in --amber", () => {
    expect(pillToneColour("idle")).toBe("var(--text-dim)");
    expect(pillToneColour("warn")).toBe("var(--amber)");
    // Self-test: the reader is not silently returning a default. A tone with no
    // rule throws rather than reporting a colour nothing declares.
    expect(() => pillToneColour("nosuchtone")).toThrow();
  });
});

describe("EvidenceDrawer + InspectSwitch (§1.4)", () => {
  beforeEach(() => sessionStorage.clear());

  it("states its contents and count in the summary", () => {
    render(
      <InspectProvider>
        <EvidenceDrawer summary="CI tests" count={6}>
          <p>evidence</p>
        </EvidenceDrawer>
      </InspectProvider>,
    );
    expect(screen.getByText("Show evidence — CI tests (6)")).toBeInTheDocument();
  });

  it("is a native <details> — closed by default, keyboard-openable with no JS trap", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <InspectProvider>
        <EvidenceDrawer summary="event hash, prev hash, payload">
          <p>raw</p>
        </EvidenceDrawer>
      </InspectProvider>,
    );
    const details = container.querySelector("details");
    expect(details?.open).toBe(false);
    await user.click(screen.getByText(/Show evidence/));
    expect(details?.open).toBe(true);
  });

  it("INSPECT ON force-opens every drawer on the page and says so in words", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <InspectProvider>
        <InspectSwitch />
        <EvidenceDrawer summary="one">
          <p>a</p>
        </EvidenceDrawer>
        <EvidenceDrawer summary="two">
          <p>b</p>
        </EvidenceDrawer>
      </InspectProvider>,
    );
    const drawers = () => [...container.querySelectorAll("details")];
    expect(drawers().every((d) => !d.open)).toBe(true);

    // A labelled switch with a VISIBLE state word — never an icon.
    const sw = screen.getByRole("switch");
    expect(sw).toHaveTextContent("INSPECT OFF");
    expect(sw).toHaveAttribute("aria-checked", "false");

    await user.click(sw);
    expect(sw).toHaveTextContent("INSPECT ON");
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(drawers().every((d) => d.open)).toBe(true);
  });

  it("the I shortcut toggles inspection from anywhere that is not a field", async () => {
    const user = userEvent.setup();
    render(
      <InspectProvider>
        <InspectSwitch />
        <input aria-label="a field" />
      </InspectProvider>,
    );
    await user.keyboard("i");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");

    // Typing "i" INTO a field must not flip the console's disclosure mode.
    await user.click(screen.getByLabelText("a field"));
    await user.keyboard("i");
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("a shortened principal visibly names the mounted keyboard affordance", async () => {
    const user = userEvent.setup();
    const id = "c21456b9-c8dc-4458-b393-bc3230db0081";
    render(
      <InspectProvider>
        <PrincipalLabel id={id} />
      </InspectProvider>,
    );
    expect(screen.getByText("c21456b9")).toBeInTheDocument();
    expect(
      screen.getByText("principal id, shortened — press I to show it in full"),
    ).toBeInTheDocument();
    await user.keyboard("i");
    expect(screen.getByText(id)).toBeInTheDocument();
  });
});

describe("RefusalProof (L4) + EmptyState", () => {
  it("names what was refused and that the SERVER refused it, and opens onto the record when the chain is readable", () => {
    render(
      <MemoryRouter>
        <RefusalProof role="ops" surface="The audit ledger" chainReadable />
      </MemoryRouter>,
    );
    expect(screen.getByText(/GATED — OPS KEY REQUIRED/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "The audit ledger is gated to ops roles. The server refused this key with a 403.",
      ),
    ).toBeInTheDocument();
    // The audit claim is made ONLY alongside the link that discharges it.
    expect(
      screen.getByText(/Refused attempts are written to the audit chain — check this one there\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /See the refusal in the chain/ })).toHaveAttribute(
      "href",
      "/audit",
    );
  });

  it("NEVER asserts the refusal was audited when this key cannot read the chain", () => {
    // The unverifiable-proof-claim defect. "…and audited the attempt" appeared on
    // five surfaces, none of them a link, and it is the first claim a skeptical
    // reader tests. With no chain read available the component must attribute the
    // write to design rather than assert it as an observation — and must still
    // offer no dead-end link.
    const { container } = render(
      <MemoryRouter>
        <RefusalProof role="approver" surface="This queue" />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.textContent).not.toContain("audited the attempt");
    expect(container.textContent).toContain("does not claim to have seen it");
  });

  it("an empty state is a designed state: a heading that is not a fault, and one action", () => {
    render(
      <MemoryRouter>
        <EmptyState title="Nothing is waiting on you." action={{ label: "GO →", to: "/chat" }}>
          <p>Tier-2 actions appear here.</p>
        </EmptyState>
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Nothing is waiting on you." })).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });
});
