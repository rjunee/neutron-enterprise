// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { ApiError } from "./client";
import { createOntologyInspectionClient } from "./ontologyInspectionClient";

const TOKEN = "ontology-inspection-token";

function client(onUnauthorized = () => {}) {
  return createOntologyInspectionClient({ getAccessToken: () => TOKEN, onUnauthorized });
}

describe("createOntologyInspectionClient", () => {
  it("reads the exact model and issue-list routes with the primary bearer", async () => {
    const seen: string[] = [];
    const auth: Array<string | null> = [];
    server.use(
      http.get("*/api/v1/ontology/model", ({ request }) => {
        seen.push(new URL(request.url).pathname);
        auth.push(request.headers.get("authorization"));
        return HttpResponse.json({ marker: "model" });
      }),
      http.get("*/api/v1/ontology/violations", ({ request }) => {
        seen.push(new URL(request.url).pathname);
        auth.push(request.headers.get("authorization"));
        return HttpResponse.json({ configured: false, count: 0, entries: [] });
      }),
    );

    await client().readModel();
    await client().listIssues();

    expect(seen).toEqual(["/api/v1/ontology/model", "/api/v1/ontology/violations"]);
    expect(auth).toEqual([`Bearer ${TOKEN}`, `Bearer ${TOKEN}`]);
  });

  it("encodes the issue identity as one path segment", async () => {
    let path = "";
    server.use(
      http.get("*/api/v1/ontology/violations/:id", ({ request }) => {
        path = new URL(request.url).pathname;
        return HttpResponse.json({ issue_id: "issue 1/with?special" });
      }),
    );

    await client().readIssue("issue 1/with?special");
    expect(path).toBe("/api/v1/ontology/violations/issue%201%2Fwith%3Fspecial");
  });

  it("preserves a governed refusal status and safe detail", async () => {
    server.use(
      http.get("*/api/v1/ontology/violations/:id", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );

    await expect(client().readIssue("issue-1")).rejects.toMatchObject({
      status: 403,
      detail: "not authorized",
    });
  });

  it("invokes the shared re-auth behavior exactly once on 401", async () => {
    server.use(http.get("*/api/v1/ontology/model", () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).readModel()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("forwards cancellation to each bounded GET", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(client().readModel(controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
