// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-136 T3-drawer fixtures. These mirror the redacted ne-api ontology/verified
// wire shapes EXACTLY (counts/ids/hashes only on violations — no delta_nt, no
// SHACL messages, C12) and are the Phase-5 stand-in for a live T3 store (live
// e2e parked to Phase 6). Kept in a SEPARATE file from fixtures.ts/graphFixtures
// .ts to stay conflict-free with the concurrent console work. The verified
// templates mirror the REAL registry TOMLs
// (packages/ne-brain-ontology/tests/data/verified_templates/): accounts-by-name
// (a pattern-constrained str arg) and entity-by-iri (an iri arg — which the
// server's _build_args_model maps to a PLAIN type:'string' on the wire). The
// invoke result's citations are REAL GRAPH_FIXTURE node keys so the ASK gesture
// lights an actual chamber node.

import type {
  OntologyClassesWire,
  OntologyShapesWire,
  OntologyViolationsWire,
  VerifiedInvokeWire,
  VerifiedTemplatesWire,
} from "../../api/types";

const OBO = "http://purl.obolibrary.org/obo/";
const CCO = "https://www.commoncoreontologies.org/";
const NEOP = "https://neutron.enterprise/schema/";

export const ONTOLOGY_SHAPES_HASH = "shapes-fixture-hash-000001";

/** ≈8 classes spanning all sources, with `parents` links so the tree indents:
 * a BFO root + child, two CCO classes, and neop Account/Person/Document each
 * under a CCO parent. */
export const ONTOLOGY_CLASSES: OntologyClassesWire = {
  classes: [
    { iri: `${OBO}BFO_0000001`, label: "entity", parents: [], source: "bfo" },
    {
      iri: `${OBO}BFO_0000002`,
      label: "continuant",
      parents: [`${OBO}BFO_0000001`],
      source: "bfo",
    },
    {
      iri: `${CCO}ont/Agent`,
      label: "Agent",
      parents: [`${OBO}BFO_0000002`],
      source: "cco",
    },
    {
      iri: `${CCO}ont/InformationContentEntity`,
      label: "Information Content Entity",
      parents: [`${OBO}BFO_0000002`],
      source: "cco",
    },
    {
      iri: `${NEOP}operational/Account`,
      label: "Account",
      parents: [`${CCO}ont/Agent`],
      source: "neop",
    },
    {
      iri: `${NEOP}operational/Person`,
      label: "Person",
      parents: [`${CCO}ont/Agent`],
      source: "neop",
    },
    {
      iri: `${NEOP}operational/Document`,
      label: "Document",
      parents: [`${CCO}ont/InformationContentEntity`],
      source: "neop",
    },
    // A class whose source falls outside the three named groups → OTHER heading.
    { iri: "https://example.org/misc/Thing", label: null, parents: [], source: "other" },
  ],
  total: 8,
  sources: { bfo: "2020", cco: "2.1", neop: "0.1" },
  shapes_hash: ONTOLOGY_SHAPES_HASH,
};

/** Two compiled shapes: one class-targeted (Account, per-write scope), one
 * global (no target_class). */
export const ONTOLOGY_SHAPES: OntologyShapesWire = {
  shapes: [
    {
      iri: `${NEOP}shapes/AccountShape`,
      target_class: `${NEOP}operational/Account`,
      scope: "per-write",
    },
    { iri: `${NEOP}shapes/GlobalCardinalityShape`, target_class: null, scope: "global" },
  ],
  shapes_hash: ONTOLOGY_SHAPES_HASH,
};

/** Two quarantined batches, each fail-closed (quarantined_whole:true). Redacted
 * by construction — no delta_nt, no message strings. */
export const ONTOLOGY_VIOLATIONS: OntologyViolationsWire = {
  configured: true,
  count: 2,
  entries: [
    {
      quarantine_id: "quar-7f3a19b2c4d5",
      created_at: "2026-07-05T09:14:00Z",
      graph_iri: `${NEOP}graph/ingest-7`,
      shapes_hash: ONTOLOGY_SHAPES_HASH,
      violation_count: 3,
      quarantined_whole: true,
      provenance: "ingest:batch-7",
    },
    {
      quarantine_id: "quar-1c0d55ee8801",
      created_at: "2026-07-06T02:41:00Z",
      graph_iri: null,
      shapes_hash: ONTOLOGY_SHAPES_HASH,
      violation_count: 3,
      quarantined_whole: true,
      provenance: "ingest:batch-8",
    },
  ],
};

/** Zero-batch, still-configured feed (honest empty state). */
export const ONTOLOGY_VIOLATIONS_EMPTY: OntologyViolationsWire = {
  configured: true,
  count: 0,
  entries: [],
};

/** Unconfigured feed — the write gateway has no inspectable quarantine here. */
export const ONTOLOGY_VIOLATIONS_UNCONFIGURED: OntologyViolationsWire = {
  configured: false,
  count: 0,
  entries: [],
};

export const VERIFIED_REGISTRY_HASH = "registry-fixture-hash-abc123";

/** Mirrors the REAL registry TOMLs, args_schema exactly as Pydantic emits it.
 * `accounts-by-name` v2 (pattern-constrained str) + `entity-by-iri` v1 (an iri
 * arg that wires as a PLAIN type:'string'). */
export const VERIFIED_TEMPLATES: VerifiedTemplatesWire = {
  enabled: true,
  registry_hash: VERIFIED_REGISTRY_HASH,
  templates: [
    {
      template_id: "accounts-by-name",
      version: 2,
      title: "Accounts whose name exactly matches the (pattern-constrained) query string.",
      tool_name: "read.kg.accounts-by-name",
      args_schema: {
        title: "accounts_by_name_Args",
        type: "object",
        additionalProperties: false,
        required: ["q_name"],
        properties: {
          q_name: {
            title: "Q Name",
            type: "string",
            pattern: "^[A-Za-z0-9_ ]{1,64}$",
            maxLength: 64,
          },
        },
      },
      columns: ["account", "name"],
      citation_var: "account",
      // The mirrored bundle TOML declares no label_var — these two fixtures are
      // the renderer's DEGRADE case: served order verbatim, no invented label.
      label_var: null,
      max_rows: null,
    },
    {
      template_id: "entity-by-iri",
      version: 1,
      title: "Fetch one entity's name by its exact IRI (the fetch-by-id parity probe).",
      tool_name: "read.kg.entity-by-iri",
      args_schema: {
        title: "entity_by_iri_Args",
        type: "object",
        additionalProperties: false,
        required: ["account"],
        properties: {
          // An iri arg is INDISTINGUISHABLE from str on the wire (the server's
          // _build_args_model maps both to a plain string annotation).
          account: { title: "Account", type: "string" },
        },
      },
      columns: ["account", "name"],
      citation_var: "account",
      label_var: null,
      max_rows: null,
    },
  ],
};

/** Disabled deployment: verified answers off (registry not configured). */
export const VERIFIED_TEMPLATES_DISABLED: VerifiedTemplatesWire = {
  enabled: false,
  registry_hash: null,
  templates: [],
};

const INVOKE_CONFIG_HASH = "cfg-fixture-hash-9911aabb";
const INVOKE_ONTOLOGY_VERSION = "onto-fixture-2026-07-01";

/** The deterministic result: two rows, citations are REAL GRAPH_FIXTURE node
 * keys (acct-acme / acct-beta) so the ASK test lights an actual chamber node.
 * `replay_match:true` — the server invoked twice and answers_equal held. */
export const VERIFIED_INVOKE_RESULT: VerifiedInvokeWire = {
  answer: {
    template_id: "accounts-by-name",
    template_version: 2,
    args: { q_name: "Acme Corp" },
    columns: ["account", "name"],
    rows: [
      ["urn:ne:entity:acct-acme", "Acme Corp"],
      ["urn:ne:entity:acct-beta", "Beta LLC"],
    ],
    citations: ["urn:ne:entity:acct-acme", "urn:ne:entity:acct-beta"],
    truncated: false,
    row_limit: 100,
    returned_rows: 2,
    total_rows: 2,
    truncation_reason: null,
    config_hash: INVOKE_CONFIG_HASH,
    ontology_version: INVOKE_ONTOLOGY_VERSION,
    executed_at: "2026-07-07T12:00:00Z",
  },
  replay_match: true,
};

/** Same shape, but the server's double-invoke DIVERGED (replay_match:false). */
export const VERIFIED_INVOKE_DIVERGED: VerifiedInvokeWire = {
  answer: { ...VERIFIED_INVOKE_RESULT.answer, executed_at: "2026-07-07T12:00:05Z" },
  replay_match: false,
};

/** Same shape, but the server row-capped the result (truncated, reason=rows). */
export const VERIFIED_INVOKE_TRUNCATED: VerifiedInvokeWire = {
  answer: {
    ...VERIFIED_INVOKE_RESULT.answer,
    truncated: true,
    truncation_reason: "rows",
    total_rows: null,
  },
  replay_match: true,
};

/** Token-budget truncation (reason=tokens): the placard names the token budget
 * and the shown-row count. */
export const VERIFIED_INVOKE_TOKENS_TRUNCATED: VerifiedInvokeWire = {
  answer: {
    ...VERIFIED_INVOKE_RESULT.answer,
    truncated: true,
    truncation_reason: "tokens",
    returned_rows: 3,
    total_rows: null,
  },
  replay_match: true,
};
