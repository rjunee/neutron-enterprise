// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE READS §15 IS BUILT FOR, AND THE ONE THIS DEPLOYMENT HAS.
//
// §15 is entirely §18 B6: the principals, roles and Tier-3 lists, plus the one
// governed membership write. None of them exists on this deployment's API yet.
//
// The screen is nevertheless built whole, exactly as §10's did: these functions
// ask the ONE governed client whether it can serve each read, and when it can,
// the tables, the row panes and the two write actions appear with no further
// edit. When it cannot, they return null and the screen renders the stated
// fallback instead of a table of nothing. There is therefore no flag to flip and
// no dead branch nothing exercises — the tests drive both answers.
//
// THE WRITE IS PROBED SEPARATELY FROM THE READS. A deployment that can serve the
// lists but not the governed assignment write renders the lists and NO write
// control: §2.5's contract is that every write goes through the governed path,
// and a button with no endpoint behind it is a fabricated capability.
//
// THIN CLIENT (C7). There is no network primitive here and there cannot be one —
// each probe reads a method off the injected client and calls it ON the client,
// so the bearer, the base and the refusal path are the governed ones.

import type { ApiClient } from "../api/client";
import type {
  AccessChange,
  AccessPrincipalsView,
  AccessRolesView,
  GovernedWriteResult,
  Tier3ApproversView,
} from "./access";

/** The four surfaces §18 B6 adds to the governed client. */
export interface AccessReadApi {
  /** Principals with their groups and the server-computed surface grants. */
  accessPrincipals(): Promise<AccessPrincipalsView>;
  /** The role → surface map, with the count of members per role. */
  accessRoles(): Promise<AccessRolesView>;
  /** The named list the Tier-3 gate enforces. */
  accessTier3Approvers(): Promise<Tier3ApproversView>;
  /** The governed membership write: applied / pending / denied. */
  submitAccessChange(change: AccessChange): Promise<GovernedWriteResult>;
}

export interface AccessReaders {
  principals: () => Promise<AccessPrincipalsView>;
  roles: () => Promise<AccessRolesView>;
  tier3: () => Promise<Tier3ApproversView>;
}

export type AccessWriter = (change: AccessChange) => Promise<GovernedWriteResult>;

function probe(client: ApiClient): Partial<AccessReadApi> {
  return client as Partial<AccessReadApi>;
}

/**
 * The three reads, or null on a deployment whose client cannot serve them.
 *
 * ALL THREE OR NONE. They are one endpoint group and they ship together; a
 * partial answer would put a People tab beside a Roles tab that silently had no
 * Tier-3 section, and the reader could not tell an empty list from an absent
 * one.
 */
export function accessReaders(client: ApiClient): AccessReaders | null {
  const api = probe(client);
  const { accessPrincipals, accessRoles, accessTier3Approvers } = api;
  if (
    typeof accessPrincipals !== "function" ||
    typeof accessRoles !== "function" ||
    typeof accessTier3Approvers !== "function"
  ) {
    return null;
  }
  return {
    principals: () => accessPrincipals.call(client),
    roles: () => accessRoles.call(client),
    tier3: () => accessTier3Approvers.call(client),
  };
}

/** The governed membership write, or null when the client cannot serve it. */
export function accessWriter(client: ApiClient): AccessWriter | null {
  const write = probe(client).submitAccessChange;
  return typeof write === "function" ? (change: AccessChange) => write.call(client, change) : null;
}
