// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 Dual-Key Compare fixtures. Kept in a SEPARATE file from
// handlers.ts/fixtures.ts/graphFixtures.ts to stay conflict-free with the
// concurrent NE-132/137 console work. The bearer-routed MSW helpers below model
// the SERVER trimming each key's response INDEPENDENTLY: the primary bearer
// ("Bearer test-access-token") gets the full response, the compare bearer
// ("Bearer compare-access-token-b") gets the trimmed one, and any other bearer is
// a 401 — so a test proves the compare token reached the wire, never a client
// diff (C7). GRAPH_FIXTURE_TRIMMED_B is derived from GRAPH_FIXTURE by dropping 3
// of its 8 nodes (the server never sent them to key B).

import { HttpResponse, http } from "msw";
import { vi } from "vitest";
import type { CompareKey } from "../../api/compareAuth";
import type { ConsoleRuntimeConfig } from "../../api/runtimeConfig";
import type {
  GraphEdgeWire,
  RetrievalResult,
  SubgraphWire,
  VerifiedInvokeWire,
} from "../../api/types";
import { GRAPH_FIXTURE } from "./graphFixtures";

/** The primary session bearer (mirrors authStub.TEST_ACCESS_TOKEN). */
export const PRIMARY_TOKEN = "test-access-token";
/** The compare (key-B) bearer the mocked acquireCompareKey yields on the wire. */
export const COMPARE_TOKEN_B = "compare-access-token-b";

/** Runtime config for the compare tests (mirrors the fixtures RUNTIME_CONFIG). */
export const TEST_RUNTIME_CONFIG: ConsoleRuntimeConfig = {
  issuer: "https://idp.ne.test/realms/ne-test",
  client_id: "ne-console",
  audience: "ne-console",
  api_base: "/api",
};

/** Key B's trimmed subgraph: the server sent key B only 5 of key A's 8 nodes
 * (doc-contract, acct-beta, order-7 were never served), and only edges whose
 * BOTH endpoints survive. This is the honest A∖B the contraction animates. */
const B_KEPT = new Set(["acct-acme", "person-dana", "doc-runbook", "ticket-42", "person-sam"]);

const B_EDGES: GraphEdgeWire[] = GRAPH_FIXTURE.edges.filter(
  (e) => B_KEPT.has(e.from_key) && B_KEPT.has(e.to_key),
);

export const GRAPH_FIXTURE_TRIMMED_B: SubgraphWire = {
  nodes: GRAPH_FIXTURE.nodes.filter((n) => B_KEPT.has(n.key)),
  edges: B_EDGES,
  truncated: false,
};

/** A B-response that ALSO carries a node absent from key A — proves the pane
 * renders "N entities appear only under key B" and the b-only DOM state. */
export const GRAPH_FIXTURE_B_ONLY: SubgraphWire = {
  nodes: [
    ...GRAPH_FIXTURE_TRIMMED_B.nodes,
    {
      key: "acct-gamma",
      entity_class: "Account",
      display_name: "Gamma Only",
      source_system: "crm",
      source_ref: "crm/acct-3",
      properties: {},
      assurance: "source",
    },
  ],
  edges: GRAPH_FIXTURE_TRIMMED_B.edges,
  truncated: false,
};

/** A CompareKey stub whose token is discarded by withdraw() (a vi.fn spy). Used
 * where the SPEC mock point is the src/api seam: acquireCompareKey resolves this,
 * and createCompareClient wraps it with the REAL createApiClient so MSW sees the
 * real compare bearer. */
export function makeCompareKeyStub(
  token: string,
  label = "beta@example.com",
): CompareKey & { withdraw: ReturnType<typeof vi.fn> } {
  let held: string | undefined = token;
  const withdraw = vi.fn(async () => {
    held = undefined;
  });
  return {
    label,
    getAccessToken: () => held,
    withdraw,
  };
}

function bearerOf(request: Request): string | null {
  return request.headers.get("Authorization");
}

/** Bearer-routed POST /v1/graph/query: full for the primary bearer, trimmed for
 * the compare bearer, 401 otherwise. */
export function graphQueryByBearer(full: SubgraphWire, trimmed: SubgraphWire) {
  return http.post("*/api/v1/graph/query", ({ request }) => {
    const auth = bearerOf(request);
    if (auth === `Bearer ${PRIMARY_TOKEN}`) return HttpResponse.json(full);
    if (auth === `Bearer ${COMPARE_TOKEN_B}`) return HttpResponse.json(trimmed);
    return HttpResponse.json({ detail: "unauthorized" }, { status: 401 });
  });
}

/** Bearer-routed POST /v1/brain/retrieve. `full`/`trimmed` are RetrievalResult-
 * shaped (only `citations` is read by the pane). */
export function retrieveByBearer(full: RetrievalResult, trimmed: RetrievalResult) {
  return http.post("*/api/v1/brain/retrieve", ({ request }) => {
    const auth = bearerOf(request);
    if (auth === `Bearer ${PRIMARY_TOKEN}`) return HttpResponse.json(full);
    if (auth === `Bearer ${COMPARE_TOKEN_B}`) return HttpResponse.json(trimmed);
    return HttpResponse.json({ detail: "unauthorized" }, { status: 401 });
  });
}

/** Bearer-routed POST /v1/verified/invoke. `full`/`trimmed` are
 * VerifiedInvokeWire-shaped (only `answer.rows` is read by the pane). */
export function verifiedInvokeByBearer(full: VerifiedInvokeWire, trimmed: VerifiedInvokeWire) {
  return http.post("*/api/v1/verified/invoke", ({ request }) => {
    const auth = bearerOf(request);
    if (auth === `Bearer ${PRIMARY_TOKEN}`) return HttpResponse.json(full);
    if (auth === `Bearer ${COMPARE_TOKEN_B}`) return HttpResponse.json(trimmed);
    return HttpResponse.json({ detail: "unauthorized" }, { status: 401 });
  });
}
