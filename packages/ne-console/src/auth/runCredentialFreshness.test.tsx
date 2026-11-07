// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE PRE-SUBMIT RUN-CREDENTIAL GUARD. A Run freezes the submitting bearer's
// exp into its authorization snapshot at admission, and the worker fail-closed
// re-checks that snapshot before every protected disclosure — so a run
// admitted on a nearly-expired token dies mid-flight with
// AuthorizationExpiredError (measured live: run 655e65f6 was admitted with
// 13.1s of credential left and died at 14.7s). `ensureFreshAccessToken` renews
// on demand BEFORE a run-starting request so the snapshot outlives the run.
//
// What it must NOT do is as load-bearing as what it does:
//  - it never blocks a submission (a failed renew proceeds on the current
//    token; expiry stays server-enforced — nothing here weakens authorization);
//  - it never crosses a principal boundary (a renew that comes back as a
//    different subject is discarded; the identity-keyed client rebuild in
//    useSessionBoundClient owns that transition).
//
// Same seam as sessionRenewal.test.tsx: the REAL react-oidc-context state
// machine runs; the fake is the oidc-client-ts UserManager.

import { render, waitFor } from "@testing-library/react";
import { AuthProvider as OidcAuthProvider } from "react-oidc-context";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emitUserLoaded, fakeUser, fakeUserManager } from "../test/fakeUserManager";
import type { SessionBoundClientOptions } from "./useSessionBoundClient";
import { useSessionBoundClient } from "./useSessionBoundClient";

const FLOOR = 420;

const nowSeconds = () => Date.now() / 1000;

/** Stable module-level factory (the hook's contract): the "client" IS the
 * options object, so tests drive the exact seam every governed client gets. */
const optionsAsClient = (options: SessionBoundClientOptions) => options;

function Probe({ onOptions }: { onOptions: (options: SessionBoundClientOptions) => void }) {
  onOptions(useSessionBoundClient(optionsAsClient));
  return null;
}

async function renderOptions(manager: ReturnType<typeof fakeUserManager>) {
  let latest: SessionBoundClientOptions | undefined;
  render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <OidcAuthProvider userManager={manager as any}>
      <Probe onOptions={(options) => (latest = options)} />
    </OidcAuthProvider>,
  );
  await waitFor(() => expect(latest?.getAccessToken()).toBeDefined());
  // The seam object is retained across renders; capture once, use throughout.
  return latest as SessionBoundClientOptions;
}

afterEach(() => vi.restoreAllMocks());

describe("ensureFreshAccessToken (pre-submit run-credential guard)", () => {
  it("does not renew a token that already has at least the floor remaining", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 800 }),
    );
    const options = await renderOptions(manager);

    await options.ensureFreshAccessToken?.(FLOOR);

    expect(manager.signinSilent).not.toHaveBeenCalled();
    expect(options.getAccessToken()).toBe("token-1");
  });

  it("renews below the floor and presents the renewed bearer IMMEDIATELY — before any re-render", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 100 }),
    );
    manager.signinSilent.mockResolvedValue(
      fakeUser({ sub: "alice", access_token: "token-2", expires_at: nowSeconds() + 900 }),
    );
    const options = await renderOptions(manager);

    await options.ensureFreshAccessToken?.(FLOOR);

    expect(manager.signinSilent).toHaveBeenCalledTimes(1);
    // No userLoaded was emitted: the auth state still holds token-1. The next
    // request must nevertheless present the renewed bearer, or the awaited
    // renew was theater and the run still races its own credential.
    expect(options.getAccessToken()).toBe("token-2");
  });

  it("NEVER blocks the submission: a failed renew resolves and the current token is presented", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 100 }),
    );
    manager.signinSilent.mockRejectedValue(new Error("silent renew iframe blocked"));
    const options = await renderOptions(manager);

    await expect(options.ensureFreshAccessToken?.(FLOOR)).resolves.toBeUndefined();

    expect(options.getAccessToken()).toBe("token-1");
  });

  it("discards a renew that returns a DIFFERENT subject — no cross-principal bearer", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 100 }),
    );
    manager.signinSilent.mockResolvedValue(
      fakeUser({ sub: "mallory", access_token: "token-evil", expires_at: nowSeconds() + 900 }),
    );
    const options = await renderOptions(manager);

    await options.ensureFreshAccessToken?.(FLOOR);

    expect(options.getAccessToken()).toBe("token-1");
  });

  it("yields to the auth state once it catches up with an equal-or-later expiry", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 100 }),
    );
    const caughtUp = nowSeconds() + 900;
    manager.signinSilent.mockResolvedValue(
      fakeUser({ sub: "alice", access_token: "token-2", expires_at: caughtUp }),
    );
    const options = await renderOptions(manager);
    await options.ensureFreshAccessToken?.(FLOOR);
    expect(options.getAccessToken()).toBe("token-2");

    // The USER_LOADED path lands (as production's userLoaded listener does).
    emitUserLoaded(
      manager,
      fakeUser({ sub: "alice", access_token: "token-3", expires_at: caughtUp }),
    );

    await waitFor(() => expect(options.getAccessToken()).toBe("token-3"));
  });

  it("renews once for concurrent submissions (single-flight)", async () => {
    const manager = fakeUserManager(
      fakeUser({ sub: "alice", access_token: "token-1", expires_at: nowSeconds() + 100 }),
    );
    manager.signinSilent.mockResolvedValue(
      fakeUser({ sub: "alice", access_token: "token-2", expires_at: nowSeconds() + 900 }),
    );
    const options = await renderOptions(manager);

    await Promise.all([
      options.ensureFreshAccessToken?.(FLOOR),
      options.ensureFreshAccessToken?.(FLOOR),
      options.ensureFreshAccessToken?.(FLOOR),
    ]);

    expect(manager.signinSilent).toHaveBeenCalledTimes(1);
    expect(options.getAccessToken()).toBe("token-2");
  });

  it("no-ops when signed out — nothing to renew, nothing to judge", async () => {
    const manager = fakeUserManager(null);
    let latest: SessionBoundClientOptions | undefined;
    render(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <OidcAuthProvider userManager={manager as any}>
        <Probe onOptions={(options) => (latest = options)} />
      </OidcAuthProvider>,
    );
    await waitFor(() => expect(latest).toBeDefined());

    await expect(latest?.ensureFreshAccessToken?.(FLOOR)).resolves.toBeUndefined();

    expect(manager.signinSilent).not.toHaveBeenCalled();
    expect(latest?.getAccessToken()).toBeUndefined();
  });
});
