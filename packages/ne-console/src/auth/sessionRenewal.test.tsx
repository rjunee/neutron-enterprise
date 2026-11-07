// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE PAIR. Both halves are required; either one alone certifies a lie.
//
//  (a) A SILENT TOKEN RENEWAL must not disturb a governed surface. Measured on
//      the live pilot deployment: Keycloak's default 300s access-token lifespan,
//      oidc-client-ts renewing 60s early — a new auth object every 240s, which
//      unmounted the loaded Documents list 8ms later and refilled it a second
//      after that, on an idle screen.
//
//  (b) A REAL IDENTITY CHANGE must still invalidate everything on screen. The
//      console's ownership boundary is ApiClient identity: PrincipalContext's
//      generation, useGovernedRead's ownershipKey and DocumentsWorkspace's fetch
//      context all key on it so that no principal's rows can survive into
//      another principal's session. Half (a) is a comfort; half (b) is the
//      security property, and (a) must never be bought with (b).
//
// The real react-oidc-context state machine runs here — the fake is at the
// oidc-client-ts UserManager boundary, as in RequireAuth.test.tsx — so these
// exercise the actual USER_LOADED path a silent renew takes in production.

import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { act } from "react";
import { AuthProvider as OidcAuthProvider } from "react-oidc-context";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emitUserLoaded, fakeUser as user, fakeUserManager } from "../test/fakeUserManager";
import type { FakeUser } from "../test/fakeUserManager";
import { server } from "../test/mocks/server";
import { PrincipalProvider, usePrincipalOwner } from "./PrincipalContext";
import { useApiClient } from "./useApiClient";

/** A governed surface, rendered exactly the way the shell renders one: rows are
 * shown only while the ownership generation that fetched them still owns the
 * screen. `generation` is surfaced so a test can prove invalidation directly. */
function GovernedSurface() {
  const owner = usePrincipalOwner();
  return (
    <div>
      <div data-testid="generation">{owner.generation}</div>
      <div data-testid="principal">
        {owner.principal.status === "ok" && owner.principal.principal !== null
          ? `ROWS OF ${owner.principal.principal.id}`
          : `NO ROWS (${owner.principal.status})`}
      </div>
    </div>
  );
}

function ClientProbe({ onClient }: { onClient: (client: object) => void }) {
  onClient(useApiClient());
  return null;
}

function renderSurface(manager: ReturnType<typeof fakeUserManager>) {
  const clients: object[] = [];
  render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <OidcAuthProvider userManager={manager as any}>
      <ClientProbe onClient={(client) => clients.push(client)} />
      <PrincipalProvider>
        <GovernedSurface />
      </PrincipalProvider>
    </OidcAuthProvider>,
  );
  return clients;
}

/** Serve /v1/me as whoever the presented bearer token says, and record every
 * request — so a test can count refetches AND read the token actually sent. */
function serveMe() {
  const seen: { authorization: string | null }[] = [];
  server.use(
    http.get("*/api/v1/me", ({ request }) => {
      const authorization = request.headers.get("authorization");
      seen.push({ authorization });
      const identity = authorization === null ? "anonymous" : authorization.split(" ")[1];
      return HttpResponse.json({ id: `principal-for-${identity}`, groups: [] });
    }),
  );
  return seen;
}

afterEach(() => vi.restoreAllMocks());

describe("silent token renewal vs. a real identity change", () => {
  it("(a) a rotated access token neither rebuilds the client nor refetches or blanks the surface", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "token-1" }));
    const seen = serveMe();
    const clients = renderSurface(manager);

    await screen.findByText("ROWS OF principal-for-token-1");
    const settledGeneration = screen.getByTestId("generation").textContent;
    const settledClient = clients[clients.length - 1];
    // The shell issues one anonymous /v1/me before getUser resolves and one as
    // the signed-in principal; what matters is that NOTHING follows the renew.
    const settledReads = seen.length;

    // THE SILENT RENEW: same principal, same SSO session, a rotated bearer.
    emitUserLoaded(manager, user({ sub: "alice", access_token: "token-2-rotated" }));

    // Nothing moved. No new client, no new generation, no second /v1/me, and —
    // the defect the owner actually sees — the rows never left the screen.
    expect(clients[clients.length - 1]).toBe(settledClient);
    expect(screen.getByTestId("generation").textContent).toBe(settledGeneration);
    expect(screen.getByText("ROWS OF principal-for-token-1")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(seen).toHaveLength(settledReads);
    expect(screen.getByText("ROWS OF principal-for-token-1")).toBeInTheDocument();
  });

  it("(a2) the retained client sends the ROTATED token, not the one it was built with", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "token-1" }));
    const seen = serveMe();
    const clients = renderSurface(manager);
    await screen.findByText("ROWS OF principal-for-token-1");
    const settledClient = clients[clients.length - 1] as { me: () => Promise<unknown> };

    emitUserLoaded(manager, user({ sub: "alice", access_token: "token-2-rotated" }));
    expect(clients[clients.length - 1]).toBe(settledClient);

    // Retaining the client is only safe because the bearer is read at call
    // time. A client that kept its build-time token would authenticate with an
    // expiring credential — the renewal would be pointless and the session
    // would die at the 5-minute mark.
    await act(async () => {
      await settledClient.me();
    });
    expect(seen[seen.length - 1]?.authorization).toBe("Bearer token-2-rotated");
  });

  it("(b) a DIFFERENT principal rebuilds the client, invalidates the generation, and removes the prior principal's rows", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "alice-token" }));
    const seen = serveMe();
    const clients = renderSurface(manager);

    await screen.findByText("ROWS OF principal-for-alice-token");
    const aliceGeneration = screen.getByTestId("generation").textContent;
    const aliceClient = clients[clients.length - 1];
    const settledReads = seen.length;

    // A different subject — the case that must never be mistaken for a renew.
    emitUserLoaded(
      manager,
      user({ sub: "mallory", access_token: "mallory-token", session_state: "session-2" }),
    );

    // Alice's rows are gone SYNCHRONOUSLY, before the new /v1/me settles. This
    // assertion is the security property: no window exists in which Mallory's
    // session shows Alice's rows.
    expect(screen.getByTestId("principal").textContent).toBe("NO ROWS (loading)");
    expect(clients[clients.length - 1]).not.toBe(aliceClient);
    expect(screen.getByTestId("generation").textContent).not.toBe(aliceGeneration);

    await screen.findByText("ROWS OF principal-for-mallory-token");
    await waitFor(() => expect(seen.length).toBe(settledReads + 1));
    expect(seen[seen.length - 1]?.authorization).toBe("Bearer mallory-token");
    expect(screen.queryByText("ROWS OF principal-for-alice-token")).not.toBeInTheDocument();
  });

  it("(b2) the SAME principal signing out and back in rebuilds the client and invalidates the surface", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "alice-token" }));
    serveMe();
    const clients = renderSurface(manager);

    await screen.findByText("ROWS OF principal-for-alice-token");
    const firstSession = clients[clients.length - 1];
    const firstGeneration = screen.getByTestId("generation").textContent;

    // Same subject, NEW SSO session: sign-out/in, not a refresh. The token
    // rotated exactly as a renew rotates it — only the session discriminator
    // separates the two cases, which is why it is in the identity key.
    emitUserLoaded(manager, {
      access_token: "alice-token-2",
      session_state: "session-2",
      expired: false,
      profile: { sub: "alice", sid: "session-2" },
    });

    expect(clients[clients.length - 1]).not.toBe(firstSession);
    expect(screen.getByTestId("principal").textContent).toBe("NO ROWS (loading)");
    expect(screen.getByTestId("generation").textContent).not.toBe(firstGeneration);
  });

  it("(b3) FAIL SAFE: an unidentifiable session is never treated as the same identity", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "alice-token" }));
    serveMe();
    const clients = renderSurface(manager);
    await screen.findByText("ROWS OF principal-for-alice-token");
    const identified = clients[clients.length - 1];

    // No session discriminator at all: a subject alone cannot distinguish a
    // refresh from a fresh sign-in, so this must fall back to invalidating —
    // a redundant refetch, never a retained row.
    const unidentifiable = {
      access_token: "mystery-token",
      session_state: null,
      expired: false,
      profile: { sub: "alice" },
    };
    emitUserLoaded(manager, unidentifiable);
    const first = clients[clients.length - 1];
    expect(first).not.toBe(identified);
    expect(screen.getByTestId("principal").textContent).toBe("NO ROWS (loading)");

    // And it stays unidentified: a second such load must not be folded into the
    // first one either.
    emitUserLoaded(manager, { ...unidentifiable, access_token: "mystery-token-2" });
    expect(clients[clients.length - 1]).not.toBe(first);
  });

  it("(b4) signing out drops the authenticated client", async () => {
    const manager = fakeUserManager(user({ sub: "alice", access_token: "alice-token" }));
    serveMe();
    const clients = renderSurface(manager);
    await screen.findByText("ROWS OF principal-for-alice-token");
    const authenticated = clients[clients.length - 1];

    act(() => {
      manager.getUser.mockResolvedValue(null);
      for (const listener of manager.listeners) listener(null as unknown as FakeUser);
    });

    expect(clients[clients.length - 1]).not.toBe(authenticated);
    expect(screen.getByTestId("principal").textContent).toBe("NO ROWS (loading)");
  });
});
