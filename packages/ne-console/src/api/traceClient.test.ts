// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-167b trace-client tests. A NEW file (mirrors graphClient.test.ts's
// convention of a dedicated file per unit). Proves trace() sends the correct
// method + path (with encodeURIComponent on a slashed id), carries the OIDC
// bearer, returns the parsed TraceAssemblyView, and surfaces 401/403/404 like
// every other governed call.

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { TRACE_ASSEMBLY, TRACE_ID } from "../test/mocks/traceFixtures";
import { ApiError, createApiClient } from "./client";

const TOKEN = "tok-trace";

function client(onUnauthorized = () => {}) {
  return createApiClient({ getAccessToken: () => TOKEN, onUnauthorized });
}

describe("trace", () => {
  it("GETs /v1/trace/{trace_id} with the bearer header and returns the parsed view", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/trace/:traceId", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(TRACE_ASSEMBLY);
      }),
    );
    const result = await client().trace(TRACE_ID);
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe(`/api/v1/trace/${TRACE_ID}`);
    expect(result).toEqual(TRACE_ASSEMBLY);
  });

  it("encodeURIComponent-encodes a trace id containing a slash", async () => {
    let seenPath = "";
    server.use(
      http.get("*/api/v1/trace/:traceId", ({ request }) => {
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(TRACE_ASSEMBLY);
      }),
    );
    await client().trace("weird/trace-id");
    expect(seenPath).toBe("/api/v1/trace/weird%2Ftrace-id");
  });

  it("maps a 404 to ApiError{status:404}", async () => {
    server.use(
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
      ),
    );
    await expect(client().trace("unknown-id")).rejects.toMatchObject({ status: 404 });
    await expect(client().trace("unknown-id")).rejects.toBeInstanceOf(ApiError);
  });

  it("maps a 403 to ApiError{status:403}", async () => {
    server.use(
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    await expect(client().trace(TRACE_ID)).rejects.toMatchObject({ status: 403 });
  });

  it("invokes onUnauthorized exactly once and throws ApiError(401) on a 401", async () => {
    server.use(http.get("*/api/v1/trace/:traceId", () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).trace(TRACE_ID)).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
