// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ROUTE HALF OF THE RENEWAL PAIR — "it put me back on Documents".
//
// ./sessionRenewal.test.tsx pins what a silent renewal must not do to a
// governed SURFACE (no rebuilt client, no refetch, no blanked rows). This file
// pins what it must not do to the ROUTE, which is a different mechanism and
// therefore needs its own assertion: nothing may NAVIGATE because a token
// rotated. On stage, a presenter mid-sentence on the Graph or the audit walk
// being returned to Documents every four minutes is the whole demo.
//
// The pair, again, because either half alone certifies a lie:
//
//  (c) A TOKEN ROTATION must leave the address bar exactly as it was — the
//      pathname AND the screen's own route state (the selected entity, the
//      mode, the depth), because that route state IS the thing the presenter
//      navigated to.
//
//  (d) A REAL IDENTITY CHANGE must still reset the screen's route state — a
//      previous principal's entity selection is that principal's data and may
//      not survive into another's session — and must do it WITHOUT sending the
//      user to a different screen. This half is why the fix for (c) can never
//      be "suppress programmatic navigation": that would keep one principal's
//      selection on another principal's screen, and it would break the
//      legitimate auto-selection that Documents and Graph both depend on.
//
// The real react-oidc-context state machine and the real router run here; the
// fake is at the oidc-client-ts UserManager boundary (../test/fakeUserManager),
// so these take the same USER_LOADED path an idle production renewal takes.

import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { AuthProvider as OidcAuthProvider } from "react-oidc-context";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { dismissTour } from "../demo/tourStorage";
import { emitUserLoaded, fakeUser, fakeUserManager } from "../test/fakeUserManager";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";

function renderAppAt(path: string, manager: ReturnType<typeof fakeUserManager>) {
  window.history.pushState({}, "", path);
  return render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <OidcAuthProvider userManager={manager as any}>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </OidcAuthProvider>,
  );
}

/** The address bar, as the presenter reads it. */
function url(): string {
  return `${window.location.pathname}${window.location.search}`;
}

/** Let every effect a USER_LOADED could have scheduled — the client rebuild,
 * the ownership generation, the layout effects that navigate — actually run.
 * Asserting "nothing happened" the same tick would pass on a broken build. */
async function settle(): Promise<void> {
  await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument());
  await new Promise((resolve) => setTimeout(resolve, 50));
}

const GRAPH_ROUTE = "/knowledge/graph?entity=acct-bluepeak&mode=record&depth=2&keep=one";

beforeEach(() => {
  dismissTour();
  server.use(
    http.get("*/api/v1/graph/entities", () =>
      HttpResponse.json({ entities: [], next_cursor: null }),
    ),
  );
});

afterEach(() => {
  window.history.pushState({}, "", "/");
});

describe("a silent token renewal vs. the route the user is on", () => {
  it("(c) a rotated token leaves the Graph route — path AND route state — untouched", async () => {
    const manager = fakeUserManager(fakeUser({ sub: "alice", access_token: "token-1" }));
    renderAppAt(GRAPH_ROUTE, manager);

    expect(await screen.findByRole("heading", { name: "Graph", level: 1 })).toBeInTheDocument();
    await settle();
    expect(url()).toBe(GRAPH_ROUTE);

    // THE SILENT RENEW: same principal, same SSO session, a rotated bearer.
    emitUserLoaded(manager, fakeUser({ sub: "alice", access_token: "token-2-rotated" }));
    await settle();

    // The presenter is still where they were, still looking at what they opened.
    expect(url()).toBe(GRAPH_ROUTE);
    expect(screen.getByRole("heading", { name: "Graph", level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Documents", level: 1 })).not.toBeInTheDocument();
  });

  it("(c2) a rotated token leaves the Audit log route untouched", async () => {
    const manager = fakeUserManager(fakeUser({ sub: "alice", access_token: "token-1" }));
    renderAppAt("/audit", manager);

    expect(await screen.findByRole("heading", { name: "Audit log", level: 1 })).toBeInTheDocument();
    await settle();

    emitUserLoaded(manager, fakeUser({ sub: "alice", access_token: "token-2-rotated" }));
    await settle();

    expect(url()).toBe("/audit");
    expect(screen.getByRole("heading", { name: "Audit log", level: 1 })).toBeInTheDocument();
  });

  it("(d) a real identity change resets the screen's route state but does NOT change screen", async () => {
    const manager = fakeUserManager(fakeUser({ sub: "alice", access_token: "alice-token" }));
    renderAppAt(GRAPH_ROUTE, manager);

    expect(await screen.findByRole("heading", { name: "Graph", level: 1 })).toBeInTheDocument();
    await settle();
    expect(url()).toBe(GRAPH_ROUTE);

    // A different subject — never a renewal, and never mistakable for one.
    emitUserLoaded(
      manager,
      fakeUser({ sub: "mallory", access_token: "mallory-token", session_state: "session-2" }),
    );
    await settle();

    // Alice's entity selection is gone: the graph-owned route state is scrubbed.
    await waitFor(() => {
      const params = new URLSearchParams(window.location.search);
      expect(params.get("entity")).toBeNull();
      expect(params.get("mode")).toBeNull();
      expect(params.get("depth")).toBeNull();
    });
    // Everything that is not the graph's own state survives, and — the point of
    // this half — the user is still on Graph. Invalidation is not navigation.
    expect(window.location.pathname).toBe("/knowledge/graph");
    expect(new URLSearchParams(window.location.search).get("keep")).toBe("one");
    expect(screen.getByRole("heading", { name: "Graph", level: 1 })).toBeInTheDocument();
  });
});
