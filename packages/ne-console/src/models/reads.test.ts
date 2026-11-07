// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The seam that decides whether §10 renders its full surface or its stated
// fallback. It must answer "no" on the client this deployment actually builds —
// otherwise the screen would offer columns and a spend table over reads nobody
// can serve — and "yes" the moment the reads exist, with the call made ON the
// client so the bearer, the base and the refusal path are the governed ones.

import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "../api/client";
import type { ApiClient } from "../api/client";
import type { ModelPolicyView } from "./policy";
import { policyReader, usageReader } from "./reads";
import type { ModelsReadApi } from "./reads";

const realClient = () =>
  createApiClient({ getAccessToken: () => "t", onUnauthorized: () => undefined });

describe("the model reads", () => {
  it("are ABSENT on the client this deployment ships", () => {
    const client = realClient();
    expect(policyReader(client)).toBeNull();
    expect(usageReader(client)).toBeNull();
  });

  it("are used the moment the governed client can serve them", async () => {
    const models = vi.fn(() => Promise.resolve({ models: [], air_gap: true }));
    const modelsUsage = vi.fn((window: string) =>
      Promise.resolve({
        window,
        models: [],
        totals: { calls: 0, input_tokens: null, output_tokens: null, cost_usd: null },
      }),
    );
    const client = { ...realClient(), models, modelsUsage } as ApiClient & ModelsReadApi;

    const policy = policyReader(client);
    const usage = usageReader(client);
    expect(policy).not.toBeNull();
    expect(usage).not.toBeNull();

    await policy?.();
    const window = await usage?.("2026-08");
    expect(models).toHaveBeenCalledTimes(1);
    // The window reaches the read: a probe that dropped its argument would
    // silently serve the same month for every selection.
    expect(modelsUsage).toHaveBeenCalledWith("2026-08");
    expect(window?.window).toBe("2026-08");
  });

  it("calls the read ON the client, so `this` is the governed client", async () => {
    // A probe that detached the method would break any client whose read uses
    // its own fields — which every real one does, for the bearer and the base.
    interface Marked {
      marker: string;
      models(): Promise<ModelPolicyView>;
    }
    const marked: Marked = {
      marker: "governed",
      models(this: Marked) {
        return Promise.resolve({ models: [], air_gap: this.marker === "governed" });
      },
    };
    const client = { ...realClient(), ...marked } as unknown as ApiClient;
    const view = await policyReader(client)?.();
    expect(view?.air_gap).toBe(true);
  });
});
