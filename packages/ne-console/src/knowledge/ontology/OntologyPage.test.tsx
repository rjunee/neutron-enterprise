// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { useLocation, useNavigate, MemoryRouter } from "react-router";
import type { User } from "oidc-client-ts";
import { describe, expect, it, vi } from "vitest";
import type { OntologyInspectionClient } from "../../api/ontologyInspectionClient";
import { AuthStub } from "../../test/authStub";
import { server } from "../../test/mocks/server";
import type {
  ActiveOntologyModel,
  OntologyConcept,
  OntologyInspectionResponse,
  OntologyIssueDetail,
  OntologyIssueList,
  OntologyRule,
} from "./types";
import { OntologyPage } from "./OntologyPage";

const ontologyClientOverride = vi.hoisted(() => ({
  current: null as OntologyInspectionClient | null,
}));

vi.mock("./useOntologyInspectionClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./useOntologyInspectionClient")>();
  return {
    ...actual,
    useOntologyInspectionClient: () => {
      const composed = actual.useOntologyInspectionClient();
      return ontologyClientOverride.current ?? composed;
    },
  };
});

const HASH = "a".repeat(64);

function concept(name: string, options: Partial<OntologyConcept> = {}): OntologyConcept {
  return {
    name,
    label: name.replace(/([a-z])([A-Z])/g, "$1 $2"),
    description: `${name} description`,
    canonical_iri: `https://neutron.enterprise/schema/operational/${name}`,
    abstract: false,
    tree_root: false,
    parents: name === "OperationalRecordSet" ? [] : ["OperationalEntity"],
    children: [],
    fields: [],
    ...options,
  };
}

const DOCUMENT = concept("Document", {
  description: "Governed enterprise content authored by a person.",
  fields: [
    {
      name: "title",
      label: "Title",
      description: "Human-readable document title.",
      canonical_iri: "https://neutron.enterprise/schema/operational/title",
      value_type: "string",
      required: true,
      multivalued: false,
      min_count: 1,
      max_count: 1,
      enum_values: [],
      related_concepts: [],
    },
    {
      name: "doc_type",
      label: "Doc type",
      description: "The kind of document this is.",
      canonical_iri: "https://neutron.enterprise/schema/operational/doc_type",
      value_type: "enum:DocumentType",
      required: true,
      multivalued: false,
      min_count: 1,
      max_count: 1,
      enum_values: ["contract", "invoice", "report", "policy", "other"],
      related_concepts: [],
    },
    {
      name: "author",
      label: "Author",
      description: "The person who authored the document.",
      canonical_iri: "https://neutron.enterprise/schema/operational/author",
      value_type: "concept:Person",
      required: false,
      multivalued: false,
      min_count: 0,
      max_count: 1,
      enum_values: [],
      related_concepts: ["Person"],
    },
  ],
});

const CONCEPTS: OntologyConcept[] = [
  concept("OperationalEntity", {
    abstract: true,
    parents: [],
    children: ["Account", "Document", "Order", "Person", "Ticket"],
  }),
  concept("Account"),
  DOCUMENT,
  concept("OperationalRecordSet", { tree_root: true, fields: [] }),
  concept("Order"),
  concept("Person"),
  concept("Ticket"),
];

const RULES: OntologyRule[] = [
  {
    rule_id: "rule-document-title",
    label: "Document — Title",
    purpose: "Every document is identifiable to a reader.",
    condition: "Required; Must be text",
    severity: "violation",
    scope: "per-write",
    source: "generated",
    concept: "Document",
    field_name: "title",
    shape_iri: "https://neutron.enterprise/schema/shapes/DocumentShape",
    path_iri: "https://neutron.enterprise/schema/operational/title",
    artifact_sha256: HASH,
  },
  {
    rule_id: "rule-labels-unique",
    label: "Labels are unique",
    purpose: "Prevents two records from sharing one corpus label.",
    condition: "No other record may use this label",
    severity: "violation",
    scope: "global",
    source: "curated",
    concept: null,
    field_name: null,
    shape_iri: "urn:ne:shapes:global#UniqueLabelShape",
    path_iri: null,
    artifact_sha256: HASH,
  },
];

const MODEL: ActiveOntologyModel = {
  provenance: {
    schema_id: "https://neutron.enterprise/schema/operational",
    title: "Neutron Enterprise Operational Schema",
    schema_version: "1.0.0",
    linkml_version: "1.8.0",
    source_sha256: "b".repeat(64),
    json_schema_sha256: "c".repeat(64),
    shapes_sha256: HASH,
    source_artifact: "ne.schema.generated.operational",
  },
  concept_count: 7,
  rule_count: 2,
  concepts: CONCEPTS,
  relationships: [
    {
      kind: "AUTHORED_BY",
      label: "Authored by",
      source_concept: "Document",
      target_concept: "Person",
      field_name: "author",
      required: false,
      multivalued: false,
    },
    {
      kind: "CONTAINS",
      label: "Contains",
      source_concept: "OperationalRecordSet",
      target_concept: "Document",
      field_name: "documents",
      required: false,
      multivalued: true,
    },
  ],
  rules: RULES,
  standards: [
    {
      label: "Neutron operational model",
      role: "Active enterprise concepts and relationships",
      namespace_iri: "https://neutron.enterprise/schema/operational/",
      version: "1.0.0",
    },
    {
      label: "LinkML",
      role: "Schema source and artifact generation standard",
      namespace_iri: "https://w3id.org/linkml/",
      version: null,
    },
    {
      label: "Basic Formal Ontology (BFO)",
      role: "Upper-ontology alignment",
      namespace_iri: "http://purl.obolibrary.org/obo/BFO_",
      version: null,
    },
    {
      label: "Common Core Ontologies (CCO)",
      role: "Enterprise-domain alignment",
      namespace_iri: "https://www.commoncoreontologies.org/",
      version: null,
    },
  ],
};

const BASE_RESPONSE: OntologyInspectionResponse = {
  model: MODEL,
  artifact_health: { state: "healthy", detail: null },
  ontology_graph: { state: "not_loaded", optional_class_count: 0 },
  issue_tracking: { state: "unconfigured", count: null },
  validation_health: "unavailable",
};

const ISSUE_LIST: OntologyIssueList = {
  configured: true,
  count: 1,
  entries: [
    {
      quarantine_id: "issue-1",
      created_at: "2026-08-20T00:00:00Z",
      graph_iri: "urn:ne:graph:acl:opaque",
      shapes_hash: HASH,
      violation_count: 2,
      quarantined_whole: true,
      provenance: "ne:brain-ontology:gateway",
    },
  ],
};

const ISSUE_DETAIL: OntologyIssueDetail = {
  issue_id: "issue-1",
  created_at: "2026-08-20T00:00:00Z",
  graph_iri: "urn:ne:graph:acl:opaque",
  shapes_sha256: HASH,
  provenance: "ne:brain-ontology:gateway",
  lifecycle_state: "quarantined",
  reasons: [
    {
      affected_reference: "urn:record:1",
      rule_reference: "urn:rule:required",
      reason: "Required field is missing",
    },
  ],
};

function serveModel(response: OntologyInspectionResponse = BASE_RESPONSE) {
  server.use(http.get("*/api/v1/ontology/model", () => HttpResponse.json(response)));
}

function LocationHarness() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="Location">{`${location.pathname}${location.search}`}</output>
      <button type="button" onClick={() => navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        Forward
      </button>
    </>
  );
}

function renderPage(entry = "/knowledge/ontology", token = "token-a") {
  const user = { access_token: token, profile: { sub: token } } as User;
  return render(
    <AuthStub value={{ user }}>
      <MemoryRouter initialEntries={[entry]}>
        <LocationHarness />
        <OntologyPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("Ontology overview and independent truth", () => {
  it("keeps the seven-concept generated model active when the optional graph is absent", async () => {
    serveModel();
    renderPage();

    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    // Absent optional features and unreportable states get NO card: no
    // "not loaded" graph card, no unconfigured issue-tracking card, and no
    // derived "validation health" indicator at all.
    expect(screen.queryByText("Optional ontology graph")).toBeNull();
    expect(screen.queryByText(/Issue tracking/)).toBeNull();
    expect(screen.queryByText("Validation health")).toBeNull();
    expect(screen.queryByText("unavailable")).toBeNull();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText(/Model definitions are shared metadata/)).toBeInTheDocument();
    expect(screen.getByText("T1 · Documents")).toBeInTheDocument();
    expect(screen.getByText("T2 · Graph")).toBeInTheDocument();
    expect(screen.getByText("T3 · Ontology")).toBeInTheDocument();
  });

  it("keeps loaded and unavailable optional-graph states distinct without erasing the model", async () => {
    serveModel({
      ...BASE_RESPONSE,
      ontology_graph: { state: "loaded", optional_class_count: 3 },
    });
    const view = renderPage();
    expect(await screen.findByText("3 optional classes loaded")).toBeInTheDocument();
    expect(screen.getByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();

    serveModel({
      ...BASE_RESPONSE,
      ontology_graph: { state: "unavailable", optional_class_count: null },
    });
    view.unmount();
    renderPage();
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    expect(screen.getByText("Optional ontology graph")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("shows the open-issue card only where the quarantine lane reports", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 0 },
      validation_health: "healthy",
    });
    const view = renderPage();
    expect(await screen.findByText("No open issues")).toBeInTheDocument();
    const states = screen.getByLabelText("Ontology state");
    expect(within(states).getByText("Validation issues")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Validation issues" })).toBeInTheDocument();
    expect(screen.queryByText("Validation health")).toBeNull();

    view.unmount();
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 2 },
      validation_health: "issues",
    });
    renderPage();
    expect(await screen.findByText("2 open issues")).toBeInTheDocument();
  });

  it("draws the relationship graph from the model and selects concepts from it", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=model&view=relationships");
    const graph = await screen.findByRole("group", { name: "Concept relationship graph" });
    // Every concept named by a relationship is a node; edge labels are the
    // model's own relationship labels.
    expect(within(graph).getByRole("button", { name: "Document concept" })).toBeInTheDocument();
    expect(within(graph).getByRole("button", { name: "Person concept" })).toBeInTheDocument();
    expect(
      within(graph).getByRole("button", { name: "Operational Record Set concept" }),
    ).toBeInTheDocument();
    expect(within(graph).getByText("Authored by")).toBeInTheDocument();
    expect(within(graph).getByText("Contains")).toBeInTheDocument();
    // Concepts with no relationships stay out of the picture.
    expect(within(graph).queryByRole("button", { name: "Ticket concept" })).toBeNull();

    await userEvent.click(within(graph).getByRole("button", { name: "Person concept" }));
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Person");
    expect(await screen.findByRole("article", { name: "Person details" })).toBeInTheDocument();
    expect(within(graph).getByRole("button", { name: "Person concept" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("reveals immutable provenance only through its technical disclosure", async () => {
    serveModel();
    renderPage();
    await screen.findByText("Neutron Enterprise Operational Schema");
    const fingerprint = screen.getByText("bbbbbbbb");
    expect(fingerprint).not.toBeVisible();
    await userEvent.click(screen.getByText("Technical provenance"));
    expect(fingerprint).toBeVisible();
    expect(screen.getByText("ne.schema.generated.operational")).toBeVisible();
  });

  // §2.7 (primitives/HashChip): eight characters is what a person reads, sixty-four
  // is what a person pastes. Three fingerprints sit in one `dl` here, so three
  // buttons all reading "Copy hash" would be its own defect — each names its own.
  it("truncates all three model fingerprints and names each copy button for its own hash", async () => {
    serveModel();
    renderPage();
    await screen.findByText("Neutron Enterprise Operational Schema");
    await userEvent.click(screen.getByText("Technical provenance"));

    const fingerprints = [
      ["b".repeat(64), "Copy source fingerprint"],
      ["c".repeat(64), "Copy JSON Schema fingerprint"],
      ["a".repeat(64), "Copy validation fingerprint"],
    ] as const;
    for (const [full, label] of fingerprints) {
      expect(screen.queryByText(full)).toBeNull();
      expect(screen.getByText(full.slice(0, 8))).toHaveAttribute("title", full);
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("reports invalid artifacts as invalid instead of an empty model", async () => {
    serveModel({
      ...BASE_RESPONSE,
      model: null,
      artifact_health: { state: "invalid", detail: "compiled-shapes-fingerprint-mismatch" },
    });
    renderPage("/knowledge/ontology?section=model");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Active model artifacts are invalid",
    );
    expect(screen.queryByText("0 concepts")).toBeNull();
  });
});

describe("Model explorer URL state", () => {
  it("searches and selects two concepts while showing fields, enums and related concepts", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=model");
    await userEvent.type(await screen.findByLabelText("Filter concepts…"), "Document");
    expect(screen.getByText("1 concepts")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("treeitem", { name: "Document" }));
    expect(screen.getByRole("article", { name: "Document details" })).toHaveTextContent(
      "Allowed: contract, invoice, report, policy, other",
    );
    expect(screen.getByLabelText("Location")).toHaveTextContent("q=Document");
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Document");

    await userEvent.click(screen.getByRole("button", { name: "Related: Person" }));
    expect(screen.getByRole("article", { name: "Person details" })).toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Person");
  });

  it("preserves selection across Tree, List and relationship views and browser history", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=model&concept=Document");
    expect(await screen.findByRole("article", { name: "Document details" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "List" }));
    expect(screen.getByRole("table", { name: "Concept list" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Document details" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Relationships" }));
    expect(screen.getByRole("list", { name: "Concept relationships" })).toHaveTextContent(
      "Authored by",
    );
    await userEvent.click(
      within(screen.getByRole("list", { name: "Concept relationships" })).getByRole("button", {
        name: "Person",
      }),
    );
    expect(screen.getByRole("article", { name: "Person details" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("article", { name: "Document details" })).toBeInTheDocument();
  });

  it("supports keyboard section navigation and owns the section in the URL", async () => {
    serveModel();
    renderPage();
    await screen.findByText("Neutron Enterprise Operational Schema");
    const overview = screen.getByRole("tab", { name: "Overview" });
    overview.focus();
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Model explorer" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.getByRole("tab", { name: "Model explorer" })).toHaveFocus();
    expect(screen.getAllByRole("tab").filter((tab) => tab.tabIndex === 0)).toEqual([
      screen.getByRole("tab", { name: "Model explorer" }),
    ]);
    expect(screen.getByLabelText("Location")).toHaveTextContent("section=model");

    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Standards" })).toHaveFocus();
    expect(screen.getByLabelText("Location")).toHaveTextContent("section=standards");
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    expect(screen.getByLabelText("Location")).not.toHaveTextContent("section=");
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Standards" })).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    expect(screen.getByLabelText("Location")).not.toHaveTextContent("section=");
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Standards" })).toHaveFocus();
  });

  it("continues section navigation from the focused tab after browser history changes selection", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 0 },
      validation_health: "healthy",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () =>
        HttpResponse.json({ configured: true, count: 0, entries: [] }),
      ),
    );
    renderPage("/knowledge/ontology?section=model");
    await screen.findByLabelText("Filter concepts…");
    const model = screen.getByRole("tab", { name: "Model explorer" });
    const rules = screen.getByRole("tab", { name: "Validation rules" });
    const issues = screen.getByRole("tab", { name: "Validation issues" });

    model.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(rules).toHaveFocus();
    expect(rules).toHaveAttribute("aria-selected", "true");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(model).toHaveAttribute("aria-selected", "true"));
    expect(rules).toHaveFocus();

    await userEvent.keyboard("{ArrowRight}");
    expect(issues).toHaveFocus();
    expect(issues).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Location")).toHaveTextContent("section=issues");
  });

  it("uses one roving treeitem focus and follows the visible hierarchy with the keyboard", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=model");
    const tree = await screen.findByRole("tree", { name: "Concept tree" });
    const operational = within(tree).getByRole("treeitem", { name: /Operational Entity/ });
    const account = within(tree).getByRole("treeitem", { name: /^Account/ });
    const document = within(tree).getByRole("treeitem", { name: /^Document/ });
    const recordSet = within(tree).getByRole("treeitem", { name: /Operational Record Set/ });

    expect(operational).toHaveAttribute("aria-expanded", "true");
    expect(operational).toHaveAttribute("aria-level", "1");
    expect(account).not.toHaveAttribute("aria-expanded");
    expect(account).toHaveAttribute("aria-level", "2");
    expect(recordSet).toHaveAttribute("aria-level", "1");
    expect(
      within(tree)
        .getAllByRole("treeitem")
        .filter((item) => item.tabIndex === 0),
    ).toEqual([operational]);

    operational.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(account).toHaveFocus();
    expect(account).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Account");
    expect(
      within(tree)
        .getAllByRole("treeitem")
        .filter((item) => item.tabIndex === 0),
    ).toEqual([account]);

    await userEvent.keyboard("{ArrowDown}");
    expect(document).toHaveFocus();
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Document");
    await userEvent.keyboard("{ArrowUp}");
    expect(account).toHaveFocus();
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=Account");
    await userEvent.keyboard("{ArrowDown}");
    expect(document).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(operational).toHaveFocus();
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=OperationalEntity");
    await userEvent.keyboard("{End}");
    expect(recordSet).toHaveFocus();
    expect(screen.getByLabelText("Location")).toHaveTextContent("concept=OperationalRecordSet");
    await userEvent.keyboard("{Home}");
    expect(operational).toHaveFocus();
  });

  it.each([
    ["Enter", "{Enter}"],
    ["Space", " "],
  ])("selects the focused treeitem with %s and owns it in the URL", async (_label, key) => {
    serveModel();
    renderPage("/knowledge/ontology?section=model");
    const tree = await screen.findByRole("tree", { name: "Concept tree" });
    const operational = within(tree).getByRole("treeitem", { name: /Operational Entity/ });

    expect(operational).toHaveAttribute("aria-selected", "false");
    expect(screen.getByLabelText("Location")).not.toHaveTextContent("concept=");
    operational.focus();
    await userEvent.keyboard(key);

    expect(operational).toHaveFocus();
    expect(operational).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Location")).toHaveTextContent(
      "section=model&concept=OperationalEntity",
    );
  });
});

describe("Rules, standards and issue safety", () => {
  it("leads validation rules with human copy and keeps raw SHACL under Technical details", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=rules");
    await userEvent.click(await screen.findByRole("button", { name: /Document — Title/ }));
    const detail = screen.getByRole("article", { name: "Document — Title rule details" });
    expect(detail).toHaveTextContent("Every document is identifiable to a reader");
    const rawShape = within(detail).getByText(
      "https://neutron.enterprise/schema/shapes/DocumentShape",
    );
    expect(rawShape).not.toBeVisible();
    await userEvent.click(within(detail).getByText("Technical details"));
    expect(rawShape).toBeVisible();
    // The artifact digest is a §2.7 chip, not 64 characters in a narrow column.
    expect(within(detail).queryByText(HASH)).toBeNull();
    expect(within(detail).getByText("aaaaaaaa")).toHaveAttribute("title", HASH);
    expect(
      within(detail).getByRole("button", { name: "Copy rule artifact fingerprint" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toHaveTextContent("rule=rule-document-title");
  });

  it("presents standards as labelled mappings and never fabricates absent versions", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=standards");
    expect(await screen.findByText("Basic Formal Ontology (BFO)")).toBeInTheDocument();
    expect(screen.getByText("Common Core Ontologies (CCO)")).toBeInTheDocument();
    expect(screen.getAllByText("Version not reported")).toHaveLength(3);
    expect(screen.getByText("http://purl.obolibrary.org/obo/BFO_")).toBeInTheDocument();
  });

  it("shows no issues tab on an unconfigured deployment and renders no fake actions", async () => {
    serveModel();
    renderPage("/knowledge/ontology?section=issues");
    // The quarantine lane cannot report here, so the section does not exist:
    // the deep link lands on the overview and no issues tab is offered.
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Validation issues" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText(/Issue tracking/)).toBeNull();
    for (const forbidden of [
      "Release",
      "Revalidate",
      "Edit rule",
      "Create concept",
      "Verified answers",
      "Run query",
    ]) {
      expect(screen.queryByRole("button", { name: forbidden })).toBeNull();
      expect(screen.queryByText(forbidden)).toBeNull();
    }
  });

  it("keeps configured-empty and unavailable issue-tracking states distinct", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 0 },
      validation_health: "healthy",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () =>
        HttpResponse.json({ configured: true, count: 0, entries: [] }),
      ),
    );
    const view = renderPage("/knowledge/ontology?section=issues");
    expect(await screen.findByText("No validation issues are open.")).toBeInTheDocument();

    view.unmount();
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "unavailable", count: null },
      validation_health: "unavailable",
    });
    renderPage("/knowledge/ontology?section=issues");
    // An unavailable quarantine lane cannot report either — no tab, no card,
    // and the deep link falls back to the overview.
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Validation issues" })).toBeNull();
    expect(screen.queryByText("No validation issues are open.")).toBeNull();
  });

  it("renders only the ops-authorized safe issue projection and ignores raw payload fields", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () => HttpResponse.json(ISSUE_LIST)),
      http.get("*/api/v1/ontology/violations/:id", () =>
        HttpResponse.json({ ...ISSUE_DETAIL, delta_nt: "SMUGGLE-RAW-DELTA" }),
      ),
    );
    renderPage("/knowledge/ontology?section=issues");
    await userEvent.click(await screen.findByRole("button", { name: /2 violated rules/ }));
    const detail = await screen.findByRole("article", { name: "issue-1 issue details" });
    expect(detail).toHaveTextContent("Required field is missing");
    expect(detail).toHaveTextContent("urn:record:1");
    expect(detail).toHaveTextContent("ne:brain-ontology:gateway");
    expect(screen.queryByText("SMUGGLE-RAW-DELTA")).toBeNull();
    expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
    // The validation digest is a §2.7 chip, and its button names which hash.
    expect(within(detail).queryByText(HASH)).toBeNull();
    expect(within(detail).getByText("aaaaaaaa")).toHaveAttribute("title", HASH);
    expect(
      within(detail).getByRole("button", { name: "Copy issue validation fingerprint" }),
    ).toBeInTheDocument();
  });

  // `routers/ontology.py` builds `reasons` from `entry.violations`, which can be
  // empty — and this list is hand-rolled, so it has no DataTable §2.4 empty state
  // behind it. A heading over nothing is not a report.
  it("says in words that an entry carried no violations instead of an empty list", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () => HttpResponse.json(ISSUE_LIST)),
      http.get("*/api/v1/ontology/violations/:id", () =>
        HttpResponse.json({ ...ISSUE_DETAIL, reasons: [] }),
      ),
    );
    renderPage("/knowledge/ontology?section=issues&issue=issue-1");
    const detail = await screen.findByRole("article", { name: "issue-1 issue details" });
    expect(within(detail).getByText("Safe reasons")).toBeInTheDocument();
    expect(
      within(detail).getByText("No safe reasons were reported for this issue."),
    ).toBeInTheDocument();
    expect(within(detail).queryAllByRole("list")).toHaveLength(0);
  });

  it("renders an ordinary persona's issue-detail 403 as a refusal, not an outage", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () => HttpResponse.json(ISSUE_LIST)),
      http.get("*/api/v1/ontology/violations/:id", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    renderPage("/knowledge/ontology?section=issues&issue=issue-1");
    expect(
      await screen.findByText(/Your role doesn.t include sensitive validation issue details/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Couldn’t load the validation issue.")).toBeNull();
  });

  it("preserves a detail-route 503 as the truthful unconfigured state", async () => {
    serveModel({
      ...BASE_RESPONSE,
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    });
    server.use(
      http.get("*/api/v1/ontology/violations", () => HttpResponse.json(ISSUE_LIST)),
      http.get("*/api/v1/ontology/violations/:id", () =>
        HttpResponse.json({ detail: "issue-tracking-not-configured" }, { status: 503 }),
      ),
    );
    renderPage("/knowledge/ontology?section=issues&issue=issue-1");
    expect(
      await screen.findByText("Issue tracking isn't configured on this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Couldn’t load the validation issue.")).toBeNull();
  });
});

describe("request identity and resilient states", () => {
  it("withdraws served model and issue bytes during a same-client subject replacement", async () => {
    let modelCalls = 0;
    let releaseReplacement: ((value: OntologyInspectionResponse) => void) | undefined;
    const replacement = new Promise<OntologyInspectionResponse>((resolve) => {
      releaseReplacement = resolve;
    });
    const aliceResponse: OntologyInspectionResponse = {
      ...BASE_RESPONSE,
      model: {
        ...MODEL,
        provenance: { ...MODEL.provenance, title: "Alice private ontology" },
      },
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    };
    const client: OntologyInspectionClient = {
      readModel: () => {
        modelCalls += 1;
        return modelCalls === 1 ? Promise.resolve(aliceResponse) : replacement;
      },
      listIssues: async () => ({
        ...ISSUE_LIST,
        entries: [{ ...ISSUE_LIST.entries[0]!, provenance: "ALICE-ISSUE-LIST" }],
      }),
      readIssue: async () => ({ ...ISSUE_DETAIL, provenance: "ALICE-ISSUE-DETAIL" }),
    };
    ontologyClientOverride.current = client;
    const alice = { access_token: "shared-token", profile: { sub: "alice" } } as User;
    const bob = { access_token: "shared-token", profile: { sub: "bob" } } as User;
    const view = render(
      <AuthStub value={{ user: alice }}>
        <MemoryRouter initialEntries={["/knowledge/ontology?issue=issue-1"]}>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );

    try {
      expect(await screen.findByText("Alice private ontology")).toBeInTheDocument();
      await userEvent.click(screen.getByRole("tab", { name: "Validation issues" }));
      expect(await screen.findByText("ALICE-ISSUE-LIST")).toBeInTheDocument();
      expect(await screen.findByText("ALICE-ISSUE-DETAIL")).toBeInTheDocument();

      view.rerender(
        <AuthStub value={{ user: bob }}>
          <MemoryRouter initialEntries={["/knowledge/ontology?section=issues&issue=issue-1"]}>
            <OntologyPage />
          </MemoryRouter>
        </AuthStub>,
      );
      expect(screen.queryByText("Alice private ontology")).toBeNull();
      expect(screen.queryByText("ALICE-ISSUE-LIST")).toBeNull();
      expect(screen.queryByText("ALICE-ISSUE-DETAIL")).toBeNull();
      await waitFor(() => expect(modelCalls).toBe(2));
    } finally {
      view.unmount();
      releaseReplacement?.(BASE_RESPONSE);
      ontologyClientOverride.current = null;
    }
  });

  it("does not issue or render ontology reads without a ready OIDC subject", () => {
    const readModel = vi.fn<OntologyInspectionClient["readModel"]>();
    ontologyClientOverride.current = {
      readModel,
      listIssues: vi.fn<OntologyInspectionClient["listIssues"]>(),
      readIssue: vi.fn<OntologyInspectionClient["readIssue"]>(),
    };
    const view = render(
      <AuthStub value={{ isLoading: true }}>
        <MemoryRouter>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );
    try {
      expect(screen.getByText("Loading…")).toBeInTheDocument();
      expect(readModel).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      ontologyClientOverride.current = null;
    }
  });

  it("ignores late issue completions from a replaced subject", async () => {
    let modelCalls = 0;
    let issueListStarted = false;
    let issueDetailStarted = false;
    let releaseAliceList: ((value: OntologyIssueList) => void) | undefined;
    let releaseAliceDetail: ((value: OntologyIssueDetail) => void) | undefined;
    const aliceList = new Promise<OntologyIssueList>((resolve) => {
      releaseAliceList = resolve;
    });
    const aliceDetail = new Promise<OntologyIssueDetail>((resolve) => {
      releaseAliceDetail = resolve;
    });
    const aliceResponse: OntologyInspectionResponse = {
      ...BASE_RESPONSE,
      model: {
        ...MODEL,
        provenance: { ...MODEL.provenance, title: "Alice loading ontology" },
      },
      issue_tracking: { state: "configured", count: 1 },
      validation_health: "issues",
    };
    const bobResponse: OntologyInspectionResponse = {
      ...BASE_RESPONSE,
      model: {
        ...MODEL,
        provenance: { ...MODEL.provenance, title: "Bob current ontology" },
      },
    };
    const client: OntologyInspectionClient = {
      readModel: async () => {
        modelCalls += 1;
        return modelCalls === 1 ? aliceResponse : bobResponse;
      },
      listIssues: () => {
        issueListStarted = true;
        return aliceList;
      },
      readIssue: () => {
        issueDetailStarted = true;
        return aliceDetail;
      },
    };
    ontologyClientOverride.current = client;
    const alice = { access_token: "shared-token", profile: { sub: "alice" } } as User;
    const bob = { access_token: "shared-token", profile: { sub: "bob" } } as User;
    const view = render(
      <AuthStub value={{ user: alice }}>
        <MemoryRouter initialEntries={["/knowledge/ontology?section=issues&issue=issue-1"]}>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );

    try {
      await waitFor(() => {
        expect(issueListStarted).toBe(true);
        expect(issueDetailStarted).toBe(true);
      });
      view.rerender(
        <AuthStub value={{ user: bob }}>
          <MemoryRouter initialEntries={["/knowledge/ontology?section=issues&issue=issue-1"]}>
            <OntologyPage />
          </MemoryRouter>
        </AuthStub>,
      );
      // Bob's deployment has no configured quarantine lane, so his view has
      // no issues section at all — the deep link lands on his overview.
      expect(await screen.findByText("Bob current ontology")).toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: "Validation issues" })).toBeNull();

      releaseAliceList?.({
        ...ISSUE_LIST,
        entries: [{ ...ISSUE_LIST.entries[0]!, provenance: "LATE-ALICE-ISSUE-LIST" }],
      });
      releaseAliceDetail?.({ ...ISSUE_DETAIL, provenance: "LATE-ALICE-ISSUE-DETAIL" });
      await Promise.resolve();
      expect(screen.queryByText("Alice loading ontology")).toBeNull();
      expect(screen.queryByText("LATE-ALICE-ISSUE-LIST")).toBeNull();
      expect(screen.queryByText("LATE-ALICE-ISSUE-DETAIL")).toBeNull();
      expect(screen.getByText("Bob current ontology")).toBeInTheDocument();
    } finally {
      view.unmount();
      ontologyClientOverride.current = null;
    }
  });

  it("ignores late issue completions from a replaced client for the same subject", async () => {
    let aliceListStarted = false;
    let aliceDetailStarted = false;
    let releaseAliceList: ((value: OntologyIssueList) => void) | undefined;
    let releaseAliceDetail: ((value: OntologyIssueDetail) => void) | undefined;
    const aliceList = new Promise<OntologyIssueList>((resolve) => {
      releaseAliceList = resolve;
    });
    const aliceDetail = new Promise<OntologyIssueDetail>((resolve) => {
      releaseAliceDetail = resolve;
    });
    const aliceClient: OntologyInspectionClient = {
      readModel: async () => ({
        ...BASE_RESPONSE,
        issue_tracking: { state: "configured", count: 1 },
        validation_health: "issues",
      }),
      listIssues: () => {
        aliceListStarted = true;
        return aliceList;
      },
      readIssue: () => {
        aliceDetailStarted = true;
        return aliceDetail;
      },
    };
    const bobClient: OntologyInspectionClient = {
      readModel: async () => ({
        ...BASE_RESPONSE,
        issue_tracking: { state: "configured", count: 1 },
        validation_health: "issues",
      }),
      listIssues: async () => ({
        ...ISSUE_LIST,
        entries: [{ ...ISSUE_LIST.entries[0]!, provenance: "BOB-ISSUE-LIST" }],
      }),
      readIssue: async () => ({ ...ISSUE_DETAIL, provenance: "BOB-ISSUE-DETAIL" }),
    };
    ontologyClientOverride.current = aliceClient;
    const first = { access_token: "token-a", profile: { sub: "same-subject" } } as User;
    const second = { access_token: "token-b", profile: { sub: "same-subject" } } as User;
    const view = render(
      <AuthStub value={{ user: first }}>
        <MemoryRouter initialEntries={["/knowledge/ontology?section=issues&issue=issue-1"]}>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );

    try {
      await waitFor(() => {
        expect(aliceListStarted).toBe(true);
        expect(aliceDetailStarted).toBe(true);
      });
      ontologyClientOverride.current = bobClient;
      view.rerender(
        <AuthStub value={{ user: second }}>
          <MemoryRouter initialEntries={["/knowledge/ontology?section=issues&issue=issue-1"]}>
            <OntologyPage />
          </MemoryRouter>
        </AuthStub>,
      );
      expect(await screen.findByText("BOB-ISSUE-LIST")).toBeInTheDocument();
      expect(await screen.findByText("BOB-ISSUE-DETAIL")).toBeInTheDocument();

      await act(async () => {
        releaseAliceList?.({
          ...ISSUE_LIST,
          entries: [{ ...ISSUE_LIST.entries[0]!, provenance: "LATE-CLIENT-ISSUE-LIST" }],
        });
        releaseAliceDetail?.({ ...ISSUE_DETAIL, provenance: "LATE-CLIENT-ISSUE-DETAIL" });
        await Promise.all([aliceList, aliceDetail]);
      });
      expect(screen.queryByText("LATE-CLIENT-ISSUE-LIST")).toBeNull();
      expect(screen.queryByText("LATE-CLIENT-ISSUE-DETAIL")).toBeNull();
      expect(screen.getByText("BOB-ISSUE-LIST")).toBeInTheDocument();
      expect(screen.getByText("BOB-ISSUE-DETAIL")).toBeInTheDocument();
    } finally {
      view.unmount();
      ontologyClientOverride.current = null;
    }
  });

  it("ignores a late model completion from a replaced subject", async () => {
    let modelCalls = 0;
    let releaseAlice: ((value: OntologyInspectionResponse) => void) | undefined;
    const aliceModel = new Promise<OntologyInspectionResponse>((resolve) => {
      releaseAlice = resolve;
    });
    const bobResponse: OntologyInspectionResponse = {
      ...BASE_RESPONSE,
      model: {
        ...MODEL,
        provenance: { ...MODEL.provenance, title: "Bob current ontology" },
      },
    };
    const client: OntologyInspectionClient = {
      readModel: async () => {
        modelCalls += 1;
        return modelCalls === 1 ? aliceModel : bobResponse;
      },
      listIssues: async () => ISSUE_LIST,
      readIssue: async () => ISSUE_DETAIL,
    };
    ontologyClientOverride.current = client;
    const alice = { access_token: "shared-token", profile: { sub: "alice" } } as User;
    const bob = { access_token: "shared-token", profile: { sub: "bob" } } as User;
    const view = render(
      <AuthStub value={{ user: alice }}>
        <MemoryRouter>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );

    try {
      await waitFor(() => expect(modelCalls).toBe(1));
      view.rerender(
        <AuthStub value={{ user: bob }}>
          <MemoryRouter>
            <OntologyPage />
          </MemoryRouter>
        </AuthStub>,
      );
      expect(await screen.findByText("Bob current ontology")).toBeInTheDocument();
      releaseAlice?.({
        ...BASE_RESPONSE,
        model: {
          ...MODEL,
          provenance: { ...MODEL.provenance, title: "LATE-ALICE-ONTOLOGY" },
        },
      });
      await Promise.resolve();
      expect(screen.queryByText("LATE-ALICE-ONTOLOGY")).toBeNull();
      expect(screen.getByText("Bob current ontology")).toBeInTheDocument();
    } finally {
      view.unmount();
      ontologyClientOverride.current = null;
    }
  });

  it("never displays a prior client model while the same subject's bearer is loading", async () => {
    let releaseNew: (() => void) | undefined;
    server.use(
      http.get("*/api/v1/ontology/model", async ({ request }) => {
        const auth = request.headers.get("authorization");
        if (auth === "Bearer token-b")
          await new Promise<void>((resolve) => {
            releaseNew = resolve;
          });
        return HttpResponse.json(BASE_RESPONSE);
      }),
    );
    const firstUser = { access_token: "token-a", profile: { sub: "a" } } as User;
    const secondUser = { access_token: "token-b", profile: { sub: "a" } } as User;
    const view = render(
      <AuthStub value={{ user: firstUser }}>
        <MemoryRouter>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    view.rerender(
      <AuthStub value={{ user: secondUser }}>
        <MemoryRouter>
          <OntologyPage />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("Neutron Enterprise Operational Schema")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await waitFor(() => expect(releaseNew).toBeDefined());
    releaseNew?.();
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
  });

  it("clears failed model evidence and retries the same governed read", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/ontology/model", () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ detail: "temporary" }, { status: 500 })
          : HttpResponse.json(BASE_RESPONSE);
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Neutron Enterprise Operational Schema")).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});

describe("responsive and reduced-motion contract", () => {
  it("keeps explicit narrow breakpoints and a reduced-motion final-state rule", () => {
    const css = readFileSync(
      resolve(process.cwd(), "src/knowledge/ontology/ontology.css"),
      "utf-8",
    );
    expect(css).toContain("@media (max-width: 900px)");
    expect(css).toContain("@media (max-width: 560px)");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain("animation-iteration-count: 1 !important");
    expect(css).toContain("transition-duration: 0.01ms !important");
  });
});
