// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Unit coverage for the console's ONE shared latest-request-wins guard:
// out-of-order supersession, plain failure, superseded-failure, and unmount.
// All deferred manually (no fake timers, no network — nothing to MSW-mock).

import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useLatestRequest } from "./useLatestRequest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useLatestRequest", () => {
  it("the latest of two overlapping requests wins — the older request resolves superseded even when it settles last", async () => {
    const { result } = renderHook(() => useLatestRequest());
    const runLatest = result.current;

    const a = deferred<string>();
    const b = deferred<string>();

    const outcomeAPromise = runLatest(() => a.promise);
    const outcomeBPromise = runLatest(() => b.promise);

    // B (the newer request) resolves FIRST.
    b.resolve("b-value");
    expect(await outcomeBPromise).toEqual({ kind: "success", value: "b-value" });

    // A (the older request) resolves LAST — it must be superseded, never
    // overwriting B's already-committed result.
    a.resolve("a-value");
    expect(await outcomeAPromise).toEqual({ kind: "superseded" });
  });

  it("a failed latest request reports kind=error with the thrown value", async () => {
    const { result } = renderHook(() => useLatestRequest());
    const runLatest = result.current;

    const boom = new Error("boom");
    const outcomePromise = runLatest(() => Promise.reject(boom));

    expect(await outcomePromise).toEqual({ kind: "error", error: boom });
  });

  it("a failure on a superseded request reports superseded, never error", async () => {
    const { result } = renderHook(() => useLatestRequest());
    const runLatest = result.current;

    const a = deferred<string>();
    const b = deferred<string>();

    const outcomeAPromise = runLatest(() => a.promise);
    const outcomeBPromise = runLatest(() => b.promise);

    // A (now superseded) REJECTS — this must not surface as an error outcome.
    a.reject(new Error("stale failure"));
    expect(await outcomeAPromise).toEqual({ kind: "superseded" });

    // B (the latest) still resolves normally.
    b.resolve("b-value");
    expect(await outcomeBPromise).toEqual({ kind: "success", value: "b-value" });
  });

  it("unmount supersedes all in-flight requests", async () => {
    const { result, unmount } = renderHook(() => useLatestRequest());
    const runLatest = result.current;

    const a = deferred<string>();
    const outcomeAPromise = runLatest(() => a.promise);

    unmount();
    a.resolve("a-value");

    expect(await outcomeAPromise).toEqual({ kind: "superseded" });
  });
});
