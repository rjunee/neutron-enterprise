// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-134 graph-surface client tests. A NEW file (leaves client.test.ts to the
// concurrent NE-138/139 units). Proves the three graph methods send the correct
// method + path + query/body, carry the OIDC bearer, and surface 401/errors like
// every other governed call.

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { ApiError, createApiClient } from "./client";
import type {
  GraphEntityFacetsWire,
  GraphEntityListWire,
  GraphPathsWire,
  SubgraphWire,
} from "./types";

const TOKEN = "tok-graph";
const EMPTY_SUB: SubgraphWire = { nodes: [], edges: [], truncated: false };
const EMPTY_PATHS: GraphPathsWire = { paths: [], truncated: false };
const ENTITY_PAGE: GraphEntityListWire = {
  entities: [
    {
      key: "entity-zulu",
      entity_class: "Account",
      display_name: "Zulu Holdings",
      source_system: "salesforce",
      assurance: "source",
    },
    {
      key: "entity-alpha",
      entity_class: "Person",
      display_name: "Alpha Contact",
      source_system: "m365",
      assurance: "confirmed",
    },
  ],
  next_cursor: "opaque/+ cursor",
};

function client(onUnauthorized = () => {}) {
  return createApiClient({ getAccessToken: () => TOKEN, onUnauthorized });
}

describe("graphEntities", () => {
  it("GETs the complete discovery filter set verbatim and preserves the served page order", async () => {
    let seen: URL | undefined;
    let seenAuth: string | null = null;
    server.use(
      http.get("*/api/v1/graph/entities", ({ request }) => {
        seen = new URL(request.url);
        seenAuth = request.headers.get("authorization");
        return HttpResponse.json(ENTITY_PAGE);
      }),
    );

    await expect(
      client().graphEntities({
        q: " Acme + West ",
        entity_class: "Account",
        source: "M365 SharePoint",
        assurance: "confirmed",
        cursor: "opaque/+ cursor",
        limit: 25,
      }),
    ).resolves.toEqual(ENTITY_PAGE);

    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seen!.pathname).toBe("/api/v1/graph/entities");
    expect([...seen!.searchParams.entries()]).toEqual([
      ["q", " Acme + West "],
      ["entity_class", "Account"],
      ["source", "M365 SharePoint"],
      ["assurance", "confirmed"],
      ["cursor", "opaque/+ cursor"],
      ["limit", "25"],
    ]);
  });

  it("leaves an omitted query to server defaults and forwards the AbortSignal", async () => {
    let seen: URL | undefined;
    server.use(
      http.get("*/api/v1/graph/entities", ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json({ entities: [], next_cursor: null });
      }),
    );
    const controller = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await client().graphEntities(undefined, controller.signal);

    expect(seen!.search).toBe("");
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal }),
    );
    fetchSpy.mockRestore();
  });

  it("surfaces the safe server detail for an invalid opaque cursor", async () => {
    server.use(
      http.get("*/api/v1/graph/entities", () =>
        HttpResponse.json({ detail: "invalid-cursor" }, { status: 422 }),
      ),
    );

    await expect(client().graphEntities({ cursor: "stale" })).rejects.toMatchObject({
      status: 422,
      detail: "invalid-cursor",
    });
  });
});

describe("graphFacets", () => {
  it("GETs /v1/graph/facets with the bearer, no filters, and returns the served lists verbatim", async () => {
    const served: GraphEntityFacetsWire = {
      entity_classes: ["AutoLoan", "Customer", "Document"],
      source_systems: ["m365-sharepoint"],
      truncated: false,
    };
    let seen: URL | undefined;
    let seenAuth: string | null = null;
    server.use(
      http.get("*/api/v1/graph/facets", ({ request }) => {
        seen = new URL(request.url);
        seenAuth = request.headers.get("authorization");
        return HttpResponse.json(served);
      }),
    );

    await expect(client().graphFacets()).resolves.toEqual(served);
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seen!.pathname).toBe("/api/v1/graph/facets");
    // The vocabulary is a property of the principal, not of the current
    // filters — so this call carries no query string at all.
    expect(seen!.search).toBe("");
  });

  it("forwards the abort signal", async () => {
    const controller = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    server.use(
      http.get("*/api/v1/graph/facets", () =>
        HttpResponse.json({ entity_classes: [], source_systems: [], truncated: false }),
      ),
    );

    await client().graphFacets(controller.signal);

    expect(fetchSpy).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal }),
    );
    fetchSpy.mockRestore();
  });

  it("surfaces the honest 404 when the graph tier is off", async () => {
    server.use(
      http.get("*/api/v1/graph/facets", () =>
        HttpResponse.json({ detail: "graph tier not enabled" }, { status: 404 }),
      ),
    );

    await expect(client().graphFacets()).rejects.toMatchObject({
      status: 404,
      detail: "graph tier not enabled",
    });
  });
});

describe("graphQuery", () => {
  it("POSTs /v1/graph/query with the anchor body and bearer, omitting empty anchor sets", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    let seenBody: unknown = null;
    server.use(
      http.post("*/api/v1/graph/query", async ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        seenBody = await request.json();
        return HttpResponse.json(EMPTY_SUB);
      }),
    );
    await client().graphQuery({ anchor_keys: ["k1", "k2"], hops: 2 });
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/graph/query");
    expect(seenBody).toEqual({ anchor_keys: ["k1", "k2"], hops: 2 });
  });

  it("includes anchor_citations when present", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post("*/api/v1/graph/query", async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json(EMPTY_SUB);
      }),
    );
    await client().graphQuery({ anchor_citations: ["ref/doc-alpha"] });
    expect(seenBody).toEqual({ anchor_citations: ["ref/doc-alpha"] });
  });

  it("invokes onUnauthorized and throws ApiError(401) on 401", async () => {
    server.use(http.post("*/api/v1/graph/query", () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).graphQuery({ anchor_keys: ["k"] })).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("surfaces the server detail on a non-2xx (502 fail-closed)", async () => {
    server.use(
      http.post("*/api/v1/graph/query", () =>
        HttpResponse.json({ detail: "GraphBackendError" }, { status: 502 }),
      ),
    );
    await expect(client().graphQuery({ anchor_keys: ["k"] })).rejects.toMatchObject({
      status: 502,
      detail: "GraphBackendError",
    });
  });
});

describe("graphNeighborhood", () => {
  it("GETs /v1/graph/neighborhood with an encoded entity + hops query", async () => {
    let seenPath = "";
    let seenEntity: string | null = null;
    let seenHops: string | null = null;
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        const url = new URL(request.url);
        seenPath = url.pathname;
        seenEntity = url.searchParams.get("entity");
        seenHops = url.searchParams.get("hops");
        return HttpResponse.json(EMPTY_SUB);
      }),
    );
    await client().graphNeighborhood("acct/acme:1", { hops: 1 });
    expect(seenPath).toBe("/api/v1/graph/neighborhood");
    expect(seenEntity).toBe("acct/acme:1");
    expect(seenHops).toBe("1");
  });
});

describe("graphPaths", () => {
  it("POSTs /v1/graph/paths with the {from,to,max_hops,limit} wire body", async () => {
    let seenPath = "";
    let seenBody: unknown = null;
    server.use(
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        seenPath = new URL(request.url).pathname;
        seenBody = await request.json();
        return HttpResponse.json(EMPTY_PATHS);
      }),
    );
    await client().graphPaths("a-key", "b-key");
    expect(seenPath).toBe("/api/v1/graph/paths");
    expect(seenBody).toEqual({ from: "a-key", to: "b-key", max_hops: 4, limit: 20 });
  });
});
