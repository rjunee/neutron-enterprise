// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RENDER IS THE SPEC, AND THIS TEST READS IT.
//
// design/round3/screens/home.html is the approved round-3 rail. It carries a
// sprite of twenty icon ids and puts one of them on every destination. The
// shipped rail had none: it drew a two-letter monogram (`Audit log` → `AU`)
// written as a stand-in for the collapsed rail, invisible at full width.
//
// So this suite does not freeze a hand-copied table of paths — a table is
// exactly the artefact that drifts from the render it was copied out of. It
// PARSES the render at test time and compares, element by element and attribute
// by attribute, the geometry the console draws for each destination against the
// geometry the render draws for that same destination's label. A redrawn icon,
// a nudged coordinate, a heavier stroke, or an icon put on the wrong row all
// fail here.
//
// Each rule is paired with its complement (the icon is drawn AND it is silent
// to AT; the mapping is complete AND it maps nothing the rail does not have)
// and with a self-test that plants a divergence, so the comparison can never
// pass by comparing nothing.

import { render, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter } from "react-router";
import { beforeAll, describe, expect, it } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { PostureProvider } from "../posture/PostureContext";
import { AuthStub } from "../test/authStub";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { NavRail } from "./NavRail";
import { NAV_FOOT, NAV_SECTIONS } from "./navSections";

// ===========================================================================
// The approved render, parsed.
// ===========================================================================

const RENDER_HTML = resolve(__dirname, "..", "..", "design", "round3", "screens", "home.html");
const renderSource = readFileSync(RENDER_HTML, "utf-8");

/** The render's sprite: icon id → the raw markup of its `<g>` body. */
function spriteBodies(html: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of html.matchAll(/<g id="(i-[a-z]+)">([\s\S]*?)<\/g>/g)) out.set(m[1]!, m[2]!);
  return out;
}

/** The render's rail, in DOM order: [visible label, the icon id it wears]. */
function railNodes(html: string): Array<{ label: string; icon: string }> {
  const out: Array<{ label: string; icon: string }> = [];
  for (const m of html.matchAll(/<a class="ne-rail__node[^"]*"[^>]*>([\s\S]*?)<\/a>/g)) {
    const inner = m[1]!;
    const icon = /<use href="#(i-[a-z]+)"\/>/.exec(inner)?.[1];
    const label = /<span>([^<]*)<\/span>/.exec(inner)?.[1];
    if (icon !== undefined && label !== undefined) out.push({ label, icon });
  }
  return out;
}

const SPRITE = spriteBodies(renderSource);
const RENDER_NODES = railNodes(renderSource);

/**
 * INTEGRATION NOTE (ne/ux-batch-1): this suite was written against the rail's
 * base (1b804d48), where Access was still a NAV_SECTIONS destination and the
 * round-3 render matched it 1:1. Main has since shipped PR #905
 * ("hide Access until B6 is served"): navSections.ts drops /access from the
 * live registry until B6 exists (see its own comment there), but the frozen,
 * approved round-3 render was correctly left untouched — it is a historical
 * artefact, not a live nav mirror, and its Access row/icon stay valid for when
 * B6 restores the destination. So the render legitimately draws one more
 * destination than the shipped rail currently has. Every comparison below of
 * "what the render draws" against "what ships" excludes that one, known,
 * already-product-decided gap — everywhere else the 1:1 fidelity this suite
 * exists to guarantee is unchanged and still fails shut on real drift.
 */
/**
 * SECOND KNOWN GAP (2026-08-27): Ask is no longer a rail destination.
 *
 * The owner's instruction was that Agents is the top level and Ask is the Chat
 * tab of the Agent it always ran, so `Ask` left NAV_SECTIONS and `Agents` moved
 * to the head of Execution. The `/ask` ROUTE is alive and redirects (App.tsx) —
 * only the rail entry is gone. The frozen round-3 render is again correctly
 * untouched: it is a historical artefact, and its Ask row/icon record what the
 * rail looked like when it was approved.
 *
 * That makes the render differ from the shipped rail in ORDER as well as in
 * membership, so the artwork comparison below joins the two BY LABEL rather
 * than by position. That is strictly stronger for what this suite exists to
 * prove — every shipped destination wears the render's artwork FOR THAT
 * destination — and it stops encoding an ordering the product has since
 * changed. The rail's own order is pinned, ordered and in full, by
 * NavRail.test.tsx.
 */
/**
 * THIRD KNOWN GAP (2026-08-28): Workflows is no longer a rail destination.
 *
 * The owner directed the page out of the product — SPEC §13.1 item 8
 * (PD-WORKFLOWS-REMOVE): it was a second navigation item for the same three
 * deterministic profiles the Agents surface already runs, on the same engine
 * and the same frozen config hash. Commit 6480ea04 removed the NAV_SECTIONS
 * entry; the `/workflows` route, `WorkflowsPage` and their suites deliberately
 * remain until the rest of that item lands. The frozen round-3 render is, for
 * the third time, correctly untouched: it records what the rail looked like
 * when it was approved, and it is a historical artefact rather than a live nav
 * mirror.
 */
const RENDER_NODES_LIVE = RENDER_NODES.filter(
  (n) => n.label !== "Access" && n.label !== "Ask" && n.label !== "Workflows",
);
const RENDER_BY_LABEL = new Map(RENDER_NODES_LIVE.map((n) => [n.label, n]));

// ===========================================================================
// Geometry, as comparable data.
//
// Attribute ORDER is not meaning — React emits props in source order and the
// parser reports them as written — so each element becomes {tag, attrs} and the
// attrs are a plain object, compared by value.
// ===========================================================================

type Shape = { tag: string; attrs: Record<string, string> };

function shapesOf(parent: Element): Shape[] {
  return [...parent.children].map((el) => {
    const attrs: Record<string, string> = {};
    for (const a of [...el.attributes]) attrs[a.name] = a.value;
    return { tag: el.tagName.toLowerCase(), attrs };
  });
}

/** The render's artwork for one sprite id, as shapes. */
function spriteShapes(icon: string): Shape[] {
  const body = SPRITE.get(icon);
  expect(body, `sprite #${icon} present in the render`).toBeDefined();
  const doc = new DOMParser().parseFromString(
    `<svg xmlns="http://www.w3.org/2000/svg">${body}</svg>`,
    "image/svg+xml",
  );
  expect(doc.querySelector("parsererror")).toBeNull();
  return shapesOf(doc.documentElement);
}

// ===========================================================================
// The shipped rail.
// ===========================================================================

/** Nesting mirrors App.tsx's AuthedLayout, as NavRail.test.tsx's harness does. */
function renderRail(collapsed = false) {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={100000}>
              <PendingApprovalsProvider intervalMs={100000}>
                <PostureProvider>
                  <NavRail collapsed={collapsed} />
                </PostureProvider>
              </PendingApprovalsProvider>
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

async function settledRail(collapsed = false): Promise<HTMLElement> {
  const { container } = renderRail(collapsed);
  await waitFor(() => expect(container.querySelector(".ne-rail__slotpending")).toBeNull());
  return container;
}

/** Every destination the SHIPPED rail knows, label → route, in rail order. */
const SHIPPED: ReadonlyArray<{ label: string; to: string }> = [
  ...NAV_SECTIONS.flatMap((section) =>
    section.items === undefined
      ? [{ label: section.label, to: section.to }]
      : section.items.map((item) => ({ label: item.label, to: item.to })),
  ),
  { label: NAV_FOOT.label, to: NAV_FOOT.to },
];

function iconIn(container: HTMLElement, to: string): SVGElement | null {
  const link = container.querySelector(
    `.ne-rail__nav a[href="${to}"], .ne-rail__foot a[href="${to}"]`,
  );
  expect(link, `the rail links ${to}`).not.toBeNull();
  return link!.querySelector("svg.ne-rail__icon");
}

describe("the rail's per-destination icon — parsed out of the approved render", () => {
  it("finds a render to compare against (never passes by examining nothing)", () => {
    expect(SPRITE.size).toBe(20);
    expect(RENDER_NODES.length).toBe(20);
    // Every id the sprite defines is actually worn by a rail node, and every
    // node's id is actually defined: no dead artwork, no dangling reference.
    expect([...SPRITE.keys()].sort()).toEqual([...RENDER_NODES.map((n) => n.icon)].sort());
    // …and the artwork is real geometry, not an empty <g>.
    for (const icon of SPRITE.keys()) expect(spriteShapes(icon).length).toBeGreaterThan(0);
  });

  it("the render's destinations ARE the shipped rail's destinations", () => {
    // The 1:1 claim, checked rather than assumed: same labels, same count, in
    // both directions. If a destination is ever added to navSections without
    // the render gaining one — or the render keeps artwork for a destination
    // that no longer ships — this is where that shows up. Order is deliberately
    // not asserted here any more (see RENDER_NODES_LIVE); NavRail.test.tsx owns
    // the shipped rail's exact ordered identity.
    expect([...RENDER_NODES_LIVE.map((n) => n.label)].sort()).toEqual(
      [...SHIPPED.map((d) => d.label)].sort(),
    );
  });

  it("draws, for EVERY destination, the render's own artwork for that destination", async () => {
    const container = await settledRail();
    const compared: string[] = [];
    for (const dest of SHIPPED) {
      const node = RENDER_BY_LABEL.get(dest.label);
      expect(node, `the render draws ${dest.label}`).toBeDefined();
      const svg = iconIn(container, dest.to);
      expect(svg, `${dest.label} wears an icon`).not.toBeNull();
      expect(svg!.getAttribute("viewBox")).toBe("0 0 16 16");
      // THE COMPARISON: shipped geometry vs the render's, element by element.
      expect(shapesOf(svg!), `${dest.label} → #${node!.icon}`).toEqual(spriteShapes(node!.icon));
      compared.push(dest.label);
    }
    // Three of the render's rows are not shipped destinations — Access (frozen,
    // pending B6), Ask (now the Agent workspace's Chat tab) and Workflows
    // (directed out of the product, 2026-08-28) — so this counts against
    // SHIPPED.length rather than a magic number. See RENDER_NODES_LIVE.
    expect(compared).toHaveLength(SHIPPED.length);
  });

  it("puts one icon on every destination and nothing else — no bare row, no spare mark", async () => {
    const container = await settledRail();
    const links = container.querySelectorAll(".ne-rail__nav a, .ne-rail__foot a");
    expect(links.length).toBe(SHIPPED.length);
    for (const link of links) expect(link.querySelectorAll("svg.ne-rail__icon").length).toBe(1);
    // The complement: the icon count is the destination count exactly — the
    // wordmark's mark is not one of these, and no row grew a second glyph.
    expect(container.querySelectorAll("svg.ne-rail__icon").length).toBe(SHIPPED.length);
  });

  it("THE MONOGRAM IS GONE — nothing renders the two-letter stand-in any more", async () => {
    const container = await settledRail(true);
    expect(container.querySelector(".ne-rail__mono")).toBeNull();
    // The collapsed rail is where the monogram used to be the whole visible
    // destination. Every row's text is now the FULL label and nothing else —
    // no `AU`, no `CO`, no `KP` sitting beside or instead of the word. (The
    // label is display:none there, which jsdom does not apply and CSS does;
    // what matters to AT is that the text is the name, not a truncation.)
    for (const dest of SHIPPED) {
      const link = container.querySelector<HTMLElement>(
        `.ne-rail__nav a[href="${dest.to}"], .ne-rail__foot a[href="${dest.to}"]`,
      );
      expect(link, dest.label).not.toBeNull();
      link!.querySelector(".ne-rail__badge")?.remove();
      expect(link!.textContent).toBe(dest.label);
    }
  });
});

describe("the icon is decorative — it changes nothing an assistive technology reads", () => {
  it("is aria-hidden on every destination, and carries no text of its own", async () => {
    const container = await settledRail();
    const icons = [...container.querySelectorAll("svg.ne-rail__icon")];
    expect(icons.length).toBe(SHIPPED.length);
    for (const icon of icons) {
      expect(icon.getAttribute("aria-hidden")).toBe("true");
      expect(icon.getAttribute("role")).toBeNull();
      expect(icon.getAttribute("aria-label")).toBeNull();
      expect(icon.textContent).toBe("");
    }
  });

  it("leaves every accessible name EXACTLY the destination's label — expanded and collapsed", async () => {
    for (const collapsed of [false, true]) {
      const container = await settledRail(collapsed);
      for (const dest of SHIPPED) {
        // Approvals' name absorbs its badge; every other row is the bare label.
        if (dest.to === "/approvals") continue;
        const link = within(container).getByRole("link", { name: dest.label });
        expect(link.getAttribute("href"), `${dest.label} (collapsed=${collapsed})`).toBe(dest.to);
      }
      // The collapsed rail hides the label VISUALLY and still names the link
      // through aria-labelledby — the property the icon swap could have broken.
      const audit = container.querySelector<HTMLElement>('a[href="/audit"]')!;
      expect(audit.getAttribute("aria-labelledby")).not.toBeNull();
      expect(within(audit).getByText("Audit log")).toBeInTheDocument();
      expect(within(container).getByRole("link", { name: "Audit log" })).toBe(audit);
    }
  });

  it("keeps the Approvals badge in the link's name, beside its icon", async () => {
    const container = await settledRail();
    const approvals = container.querySelector('a[href="/approvals"]')!;
    const badge = await waitFor(() => {
      const b = approvals.querySelector(".ne-rail__badge");
      expect(b).not.toBeNull();
      return b!;
    });
    // aria-labelledby still points at BOTH the label and the badge…
    const ids = (approvals.getAttribute("aria-labelledby") ?? "").split(" ").filter(Boolean);
    expect(ids).toHaveLength(2);
    expect(ids).toContain(badge.id);
    // …and the icon is present on that row without joining the name.
    expect(approvals.querySelector("svg.ne-rail__icon")).not.toBeNull();
    expect(ids.some((id) => approvals.querySelector(`#${CSS.escape(id)}`)?.tagName === "svg")).toBe(
      false,
    );
  });
});

describe("the icon's style is the render's style, declaration for declaration", () => {
  // jsdom applies no stylesheet, so the rules are read off the committed CSS —
  // the method src/styles/tokens.test.ts already uses for the token layer — and
  // compared against the SAME declarations in the approved render.

  const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

  /** One rule's declarations as {prop: value}. The selector must start a rule,
   * so `.ne-rail__icon` never matches inside `.ne-rail__node.active .ne-rail__icon`. */
  function declarations(css: string, selector: string): Record<string, string> {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const m = new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(stripComments(css));
    expect(m, `rule ${selector} present`).not.toBeNull();
    const out: Record<string, string> = {};
    for (const decl of m![1]!.split(";")) {
      const at = decl.indexOf(":");
      if (at === -1) continue;
      out[decl.slice(0, at).trim()] = decl.slice(at + 1).trim();
    }
    return out;
  }

  const shellCss = readFileSync(resolve(__dirname, "..", "styles", "shell.css"), "utf-8");

  it("reads real rules out of both files (never passes by examining nothing)", () => {
    expect(Object.keys(declarations(renderSource, ".ne-rail__icon"))).toHaveLength(4);
    expect(
      Object.keys(declarations(renderSource, ".ne-rail__node.active .ne-rail__icon")),
    ).toHaveLength(2);
  });

  it("matches the render on the resting icon and on the active one", () => {
    expect(declarations(shellCss, ".ne-rail__icon")).toEqual(
      declarations(renderSource, ".ne-rail__icon"),
    );
    expect(declarations(shellCss, ".ne-rail__node.active .ne-rail__icon")).toEqual(
      declarations(renderSource, ".ne-rail__node.active .ne-rail__icon"),
    );
  });

  it("draws in currentColor and in tokens — never a literal colour", () => {
    // The complement of "it matches the render": WHAT it matches is a
    // monochrome mark that inherits its row's colour, and one accent that
    // resolves through the token layer. The token-hardcode guardian bans a raw
    // hex globally; this pins the positive form for this element.
    const active = declarations(shellCss, ".ne-rail__node.active .ne-rail__icon");
    expect(active.color).toBe("var(--cherenkov)");
    const art = readFileSync(resolve(__dirname, "RailIcon.tsx"), "utf-8");
    for (const paint of art.matchAll(/(?:fill|stroke)="([^"]*)"/g)) {
      expect(["none", "currentColor"]).toContain(paint[1]);
    }
  });

  it("carries no motion: the mark is not an animation (law 4)", () => {
    const resting = declarations(shellCss, ".ne-rail__icon");
    const active = declarations(shellCss, ".ne-rail__node.active .ne-rail__icon");
    for (const rule of [resting, active]) {
      expect(rule.transition).toBeUndefined();
      expect(rule.animation).toBeUndefined();
    }
  });

  it("the collapsed rail's loading slot holds the width of what arrives in it", () => {
    // The slot's job is the destination's geometry while its verdict is in
    // flight. In the collapsed rail what arrives is the icon, so the shimmer is
    // the icon's width — DERIVED from the same declaration, not a second
    // literal that can drift. (It was 24px: the monogram's box.)
    const icon = declarations(shellCss, ".ne-rail__icon");
    const slot = declarations(
      shellCss,
      ".ne-shell--collapsed .ne-rail__slotpending > .ne-skeleton",
    );
    expect(slot.width).toBe(icon.width);
    // The complement: the EXPANDED slot is deliberately NOT the icon's width —
    // there the row that arrives is an icon plus a full label, so the shimmer
    // spans the row instead of the mark.
    expect(declarations(shellCss, ".ne-rail__slotpending > .ne-skeleton").width).not.toBe(
      icon.width,
    );
  });

  it("THE MONOGRAM'S STYLE IS GONE TOO — no rule is left to resurrect it", () => {
    expect(stripComments(shellCss)).not.toContain("ne-rail__mono");
    // …and the collapsed rail still hides exactly what it hid before, so
    // deleting the monogram did not take a neighbouring rule with it.
    expect(shellCss).toContain(".ne-shell--collapsed .ne-rail__nodelabel");
    expect(shellCss).toContain(".ne-shell--collapsed .ne-rail__grouplabel");
    expect(shellCss).toContain(".ne-shell--collapsed .ne-rail__deployment");
  });
});

describe("self-test: the comparison bites a divergence and spares a match", () => {
  let home: Shape[];
  beforeAll(() => {
    home = spriteShapes("i-home");
  });

  it("an identical copy passes", () => {
    expect(spriteShapes("i-home")).toEqual(home);
  });

  it("a nudged coordinate, a heavier stroke, an extra element and a wrong icon all fail", () => {
    const nudged = structuredClone(home);
    nudged[0]!.attrs.d = `${nudged[0]!.attrs.d} `;
    expect(nudged).not.toEqual(home);

    const heavier = structuredClone(home);
    heavier[0]!.attrs["stroke-width"] = "2";
    expect(heavier).not.toEqual(home);

    const extra = [...structuredClone(home), { tag: "circle", attrs: { r: "2" } }];
    expect(extra).not.toEqual(home);

    // The wrong-row failure: Posture's shield and Verified Queries' shield are
    // the closest pair in the sprite, and they are still not equal.
    expect(spriteShapes("i-posture")).not.toEqual(spriteShapes("i-verified"));
    expect(spriteShapes("i-settings")).not.toEqual(home);
  });

  it("every sprite id is distinguishable from every other (no two rows share art)", () => {
    const seen = new Map<string, string>();
    for (const icon of SPRITE.keys()) {
      const key = JSON.stringify(spriteShapes(icon));
      expect(seen.has(key), `${icon} duplicates ${seen.get(key) ?? ""}`).toBe(false);
      seen.set(key, icon);
    }
    expect(seen.size).toBe(20);
  });
});
