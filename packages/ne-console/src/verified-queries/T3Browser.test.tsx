// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { VerifiedInvokeWire, VerifiedTemplateWire, VerifiedTemplatesWire } from "../api/types";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { VerifiedQueriesPage } from "./VerifiedQueriesPage";
import { shortIri, t3ClassRoster } from "./T3Browser";

const CLASS_IRI = "https://neutron.enterprise/schema/meridian/DisasterDeclaration";
const RECORD_IRI = "urn:ne:meridian:disaster:DR-4830";

const CLASS_TEMPLATE: VerifiedTemplateWire = {
  template_id: "t3-records-in-class",
  version: 1,
  title: "Every Tier-3 record of one declared Meridian class the caller may see.",
  tool_name: "read.kg.t3_records_in_class",
  args_schema: {
    type: "object",
    additionalProperties: false,
    required: ["q_class"],
    properties: {
      q_class: { title: "Q Class", type: "string", enum: ["Customer", "DisasterDeclaration"] },
    },
  },
  columns: ["record", "class"],
  citation_var: "record",
  label_var: null,
  max_rows: 100,
  graph_classes: ["bank.customer.read", "bank.servicing.read", "bank.dispute.read"],
};

const RECORD_TEMPLATE: VerifiedTemplateWire = {
  ...CLASS_TEMPLATE,
  template_id: "t3-record",
  title: "Every served field of one Tier-3 record the caller may see.",
  tool_name: "read.kg.t3_record",
  args_schema: {
    type: "object",
    additionalProperties: false,
    required: ["q_record"],
    properties: { q_record: { title: "Q Record", type: "string", maxLength: 96 } },
  },
  columns: ["record", "predicate", "value"],
};

const CATALOG: VerifiedTemplatesWire = {
  enabled: true,
  registry_hash: "r".repeat(64),
  templates: [CLASS_TEMPLATE, RECORD_TEMPLATE],
};

function invokeResult(
  template: VerifiedTemplateWire,
  args: Record<string, unknown>,
  rows: string[][],
): VerifiedInvokeWire {
  return {
    replay_match: true,
    answer: {
      template_id: template.template_id,
      template_version: template.version,
      args,
      columns: template.columns,
      rows,
      citations: rows.map((row) => row[0] ?? ""),
      truncated: false,
      row_limit: 100,
      returned_rows: rows.length,
      total_rows: rows.length,
      truncation_reason: null,
      config_hash: "c".repeat(64),
      ontology_version: "meridian-v1",
      executed_at: "2026-08-27T12:00:00Z",
    },
  };
}

function renderBrowser(entry: string) {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={[entry]}>
        <PrincipalProvider>
          <main>
            <VerifiedQueriesPage />
          </main>
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("Tier-3 record browser", () => {
  it("derives the class roster from the served enum alone", () => {
    expect(t3ClassRoster(CATALOG)).toEqual(["Customer", "DisasterDeclaration"]);
    expect(
      t3ClassRoster({ enabled: true, registry_hash: null, templates: [RECORD_TEMPLATE] }),
    ).toEqual([]);
  });

  it("shortens known IRIs for display without inventing names", () => {
    expect(shortIri(CLASS_IRI)).toBe("DisasterDeclaration");
    expect(shortIri("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")).toBe("type");
    expect(shortIri("https://elsewhere.example/x")).toBe("https://elsewhere.example/x");
  });

  it("enumerates a class and opens a record through the two reviewed templates only", async () => {
    server.use(http.get("*/api/v1/verified/templates", () => HttpResponse.json(CATALOG)));
    const invoked: { template_id: string; args: Record<string, unknown> }[] = [];
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        const body = (await request.json()) as {
          template_id: string;
          args: Record<string, unknown>;
        };
        invoked.push({ template_id: body.template_id, args: body.args });
        if (body.template_id === "t3-records-in-class") {
          return HttpResponse.json(
            invokeResult(CLASS_TEMPLATE, body.args, [[RECORD_IRI, CLASS_IRI]]),
          );
        }
        return HttpResponse.json(
          invokeResult(RECORD_TEMPLATE, body.args, [
            [RECORD_IRI, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", CLASS_IRI],
            [RECORD_IRI, "https://neutron.enterprise/schema/meridian/declaration_id", "DR-4830"],
            [RECORD_IRI, "https://neutron.enterprise/schema/meridian/county", "Richmond"],
          ]),
        );
      }),
    );
    renderBrowser("/verified-queries?view=browse");
    await userEvent.click(await screen.findByRole("button", { name: "DisasterDeclaration" }));
    await userEvent.click(await screen.findByRole("button", { name: RECORD_IRI }));
    const detail = await screen.findByRole("region", { name: "Record detail" });
    expect(detail).toHaveTextContent("declaration_id");
    expect(detail).toHaveTextContent("DR-4830");
    expect(detail).toHaveTextContent("Richmond");
    expect(invoked).toEqual([
      { template_id: "t3-records-in-class", args: { q_class: "DisasterDeclaration" } },
      { template_id: "t3-record", args: { q_record: RECORD_IRI } },
    ]);
  });

  it("says honestly when the browse templates are not registered", async () => {
    server.use(
      http.get("*/api/v1/verified/templates", () =>
        HttpResponse.json({ enabled: true, registry_hash: null, templates: [] }),
      ),
    );
    renderBrowser("/verified-queries?view=browse");
    expect(await screen.findByText(/browse templates .* are not registered/i)).toBeInTheDocument();
  });

  it("renders a zero-row class as an honest ACL/absence gap, never an error", async () => {
    server.use(http.get("*/api/v1/verified/templates", () => HttpResponse.json(CATALOG)));
    server.use(
      http.post("*/api/v1/verified/invoke", async ({ request }) => {
        const body = (await request.json()) as { args: Record<string, unknown> };
        return HttpResponse.json(invokeResult(CLASS_TEMPLATE, body.args, []));
      }),
    );
    renderBrowser("/verified-queries?view=browse&cls=Customer");
    expect(await screen.findByText(/no Customer records for your role/)).toBeInTheDocument();
  });
});
