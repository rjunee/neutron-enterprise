// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ROUND-3 BLOCKER, ROOT CAUSE. Three reviewers reported the same thing: on
// /approvals, RUN THE GOVERNED DELEGATION rendered as an ENABLED, full-weight
// primary button that answered 403 only AFTER the click — while /trace pre-
// disabled the identical control with the reason stated first.
//
// Round 2 had already written the correct treatment for /approvals. It did not
// take effect because of WHICH ANSWER IT ASKED: it gated on `entitlements` from
// GET /v1/me, and the deployment the console is demoed against returns
// `{id, groups}` with no `entitlements` field at all. "Unknown is not denied" —
// correctly — left the button live. /trace gated on an OBSERVED 403 from the
// shared audit poll, which is why /trace was right and /approvals was wrong.
//
// The console had two sources of truth for one question and shipped the one that
// does not work on the deployment. These cases hold the merge.

import { describe, expect, it } from "vitest";
import type { Access, Surface } from "./entitlements";
import { mergeAccess } from "./useSurfaceAccess";

const silent = () => "unknown" as Access;

describe("mergeAccess — the advisory answer, plus the observed one", () => {
  it("uses the OBSERVED refusal when the deployment sends no entitlements", () => {
    // The live deployment: /v1/me is {id, groups}. Without the observed answer
    // every gated control on every screen renders as if it were available.
    const access = mergeAccess(silent, { ops: "refused" });
    expect(access.refused("ops")).toBe(true);
  });

  it("still says UNKNOWN for a surface nothing has answered for", () => {
    // Unknown is not denied: the console asks the server and renders the refusal.
    // Guessing "refused" here would lock a key out of a surface it holds.
    const access = mergeAccess(silent, { ops: "refused" });
    expect(access.refused("steward")).toBe(false);
    expect(access.access("steward")).toBe("unknown");
  });

  it("prefers the ADVISORY answer, which can speak before any request is made", () => {
    const advisory = (s: Surface): Access => (s === "approvals" ? "granted" : "unknown");
    // The observed column has not seen a 403 for approvals; the advisory has
    // positively granted it.
    const access = mergeAccess(advisory, {});
    expect(access.refused("approvals")).toBe(false);
    expect(access.access("approvals")).toBe("granted");
  });

  it("does not let a stale observation override an explicit server grant", () => {
    const advisory = (s: Surface): Access => (s === "ops" ? "granted" : "unknown");
    const access = mergeAccess(advisory, { ops: "refused" });
    expect(access.access("ops")).toBe("granted");
    expect(access.refused("ops")).toBe(false);
  });

  it("THE CONTROL: a fully silent pair refuses nothing", () => {
    // Without this, every assertion above is satisfiable by returning "refused"
    // for everything — which would lock every key out of every gated surface and
    // would look, on screen, exactly like the product being broken.
    const access = mergeAccess(silent, {});
    for (const s of ["approvals", "steward", "ops"] as const) {
      expect(access.refused(s), s).toBe(false);
      expect(access.access(s), s).toBe("unknown");
    }
  });
});
