// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Global test setup: jest-dom matchers + MSW node server lifecycle. Handlers
// are asserted to fully cover every request (onUnhandledRequest: "error") so a
// stray, ungoverned network call in a test fails loudly rather than silently.

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { resetWorkRecordCache } from "../landing/workStorage";
import { server } from "./mocks/server";

// With vitest globals:false, @testing-library/react's automatic afterEach
// cleanup does not self-register (it keys off global afterEach), so we unmount
// explicitly — otherwise DOM accumulates across tests in a file.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  // Browser storage is shared across a file's tests in jsdom. Two features now
  // persist there — the guided tour's first-run dismissal (localStorage) and the
  // work surface's visit mark + ask journal — so a test that dismisses the tour
  // or records an ask must not silently decide the next test's starting state.
  // The module-level snapshot caches are dropped with it, or a cleared store
  // would still read back through a stale cache.
  localStorage.clear();
  sessionStorage.clear();
  resetWorkRecordCache();
});
afterAll(() => server.close());
