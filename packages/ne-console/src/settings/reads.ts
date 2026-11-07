// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE DEPLOYMENT-SETTINGS SEAM (§18 B7).
//
// §16's Deployment section is READ-ONLY on this deployment, from the
// configuration it already serves, with one caption saying so. That is the
// stated degradation and it is what renders today.
//
// B7 adds a settings read and a governed patch. These probes ask the ONE
// governed client whether it can serve them; when it can, the two fields §16
// names become editable with the §2.5 pending pattern and the read-only caption
// — which would then be false — goes away. When it cannot, they return null.
//
// THE WRITE IS PROBED SEPARATELY FROM THE READ. A deployment that serves the
// read but not the patch renders served values and no submit control: a button
// with no endpoint behind it is a fabricated capability.
//
// THIN CLIENT (C7). Each probe reads a method off the injected client and calls
// it ON the client, so the bearer, the base and the refusal path are the
// governed ones.

import type { ApiClient } from "../api/client";
import type { DeploymentSettingsView, GovernedWriteResult, SettingsPatch } from "./settings";

/** The two surfaces §18 B7 adds to the governed client. */
export interface SettingsApi {
  deploymentSettings(): Promise<DeploymentSettingsView>;
  updateDeploymentSettings(patch: SettingsPatch): Promise<GovernedWriteResult>;
}

export type SettingsReader = () => Promise<DeploymentSettingsView>;
export type SettingsWriter = (patch: SettingsPatch) => Promise<GovernedWriteResult>;

function probe(client: ApiClient): Partial<SettingsApi> {
  return client as Partial<SettingsApi>;
}

/** The settings read, or null on a deployment whose client cannot serve it. */
export function settingsReader(client: ApiClient): SettingsReader | null {
  const read = probe(client).deploymentSettings;
  return typeof read === "function" ? () => read.call(client) : null;
}

/** The governed settings patch, or null when the client cannot serve it. */
export function settingsWriter(client: ApiClient): SettingsWriter | null {
  const write = probe(client).updateDeploymentSettings;
  return typeof write === "function" ? (patch: SettingsPatch) => write.call(client, patch) : null;
}
