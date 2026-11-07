// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-136 ontology/verified client tests. A NEW file (leaves client.test.ts to
// the concurrent console units). Proves the five T3-drawer methods hit the exact
// method + path, that verifiedInvoke sends EXACTLY {template_id,version,args}
// (server extra='forbid' compliance) and omits version when undefined, and that
// non-2xx surfaces ApiError with status+detail (503 ontology-disabled) while a
// 401 drives onUnauthorized like every other governed call.

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import {
  ONTOLOGY_CLASSES,
  ONTOLOGY_SHAPES,
  ONTOLOGY_VIOLATIONS,
  VERIFIED_INVOKE_RESULT,
  VERIFIED_TEMPLATES,
} from "../test/mocks/ontologyFixtures";
import { ApiError, createApiClient } from "./client";

const TOKEN = "tok-onto";

function client(onUnauthorized = () => {}) {
  return createApiClient({ getAccessToken: () => TOKEN, onUnauthorized });
}

describe("ontology read methods", () => {
  it("ontologyClasses GETs /v1/ontology/classes with the bearer", async () => {
    let seenPath = "";
    let seenAuth: string | null = null;
    server.use(
      http.get("*/api/v1/ontology/classes", ({ request }) => {
        seenPath = new URL(request.url).pathname;
        seenAuth = request.headers.get("authorization");
        return HttpResponse.json(ONTOLOGY_CLASSES);
      }),
    );
    const res = await client().ontologyClasses();
    expect(seenPath).toBe("/api/v1/ontology/classes");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(res.total).toBe(8);
  });

  it("ontologyShapes GETs /v1/ontology/shapes", async () => {
    let seenPath = "";
    server.use(
      http.get("*/api/v1/ontology/shapes", ({ request }) => {
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(ONTOLOGY_SHAPES);
      }),
    );
    await client().ontologyShapes();
    expect(seenPath).toBe("/api/v1/ontology/shapes");
  });

  it("ontologyViolations GETs /v1/ontology/violations", async () => {
    let seenPath = "";
    server.use(
      http.get("*/api/v1/ontology/violations", ({ request }) => {
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(ONTOLOGY_VIOLATIONS);
      }),
    );
    const res = await client().ontologyViolations();
    expect(seenPath).toBe("/api/v1/ontology/violations");
    expect(res.configured).toBe(true);
  });

  it("verifiedTemplates GETs /v1/verified/templates", async () => {
    let seenPath = "";
    server.use(
      http.get("*/api/v1/verified/templates", ({ request }) => {
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(VERIFIED_TEMPLATES);
      }),
    );
    await client().verifiedTemplates();
    expect(seenPath).toBe("/api/v1/verified/templates");
  });

  it("surfaces ApiError(503, 'ontology-disabled') on a disabled tier", async () => {
    server.use(
      http.get("*/api/v1/ontology/classes", () =>
        HttpResponse.json({ detail: "ontology-disabled" }, { status: 503 }),
      ),
    );
    await expect(client().ontologyClasses()).rejects.toMatchObject({
      status: 503,
      detail: "ontology-disabled",
    });
  });

  it("invokes onUnauthorized and throws ApiError(401) on 401", async () => {
    server.use(
      http.get("*/api/v1/ontology/classes", () => new HttpResponse(null, { status: 401 })),
    );
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).ontologyClasses()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});

describe("verifiedInvoke", () => {
  it("POSTs EXACTLY {template_id,version,args} to /v1/verified/invoke", async () => {
    let seenPath = "";
    let seenBody: unknown = null;
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        seenPath = new URL(request.url).pathname;
        seenBody = await request.json();
        return HttpResponse.json(VERIFIED_INVOKE_RESULT);
      }),
    );
    await client().verifiedInvoke({
      template_id: "accounts-by-name",
      version: 2,
      args: { q_name: "Acme Corp" },
    });
    expect(seenPath).toBe("/api/v1/verified/invoke");
    expect(seenBody).toEqual({
      template_id: "accounts-by-name",
      version: 2,
      args: { q_name: "Acme Corp" },
    });
  });

  it("omits version when undefined (never sends a null/extra key)", async () => {
    let seenBody: Record<string, unknown> | null = null;
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        seenBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(VERIFIED_INVOKE_RESULT);
      }),
    );
    await client().verifiedInvoke({ template_id: "accounts-directory", args: {} });
    // The exact-equal body proves no `version` (or any other) key is sent.
    expect(seenBody).toEqual({ template_id: "accounts-directory", args: {} });
    expect(seenBody).not.toHaveProperty("version");
  });

  it("surfaces ApiError with status+detail on a 422 invalid-args", async () => {
    server.use(
      http.post("*/api/v1/verified/invoke", () =>
        HttpResponse.json({ detail: "invalid-args" }, { status: 422 }),
      ),
    );
    await expect(
      client().verifiedInvoke({ template_id: "accounts-by-name", version: 2, args: {} }),
    ).rejects.toMatchObject({ status: 422, detail: "invalid-args" });
  });
});
