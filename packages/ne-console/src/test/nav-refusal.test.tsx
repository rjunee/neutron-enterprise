// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE C7 GUARD (NE-CONSOLE-IA) — nav filtering is a LEGIBILITY affordance, not
// authorization. A principal whose entitlements do not cover "approvals" who
// nonetheless reaches /approvals (a hidden route, hand-typed or deep-linked —
// exactly what a stale bookmark or a shared link does) must get the SERVER's
// own refusal, rendered by the page itself from its own governed read — never
// a client-side redirect, never a client-side "you can't be here" interstitial
// that never asked the server anything.
//
// Both halves matter, together, in one file: (1) the rail filtered the link
// (proving the entitlement was consulted at all) and (2) the route body still
// mounted, issued GET /v1/approvals/pending, and rendered the 403 the server
// actually returned (proving the filter decided NOTHING about authorization).

import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { AuthStub } from "./authStub";
import { PRINCIPAL_OPS_STEWARD, RUNTIME_CONFIG } from "./mocks/fixtures";
import { server } from "./mocks/server";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

describe("nav filtering is legibility, not authorization (C7)", () => {
  it("hides the Approvals link AND still renders the server's own 403 refusal on /approvals", async () => {
    // PRINCIPAL_OPS_STEWARD holds "ops"/"steward" — NOT "approvals". The
    // governed read itself also 403s, exactly as the real gate would for this
    // key: the fixture does not merely omit the entitlement, it makes the
    // server refuse the actual request too.
    server.use(
      http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_OPS_STEWARD)),
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({ detail: "not an approver" }, { status: 403 }),
      ),
    );

    const { container } = renderAt("/approvals");

    // (1) FILTERING HAPPENED. While GET /v1/me is in flight the Approvals slot
    // is a skeleton, never a link (pending is not permitted — the flash fix),
    // so the link is absent from the FIRST paint; once the slots settle, its
    // absence is the server's verdict rather than the loading state.
    // BY HREF, not by accessible name. The lock affordance rewrites a
    // still-visible link's aria-label to "Approvals — needs an approver key…",
    // so queryByRole({name:"Approvals"}) returns null whether or not filtering
    // ran — this assertion passed with visibleSections disabled entirely.
    expect(container.querySelector('.ne-rail__nav a[href="/approvals"]')).toBeNull();
    await waitFor(() => expect(container.querySelector(".ne-rail__slotpending")).toBeNull());
    expect(container.querySelector('.ne-rail__nav a[href="/approvals"]')).toBeNull();

    // (2) IT IS NOT AUTHORIZATION. The route still mounted (no redirect: the
    // page's own "Approvals" heading is on screen, and the URL never moved)
    // and rendered the SERVER's refusal — the one standard refused state
    // (§2.4) — not a client-side "you can't be here" the console invented
    // itself. The copy is the redesign's; the claim under test is unchanged.
    expect(window.location.pathname).toBe("/approvals");
    expect(await screen.findByRole("heading", { name: "Approvals" })).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Your role doesn’t include approvals. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    // A role boundary is drawn NEUTRAL, never as a fault (§2.3).
    expect(screen.getByText("GATED")).toBeInTheDocument();

    // No parallel, console-authored denial exists anywhere on the page — the
    // ONLY refusal copy on screen is the standard state rendered from the
    // server's own 403.
    expect(screen.queryByText(/access denied/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/you (do not|don't) have permission/i)).not.toBeInTheDocument();
  });
});
