// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The two AMBIENT motion effects — the §4.3 rail transit pulse and the §4.4
// Approvals badge tick (docs/design/ne-console-motion-design.md). These are the
// two effects a person sees without clicking anything, and both are one-shots
// whose entire contract is WHEN they fire:
//
//   pulse — a governed event mapped to that route landed in the shared poll's
//           CURRENT batch; never on first load; gone again at the next quiet
//           poll (law 4 cl. 1–2).
//   tick  — the pending count ROSE between two known polled readings; never on
//           first load, a decrease, or an unchanged count (cl. 2).
//
// Every guard here is bidirectional per docs/testing/vacuous-guards.md: the
// RED direction names its offender in a comment, and the GREEN control is a
// case a naive over-broad version (e.g. "the rail never renders a dot", "the
// badge never animates") would have flagged. All state-dependent assertions on
// `transits` live inside ONE waitFor — it is a single-poll pulse and reading
// it across statements races the next tick (useAuditPoll.test.tsx documents
// the CI flake that rule comes from).
//
// jsdom runs no animations, so these tests prove STRUCTURE (which elements
// exist when, which properties the keyframes declare, which tokens time them)
// — never a frame rate.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuditPollProvider, TRANSIT_ROUTES } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { PostureProvider } from "../posture/PostureContext";
import { AuthStub } from "../test/authStub";
import {
  AUDIT_EVENTS,
  PENDING_APPROVALS,
  RUNTIME_CONFIG,
  VERIFICATION_OK,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { NavRail } from "./NavRail";
import { flattenSections, NAV_FOOT, NAV_SECTIONS } from "./navSections";

// THE ORDER MIRRORS App.tsx's AuthedLayout (and NavRail.test.tsx): a harness
// nested differently from production is a test-local composition that proves
// nothing about the shell.
function renderRail({ auditMs = 100000, pendingMs = 100000 } = {}) {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={auditMs}>
              <PendingApprovalsProvider intervalMs={pendingMs}>
                <PostureProvider>
                  <NavRail />
                </PostureProvider>
              </PendingApprovalsProvider>
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

/** A fresh audit event above the fixture chain's seq 3. */
function auditEvent(seq: number, event_type: string) {
  return {
    ...AUDIT_EVENTS[0]!,
    id: seq,
    seq,
    event_id: `evt-${seq}`,
    event_type,
    action: null,
  };
}

/** n pending rows, none overdue, distinct ids — so the badge text is exactly
 * the count and the overdue tint never muddies a class assertion. */
function pendingRows(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    ...PENDING_APPROVALS[0]!,
    request_id: `req-tick-${i}`,
    sla_deadline: new Date(Date.now() + 60 * 60_000).toISOString(),
  }));
}

describe("the rail transit pulse (§4.3)", () => {
  it("renders NO dot on first load — history that merely already existed is not activity (cl. 1)", async () => {
    // RED partner (mutation-proven): a consumer that renders the dot from
    // `events`/`latestSeq` — i.e. from ANY non-empty audit history — instead
    // of from the single-batch `transits` shows a dot here after the first
    // poll and fails. The GREEN control for the over-broad inverse ("the rail
    // never renders a dot") is the fresh-event test below.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
      }),
    );
    const { container } = renderRail({ auditMs: 150 });
    // ≥3 requests issued means ≥2 full polls landed state; the chain held
    // seq 1–3 the whole time, so nothing was ever fresh.
    await waitFor(() => expect(polls).toBeGreaterThanOrEqual(3), { timeout: 5000 });
    expect(container.querySelector(".ne-rail__transit")).toBeNull();
  });

  it("pulses the mapped nav item when a governed event lands, then clears at the next quiet poll", async () => {
    // RED partners (mutation-proven): (a) drop the fresh-batch condition and
    // the dot never appears on /sources specifically or appears everywhere —
    // the closest('a') href pins WHERE the pulse landed; (b) make the poll
    // KEEP `transits` across quiet polls (persistence reintroduced in
    // useAuditPoll's setState) and the final assertion fails — the dot must
    // leave the DOM, because a rail at rest holds zero animation elements.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        return HttpResponse.json({
          // Poll 1: the baseline chain. Poll 2+: one NEW connector event
          // (seq 4 → /sources). Poll 3 sees nothing newer than 4 — quiet.
          events: polls === 1 ? AUDIT_EVENTS : [auditEvent(4, "connector.sync"), ...AUDIT_EVENTS],
          verification: VERIFICATION_OK,
        });
      }),
    );
    const { container } = renderRail({ auditMs: 150 });
    // Single waitFor for the whole pulse-visible state: `transits` is a
    // single-poll pulse and asserting across statements races the next tick.
    await waitFor(
      () => {
        const dot = container.querySelector(".ne-rail__transit");
        expect(dot).not.toBeNull();
        expect(dot!.closest("a")).toHaveAttribute("href", "/sources");
        // Redundant channel (cl. 6): no text, hidden from AT — the fact lives
        // in the audit ledger, not in this dot.
        expect(dot).toHaveAttribute("aria-hidden", "true");
        expect(dot!.textContent).toBe("");
      },
      { timeout: 5000 },
    );
    // The next poll surfaces nothing newer; the batch resets to [] and the
    // long-finished dot unmounts (cl. 1 — a screen at rest runs zero
    // animations, and here holds zero animation nodes at all).
    await waitFor(() => expect(container.querySelector(".ne-rail__transit")).toBeNull(), {
      timeout: 5000,
    });
  });

  it("renders one dot per mapped ROUTE for a multi-event batch — deduped, never per event", async () => {
    // Two same-route events + one other-route event → exactly two dots. RED
    // partner: rendering from per-event `transitEvents` (three dots) or
    // pulsing a single aggregate dot (one). This is also the GREEN control
    // proving the ceiling is a dedup, not a "at most one dot ever" clamp.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        return HttpResponse.json({
          events:
            polls === 1
              ? AUDIT_EVENTS
              : [
                  auditEvent(6, "eval.run"),
                  auditEvent(5, "connector.resync"),
                  auditEvent(4, "connector.sync"),
                  ...AUDIT_EVENTS,
                ],
          verification: VERIFICATION_OK,
        });
      }),
    );
    const { container } = renderRail({ auditMs: 150 });
    await waitFor(
      () => {
        const hrefs = [...container.querySelectorAll(".ne-rail__transit")]
          .map((d) => d.closest("a")?.getAttribute("href"))
          .sort();
        expect(hrefs).toEqual(["/evaluations", "/sources"]);
      },
      { timeout: 5000 },
    );
  });

  it("REPULSES when consecutive polls each land fresh events on one route — keyed by batch, not by route", async () => {
    // jsdom runs no animations, so the observable for "the one-shot restarts"
    // is element identity: the dot is keyed by the batch's latestSeq, and a
    // new fresh batch must REMOUNT it (a CSS animation on a surviving node
    // never replays). RED partner (mutation-proven): key the dot by its route
    // — the node survives across batches and the second waitFor times out.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        // Every poll surfaces one NEW connector event: seq 4, 5, 6, …
        return HttpResponse.json({
          events: [auditEvent(3 + polls, "connector.sync"), ...AUDIT_EVENTS],
          verification: VERIFICATION_OK,
        });
      }),
    );
    const { container } = renderRail({ auditMs: 150 });
    let first: Element | null = null;
    await waitFor(
      () => {
        const dot = container.querySelector(".ne-rail__transit");
        expect(dot).not.toBeNull();
        first = dot;
      },
      { timeout: 5000 },
    );
    await waitFor(
      () => {
        const dot = container.querySelector(".ne-rail__transit");
        expect(dot).not.toBeNull();
        expect(dot!.closest("a")).toHaveAttribute("href", "/sources");
        expect(dot).not.toBe(first);
      },
      { timeout: 5000 },
    );
  });

  it("a transit that maps to a PENDING slot renders NO dot — an unknown permission has no activity to depict", async () => {
    // While GET /v1/me is in flight, the shared poll has no authenticated
    // principal owner. It must not issue a ledger read and therefore cannot
    // depict activity for either an open or gated route. Other tests in this
    // file are the ready-owner controls that prove pulses still render.
    server.use(http.get("*/api/v1/me", () => new Promise<never>(() => {})));
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
      }),
    );
    const { container } = renderRail({ auditMs: 15 });
    await waitFor(() =>
      expect(container.querySelectorAll(".ne-rail__slotpending").length).toBeGreaterThan(0),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(polls).toBe(0);
    expect(container.querySelector(".ne-rail__transit")).toBeNull();
  });

  it("maps every possible transit route to a real live rail destination (cl. 5)", () => {
    // The defect this pins against actually shipped: routeForAuditEvent named
    // the PRE-RENAME routes (/connect, /chat, /flywheel…), which no nav item
    // carries — a pulse that could never render. RED partner: re-point any
    // ROUTE_RULES entry at a non-destination (or grow the codomain past the
    // rail). Control: the codomain as written passes, so the pin is not
    // "TRANSIT_ROUTES must be empty" in disguise.
    const destinations = [...flattenSections(NAV_SECTIONS).map((i) => i.to), NAV_FOOT.to];
    for (const route of TRANSIT_ROUTES) {
      expect(destinations, `${route} is a rail destination`).toContain(route);
    }
    expect(TRANSIT_ROUTES.length).toBeGreaterThan(0);
    expect(new Set(TRANSIT_ROUTES).size).toBe(TRANSIT_ROUTES.length);
  });
});

describe("the Approvals badge tick (§4.4)", () => {
  it("ticks when the pending count RISES between polls — and not on the first reading", async () => {
    // The GREEN control of the pair: an over-broad "the badge never animates"
    // guard would flag this. RED partner (mutation-proven): initialize the
    // comparison so the unknown→first-reading transition counts as a rise
    // (e.g. `seen.count ?? 0`) and the mid-test no-tick assertion fails.
    let polls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        polls += 1;
        return HttpResponse.json({ approvals: pendingRows(polls === 1 ? 1 : 2) });
      }),
    );
    const { container } = renderRail({ pendingMs: 150 });
    await waitFor(() => {
      const badge = container.querySelector(".ne-rail__badge");
      expect(badge?.textContent).toBe("1");
      // First load never ticks: this count was not observed to rise, it was
      // merely already there (cl. 1).
      expect(badge?.className).not.toContain("ne-rail__badge--tick");
    });
    await waitFor(
      () => {
        const badge = container.querySelector(".ne-rail__badge");
        expect(badge?.textContent).toBe("2");
        expect(badge?.className).toContain("ne-rail__badge--tick");
      },
      { timeout: 5000 },
    );
  });

  it("does NOT tick on a decrease (cl. 2 — the tick depicts new work arriving)", async () => {
    // RED partner (mutation-proven): weaken the rise test to ANY change
    // (`count !== seen.count`) and the 2→1 reading below carries the tick
    // class. The increase test above is this guard's control.
    let polls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        polls += 1;
        return HttpResponse.json({ approvals: pendingRows(polls === 1 ? 2 : 1) });
      }),
    );
    const { container } = renderRail({ pendingMs: 150 });
    await waitFor(() => expect(container.querySelector(".ne-rail__badge")?.textContent).toBe("1"), {
      timeout: 5000,
    });
    expect(container.querySelector(".ne-rail__badge")?.className).not.toContain(
      "ne-rail__badge--tick",
    );
  });

  it("does NOT tick on an unchanged count — a re-poll and a re-render are not events", async () => {
    // RED partners: tick-on-any-poll, tick-on-any-render. After ≥3 polls of
    // the same queue the badge must still carry no tick class.
    let polls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        polls += 1;
        return HttpResponse.json({ approvals: pendingRows(2) });
      }),
    );
    const { container } = renderRail({ pendingMs: 150 });
    await waitFor(() => expect(polls).toBeGreaterThanOrEqual(3), { timeout: 5000 });
    const badge = container.querySelector(".ne-rail__badge");
    expect(badge?.textContent).toBe("2");
    expect(badge?.className).not.toContain("ne-rail__badge--tick");
  });

  it("re-ticks on EACH rise — the epoch keys a remount, because a surviving node never replays", async () => {
    // Same identity argument as the pulse's repulse test. RED partner
    // (mutation-proven): drop the `key={tick}` — the class alone stays on the
    // same node, the second rise re-styles nothing, and the identity
    // assertion fails.
    let polls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        polls += 1;
        return HttpResponse.json({ approvals: pendingRows(Math.min(polls, 3)) });
      }),
    );
    const { container } = renderRail({ pendingMs: 150 });
    let atTwo: Element | null = null;
    await waitFor(
      () => {
        const badge = container.querySelector(".ne-rail__badge--tick");
        expect(badge?.textContent).toBe("2");
        atTwo = badge;
      },
      { timeout: 5000 },
    );
    await waitFor(
      () => {
        const badge = container.querySelector(".ne-rail__badge--tick");
        expect(badge?.textContent).toBe("3");
        expect(badge).not.toBe(atTwo);
      },
      { timeout: 5000 },
    );
  });

  it("an EMPTY queue that receives work ticks on appearance — 0 → n is a real increase", async () => {
    // Control against over-correcting the first-load rule into "never tick on
    // the badge's first appearance": 0 is a KNOWN reading, so work arriving
    // into a watched-empty queue is exactly the moment the tick exists for.
    let polls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        polls += 1;
        return HttpResponse.json({ approvals: pendingRows(polls === 1 ? 0 : 2) });
      }),
    );
    const { container } = renderRail({ pendingMs: 150 });
    await waitFor(
      () => {
        const badge = container.querySelector(".ne-rail__badge");
        expect(badge?.textContent).toBe("2");
        expect(badge?.className).toContain("ne-rail__badge--tick");
      },
      { timeout: 5000 },
    );
  });
});

/* ------------------------------------------------------------------------- *
 * Compositor grammar pins (law 4 cl. 3) — the dot animates opacity ONLY, the
 * tick transform ONLY. The motion guardian cannot hold this: its property
 * rules cover transitions and LOOPING keyframes, and a one-shot paint
 * keyframe is lawful in general (the paint one-shot clause) — just not for
 * THESE two effects, whose spec entries name their single property. So the
 * pin lives here, scoped by keyframes name, with its own bite/spare
 * self-test.
 * ------------------------------------------------------------------------- */

// The repo idiom for reading a stylesheet under jsdom (styles/tokens.test.ts).
const SHELL_CSS = readFileSync(resolve(__dirname, "..", "styles", "shell.css"), "utf-8");

/** The body of `@keyframes name { … }`, brace-matched (steps nest braces). */
function keyframesBody(css: string, name: string): string {
  const m = new RegExp(String.raw`@keyframes\s+${name}\s*\{`).exec(css);
  if (!m) return "";
  let depth = 1;
  let j = m.index + m[0].length;
  const start = j;
  while (j < css.length && depth > 0) {
    const ch = css[j];
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    j += 1;
  }
  return css.slice(start, Math.max(j - 1, start));
}

/** Every property declared in a css block, keyframe-step selectors skipped —
 * the same declaration shape the motion guardian parses. */
function declaredProps(body: string): string[] {
  const out: string[] = [];
  for (const chunk of body.split(/[;{}]/)) {
    const i = chunk.indexOf(":");
    if (i < 0) continue;
    const prop = chunk.slice(0, i).trim().toLowerCase();
    if (!/^-{0,2}[a-z][a-z0-9-]*$/.test(prop)) continue;
    out.push(prop);
  }
  return out;
}

/** The body of the first plain rule for `selector`. */
function ruleBody(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`);
  if (at < 0) return "";
  const start = css.indexOf("{", at) + 1;
  return css.slice(start, css.indexOf("}", start));
}

describe("compositor grammar and token timing (law 4 cl. 3 + 5)", () => {
  it("the transit dot's keyframes animate opacity and NOTHING else", () => {
    // RED partner (mutation-proven): add any paint or layout property to a
    // ne-rail-transit step (background-color is the natural offender — a
    // cherenkov "glow" is the demolished shimmer under a new name).
    expect(new Set(declaredProps(keyframesBody(SHELL_CSS, "ne-rail-transit")))).toEqual(
      new Set(["opacity"]),
    );
  });

  it("the badge tick's keyframes animate transform (scale) and NOTHING else", () => {
    const body = keyframesBody(SHELL_CSS, "ne-rail-badge-tick");
    expect(new Set(declaredProps(body))).toEqual(new Set(["transform"]));
    // §4.4's exact grammar: scale(1→1.06→1), nothing translated or rotated.
    expect(body).toContain("scale(1.06)");
    expect(body).not.toContain("translate");
  });

  it("both one-shots resolve through the token scale at their spec durations, with the one curve", () => {
    // §4.3: --motion-pulse. §4.4: --motion-snap. A raw ms or a second curve
    // is also guardian-RED (rules 7/8); this pins the SPECIFIC tokens, which
    // the guardian deliberately does not.
    expect(ruleBody(SHELL_CSS, ".ne-rail__transit")).toMatch(
      /animation:\s*ne-rail-transit var\(--motion-pulse\) var\(--ease-damped\)\s*;/,
    );
    expect(ruleBody(SHELL_CSS, ".ne-rail__badge--tick")).toMatch(
      /animation:\s*ne-rail-badge-tick var\(--motion-snap\) var\(--ease-damped\)\s*;/,
    );
    // No fill mode and an invisible base: outside its one-shot the dot is
    // opacity 0 with no live animation, and it participates in no layout.
    const dot = ruleBody(SHELL_CSS, ".ne-rail__transit");
    expect(dot).toMatch(/opacity:\s*0\s*;/);
    expect(dot).toMatch(/position:\s*absolute\s*;/);
    expect(dot).not.toContain("forwards");
    expect(dot).not.toContain("infinite");
    // Reduced motion needs no per-effect handling: the global universal
    // collapse in styles/primitives.css (guardian rule 10 pins its structure)
    // makes both instant, and each fact keeps its static co-signal — the
    // ledger row and the badge's numeral.
  });

  it("self-test: the keyframes scan bites a planted paint property and spares the lawful pair", () => {
    // Bite: the natural offender, planted.
    const planted =
      "@keyframes ne-rail-transit { 0% { opacity: 0; background-color: red; } 100% { opacity: 0; } }";
    expect(declaredProps(keyframesBody(planted, "ne-rail-transit"))).toContain("background-color");
    // Spare (the control an over-broad "keyframes may only ever animate one
    // property" version would flag): the settle grammar lawfully pairs
    // opacity with transform — out of these pins' scope by NAME, not by luck.
    const settle =
      "@keyframes ne-x { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }";
    expect(new Set(declaredProps(keyframesBody(settle, "ne-x")))).toEqual(
      new Set(["opacity", "transform"]),
    );
    // And an absent keyframes name yields no props — a renamed effect cannot
    // pass its pin vacuously…
    expect(declaredProps(keyframesBody(SHELL_CSS, "ne-rail-renamed"))).toEqual([]);
  });

  it("non-vacuity: the pinned keyframes and rules actually exist in shell.css", () => {
    // …because THIS pin fails first. An empty-body pass on the property-set
    // assertions above would equal new Set([]) ≠ the expected sets, but the
    // rule-body regexes could in principle be dodged by deletion; this makes
    // deletion RED by name.
    expect(keyframesBody(SHELL_CSS, "ne-rail-transit")).not.toBe("");
    expect(keyframesBody(SHELL_CSS, "ne-rail-badge-tick")).not.toBe("");
    expect(ruleBody(SHELL_CSS, ".ne-rail__transit")).not.toBe("");
    expect(ruleBody(SHELL_CSS, ".ne-rail__badge--tick")).not.toBe("");
  });
});
