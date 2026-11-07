// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// M4 GUARDIAN — the verdict grammar (design §4.5, §4.6), which is law 4 cl. 4:
// A DENIAL NEVER EASES.
//
// The centrepiece clause needs a BIDIRECTIONAL guardian, because both vacuous
// shapes are easy to write and both read as coverage: "the settle class is
// present" passes on a build where everything settles, and "the deny has no
// settle class" passes on a build where nothing animates at all. Every
// assertion below therefore comes in a pair — the refusal arrests AND the
// affirmation settles, asserted per verdict word over the closed set, so a
// mutation that collapses the grammar to one side fails whichever side it left.
//
// WHAT THIS CANNOT PROVE. jsdom does not run animations, and it does not apply
// media queries to computed style. These tests prove the STRUCTURE: which
// surface carries which class, that the class is applied only when a verdict
// arrives, and — by parsing approvals.css as text — which properties the
// stylesheet animates, with what duration vocabulary, and what it does under
// prefers-reduced-motion. No assertion here measures a frame rate and none may
// be reported as one (design §7).

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { render, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";
import type { ApprovalActionResult, PendingApprovalView } from "../api/types";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { DecisionPane } from "./DecisionPane";
import type { DecisionPaneProps } from "./DecisionPane";
import {
  ARREST_CLASS,
  CHAINED_CLASS,
  SETTLE_CLASS,
  motionClass,
  receiptMotion,
  verdictGrammar,
} from "./verdictMotion";
import type { Verdict } from "./verdictMotion";

/* ------------------------------------------------- the grammar, per word -- */

/** The closed set, with each word's side of cl. 4 written out INDIVIDUALLY. A
 * table this shape is the bidirectional part: a mapping that returned "settle"
 * for everything fails on DENIED and EXPIRED, and one that returned "arrest"
 * for everything fails on APPROVED and AWAITING SECOND. */
const EXPECTED: ReadonlyArray<[Verdict, "settle" | "arrest"]> = [
  ["APPROVED", "settle"],
  ["AWAITING SECOND", "settle"],
  ["DENIED", "arrest"],
  ["EXPIRED", "arrest"],
];

describe("§4.5 — the grammar over the closed verdict set", () => {
  for (const [verdict, grammar] of EXPECTED) {
    it(`${verdict} ${grammar}s`, () => {
      expect(verdictGrammar(verdict)).toBe(grammar);
    });
  }

  it("names both sides — a grammar with one side is not a grammar", () => {
    const sides = new Set(EXPECTED.map(([word]) => verdictGrammar(word)));
    expect([...sides].sort()).toEqual(["arrest", "settle"]);
  });

  it("an arrest is never gated on arrival: a refusal is instant whenever it is read", () => {
    for (const [verdict, grammar] of EXPECTED) {
      if (grammar !== "arrest") continue;
      for (const arrived of [true, false]) {
        expect(receiptMotion(verdict, arrived)).toEqual({
          verdict: ARREST_CLASS,
          record: ARREST_CLASS,
        });
      }
    }
  });

  it("an affirmation settles ONLY when it arrived — cl. 2, no replayed verdicts", () => {
    for (const [verdict, grammar] of EXPECTED) {
      if (grammar !== "settle") continue;
      expect(receiptMotion(verdict, true)).toEqual({
        verdict: SETTLE_CLASS,
        record: `${SETTLE_CLASS} ${CHAINED_CLASS}`,
      });
      // The same verdict, already on screen when the pane opened: nothing.
      expect(receiptMotion(verdict, false)).toEqual({ verdict: undefined, record: undefined });
    }
  });

  it("no verdict is ever dressed with BOTH — the two classes are exclusive", () => {
    for (const [verdict] of EXPECTED) {
      for (const arrived of [true, false]) {
        const { verdict: v, record: r } = receiptMotion(verdict, arrived);
        for (const cls of [v, r]) {
          if (cls === undefined) continue;
          expect(cls.includes(SETTLE_CLASS) && cls.includes(ARREST_CLASS)).toBe(false);
        }
      }
    }
  });

  it("§4.6 — the record line trails the decision by one --motion-chain step", () => {
    // The chained modifier rides ON the settle, so the record cannot arrive with
    // a different curve or a different duration than the decision it records.
    const { record } = receiptMotion("APPROVED", true);
    expect(record).toBe(`${SETTLE_CLASS} ${CHAINED_CLASS}`);
    // …and it is not applied to an arrest: a refusal's record does not ease in
    // 60 ms behind the refusal.
    expect(receiptMotion("DENIED", true).record).not.toContain(CHAINED_CLASS);
  });

  it("motionClass adds nothing when there is nothing to add", () => {
    expect(motionClass("ne-x", undefined)).toBe("ne-x");
    expect(motionClass("ne-x", SETTLE_CLASS)).toBe(`ne-x ${SETTLE_CLASS}`);
  });
});

/* --------------------------------------------------- the pane, as rendered -- */

const NOW = Date.parse("2026-08-03T12:00:00Z");

beforeEach(() => {
  server.use(
    http.get("*/api/v1/audit/approval-decisions/:id", () => HttpResponse.json({ seq: 4812 })),
  );
});

function approval(over: Partial<PendingApprovalView> = {}): PendingApprovalView {
  return {
    request_id: "req-a",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales",
    requested_at: new Date(NOW - 600_000).toISOString(),
    sla_deadline: new Date(NOW + 3_000_000).toISOString(),
    expires_at: new Date(NOW + 86_400_000).toISOString(),
    escalated: false,
    ...over,
  };
}

function result(over: Partial<ApprovalActionResult> = {}): ApprovalActionResult {
  return {
    status: "approved",
    approver: "alice@example.com",
    decided_at: new Date(NOW).toISOString(),
    ...over,
  };
}

function props(over: Partial<DecisionPaneProps> = {}): DecisionPaneProps {
  return {
    approval: approval(),
    now: NOW,
    state: {},
    decidedElsewhere: false,
    auditSeq: null,
    chainReadable: false,
    onClose: () => {},
    onApprove: () => {},
    onDeny: () => {},
    ...over,
  };
}

function renderPane(initial: DecisionPaneProps) {
  const tree = (p: DecisionPaneProps) => (
    <AuthStub>
      <MemoryRouter>
        <PrincipalProvider>
          <DecisionPane {...p} />
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>
  );
  const view = render(tree(initial));
  return {
    container: view.container,
    /** Re-render the SAME pane instance with new props — which is how a verdict
     * actually arrives: the pane never unmounts around a decision. */
    update: (next: DecisionPaneProps) => view.rerender(tree(next)),
  };
}

const settles = (el: Element | null) => el !== null && el.classList.contains(SETTLE_CLASS);
const arrests = (el: Element | null) => el !== null && el.classList.contains(ARREST_CLASS);
const verdictLine = (c: HTMLElement) => c.querySelector(".ne-approvals__verdict");
const recordLine = (c: HTMLElement) => c.querySelector(".ne-approvals__auditlink");

/** A decision made in front of the reader: the pane is open on the pending body
 * and the server's answer lands into the same mounted instance. */
function decide(over: Partial<ApprovalActionResult>, extra: Partial<DecisionPaneProps> = {}) {
  const pane = renderPane(props(extra));
  pane.update(props({ ...extra, state: { result: result(over) } }));
  return pane;
}

describe("§4.5 — the receipt, as the reader gets it", () => {
  it("an APPROVAL settles: the verdict line eases into place", () => {
    const { container } = decide({ status: "approved" });
    expect(settles(verdictLine(container))).toBe(true);
    // …and the same line does NOT carry the arrest class. Without this half, a
    // build that applied both would pass.
    expect(arrests(verdictLine(container))).toBe(false);
  });

  it("a DENIAL arrests: the verdict line has NO settle, and neither has anything else", () => {
    const { container } = decide({ status: "rejected" });
    expect(arrests(verdictLine(container))).toBe(true);
    expect(settles(verdictLine(container))).toBe(false);
    // The clause is about the whole refusal, not one paragraph of it: nothing in
    // a denied receipt eases.
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });

  it("an EXPIRED verdict arrests too — a request that ran out of time is a stop", () => {
    const { container } = decide({ status: "expired" });
    expect(arrests(verdictLine(container))).toBe(true);
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });

  it("CONTROL: the two verdicts differ in the DOM before the word is read", () => {
    const approved = decide({ status: "approved" }).container;
    const denied = decide({ status: "rejected" }).container;
    expect(verdictLine(approved)?.className).not.toBe(verdictLine(denied)?.className);
  });

  it("§4.6 — an approved record line trails the decision; a denied one arrests", async () => {
    const ok = decide({ status: "approved" }, { auditSeq: 4812, chainReadable: true }).container;
    await waitFor(() => expect(ok.querySelector("a[href='/audit?seq=4812']")).not.toBeNull());
    expect(settles(recordLine(ok))).toBe(true);
    expect(recordLine(ok)?.classList.contains(CHAINED_CLASS)).toBe(true);

    const no = decide({ status: "rejected" }, { auditSeq: 4813, chainReadable: true }).container;
    await waitFor(() => expect(no.querySelector("a[href='/audit?seq=4812']")).not.toBeNull());
    expect(arrests(recordLine(no))).toBe(true);
    expect(settles(recordLine(no))).toBe(false);
  });

  it("the exact lookup's checking line is dressed with nothing and never claims a write", async () => {
    const { container } = decide({ status: "approved" }, { auditSeq: null, chainReadable: true });
    await waitFor(() => expect(container.querySelector(".ne-approvals__auditlink")).not.toBeNull());
    const checking = container.querySelector(".ne-approvals__auditlink");
    expect(checking?.textContent).toBe("Checking the decision audit entry…");
    expect(checking?.textContent).not.toMatch(/writing/i);
    expect(settles(checking)).toBe(false);
    expect(arrests(checking)).toBe(false);
  });

  it("AWAITING SECOND settles — a Tier-3 first approval moved the request forward", () => {
    const pane = renderPane(props());
    pane.update(props({ state: { result: result({ status: "pending_second" }) } }));
    const line = pane.container.querySelector(".ne-approvals__second");
    expect(line?.textContent).toContain("A second, different approver must decide.");
    expect(settles(line)).toBe(true);
    expect(arrests(line)).toBe(false);
  });
});

describe("§4.5 — the arrival gate (cl. 2: depicts only what happened)", () => {
  it("a pane opened onto an ALREADY-KNOWN verdict animates nothing", () => {
    // Same props as the settling case, reached without ever being undecided.
    const { container } = renderPane(props({ state: { result: result({ status: "approved" }) } }));
    expect(verdictLine(container)?.textContent).toContain("APPROVED");
    expect(settles(verdictLine(container))).toBe(false);
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });

  it("CONTROL: the identical verdict, ARRIVING, does settle", () => {
    const { container } = decide({ status: "approved" });
    expect(settles(verdictLine(container))).toBe(true);
  });

  it("re-rendering a known state does not re-dress it — and does not undress it", () => {
    const pane = decide({ status: "approved" });
    const before = verdictLine(pane.container);
    // The screen re-renders about once a second (the Due countdown), and the
    // chain seq lands later. Neither is a new verdict.
    pane.update(props({ now: NOW + 1000, state: { result: result({ status: "approved" }) } }));
    pane.update(
      props({
        now: NOW + 2000,
        state: { result: result({ status: "approved" }) },
        auditSeq: 4812,
        chainReadable: true,
      }),
    );
    const after = verdictLine(pane.container);
    // The SAME node, still carrying the settle it was given once. A node that
    // was replaced would replay the animation on every clock tick.
    expect(after).toBe(before);
    expect(settles(after)).toBe(true);
  });

  it("switching to another request drops the previous decision's dressing", () => {
    const pane = decide({ status: "approved" });
    expect(settles(verdictLine(pane.container))).toBe(true);
    // The pane is ONE component that re-renders for whichever row is open.
    pane.update(props({ approval: approval({ request_id: "req-c", action: "billing.refund" }) }));
    expect(verdictLine(pane.container)).toBeNull();
    expect(pane.container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });
});

describe("§4.5 — every refusal surface arrests", () => {
  it("a gate conflict (409) arrests", () => {
    const { container } = renderPane(
      props({ state: { error: { detail: "request already decided", conflict: true } } }),
    );
    const line = container.querySelector(".ne-approvals__conflict");
    expect(line?.textContent).toContain("Already decided");
    expect(arrests(line)).toBe(true);
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });

  it("a write failure arrests", () => {
    const { container } = renderPane(
      props({ state: { error: { detail: "gate unreachable", conflict: false } } }),
    );
    const block = container.querySelector(".ne-state--error");
    expect(block?.textContent).toContain("Couldn’t submit the decision.");
    expect(arrests(block)).toBe(true);
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
  });

  it("a decision taken elsewhere arrests", () => {
    const { container } = renderPane(props({ decidedElsewhere: true }));
    const line = container.querySelector(".ne-approvals__conflict");
    expect(line?.textContent).toContain("Already decided");
    expect(arrests(line)).toBe(true);
  });

  it("CONTROL: an undecided pane animates nothing at all — a screen at rest (cl. 1)", () => {
    const { container } = renderPane(props());
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`)).toHaveLength(0);
    expect(container.querySelectorAll(`.${ARREST_CLASS}`)).toHaveLength(0);
  });

  it("cl. 5 — a decision moves at most two elements", async () => {
    const { container } = decide({ status: "approved" }, { auditSeq: 4812, chainReadable: true });
    await waitFor(() => expect(recordLine(container)).not.toBeNull());
    expect(container.querySelectorAll(`.${SETTLE_CLASS}`).length).toBeLessThanOrEqual(2);
  });
});

describe("cl. 6 — motion is a redundant channel", () => {
  it("stripping every motion class changes nothing a reader can read", async () => {
    for (const status of ["approved", "rejected"] as const) {
      const { container } = decide({ status }, { auditSeq: 4812, chainReadable: true });
      await waitFor(() =>
        expect(container.querySelector("a[href='/audit?seq=4812']")).not.toBeNull(),
      );
      const receipt = container.querySelector(".ne-approvals__receipt");
      expect(receipt).not.toBeNull();
      const before = receipt?.textContent ?? "";
      for (const el of container.querySelectorAll(`.${SETTLE_CLASS}, .${ARREST_CLASS}`)) {
        el.classList.remove(SETTLE_CLASS, CHAINED_CLASS, ARREST_CLASS);
      }
      // The verdict word, the approver, the time and the record link are TEXT.
      // A state distinguishable only by movement fails a screenshot; this one
      // survives the classes being deleted outright.
      expect(receipt?.textContent).toBe(before);
      expect(before).toContain(status === "approved" ? "APPROVED" : "DENIED");
      expect(before).toContain("alice@example.com");
      expect(container.querySelector("a[href='/audit?seq=4812']")).not.toBeNull();
    }
  });
});

/* ------------------------------------------------- the stylesheet as text -- */

const CSS = readFileSync(
  join(fileURLToPath(import.meta.url), "..", "approvals.css"),
  "utf-8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** The body of the first rule whose selector list contains `selector`. */
function ruleBody(selector: string): string {
  const re = /([^{}]*)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    const sel = (m[1] ?? "").trim();
    if (sel.startsWith("@")) continue;
    if (sel.split(",").some((s) => s.trim() === selector)) return m[2] ?? "";
  }
  throw new Error(`no rule for ${selector}`);
}

describe("approvals.css — the grammar as the stylesheet declares it", () => {
  it("the settle keyframes animate ONLY transform and opacity (cl. 3)", () => {
    const frames = /@keyframes\s+ne-approvals-settle\s*\{([\s\S]*?\})\s*\}/.exec(CSS);
    expect(frames, "the settle keyframes are declared").not.toBeNull();
    const body = frames?.[1] ?? "";
    const animated = [...body.matchAll(/([a-z-]+)\s*:/g)].map((m) => m[1]);
    expect(animated.length).toBeGreaterThan(0);
    expect([...new Set(animated)].sort()).toEqual(["opacity", "transform"]);
    // The shape this rules out by name: an animated height, which is what
    // "animate a row in" ships as when nobody is watching.
    expect(body).not.toMatch(/\b(height|width|margin|padding|top|left)\s*:/);
  });

  it("the settle resolves its duration and its curve through the token scale (cl. 4, cl. 5)", () => {
    const body = ruleBody(`.${SETTLE_CLASS}`);
    expect(body).toContain("var(--motion-settle)");
    expect(body).toContain("var(--ease-damped)");
    // A raw ms/s here would be a component-local constant — a parallel motion
    // vocabulary (design §6.12). M0's guardian bans it product-wide; this pins
    // it for the one rule this unit owns.
    expect(body.replace(/var\(--[a-z-]+\)/g, "")).not.toMatch(/\d+\s*m?s\b/);
    expect(body).not.toMatch(/cubic-bezier|ease-in|ease-out|linear|steps/);
    expect(body).not.toMatch(/\binfinite\b/);
  });

  it("the chained modifier delays by exactly one --motion-chain (§4.6)", () => {
    expect(ruleBody(`.${CHAINED_CLASS}`).replace(/\s+/g, " ").trim()).toBe(
      "animation-delay: var(--motion-chain);",
    );
  });

  it("the arrest DECLARES its instantness rather than relying on the absence of a rule", () => {
    const body = ruleBody(`.${ARREST_CLASS}`).replace(/\s+/g, " ");
    expect(body).toContain("animation: none");
    expect(body).toContain("transition: none");
    // Bidirectional: the arrest rule may not name a duration, a curve or a
    // keyframe — any of which would be an easing on a refusal.
    expect(body).not.toContain("ne-approvals-settle");
    expect(body).not.toContain("var(--motion-");
    expect(body).not.toContain("var(--ease-");
  });

  it("no rule gives the arrest class an animation, and none gives the settle class none", () => {
    const re = /([^{}]*)\{([^{}]*)\}/g;
    let m: RegExpExecArray | null;
    let settleRules = 0;
    let arrestRules = 0;
    while ((m = re.exec(CSS)) !== null) {
      const sel = (m[1] ?? "").trim();
      const body = m[2] ?? "";
      if (sel.startsWith("@")) continue;
      const names = sel.split(",").map((s) => s.trim());
      // A selector that carried both classes would be a rule with no grammar.
      expect(names.some((n) => n.includes(SETTLE_CLASS) && n.includes(ARREST_CLASS))).toBe(false);
      if (names.some((n) => n === `.${ARREST_CLASS}`)) {
        arrestRules += 1;
        // Read the VALUE, rather than asserting the text does not match a
        // pattern: `not.toMatch(/animation:\s*(?!none)/)` is satisfied by
        // backtracking over the whitespace and passes on any value at all.
        const anim = /animation(?:-name)?\s*:\s*([^;]+)/.exec(body);
        expect(anim?.[1]?.trim()).toBe("none");
      }
      if (names.some((n) => n === `.${SETTLE_CLASS}`)) settleRules += 1;
    }
    // Non-vacuity: both rules were actually found. The settle exists in exactly
    // two places — the rule that gives it, and the reduced-motion block that
    // takes it away.
    expect(arrestRules).toBe(1);
    expect(settleRules).toBe(2);
  });

  it("cl. 6 — reduced motion removes the settle outright, delay included", () => {
    const block = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?\}\s*)\}/.exec(CSS);
    expect(block, "approvals.css carries its own reduced-motion block").not.toBeNull();
    const body = block?.[1] ?? "";
    expect(body).toContain(`.${SETTLE_CLASS}`);
    // `animation: none` is the shorthand, so it drops the --motion-chain delay
    // too. The global collapse in primitives.css zeroes durations only, which
    // would leave the record line holding its start frame for 60 ms.
    expect(body.replace(/\s+/g, " ")).toContain("animation: none");
  });
});
