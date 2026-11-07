// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { renderHook } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { PrincipalState } from "../auth/PrincipalContext";
import {
  approvalOwnerReady,
  sameApprovalOwner,
  useApprovalOwner,
  useApprovalRequestGate,
} from "./approvalOwner";

let client = {} as ApiClient;
let principal: PrincipalState = { status: "loading", principal: null };

vi.mock("../auth/useApiClient", () => ({ useApiClient: () => client }));
vi.mock("../auth/PrincipalContext", () => ({ usePrincipal: () => principal }));

beforeEach(() => {
  client = {} as ApiClient;
  principal = { status: "loading", principal: null };
});

describe("approval owner", () => {
  it("compares client, principal status/id, and generation independently", () => {
    const sharedClient = {} as ApiClient;
    const exact = {
      client: sharedClient,
      principalStatus: "ok" as const,
      principalId: "alice",
      generation: "generation-1",
    };

    expect(sameApprovalOwner(exact, exact)).toBe(true);
    expect(sameApprovalOwner(exact, { ...exact, client: {} as ApiClient })).toBe(false);
    expect(sameApprovalOwner(exact, { ...exact, principalStatus: "loading" })).toBe(false);
    expect(sameApprovalOwner(exact, { ...exact, principalId: "bob" })).toBe(false);
    expect(sameApprovalOwner(exact, { ...exact, generation: "generation-2" })).toBe(false);
  });

  it("advances synchronously for exact client, principal status, and principal id changes", () => {
    const view = renderHook(() => useApprovalOwner());
    const loading = view.result.current;
    expect(approvalOwnerReady(loading)).toBe(false);

    principal = { status: "ok", principal: { id: "alice", groups: [] } };
    view.rerender();
    const alice = view.result.current;
    expect(alice.generation).not.toBe(loading.generation);
    expect(approvalOwnerReady(alice)).toBe(true);

    principal = { status: "ok", principal: { id: "bob", groups: [] } };
    view.rerender();
    const bob = view.result.current;
    expect(bob.generation).not.toBe(alice.generation);
    expect(sameApprovalOwner(alice, bob)).toBe(false);

    client = {} as ApiClient;
    view.rerender();
    expect(view.result.current.generation).not.toBe(bob.generation);
  });

  it("refuses retained owners and makes the newest same-request generation the only winner", () => {
    principal = { status: "ok", principal: { id: "alice", groups: [] } };
    const view = renderHook(
      ({ requestId }) => {
        const owner = useApprovalOwner();
        return { owner, gate: useApprovalRequestGate(owner, requestId) };
      },
      { initialProps: { requestId: "shared-id" } },
    );
    const alice = view.result.current.owner;
    const first = view.result.current.gate.begin(alice, "shared-id");
    const second = view.result.current.gate.begin(alice, "shared-id");
    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    expect(view.result.current.gate.owns(first!)).toBe(false);
    expect(view.result.current.gate.owns(second!)).toBe(true);

    principal = { status: "ok", principal: { id: "bob", groups: [] } };
    view.rerender({ requestId: "shared-id" });
    expect(view.result.current.gate.owns(second!)).toBe(false);
    expect(view.result.current.gate.begin(alice, "shared-id")).toBeNull();
  });

  it("refuses retained request ids after the route changes", () => {
    principal = { status: "ok", principal: { id: "alice", groups: [] } };
    const view = renderHook(
      ({ requestId }) => {
        const owner = useApprovalOwner();
        return { owner, gate: useApprovalRequestGate(owner, requestId) };
      },
      { initialProps: { requestId: "request-a" } },
    );
    const owner = view.result.current.owner;
    const requestA = view.result.current.gate.begin(owner, "request-a");
    expect(requestA).not.toBeNull();

    view.rerender({ requestId: "request-b" });
    expect(view.result.current.gate.owns(requestA!)).toBe(false);
    expect(view.result.current.gate.begin(owner, "request-a")).toBeNull();
    expect(view.result.current.gate.begin(owner, "request-b")).not.toBeNull();
  });

  it("closes retained callbacks in layout before later layout work can run", () => {
    principal = { status: "ok", principal: { id: "alice", groups: [] } };
    let retainedOwner: ReturnType<typeof useApprovalOwner> | null = null;
    let retainedResult: ReturnType<ReturnType<typeof useApprovalRequestGate>["begin"]> = null;
    const view = renderHook(() => {
      const owner = useApprovalOwner();
      const gate = useApprovalRequestGate(owner, "shared-id");
      useLayoutEffect(() => {
        if (retainedOwner !== null) {
          retainedResult = gate.begin(retainedOwner, "shared-id");
        }
      }, [gate, owner]);
      return owner;
    });
    retainedOwner = view.result.current;

    principal = { status: "ok", principal: { id: "bob", groups: [] } };
    view.rerender();
    expect(retainedResult).toBeNull();
  });
});
