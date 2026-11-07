// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { InferenceLifecycleTargetView, InferenceLifecycleView } from "../api/types";
import { LifecyclePanel } from "./LifecyclePanel";

const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

function target(
  targetId: string,
  overrides: Partial<InferenceLifecycleTargetView> = {},
): InferenceLifecycleTargetView {
  return {
    target_id: targetId,
    display_name: `Target ${targetId}`,
    aliases: [`${targetId}-alias`],
    state: "stopped",
    desired_state: "stopped",
    version: 3,
    transition_id: null,
    code: "observed",
    observed_at: "2026-08-20T20:00:00Z",
    updated_at: "2026-08-20T20:00:00Z",
    cooldown_until: null,
    pending_change: null,
    available_actions: ["start"],
    ...overrides,
  };
}

function view(
  targets: InferenceLifecycleTargetView[],
  transportAvailable = true,
): InferenceLifecycleView {
  return {
    registry_hash: "a".repeat(64),
    transport_available: transportAvailable,
    targets,
  };
}

function rowFor(displayName: string): HTMLElement {
  const heading = screen.getByText(displayName);
  const row = heading.closest("li");
  if (row === null) throw new Error(`missing row for ${displayName}`);
  return row;
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <LifecyclePanel />
    </MemoryRouter>,
  );
}

describe("LifecyclePanel", () => {
  beforeEach(() => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000001",
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("renders arbitrary targets in server order and offers only server actions", async () => {
    stub.client = {
      modelLifecycle: () =>
        Promise.resolve(
          view([
            target("vendor-z", { available_actions: ["stop"], state: "ready" }),
            target("local-cpu", { available_actions: [] }),
            target("private-gpu", { available_actions: ["start"] }),
          ]),
        ),
    };
    renderPanel();
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual([
      "Target vendor-z",
      "Target local-cpu",
      "Target private-gpu",
    ]);
    expect(within(rows[0]!).getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(within(rows[1]!).queryByRole("button")).toBeNull();
    expect(within(rows[2]!).getByRole("button", { name: "Start" })).toBeEnabled();
  });

  it("keeps the target read-only when transport is unavailable", async () => {
    stub.client = {
      modelLifecycle: () =>
        Promise.resolve(view([target("local-cpu", { available_actions: [] })], false)),
    };
    renderPanel();
    expect(await screen.findByText(/inference operator is unavailable/i)).toBeVisible();
    expect(rowFor("Target local-cpu").querySelector("button")).toBeNull();
  });

  it("does not infer actions when cooldown or server guardrails leave none available", async () => {
    stub.client = {
      modelLifecycle: () =>
        Promise.resolve(
          view([
            target("cooldown", {
              state: "ready",
              cooldown_until: "2026-08-20T20:05:00Z",
              available_actions: [],
            }),
            target("air-gap", { state: "stopped", code: "air_gap", available_actions: [] }),
            target("manifest", {
              state: "unknown",
              code: "manifest_mismatch",
              available_actions: [],
            }),
          ]),
        ),
    };
    renderPanel();
    await screen.findByText("Target cooldown");
    expect(screen.getByText("Cooldown until")).toBeVisible();
    for (const name of ["Target cooldown", "Target air-gap", "Target manifest"]) {
      expect(rowFor(name).querySelector("button")).toBeNull();
    }
  });

  it("submits start with the exact target, version, desired state, and stable idempotency key", async () => {
    let reads = 0;
    let resolveProof!: (value: InferenceLifecycleView) => void;
    const transitionModelTarget = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("unavailable", 503, "operator unavailable"))
      .mockResolvedValueOnce({ status: "applied", request_id: null });
    stub.client = {
      modelLifecycle: () => {
        reads += 1;
        if (reads === 1) return Promise.resolve(view([target("local-cpu")]));
        return new Promise<InferenceLifecycleView>((resolve) => {
          resolveProof = resolve;
        });
      },
      transitionModelTarget,
    };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("operator unavailable");
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    expect(transitionModelTarget).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(resolveProof).toBeTypeOf("function"));
    resolveProof(view([target("local-cpu")]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(transitionModelTarget).toHaveBeenCalledTimes(2));
    expect(transitionModelTarget.mock.calls).toEqual([
      ["local-cpu", "ready", 3, "console-00000000-0000-4000-8000-000000000001", undefined],
      ["local-cpu", "ready", 3, "console-00000000-0000-4000-8000-000000000001", undefined],
    ]);
  });

  it("keeps the stop confirmation token out of rendered and browser storage state", async () => {
    const token = "secret-confirmation-token-value";
    const confirmModelStop = vi.fn(() =>
      Promise.resolve({ confirmation_token: token, expires_at: "2026-08-20T20:05:00Z" }),
    );
    const transitionModelTarget = vi.fn(() =>
      Promise.resolve({ status: "pending" as const, request_id: "approval-7" }),
    );
    stub.client = {
      modelLifecycle: () =>
        Promise.resolve(
          view([target("private-gpu", { state: "ready", available_actions: ["stop"] })]),
        ),
      confirmModelStop,
      transitionModelTarget,
    };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await user.click(screen.getByRole("button", { name: "Confirm stop" }));
    await waitFor(() => expect(transitionModelTarget).toHaveBeenCalledTimes(1));
    expect(confirmModelStop).toHaveBeenCalledWith("private-gpu", 3);
    expect(transitionModelTarget).toHaveBeenCalledWith(
      "private-gpu",
      "stopped",
      3,
      "console-00000000-0000-4000-8000-000000000001",
      token,
    );
    expect(document.body.textContent).not.toContain(token);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("admits a newly advertised opposite action after the prior transition settles", async () => {
    vi.mocked(globalThis.crypto.randomUUID)
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000001")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000002");
    let reads = 0;
    const transitionModelTarget = vi
      .fn()
      .mockResolvedValueOnce({ status: "applied", request_id: null })
      .mockResolvedValueOnce({ status: "pending", request_id: "approval-stop" });
    const confirmModelStop = vi.fn(() =>
      Promise.resolve({
        confirmation_token: "new-version-stop-token",
        expires_at: "2026-08-20T20:05:00Z",
      }),
    );
    stub.client = {
      modelLifecycle: () => {
        reads += 1;
        return Promise.resolve(
          view([
            target("local-cpu", {
              state: reads === 1 ? "stopped" : "ready",
              desired_state: reads === 1 ? "stopped" : "ready",
              version: reads === 1 ? 3 : 4,
              available_actions: reads === 1 ? ["start"] : ["stop"],
            }),
          ]),
        );
      },
      confirmModelStop,
      transitionModelTarget,
    };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start" }));
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await user.click(screen.getByRole("button", { name: "Confirm stop" }));
    await waitFor(() => expect(transitionModelTarget).toHaveBeenCalledTimes(2));
    expect(confirmModelStop).toHaveBeenCalledWith("local-cpu", 4);
    expect(transitionModelTarget.mock.calls).toEqual([
      ["local-cpu", "ready", 3, "console-00000000-0000-4000-8000-000000000001", undefined],
      [
        "local-cpu",
        "stopped",
        4,
        "console-00000000-0000-4000-8000-000000000002",
        "new-version-stop-token",
      ],
    ]);
  });

  it("moves a stop request to its own busy state when the server offers both actions", async () => {
    let release!: () => void;
    const transitionModelTarget = vi.fn(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ status: "pending", request_id: "approval-stop" });
        }),
    );
    stub.client = {
      modelLifecycle: () =>
        Promise.resolve(
          view([
            target("recoverable", {
              state: "unknown",
              available_actions: ["start", "stop"],
            }),
          ]),
        ),
      confirmModelStop: () =>
        Promise.resolve({
          confirmation_token: "recovery-stop-token",
          expires_at: "2026-08-20T20:05:00Z",
        }),
      transitionModelTarget,
    };
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await user.click(screen.getByRole("button", { name: "Confirm stop" }));
    await waitFor(() => expect(transitionModelTarget).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "Confirm stop" })).toBeNull();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    release();
  });

  it("renders a reloaded pending change without optimistically changing target state", async () => {
    let reads = 0;
    stub.client = {
      modelLifecycle: () => {
        reads += 1;
        return Promise.resolve(
          view([
            target("local-cpu", {
              pending_change:
                reads > 1
                  ? {
                      request_id: "approval-9",
                      requested_by: "user:ops",
                      desired_state: "ready",
                    }
                  : null,
              available_actions: reads > 1 ? [] : ["start"],
            }),
          ]),
        );
      },
      transitionModelTarget: () => Promise.resolve({ status: "pending", request_id: "approval-9" }),
    };
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText("STOPPED")).toBeVisible();
    expect(await screen.findByText(/user:ops/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });

  it("keeps another target actionable while one target write is pending", async () => {
    let release!: () => void;
    stub.client = {
      modelLifecycle: () => Promise.resolve(view([target("one"), target("two")])),
      transitionModelTarget: (targetId: string) =>
        targetId === "one"
          ? new Promise((resolve) => {
              release = () => resolve({ status: "applied", request_id: null });
            })
          : Promise.resolve({ status: "applied", request_id: null }),
    };
    renderPanel();
    const user = userEvent.setup();
    await screen.findByText("Target one");
    await user.click(within(rowFor("Target one")).getByRole("button", { name: "Start" }));
    expect(within(rowFor("Target one")).getByRole("button", { name: "Start" })).toBeDisabled();
    expect(within(rowFor("Target two")).getByRole("button", { name: "Start" })).toBeEnabled();
    release();
  });

  it.each([
    [403, "policy denied"],
    [409, "version changed"],
    [503, "operator unavailable"],
  ])("renders the server error for HTTP %i without auto-retrying", async (status, detail) => {
    const transitionModelTarget = vi.fn(() =>
      Promise.reject(new ApiError("failed", status, detail)),
    );
    stub.client = {
      modelLifecycle: () => Promise.resolve(view([target("local-cpu")])),
      transitionModelTarget,
    };
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText(detail)).toBeVisible();
    expect(transitionModelTarget).toHaveBeenCalledTimes(1);
  });

  it.each([
    [403, "Denied by policy."],
    [409, "The transition didn’t happen; the server stated no reason."],
    [503, "The transition didn’t happen; the server stated no reason."],
  ])(
    "states an HTTP %i the server gave no reason for, without writing one for it",
    async (status, line) => {
      // The mirror of the test above: same three statuses, no served `detail`.
      // The console's §2.4 house sentence used to land in the slot that renders
      // as the SERVER'S reason, so the screen showed the server stating a
      // reason it never stated. Where it says nothing, the console answers in
      // its OWN voice and says so.
      const transitionModelTarget = vi.fn(() => Promise.reject(new ApiError("failed", status)));
      stub.client = {
        modelLifecycle: () => Promise.resolve(view([target("local-cpu")])),
        transitionModelTarget,
      };
      renderPanel();
      await userEvent.click(await screen.findByRole("button", { name: "Start" }));
      expect(await screen.findByText(line)).toBeVisible();
      expect(screen.queryByText("Your role doesn’t include this action.")).toBeNull();
      expect(screen.queryByText("The server is unavailable.")).toBeNull();
      expect(transitionModelTarget).toHaveBeenCalledTimes(1);
    },
  );

  it("lets only the latest poll replace rows and clears actions on the latest poll failure", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let read = 0;
    let resolveStale!: (value: InferenceLifecycleView) => void;
    stub.client = {
      modelLifecycle: () => {
        read += 1;
        if (read === 1) return Promise.resolve(view([target("initial")]));
        if (read === 2) {
          return new Promise<InferenceLifecycleView>((resolve) => {
            resolveStale = resolve;
          });
        }
        if (read === 3) return Promise.resolve(view([target("latest")]));
        return Promise.reject(new ApiError("unavailable", 503, "read unavailable"));
      },
    };
    renderPanel();
    expect(await screen.findByText("Target initial")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    await waitFor(() => expect(read).toBe(2));
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(await screen.findByText("Target latest")).toBeVisible();
    await act(async () => resolveStale(view([target("stale")])));
    expect(screen.queryByText("Target stale")).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(await screen.findByText(/couldn.t load inference targets/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });
});
