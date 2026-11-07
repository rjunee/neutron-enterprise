// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { HttpResponse, http } from "msw";
import { AuthContext } from "react-oidc-context";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PrincipalState } from "../auth/PrincipalContext";
import { PrincipalLabel } from "../primitives/PrincipalLabel";
import { makeAuthValue } from "../test/authStub";
import { server } from "../test/mocks/server";
import { DirectoryProvider } from "./DirectoryContext";

const ADA = "c21456b9-c8dc-4458-b393-bc3230db0081";
const BOB = "06a099a0-3333-4333-8333-000000000001";
const principalState = vi.hoisted(() => ({
  current: {
    status: "ok" as const,
    principal: { id: "reader@example.com", groups: ["approvers"] },
  } satisfies PrincipalState,
}));
const stableAuth = makeAuthValue();

vi.mock("../auth/PrincipalContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/PrincipalContext")>()),
  usePrincipal: () => principalState.current,
}));

beforeEach(() => {
  principalState.current = {
    status: "ok",
    principal: { id: "reader@example.com", groups: ["approvers"] },
  };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function renderLabels(children: ReactNode) {
  return render(
    <AuthContext.Provider value={stableAuth}>
      <DirectoryProvider>{children}</DirectoryProvider>
    </AuthContext.Provider>,
  );
}

describe("governed human-name directory", () => {
  it("batches opaque record references and never sends principal ids", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          resolved: [{ id: ADA, display_name: "Ada Lovelace", removed: false }],
        });
      }),
    );

    renderLabels(
      <>
        <PrincipalLabel id={ADA} reference={{ scope: "approvals", reference: "req-a" }} />
        <PrincipalLabel id={BOB} reference={{ scope: "approvals", reference: "req-b" }} />
      </>,
    );

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("06a099a0")).toBeInTheDocument();
    expect(bodies).toEqual([{ scope: "approvals", references: ["req-a", "req-b"] }]);
    expect(JSON.stringify(bodies)).not.toContain(ADA);
    expect(JSON.stringify(bodies)).not.toContain(BOB);
  });

  it("renders tombstones truthfully and caches a resolved principal", async () => {
    let requests = 0;
    server.use(
      http.post("*/api/v1/principals/resolve", () => {
        requests += 1;
        return HttpResponse.json({
          resolved: [{ id: BOB, display_name: "Departed Bob", removed: true }],
        });
      }),
    );

    const view = renderLabels(
      <PrincipalLabel id={BOB} reference={{ scope: "audit", reference: "evt-1" }} />,
    );
    expect(await screen.findByText("Departed Bob (removed)")).toBeInTheDocument();
    view.rerender(
      <AuthContext.Provider value={stableAuth}>
        <DirectoryProvider>
          <PrincipalLabel id={BOB} reference={{ scope: "audit", reference: "evt-2" }} />
        </DirectoryProvider>
      </AuthContext.Provider>,
    );
    await waitFor(() => expect(requests).toBe(1));
  });

  it("keeps approval and audit batches in separate role-scoped requests", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ resolved: [] });
      }),
    );

    renderLabels(
      <>
        <PrincipalLabel id={ADA} reference={{ scope: "approvals", reference: "req-a" }} />
        <PrincipalLabel id={BOB} reference={{ scope: "audit", reference: "evt-b" }} />
      </>,
    );

    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies).toEqual([
      { scope: "approvals", references: ["req-a"] },
      { scope: "audit", references: ["evt-b"] },
    ]);
  });

  it("fails honest to the stable id fallback when the governed read is refused", async () => {
    server.use(
      http.post("*/api/v1/principals/resolve", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );

    renderLabels(<PrincipalLabel id={ADA} reference={{ scope: "audit", reference: "evt-a" }} />);
    expect(screen.getByText("c21456b9")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTitle(ADA)).toBeInTheDocument());
    expect(screen.queryByText(/undefined|null/i)).not.toBeInTheDocument();
  });

  it("hides same-principal client-A cache and stale completions before client B settles", async () => {
    const staleAlice = deferred<Response>();
    const slowBob = deferred<Response>();
    const calls: Array<{ token: string; body: unknown }> = [];
    let aliceReads = 0;
    server.use(
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        calls.push({ token, body: await request.json() });
        if (token.endsWith("client-a")) {
          aliceReads += 1;
          return aliceReads === 1
            ? HttpResponse.json({
                resolved: [{ id: ADA, display_name: "CLIENT A CACHED", removed: false }],
              })
            : staleAlice.promise;
        }
        return slowBob.promise;
      }),
    );
    const authA = makeAuthValue({
      user: { ...stableAuth.user!, access_token: "client-a" } as typeof stableAuth.user,
    });
    const authB = makeAuthValue({
      user: { ...stableAuth.user!, access_token: "client-b" } as typeof stableAuth.user,
    });
    const labels = (auth: typeof stableAuth, includeBob: boolean) => (
      <AuthContext.Provider value={auth}>
        <DirectoryProvider>
          <PrincipalLabel id={ADA} reference={{ scope: "approvals", reference: "req-a" }} />
          {includeBob ? (
            <PrincipalLabel id={BOB} reference={{ scope: "approvals", reference: "req-b" }} />
          ) : null}
        </DirectoryProvider>
      </AuthContext.Provider>
    );

    const view = render(labels(authA, false));
    expect(await screen.findByText("CLIENT A CACHED")).toBeInTheDocument();
    view.rerender(labels(authA, true));
    await waitFor(() => expect(calls).toHaveLength(2));

    view.rerender(labels(authB, true));
    expect(screen.queryByText("CLIENT A CACHED")).toBeNull();
    expect(screen.getByText("c21456b9")).toBeInTheDocument();
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[2]?.token).toBe("Bearer client-b");

    staleAlice.resolve(
      HttpResponse.json({
        resolved: [{ id: BOB, display_name: "CLIENT A STALE", removed: false }],
      }),
    );
    await act(async () => Promise.resolve());
    expect(screen.queryByText(/CLIENT A/)).toBeNull();

    slowBob.resolve(
      HttpResponse.json({
        resolved: [
          { id: ADA, display_name: "CLIENT B ADA", removed: false },
          { id: BOB, display_name: "CLIENT B BOB", removed: false },
        ],
      }),
    );
    expect(await screen.findByText("CLIENT B ADA")).toBeInTheDocument();
    expect(screen.getByText("CLIENT B BOB")).toBeInTheDocument();
  });

  it("keeps a same-principal client-A rejection out of client B's directory cycle", async () => {
    const staleAlice = deferred<Response>();
    const slowBob = deferred<Response>();
    const calls: string[] = [];
    server.use(
      http.post("*/api/v1/principals/resolve", ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        calls.push(token);
        return token.endsWith("client-a") ? staleAlice.promise : slowBob.promise;
      }),
    );
    const authA = makeAuthValue({
      user: { ...stableAuth.user!, access_token: "client-a" } as typeof stableAuth.user,
    });
    const authB = makeAuthValue({
      user: { ...stableAuth.user!, access_token: "client-b" } as typeof stableAuth.user,
    });
    const label = (auth: typeof stableAuth) => (
      <AuthContext.Provider value={auth}>
        <DirectoryProvider>
          <PrincipalLabel id={ADA} reference={{ scope: "approvals", reference: "req-a" }} />
        </DirectoryProvider>
      </AuthContext.Provider>
    );

    const view = render(label(authA));
    await waitFor(() => expect(calls).toEqual(["Bearer client-a"]));
    view.rerender(label(authB));
    await waitFor(() => expect(calls).toEqual(["Bearer client-a", "Bearer client-b"]));

    staleAlice.reject(new Error("CLIENT A PRIVATE FAILURE"));
    await act(async () => Promise.resolve());
    expect(document.body.textContent).not.toContain("CLIENT A");

    slowBob.resolve(
      HttpResponse.json({
        resolved: [{ id: ADA, display_name: "CLIENT B ONLY", removed: false }],
      }),
    );
    expect(await screen.findByText("CLIENT B ONLY")).toBeInTheDocument();
    expect(calls).toEqual(["Bearer client-a", "Bearer client-b"]);
  });
});
