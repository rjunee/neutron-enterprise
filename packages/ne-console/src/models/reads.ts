// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TWO READS THIS SCREEN IS BUILT FOR, AND THE ONE IT HAS.
//
// §10's Policy table and Usage tab are served by a policy router and a usage
// rollup that this deployment's governed API does not expose yet. §10 states
// the degradation exactly — three posture-served columns, a read-only network
// row, absent write actions, and one line in place of the usage body — and that
// is what renders today.
//
// The screen is nevertheless built whole. These two functions ask the ONE
// governed client whether it can serve each read; when it can, the screen uses
// it, and the columns, the summary tiles and the per-model table appear with no
// further edit. When it cannot, they return null and the honest fallback
// renders. The screen therefore has no flag to flip and no dead branch that
// nothing exercises: the tests below drive both answers.
//
// THIN CLIENT (C7). There is no network primitive here and there cannot be one
// — the probe reads a method off the injected client and calls it. If the
// client cannot reach the surface, the console cannot either.

import type { ApiClient } from "../api/client";
import type { GovernedChangeResult } from "../api/types";
import type { ModelPolicyView } from "./policy";
import type { ModelsUsageView } from "./usage";

/** The two reads §18 B3/B4 add to the governed client. */
export interface ModelsReadApi {
  /** The allowed-model policy: entries, bindings, task routes, air gap. */
  models(): Promise<ModelPolicyView>;
  /** The cost rollup for one `YYYY-MM` window. */
  modelsUsage(window: string): Promise<ModelsUsageView>;
}

/** The one write this screen has (NE-AIRGAP-TOGGLE): the governed air-gap
 * change. Served by POST /v1/network/air-gap; probed like the reads so no
 * control renders on a client that cannot reach the surface. */
export interface AirGapWriteApi {
  setAirGap(airGap: boolean): Promise<GovernedChangeResult>;
}

export type PolicyReader = () => Promise<ModelPolicyView>;
export type UsageReader = (window: string) => Promise<ModelsUsageView>;
export type AirGapWriter = (airGap: boolean) => Promise<GovernedChangeResult>;

function probe(client: ApiClient): Partial<ModelsReadApi & AirGapWriteApi> {
  return client as Partial<ModelsReadApi & AirGapWriteApi>;
}

/** The policy read, or null on a deployment whose client cannot serve it. */
export function policyReader(client: ApiClient): PolicyReader | null {
  const read = probe(client).models;
  return typeof read === "function" ? () => read.call(client) : null;
}

/** The usage read, or null on a deployment whose client cannot serve it. */
export function usageReader(client: ApiClient): UsageReader | null {
  const read = probe(client).modelsUsage;
  return typeof read === "function" ? (window: string) => read.call(client, window) : null;
}

/** The governed air-gap write, or null when the client cannot serve it. */
export function airGapWriter(client: ApiClient): AirGapWriter | null {
  const write = probe(client).setAirGap;
  return typeof write === "function" ? (airGap: boolean) => write.call(client, airGap) : null;
}
