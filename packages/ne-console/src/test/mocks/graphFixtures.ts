// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-134 graph fixtures. These mirror the ACL-stripped graph_wire.py shapes
// EXACTLY (six-field nodes/edges, truncated flag) and are the Phase-5 stand-in
// for a live AGE-backed graph tier (live e2e parked to Phase 6). Kept in a
// SEPARATE file from fixtures.ts/handlers.ts to stay conflict-free with the
// concurrent NE-138/139 console work. One node + one path edge carry
// source_ref "ref/doc-alpha" so they match an existing TRIMMED_CITATIONS ref —
// the TRACE -> OPEN SOURCE CHUNK (/v1/brain/retrieve) path is testable.

import type { GraphNodeWire, GraphPathsWire, SubgraphWire } from "../../api/types";

/** source_ref shared with fixtures.ts TRIMMED_CITATIONS (ref/doc-alpha). */
export const MATCHING_SOURCE_REF = "ref/doc-alpha";

function node(
  key: string,
  entity_class: string,
  display_name: string,
  source_system: string,
  source_ref: string,
  assurance: string = "source",
): GraphNodeWire {
  return { key, entity_class, display_name, source_system, source_ref, properties: {}, assurance };
}

// 8 nodes covering all 5 canonical entity classes.
const ACME = node("acct-acme", "Account", "Acme Corp", "crm", "crm/acct-1");
const DANA = node("person-dana", "Person", "Dana Ops", "workday", "hr/person-1");
const RUNBOOK = node("doc-runbook", "Document", "Ops Runbook", "sharepoint", MATCHING_SOURCE_REF);
const TICKET42 = node("ticket-42", "Ticket", "Ticket 42", "zendesk", "zendesk/tick-42");
const ORDER7 = node("order-7", "Order", "Order 7", "erp", "erp/order-7");
const SAM = node("person-sam", "Person", "Sam Rep", "workday", "hr/person-2");
const CONTRACT = node("doc-contract", "Document", "Master Contract", "sharepoint", "ref/doc-bravo");
const BETA = node("acct-beta", "Account", "Beta LLC", "crm", "crm/acct-2");

/** 8 nodes / 10 edges / 9 distinct edge kinds, not truncated. */
export const GRAPH_FIXTURE: SubgraphWire = {
  nodes: [ACME, DANA, RUNBOOK, TICKET42, ORDER7, SAM, CONTRACT, BETA],
  edges: [
    {
      key: "e-works-1",
      edge_kind: "WORKS_AT",
      from_key: "person-dana",
      to_key: "acct-acme",
      source_system: "workday",
      source_ref: "hr/emp-1",
      assurance: "source",
    },
    {
      key: "e-works-2",
      edge_kind: "WORKS_AT",
      from_key: "person-sam",
      to_key: "acct-beta",
      source_system: "workday",
      source_ref: "hr/emp-2",
      assurance: "source",
    },
    {
      key: "e-author-1",
      edge_kind: "AUTHOR",
      from_key: "doc-runbook",
      to_key: "person-dana",
      source_system: "sharepoint",
      source_ref: MATCHING_SOURCE_REF,
      assurance: "source",
    },
    {
      key: "e-owned-1",
      edge_kind: "OWNED_BY",
      from_key: "doc-contract",
      to_key: "person-sam",
      source_system: "sharepoint",
      source_ref: "sp/owned-1",
      assurance: "source",
    },
    {
      key: "e-about-1",
      edge_kind: "ABOUT",
      from_key: "doc-runbook",
      to_key: "acct-acme",
      source_system: "sharepoint",
      source_ref: "sp/about-1",
      assurance: "source",
    },
    {
      key: "e-belongs-1",
      edge_kind: "BELONGS_TO",
      from_key: "ticket-42",
      to_key: "acct-acme",
      source_system: "zendesk",
      source_ref: "zd/belongs-1",
      assurance: "source",
    },
    {
      key: "e-req-1",
      edge_kind: "REQUESTER",
      from_key: "ticket-42",
      to_key: "person-dana",
      source_system: "zendesk",
      source_ref: "zd/req-1",
      assurance: "source",
    },
    {
      key: "e-assign-1",
      edge_kind: "ASSIGNEE",
      from_key: "ticket-42",
      to_key: "person-sam",
      source_system: "zendesk",
      source_ref: "zd/assign-1",
      assurance: "source",
    },
    {
      key: "e-placed-1",
      edge_kind: "PLACED_BY",
      from_key: "order-7",
      to_key: "person-dana",
      source_system: "erp",
      source_ref: "erp/placed-1",
      assurance: "source",
    },
    {
      key: "e-account-1",
      edge_kind: "ACCOUNT",
      from_key: "order-7",
      to_key: "acct-acme",
      source_system: "erp",
      source_ref: "erp/account-1",
      assurance: "source",
    },
  ],
  truncated: false,
};

/** Same shape but the server set truncated:true (caps hit). */
export const GRAPH_FIXTURE_TRUNCATED: SubgraphWire = {
  nodes: GRAPH_FIXTURE.nodes,
  edges: GRAPH_FIXTURE.edges,
  truncated: true,
};

/** BLOOM result: 2 NEW nodes + 1 DUPLICATE (doc-runbook) to prove dedupe. */
export const NEIGHBORHOOD_FIXTURE: SubgraphWire = {
  nodes: [
    node("person-grace", "Person", "Grace New", "workday", "hr/person-3"),
    node("ticket-99", "Ticket", "Ticket 99", "zendesk", "zendesk/tick-99"),
    RUNBOOK, // duplicate key doc-runbook — must NOT render twice after merge
  ],
  edges: [
    {
      key: "e-req-2",
      edge_kind: "REQUESTER",
      from_key: "ticket-99",
      to_key: "person-dana",
      source_system: "zendesk",
      source_ref: "zd/req-2",
      assurance: "source",
    },
    {
      key: "e-works-3",
      edge_kind: "WORKS_AT",
      from_key: "person-grace",
      to_key: "acct-acme",
      source_system: "workday",
      source_ref: "hr/emp-3",
      assurance: "source",
    },
  ],
  truncated: false,
};

/** Intermediate node that exists ONLY in a paths response (never in
 * GRAPH_FIXTURE) — proves path highlight merges out-of-subgraph segments. */
const LEE = node("person-lee", "Person", "Lee Broker", "workday", "hr/person-4");

/** Path 1: 3 hops (order-7 -> ... -> acct-acme), all nodes already in
 * GRAPH_FIXTURE but with path-minted edge keys (pe-*). Hop pe-author carries
 * the matching source_ref (OPEN SOURCE CHUNK path); the others do not (honest
 * no-chunk fallback). Path 2: 2 hops through person-lee, a node OUTSIDE the
 * loaded subgraph. */
export const PATHS_FIXTURE: GraphPathsWire = {
  paths: [
    {
      nodes: [ORDER7, DANA, RUNBOOK, ACME],
      edges: [
        {
          key: "pe-placed",
          edge_kind: "PLACED_BY",
          from_key: "order-7",
          to_key: "person-dana",
          source_system: "erp",
          source_ref: "erp/placed-1",
          assurance: "source",
        },
        {
          key: "pe-author",
          edge_kind: "AUTHOR",
          from_key: "doc-runbook",
          to_key: "person-dana",
          source_system: "sharepoint",
          source_ref: MATCHING_SOURCE_REF,
          assurance: "source",
        },
        {
          key: "pe-about",
          edge_kind: "ABOUT",
          from_key: "doc-runbook",
          to_key: "acct-acme",
          source_system: "sharepoint",
          source_ref: "sp/about-1",
          assurance: "source",
        },
      ],
    },
    {
      nodes: [ORDER7, LEE, ACME],
      edges: [
        {
          key: "pe-placed-2",
          edge_kind: "PLACED_BY",
          from_key: "order-7",
          to_key: "person-lee",
          source_system: "erp",
          source_ref: "erp/placed-2",
          assurance: "source",
        },
        {
          key: "pe-works-4",
          edge_kind: "WORKS_AT",
          from_key: "person-lee",
          to_key: "acct-acme",
          source_system: "workday",
          source_ref: "hr/emp-4",
          assurance: "source",
        },
      ],
    },
  ],
  truncated: false,
};
