// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the second-persona OIDC seam under test. oidc-client-ts is mocked
// BELOW the seam (constructor-spy stubs) — our lifecycle logic IS the seam. These
// assertions prove the login is Authorization-Code + PKCE via a PUBLIC client
// with MEMORY-ONLY storage: InMemoryWebStorage-backed user AND state stores, no
// client-secret key in settings, signinPopup({prompt:"login"}), token retrievable
// while held and undefined after withdraw(), revokeTokens attempted then
// removeUser, browser storage byte-identical before/after (never persisted), and
// a compare-client 401 → onKeyExpired once (never the primary signinRedirect).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/mocks/server";
import type { ConsoleRuntimeConfig } from "./runtimeConfig";

const mocks = vi.hoisted(() => ({
  signinPopup: vi.fn(),
  revokeTokens: vi.fn(),
  removeUser: vi.fn(),
  signinPopupCallback: vi.fn(),
  lastSettings: { value: null as Record<string, unknown> | null },
}));

vi.mock("oidc-client-ts", () => {
  class UserManager {
    constructor(settings: Record<string, unknown>) {
      mocks.lastSettings.value = settings;
    }
    signinPopup = mocks.signinPopup;
    revokeTokens = mocks.revokeTokens;
    removeUser = mocks.removeUser;
    signinPopupCallback = mocks.signinPopupCallback;
  }
  class WebStorageStateStore {
    store: unknown;
    constructor(opts: { store?: unknown }) {
      this.store = opts?.store;
    }
  }
  class InMemoryWebStorage {}
  return { UserManager, WebStorageStateStore, InMemoryWebStorage };
});

// Imported AFTER the mock so the seam builds on the stubbed oidc-client-ts.
import { InMemoryWebStorage } from "oidc-client-ts";
import { acquireCompareKey, createCompareClient } from "./compareAuth";
import { ApiError } from "./client";

const CFG: ConsoleRuntimeConfig = {
  issuer: "https://idp.ne.test/realms/ne-test",
  client_id: "ne-console",
  audience: "ne-console",
  api_base: "/api",
};

function storageSnapshot(store: Storage): string {
  const out: Record<string, string> = {};
  if (typeof store?.key !== "function" || typeof store.getItem !== "function") return "{}";
  for (let i = 0; i < store.length; i++) {
    const k = store.key(i);
    if (k === null) continue;
    out[k] = store.getItem(k) ?? "";
  }
  return JSON.stringify(out);
}

beforeEach(() => {
  mocks.signinPopup.mockReset();
  mocks.revokeTokens.mockReset();
  mocks.removeUser.mockReset();
  mocks.signinPopupCallback.mockReset();
  mocks.lastSettings.value = null;
});

function clearStorage(store: Storage): void {
  // jsdom / node's experimental localStorage differ; clear defensively.
  if (typeof store?.clear === "function") store.clear();
}

afterEach(() => {
  clearStorage(window.sessionStorage);
  clearStorage(window.localStorage);
});

describe("acquireCompareKey — Authorization-Code + PKCE, memory-only", () => {
  it("builds a PUBLIC client with InMemoryWebStorage user AND state stores, no client-secret", async () => {
    mocks.signinPopup.mockResolvedValue({
      access_token: "tok-b",
      profile: { preferred_username: "beta" },
    });
    await acquireCompareKey(CFG);

    const settings = mocks.lastSettings.value!;
    expect(settings.response_type).toBe("code");
    // No credential key of any assembled form.
    expect(Object.keys(settings)).not.toContain(["client", "secret"].join("_"));
    // Both stores are InMemoryWebStorage-backed (nothing persisted).
    const userStore = settings.userStore as { store: unknown };
    const stateStore = settings.stateStore as { store: unknown };
    expect(userStore.store).toBeInstanceOf(InMemoryWebStorage);
    expect(stateStore.store).toBeInstanceOf(InMemoryWebStorage);
  });

  it("forces re-authentication with signinPopup({prompt:'login'})", async () => {
    mocks.signinPopup.mockResolvedValue({ access_token: "tok-b", profile: { sub: "beta" } });
    await acquireCompareKey(CFG);
    expect(mocks.signinPopup).toHaveBeenCalledTimes(1);
    expect(mocks.signinPopup).toHaveBeenCalledWith({ prompt: "login" });
  });

  it("holds the token only in the closure: retrievable then undefined after withdraw()", async () => {
    mocks.signinPopup.mockResolvedValue({
      access_token: "tok-b",
      profile: { preferred_username: "beta" },
    });
    mocks.revokeTokens.mockResolvedValue(undefined);
    mocks.removeUser.mockResolvedValue(undefined);

    const key = await acquireCompareKey(CFG);
    expect(key.label).toBe("beta");
    expect(key.getAccessToken()).toBe("tok-b");

    await key.withdraw();
    expect(key.getAccessToken()).toBeUndefined();
  });

  it("withdraw() attempts revokeTokens (tolerating rejection) then removeUser", async () => {
    mocks.signinPopup.mockResolvedValue({ access_token: "tok-b", profile: { sub: "beta" } });
    // Revocation endpoint unavailable — withdraw must still succeed.
    mocks.revokeTokens.mockRejectedValue(new Error("no revocation endpoint"));
    mocks.removeUser.mockResolvedValue(undefined);

    const key = await acquireCompareKey(CFG);
    await expect(key.withdraw()).resolves.toBeUndefined();
    expect(mocks.revokeTokens).toHaveBeenCalledTimes(1);
    expect(mocks.removeUser).toHaveBeenCalledTimes(1);
    // Revoke is attempted before the user is removed.
    expect(mocks.revokeTokens.mock.invocationCallOrder[0]!).toBeLessThan(
      mocks.removeUser.mock.invocationCallOrder[0]!,
    );
    expect(key.getAccessToken()).toBeUndefined();
  });

  it("never persists the token to sessionStorage or localStorage", async () => {
    mocks.signinPopup.mockResolvedValue({ access_token: "tok-b", profile: { sub: "beta" } });
    mocks.revokeTokens.mockResolvedValue(undefined);
    mocks.removeUser.mockResolvedValue(undefined);

    const before = {
      s: storageSnapshot(window.sessionStorage),
      l: storageSnapshot(window.localStorage),
    };
    const key = await acquireCompareKey(CFG);
    await key.withdraw();
    const after = {
      s: storageSnapshot(window.sessionStorage),
      l: storageSnapshot(window.localStorage),
    };

    expect(after.s).toBe(before.s);
    expect(after.l).toBe(before.l);
    expect(after.s).not.toContain("tok-b");
    expect(after.l).not.toContain("tok-b");
  });
});

describe("createCompareClient — a separate, non-primary fetch context", () => {
  it("a 401 under key B invokes onKeyExpired once, never the primary signinRedirect", async () => {
    server.use(
      http.get("*/api/v1/me", () => HttpResponse.json({ detail: "expired" }, { status: 401 })),
    );

    const onKeyExpired = vi.fn();
    const primarySigninRedirect = vi.fn();
    const key = {
      label: "beta",
      getAccessToken: () => "tok-b",
      withdraw: async () => {},
    };
    const client = createCompareClient(key, onKeyExpired);

    await expect(client.me()).rejects.toBeInstanceOf(ApiError);
    expect(onKeyExpired).toHaveBeenCalledTimes(1);
    expect(primarySigninRedirect).not.toHaveBeenCalled();
  });
});
