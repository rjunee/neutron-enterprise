// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Proves the callback route restores the originally requested path: after the
// IdP round-trip returns to /auth/callback, an authenticated session with a
// captured returnTo state navigates onward (never stranding the user on the
// "Completing sign-in…" screen), falling back to /chat when no path is known
// and never bouncing back to the callback route itself.

import { render, screen } from "@testing-library/react";
import type { AuthContextProps } from "react-oidc-context";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { AuthStub } from "../test/authStub";
import { AuthCallback } from "./AuthCallback";

function renderCallback(returnTo: string | undefined, isAuthenticated = true) {
  const user = {
    access_token: "tok",
    profile: { sub: "alice" },
    ...(returnTo === undefined ? {} : { state: { returnTo } }),
  } as unknown as AuthContextProps["user"];
  return render(
    <AuthStub value={{ isAuthenticated, user }}>
      <MemoryRouter initialEntries={["/auth/callback"]}>
        <Routes>
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route path="/chat" element={<div>CHAT PAGE</div>} />
          <Route path="/search" element={<div>SEARCH PAGE</div>} />
        </Routes>
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("AuthCallback", () => {
  it("restores the originally requested route after sign-in", async () => {
    renderCallback("/search");
    expect(await screen.findByText("SEARCH PAGE")).toBeInTheDocument();
  });

  it("falls back to /chat when no returnTo state was captured", async () => {
    renderCallback(undefined);
    expect(await screen.findByText("CHAT PAGE")).toBeInTheDocument();
  });

  it("never bounces back to the callback route", async () => {
    renderCallback("/auth/callback");
    expect(await screen.findByText("CHAT PAGE")).toBeInTheDocument();
  });

  it("shows a progress message while the session is still resolving", () => {
    renderCallback(undefined, false);
    expect(screen.getByText(/completing sign-in/i)).toBeInTheDocument();
  });
});
