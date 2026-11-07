// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { InferenceLifecycleTargetView } from "../api/types";
import type { StateWord } from "../primitives/StatePill";

/** The server's row order is configuration order. Never reorder targets in the browser. */
export function lifecycleRows(
  targets: readonly InferenceLifecycleTargetView[],
): readonly InferenceLifecycleTargetView[] {
  return targets;
}

/** Closed visual projection only. Action availability remains entirely server-owned. */
export function lifecycleStateWord(state: string): StateWord {
  switch (state) {
    case "ready":
      return "READY";
    case "starting":
      return "STARTING";
    case "stopping":
      return "STOPPING";
    case "stopped":
      return "STOPPED";
    case "error":
      return "ERROR";
    case "unknown":
    default:
      return "UNKNOWN";
  }
}

export function desiredStateFor(action: "start" | "stop"): "ready" | "stopped" {
  return action === "start" ? "ready" : "stopped";
}
