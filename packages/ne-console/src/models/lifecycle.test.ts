// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { InferenceLifecycleTargetView } from "../api/types";
import { desiredStateFor, lifecycleRows, lifecycleStateWord } from "./lifecycle";

const target = (targetId: string): InferenceLifecycleTargetView => ({
  target_id: targetId,
  display_name: targetId,
  aliases: [],
  state: "unknown",
  desired_state: "stopped",
  version: 0,
  transition_id: null,
  code: "observed",
  observed_at: null,
  updated_at: "2026-08-20T20:00:00Z",
  cooldown_until: null,
  pending_change: null,
  available_actions: [],
});

describe("inference lifecycle presenter", () => {
  it("preserves server configuration order", () => {
    expect(lifecycleRows([target("z-third-party"), target("a-cpu"), target("m-gpu")])).toEqual([
      expect.objectContaining({ target_id: "z-third-party" }),
      expect.objectContaining({ target_id: "a-cpu" }),
      expect.objectContaining({ target_id: "m-gpu" }),
    ]);
  });

  it("projects only the closed visual vocabulary", () => {
    expect(
      ["ready", "starting", "stopping", "stopped", "unknown", "error", "future"].map(
        lifecycleStateWord,
      ),
    ).toEqual(["READY", "STARTING", "STOPPING", "STOPPED", "UNKNOWN", "ERROR", "UNKNOWN"]);
  });

  it("maps server actions to their exact desired states", () => {
    expect(desiredStateFor("start")).toBe("ready");
    expect(desiredStateFor("stop")).toBe("stopped");
  });
});
