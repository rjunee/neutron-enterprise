// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the /auth/compare-callback popup route. It relays the IdP response to
// window.opener via completeComparePopup (mocked at the src/api seam — jsdom has
// no real popup). These prove it calls completeComparePopup once with the runtime
// config, and renders honest text on the null-config and rejection paths.

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import { TEST_RUNTIME_CONFIG } from "../test/mocks/compareFixtures";
import { CompareCallback } from "./CompareCallback";
import { completeComparePopup } from "../api/compareAuth";

vi.mock("../api/compareAuth", () => ({
  completeComparePopup: vi.fn(),
}));

const completeMock = vi.mocked(completeComparePopup);

beforeEach(() => {
  completeMock.mockReset();
});

describe("CompareCallback", () => {
  it("calls completeComparePopup once with the runtime config and shows the return line", async () => {
    completeMock.mockResolvedValue(undefined);
    render(
      <RuntimeConfigContext.Provider value={TEST_RUNTIME_CONFIG}>
        <CompareCallback />
      </RuntimeConfigContext.Provider>,
    );
    expect(
      screen.getByText("Completing compare sign-in — return to the console window."),
    ).toBeInTheDocument();
    await waitFor(() => expect(completeMock).toHaveBeenCalledTimes(1));
    expect(completeMock).toHaveBeenCalledWith(TEST_RUNTIME_CONFIG);
  });

  it("renders an honest line and calls nothing when the runtime config is unavailable", () => {
    render(<CompareCallback />); // no provider → null config
    expect(screen.getByRole("alert")).toHaveTextContent(/Runtime configuration unavailable/);
    expect(completeMock).not.toHaveBeenCalled();
  });

  it("renders the honest failure path when the exchange relay rejects", async () => {
    completeMock.mockRejectedValue(new Error("popup closed by user"));
    render(
      <RuntimeConfigContext.Provider value={TEST_RUNTIME_CONFIG}>
        <CompareCallback />
      </RuntimeConfigContext.Provider>,
    );
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/popup closed by user/),
    );
  });
});
