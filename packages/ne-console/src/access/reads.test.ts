// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The seam that decides whether §15 renders its full surface or its stated
// fallback. It must answer "no" on the client this deployment actually builds —
// otherwise Access would offer two tables and two write buttons over reads
// nobody can serve — and "yes" the moment the reads exist, with each call made
// ON the client so the bearer, the base and the refusal path are the governed
// ones.

import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "../api/client";
import type { ApiClient } from "../api/client";
import type { AccessPrincipalsView, AccessRolesView, Tier3ApproversView } from "./access";
import { accessReaders, accessWriter } from "./reads";
import type { AccessReadApi } from "./reads";

const realClient = () =>
  createApiClient({ getAccessToken: () => "t", onUnauthorized: () => undefined });

const PRINCIPALS: AccessPrincipalsView = { principals: [] };
const ROLES: AccessRolesView = { roles: [] };
const TIER3: Tier3ApproversView = { approvers: [], pending_change: null };

describe("the access reads", () => {
  it("are ABSENT on the client this deployment ships", () => {
    const client = realClient();
    expect(accessReaders(client)).toBeNull();
    expect(accessWriter(client)).toBeNull();
  });

  it("are used the moment the governed client can serve them", async () => {
    const accessPrincipals = vi.fn(() => Promise.resolve(PRINCIPALS));
    const accessRoles = vi.fn(() => Promise.resolve(ROLES));
    const accessTier3Approvers = vi.fn(() => Promise.resolve(TIER3));
    const client = {
      ...realClient(),
      accessPrincipals,
      accessRoles,
      accessTier3Approvers,
    } as ApiClient & Partial<AccessReadApi>;

    const readers = accessReaders(client);
    expect(readers).not.toBeNull();
    await readers?.principals();
    await readers?.roles();
    await readers?.tier3();
    expect(accessPrincipals).toHaveBeenCalledTimes(1);
    expect(accessRoles).toHaveBeenCalledTimes(1);
    expect(accessTier3Approvers).toHaveBeenCalledTimes(1);
  });

  it("are ALL THREE or none — a partial answer serves no tab", () => {
    // They are one endpoint group. A Roles tab whose Tier-3 section silently had
    // no read would render an empty list indistinguishable from an absent one.
    const partial = {
      ...realClient(),
      accessPrincipals: () => Promise.resolve(PRINCIPALS),
      accessRoles: () => Promise.resolve(ROLES),
    } as ApiClient & Partial<AccessReadApi>;
    expect(accessReaders(partial)).toBeNull();
  });

  it("probe the WRITE separately: served lists with no write path offer no write", () => {
    const readOnly = {
      ...realClient(),
      accessPrincipals: () => Promise.resolve(PRINCIPALS),
      accessRoles: () => Promise.resolve(ROLES),
      accessTier3Approvers: () => Promise.resolve(TIER3),
    } as ApiClient & Partial<AccessReadApi>;
    expect(accessReaders(readOnly)).not.toBeNull();
    expect(accessWriter(readOnly)).toBeNull();
  });

  it("carry the change to the write, unaltered", async () => {
    const submitAccessChange = vi.fn(() =>
      Promise.resolve({ status: "pending" as const, request_id: "req-9" }),
    );
    const client = { ...realClient(), submitAccessChange } as ApiClient & Partial<AccessReadApi>;
    const write = accessWriter(client);
    expect(write).not.toBeNull();
    const result = await write?.({
      kind: "roles",
      principal_id: "alice@example.com",
      add_groups: ["ops"],
      remove_groups: [],
    });
    expect(submitAccessChange).toHaveBeenCalledWith({
      kind: "roles",
      principal_id: "alice@example.com",
      add_groups: ["ops"],
      remove_groups: [],
    });
    expect(result?.request_id).toBe("req-9");
  });

  it("call every read and the write ON the client, so `this` is the governed client", async () => {
    // A probe that detached the method would break any client whose call uses
    // its own fields — which every real one does, for the bearer and the base.
    const marked = {
      marker: "governed",
      accessPrincipals(this: { marker: string }) {
        return Promise.resolve({ principals: [{ principal_id: this.marker }] } as never);
      },
      accessRoles(this: { marker: string }) {
        return Promise.resolve({ roles: [{ group: this.marker }] } as never);
      },
      accessTier3Approvers(this: { marker: string }) {
        return Promise.resolve({ approvers: [{ principal_id: this.marker }] } as never);
      },
      submitAccessChange(this: { marker: string }) {
        return Promise.resolve({ status: "applied" as const, request_id: this.marker });
      },
    };
    const client = marked as unknown as ApiClient;
    const readers = accessReaders(client);
    expect((await readers?.principals())?.principals[0]?.principal_id).toBe("governed");
    expect((await readers?.roles())?.roles[0]?.group).toBe("governed");
    expect((await readers?.tier3())?.approvers[0]?.principal_id).toBe("governed");
    const write = accessWriter(client);
    const result = await write?.({
      kind: "tier3-approvers",
      add_principals: [],
      remove_principals: [],
    });
    expect(result?.request_id).toBe("governed");
  });
});
