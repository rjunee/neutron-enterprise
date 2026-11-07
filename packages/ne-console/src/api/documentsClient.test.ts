// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { createApiClient } from "./client";
import type { DocumentStageArtifactWire, DocumentStageNameWire } from "./types";

const TOKEN = "documents-token";
const HASH = "a".repeat(64);
const FINGERPRINT = "b".repeat(64);

function client() {
  return createApiClient({ getAccessToken: () => TOKEN, onUnauthorized: () => undefined });
}

function listResponse() {
  return {
    documents: [
      {
        document_id: "opaque/document+id",
        display_name: "Servicing SOP.docx",
        source_system: "M365-SharePoint",
        document_type: "document",
        version_seq: 2,
        content_hash: HASH,
        ingested_at: "2026-08-20T12:00:00Z",
        indexing: false,
      },
    ],
    next_cursor: "opaque-cursor",
    document_types: ["document"],
    source_systems: ["M365-SharePoint"],
  };
}

function stageResponse(stage: DocumentStageNameWire, artifacts?: unknown) {
  const value: Record<string, unknown> = {
    document_id: "opaque/document+id",
    version: 2,
    content_hash: HASH,
    document_type: "document",
    stage,
    status: "complete",
    occurred_at: "2026-08-20T12:00:00Z",
    audit_seq: 42,
    detail: `${stage} complete`,
    facts: [],
    comparison: { state: "available", predecessor_version: 1 },
    graph_evidence:
      stage === "graph"
        ? {
            state: "unavailable",
            reason: "version-bound graph evidence receipt unavailable",
            entities: [],
            relationships: [],
            truncated: false,
          }
        : null,
  };
  if (arguments.length > 1) value.artifacts = artifacts;
  return value;
}

function unboundAuditStageResponse(stage: DocumentStageNameWire) {
  const artifact =
    stage === "chunk"
      ? availableChunk({
          state: "unavailable",
          identifier: null,
          size_bytes: null,
          reason: "unbound_audit_receipt",
        })
      : stage === "embed"
        ? availableEmbedding({
            state: "unavailable",
            artifact_count: null,
            dimensions: null,
            model: null,
            index_reference: null,
            fingerprint: null,
            reason: "unbound_audit_receipt",
          })
        : null;
  const value = stageResponse(stage, artifact === null ? [] : [artifact]);
  value.status = "unavailable";
  value.audit_seq = null;
  value.occurred_at = null;
  value.detail = "evidence unavailable: no bound audit receipt";
  value.facts = [
    {
      key: "future_fact",
      label: "Future fact",
      value: null,
      available: false,
      internal: "must-not-cross-client",
    },
  ];
  value.semantic_chunks =
    stage === "chunk"
      ? {
          state: "unavailable",
          reason: "unbound_audit_receipt",
          chunks: [],
          truncated: false,
        }
      : null;
  value.graph_evidence =
    stage === "graph"
      ? {
          state: "unavailable",
          reason: "evidence unavailable: no bound audit receipt",
          entities: [],
          relationships: [],
          truncated: false,
        }
      : null;
  return value;
}

function availableEmbedding(
  overrides: Partial<DocumentStageArtifactWire> = {},
): DocumentStageArtifactWire {
  return {
    kind: "embedding",
    label: "Vector artifact receipt",
    state: "available",
    ordinal: 0,
    identifier: null,
    size_bytes: null,
    artifact_count: 1,
    dimensions: 1536,
    model: "private/embed-v1",
    index_reference: `sha256:${FINGERPRINT}`,
    fingerprint: FINGERPRINT,
    numeric_preview: null,
    reason: null,
    ...overrides,
  };
}

function availableChunk(
  overrides: Partial<DocumentStageArtifactWire> = {},
): DocumentStageArtifactWire {
  return {
    kind: "chunk",
    label: "Record chunk",
    state: "available",
    ordinal: 0,
    identifier: "opaque-record-chunk",
    size_bytes: 2048,
    artifact_count: null,
    dimensions: null,
    model: null,
    index_reference: null,
    fingerprint: null,
    numeric_preview: null,
    reason: null,
    ...overrides,
  };
}

describe("Documents API client", () => {
  it("sends the complete list filter set verbatim and returns the server page", async () => {
    let seen: URL | undefined;
    server.use(
      http.get("*/api/v1/documents", ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(listResponse());
      }),
    );

    await expect(
      client().listDocuments({
        q: " Servicing + SOP ",
        type: "document",
        source: "M365-SharePoint",
        cursor: "opaque/+ cursor",
        limit: 25,
      }),
    ).resolves.toEqual(listResponse());

    expect([...seen!.searchParams.entries()]).toEqual([
      ["q", " Servicing + SOP "],
      ["type", "document"],
      ["source", "M365-SharePoint"],
      ["cursor", "opaque/+ cursor"],
      ["limit", "25"],
    ]);
  });

  it("leaves an omitted list query to the server default and forwards AbortSignal", async () => {
    let seen: URL | undefined;
    server.use(
      http.get("*/api/v1/documents", ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json({ ...listResponse(), next_cursor: null });
      }),
    );
    const controller = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await client().listDocuments(undefined, controller.signal);

    expect(seen!.search).toBe("");
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal }),
    );
    fetchSpy.mockRestore();
  });

  it("encodes opaque journey and stage paths and forwards exact selectors", async () => {
    const urls: URL[] = [];
    const journeyController = new AbortController();
    const stageController = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    server.use(
      http.get("*/api/v1/documents/:documentId/journey", ({ request }) => {
        urls.push(new URL(request.url));
        return HttpResponse.json({
          document_id: "folder/doc +?#",
          display_name: "Document.docx",
          source_system: "m365-sharepoint",
          document_type: "document",
          selected_version: 4,
          selected_content_hash: HASH,
          selected_content: "visible content",
          diff: null,
          comparison: { state: "first_version", predecessor_version: null },
          stages: [],
          versions: [],
        });
      }),
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", ({ request }) => {
        urls.push(new URL(request.url));
        return HttpResponse.json(stageResponse("parse", []));
      }),
    );

    await client().documentJourney(
      "folder/doc +?#",
      {
        as_of: "2026-08-20T12:30:00-04:00",
      },
      journeyController.signal,
    );
    await client().documentStageEvidence("folder/doc +?#", 4, "parse", stageController.signal);

    expect(urls[0]!.pathname).toBe("/api/v1/documents/folder%2Fdoc%20%2B%3F%23/journey");
    expect(urls[0]!.searchParams.get("as_of")).toBe("2026-08-20T12:30:00-04:00");
    expect(urls[1]!.pathname).toBe(
      "/api/v1/documents/folder%2Fdoc%20%2B%3F%23/versions/4/stages/parse",
    );
    expect(fetchSpy.mock.calls[0]![1]).toEqual(
      expect.objectContaining({ signal: journeyController.signal }),
    );
    expect(fetchSpy.mock.calls[1]![1]).toEqual(
      expect.objectContaining({ signal: stageController.signal }),
    );
    fetchSpy.mockRestore();
  });

  it("preserves complete available PR 673 record-chunk and embedding receipts", async () => {
    const chunk = availableChunk();
    const embedding = availableEmbedding();
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", ({ params }) =>
        params.stage === "chunk"
          ? HttpResponse.json(stageResponse("chunk", [chunk]))
          : HttpResponse.json(stageResponse("embed", [embedding])),
      ),
    );

    const chunkEvidence = await client().documentStageEvidence("doc", 2, "chunk");
    const embedEvidence = await client().documentStageEvidence("doc", 2, "embed");

    expect(chunkEvidence.artifacts).toEqual([chunk]);
    expect(embedEvidence.artifacts).toEqual([embedding]);
  });

  it("admits only finite bounded embedding-owned numeric previews", async () => {
    const embedding = availableEmbedding({
      dimensions: 12,
      numeric_preview: [0.25, -1.5, 3],
    });
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", () =>
        HttpResponse.json(stageResponse("embed", [embedding])),
      ),
    );
    expect((await client().documentStageEvidence("doc", 2, "embed")).artifacts[0]).toEqual(
      embedding,
    );

    for (const numeric_preview of [
      [],
      [Number.NaN],
      Array.from({ length: 9 }, (_, index) => index),
    ]) {
      server.use(
        http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", () =>
          HttpResponse.json(
            stageResponse("embed", [availableEmbedding({ dimensions: 12, numeric_preview })]),
          ),
        ),
      );
      await expect(client().documentStageEvidence("doc", 2, "embed")).rejects.toThrow(TypeError);
    }
  });

  it.each(["chunk", "embed", "graph"] as const)(
    "admits exact audit-unbound %s truth only through the production normalizer",
    async (stage) => {
      server.use(
        http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", () =>
          HttpResponse.json(unboundAuditStageResponse(stage)),
        ),
      );

      const result = await client().documentStageEvidence("doc", 2, stage);

      expect(result).toMatchObject({
        stage,
        status: "unavailable",
        occurred_at: null,
        audit_seq: null,
        detail: "evidence unavailable: no bound audit receipt",
        facts: [{ key: "future_fact", value: null, available: false }],
      });
      expect(JSON.stringify(result.facts)).not.toContain("must-not-cross-client");
      if (stage === "chunk") {
        expect(result.semantic_chunks).toEqual({
          state: "unavailable",
          reason: "unbound_audit_receipt",
          chunks: [],
          truncated: false,
        });
      } else if (stage === "embed") {
        expect(result.artifacts[0]).toMatchObject({
          state: "unavailable",
          reason: "unbound_audit_receipt",
          identifier: null,
          artifact_count: null,
          dimensions: null,
          model: null,
          fingerprint: null,
        });
      } else {
        expect(result.graph_evidence).toEqual({
          state: "unavailable",
          reason: "evidence unavailable: no bound audit receipt",
          entities: [],
          relationships: [],
          truncated: false,
        });
      }
    },
  );

  it.each([
    [
      "non-null audit",
      "chunk",
      (value: Record<string, unknown>) => {
        value.audit_seq = 42;
      },
    ],
    [
      "complete status",
      "embed",
      (value: Record<string, unknown>) => {
        value.status = "complete";
      },
    ],
    [
      "unmasked future fact",
      "graph",
      (value: Record<string, unknown>) => {
        value.facts = [{ key: "future", label: "Future", value: "secret", available: true }];
      },
    ],
    [
      "leaked occurred-at timestamp",
      "parse",
      (value: Record<string, unknown>) => {
        value.occurred_at = "2026-08-20T12:00:00Z";
      },
    ],
    [
      "old semantic reason",
      "chunk",
      (value: Record<string, unknown>) => {
        (value.semantic_chunks as Record<string, unknown>).reason = "semantic_chunks_not_recorded";
      },
    ],
    [
      "substring semantic reason",
      "chunk",
      (value: Record<string, unknown>) => {
        (value.semantic_chunks as Record<string, unknown>).reason = "prefix-unbound_audit_receipt";
      },
    ],
    [
      "old artifact reason",
      "embed",
      (value: Record<string, unknown>) => {
        ((value.artifacts as Record<string, unknown>[])[0] as Record<string, unknown>).reason =
          "not_recorded";
      },
    ],
    [
      "old graph reason",
      "graph",
      (value: Record<string, unknown>) => {
        (value.graph_evidence as Record<string, unknown>).reason =
          "version-bound graph evidence receipt unavailable";
      },
    ],
    [
      "cross-admitted semantic graph reason",
      "graph",
      (value: Record<string, unknown>) => {
        (value.graph_evidence as Record<string, unknown>).reason = "unbound_audit_receipt";
      },
    ],
  ] as const)("rejects audit-unbound %s", async (_name, stage, mutate) => {
    const response = unboundAuditStageResponse(stage);
    mutate(response);
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", () =>
        HttpResponse.json(response),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, stage)).rejects.toThrow(TypeError);
  });

  it("accepts only bounded safe semantic chunk descriptors on CHUNK evidence", async () => {
    const response = stageResponse("chunk", [availableChunk()]);
    response.semantic_chunks = {
      state: "available",
      reason: null,
      chunks: [
        {
          identifier: "opaque-document:0000",
          size_bytes: 321,
          acl: ["must-not-cross-client"],
          source_ref: "must-not-cross-client",
        },
      ],
      truncated: false,
    };
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/chunk", () =>
        HttpResponse.json(response),
      ),
    );

    const result = await client().documentStageEvidence("doc", 2, "chunk");

    expect(result.semantic_chunks).toEqual({
      state: "available",
      reason: null,
      chunks: [{ identifier: "opaque-document:0000", size_bytes: 321 }],
      truncated: false,
    });
    expect(JSON.stringify(result.semantic_chunks)).not.toContain("must-not-cross-client");
  });

  it.each([
    [
      "semantic projection on PARSE",
      "parse",
      {
        state: "unavailable",
        reason: "semantic_chunks_not_recorded",
        chunks: [],
        truncated: false,
      },
    ],
    [
      "available with no rows",
      "chunk",
      { state: "available", reason: null, chunks: [], truncated: false },
    ],
    [
      "unbounded rows",
      "chunk",
      {
        state: "available",
        reason: null,
        chunks: Array.from({ length: 101 }, (_, index) => ({
          identifier: `chunk-${index}`,
          size_bytes: index,
        })),
        truncated: true,
      },
    ],
    [
      "malformed identifier",
      "chunk",
      {
        state: "available",
        reason: null,
        chunks: [{ identifier: "", size_bytes: 1 }],
        truncated: false,
      },
    ],
  ] as const)("fails closed for %s", async (_name, stage, semanticChunks) => {
    const response = stageResponse(stage, stage === "chunk" ? [availableChunk()] : []);
    response.semantic_chunks = semanticChunks;
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", () =>
        HttpResponse.json(response),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, stage)).rejects.toThrow(
      "malformed document semantic chunk projection",
    );
  });

  it("normalizes only an absent legacy artifacts property without inventing receipts", async () => {
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", ({ params }) =>
        HttpResponse.json(stageResponse(params.stage as DocumentStageNameWire)),
      ),
    );

    const chunk = await client().documentStageEvidence("doc", 2, "chunk");
    const embed = await client().documentStageEvidence("doc", 2, "embed");
    const graph = await client().documentStageEvidence("doc", 2, "graph");

    expect(chunk.artifacts).toEqual([
      {
        kind: "chunk",
        label: "Record chunk",
        state: "unavailable",
        ordinal: 0,
        identifier: null,
        size_bytes: null,
        artifact_count: null,
        dimensions: null,
        model: null,
        index_reference: null,
        fingerprint: null,
        numeric_preview: null,
        reason: "not_recorded",
      },
    ]);
    expect(embed.artifacts[0]).toMatchObject({
      kind: "embedding",
      state: "unavailable",
      reason: "not_recorded",
    });
    expect(graph.artifacts).toEqual([]);
    expect(chunk.semantic_chunks).toEqual({
      state: "unavailable",
      reason: "semantic_chunks_not_recorded",
      chunks: [],
      truncated: false,
    });
    expect(embed.semantic_chunks).toBeNull();
  });

  it("preserves skipped stage truth when legacy CHUNK and EMBED responses omit artifacts", async () => {
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/:stage", ({ params }) => {
        const stage = params.stage as DocumentStageNameWire;
        const response = stageResponse(stage);
        response.status = "skipped";
        response.detail = `${stage} capability unavailable`;
        return HttpResponse.json(response);
      }),
    );

    const chunk = await client().documentStageEvidence("doc", 2, "chunk");
    const embed = await client().documentStageEvidence("doc", 2, "embed");

    expect(chunk.artifacts[0]).toMatchObject({
      kind: "chunk",
      state: "skipped",
      reason: "chunk capability unavailable",
    });
    expect(embed.artifacts[0]).toMatchObject({
      kind: "embedding",
      state: "skipped",
      reason: "embed capability unavailable",
    });
  });

  it("normalizes only safe fields from available version-bound graph evidence", async () => {
    const graph = stageResponse("graph", []);
    graph.graph_evidence = {
      state: "available",
      reason: null,
      entities: [
        {
          key: "opaque/entity+1",
          entity_class: "Procedure",
          display_name: "Servicing procedure",
          source_system: "m365-sharepoint",
          source_ref: "must-not-cross-client",
          source_acl_hash: "must-not-cross-client",
        },
      ],
      relationships: [
        {
          key: "opaque/relationship+1",
          edge_kind: "REFERENCES",
          subject: {
            key: "opaque/entity+1",
            entity_class: "Procedure",
            display_name: "Servicing procedure",
            source_system: "m365-sharepoint",
            acl: ["must-not-cross-client"],
          },
          object: {
            key: "opaque/entity+2",
            entity_class: "Policy",
            display_name: "Servicing policy",
            source_system: "m365-sharepoint",
            source_snapshot_hash: "must-not-cross-client",
          },
          audit_event_id: "must-not-cross-client",
        },
      ],
      truncated: true,
    };
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/graph", () =>
        HttpResponse.json(graph),
      ),
    );

    const result = await client().documentStageEvidence("doc", 2, "graph");

    expect(result.graph_evidence).toEqual({
      state: "available",
      reason: null,
      entities: [
        {
          key: "opaque/entity+1",
          entity_class: "Procedure",
          display_name: "Servicing procedure",
          source_system: "m365-sharepoint",
        },
      ],
      relationships: [
        {
          key: "opaque/relationship+1",
          edge_kind: "REFERENCES",
          subject: {
            key: "opaque/entity+1",
            entity_class: "Procedure",
            display_name: "Servicing procedure",
            source_system: "m365-sharepoint",
          },
          object: {
            key: "opaque/entity+2",
            entity_class: "Policy",
            display_name: "Servicing policy",
            source_system: "m365-sharepoint",
          },
        },
      ],
      truncated: true,
    });
    expect(JSON.stringify(result)).not.toMatch(/source_ref|acl_hash|snapshot_hash|audit_event_id/);
  });

  it.each([
    ["empty", "no visible graph evidence"],
    ["skipped", "graph capability disabled"],
    ["unavailable", "version-bound graph evidence receipt unavailable"],
  ] as const)("preserves the graph %s state without inventing rows", async (state, reason) => {
    const graph = stageResponse("graph", []);
    if (state === "skipped") {
      graph.status = "skipped";
      graph.detail = reason;
    }
    graph.graph_evidence = { state, reason, entities: [], relationships: [], truncated: false };
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/graph", () =>
        HttpResponse.json(graph),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, "graph")).resolves.toMatchObject({
      graph_evidence: { state, reason, entities: [], relationships: [], truncated: false },
    });
  });

  it.each([
    ["missing", undefined],
    ["null", null],
    [
      "withheld",
      { state: "withheld", reason: "hidden", entities: [], relationships: [], truncated: false },
    ],
    [
      "available without rows",
      { state: "available", reason: null, entities: [], relationships: [], truncated: false },
    ],
    [
      "empty with rows",
      {
        state: "empty",
        reason: "no visible graph evidence",
        entities: [{ key: "k", entity_class: "C", display_name: "N", source_system: "s" }],
        relationships: [],
        truncated: false,
      },
    ],
    [
      "malformed entity",
      {
        state: "available",
        reason: null,
        entities: [{ key: "k" }],
        relationships: [],
        truncated: false,
      },
    ],
    [
      "skipped state with complete stage",
      {
        state: "skipped",
        reason: "graph capability disabled",
        entities: [],
        relationships: [],
        truncated: false,
      },
    ],
  ])("fails closed for a %s graph evidence projection", async (_name, graphEvidence) => {
    const graph = stageResponse("graph", []);
    if (graphEvidence === undefined) delete graph.graph_evidence;
    else graph.graph_evidence = graphEvidence;
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/graph", () =>
        HttpResponse.json(graph),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, "graph")).rejects.toThrow(
      "malformed document graph evidence projection",
    );
  });

  it("rejects graph evidence on any non-GRAPH stage", async () => {
    const parse = stageResponse("parse", []);
    parse.graph_evidence = {
      state: "empty",
      reason: "no visible graph evidence",
      entities: [],
      relationships: [],
      truncated: false,
    };
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/parse", () =>
        HttpResponse.json(parse),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, "parse")).rejects.toThrow(
      "malformed document graph evidence projection",
    );
  });

  it.each([
    ["non-array", null],
    ["partial", [{ ...availableEmbedding(), reason: undefined }]],
    ["unknown state", [availableEmbedding({ state: "unknown" as "available" })]],
    ["inconsistent available fields", [availableEmbedding({ dimensions: null })]],
    ["inconsistent index reference", [availableEmbedding({ index_reference: "opaque" })]],
    ["empty current embed projection", []],
  ])("fails closed for a present %s artifact projection", async (_name, artifacts) => {
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/embed", () =>
        HttpResponse.json(stageResponse("embed", artifacts)),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, "embed")).rejects.toThrow(
      "malformed document stage artifact projection",
    );
  });

  it("fails closed when the served stage or artifact kind differs from the requested stage", async () => {
    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/embed", () =>
        HttpResponse.json(stageResponse("chunk", [availableEmbedding()])),
      ),
    );

    await expect(client().documentStageEvidence("doc", 2, "embed")).rejects.toThrow(TypeError);

    server.use(
      http.get("*/api/v1/documents/:documentId/versions/:version/stages/embed", () =>
        HttpResponse.json(
          stageResponse("embed", [availableEmbedding({ kind: "chunk", label: "Record chunk" })]),
        ),
      ),
    );
    await expect(client().documentStageEvidence("doc", 2, "embed")).rejects.toThrow(TypeError);
  });
});
