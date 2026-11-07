// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { createRef } from "react";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import { EvidenceDrawer } from "./EvidenceDrawer";

const body: AgentEvidenceEnvelopeWire = {
  schema_version: 1,
  agent_id: "a",
  run_id: "r",
  run_status: "completed",
  mapping_state: "complete",
  mapping_message: "Claim mapping complete",
  output: {
    verdict: "refused",
    state: "unavailable",
    message: "Answer unavailable",
    display: null,
  },
  claims: [
    {
      claim_id: "c",
      claim_verdict: "verified",
      state: "available",
      message: "Claim available",
      display: "Prose says Grounded Direct",
      evidence_id: "e",
      association_hash: "a",
      verifier_event_id: "v",
      verifier_receipt_hash: "h",
    },
  ],
  evidence: [
    {
      evidence_id: "e",
      state: "available",
      message: "Evidence available",
      assurance: "extracted",
      locator: {
        entity_id: "entity / opaque?#",
        graph_path: [
          {
            subject: {
              entity_class: "Account",
              entity_key: "n1",
              display: "First",
              assurance: "source",
              source_ref: "crm:n1",
            },
            edge_kind: "RELATED_TO",
            obj: {
              entity_class: "Account",
              entity_key: "n2",
              display: "Second",
              assurance: "confirmed",
              source_ref: "crm:n2",
            },
            source_system: "crm",
            source_ref: "crm:x1",
            assurance: "extracted",
            review_state: "accepted",
          },
          {
            subject: {
              entity_class: "Account",
              entity_key: "n2",
              display: "Second",
              assurance: "confirmed",
              source_ref: "crm:n2",
            },
            edge_kind: "RELATED_TO",
            obj: {
              entity_class: "Account",
              entity_key: "n3",
              display: "Third",
              assurance: "source",
              source_ref: "crm:n3",
            },
            source_system: "crm",
            source_ref: "crm:x2",
            assurance: "confirmed",
            review_state: "pending",
          },
        ],
      },
    },
  ],
  governance: {
    status: "unavailable",
    delegated_user_label: null,
    agent_service_identity: null,
    connector_credential_owner_label: null,
    effective_scopes: [],
    policy_environment: null,
    tool_tier: null,
    tool_decision: null,
    // All six joins came back None and no receipt matched, so
    // `project_governance` emits all seven tokens. `[]` beside seven nulls is a
    // wire it cannot produce, and a fixture that cannot happen hides bugs.
    unavailable_reasons: [
      "delegation_join_unavailable",
      "service_identity_join_unavailable",
      "credential_owner_join_unavailable",
      "scope_join_unavailable",
      "policy_join_unavailable",
      "tool_decision_join_unavailable",
      "audit_receipt_join_unavailable",
    ],
  },
};

describe("PRE-015D Evidence drawer labels", () => {
  it("renders only server-fact labels, explanations, and exact per-hop review states", async () => {
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => body) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Refused")).toHaveAttribute(
      "title",
      expect.stringContaining("completed Run"),
    );
    expect(screen.getByText("Verified")).toHaveAttribute(
      "title",
      expect.stringContaining("verifier"),
    );
    expect(screen.getByText("Extracted")).toHaveAttribute(
      "title",
      expect.stringContaining("extracted"),
    );
    expect(screen.getByRole("list", { name: "Graph hop review states" })).toHaveTextContent(
      "Reviewed and acceptedWaiting for review",
    );
    expect(screen.getByRole("link", { name: "Open in Graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?mode=graph&agent=a&run=r&evidence=e",
    );
  });

  it("links available canonical Graph-path evidence without an invented entity id", async () => {
    const pathOnly: AgentEvidenceEnvelopeWire = {
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: { ...body.evidence[0]!.locator!, entity_id: null },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => pathOnly) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("link", { name: "Open in Graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?mode=graph&agent=a&run=r&evidence=e",
    );
    expect(screen.queryByRole("list", { name: "Graph hop review states" })).toBeInTheDocument();
  });

  it("omits review-state chrome when every canonical hop has truthful null state", async () => {
    const allNull: AgentEvidenceEnvelopeWire = {
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: {
            entity_id: null,
            graph_path: body.evidence[0]!.locator!.graph_path!.map((hop) => ({
              ...hop,
              review_state: null,
            })),
          },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => allNull) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("link", { name: "Open in Graph" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Graph hop review states" })).not.toBeInTheDocument();
  });

  it("names each receipt-bound served document version and links its exact journey", async () => {
    const searchRead: AgentEvidenceEnvelopeWire = {
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: {
            kind: "knowledge_search",
            chunk_ids: ["doc-1"],
            served_document_versions: [
              {
                document_id: "doc-1",
                version_seq: 2,
                content_hash: "a".repeat(64),
                display_name: "Debit Card Dispute Handling SOP.docx",
              },
            ],
          },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => searchRead) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );

    const link = await screen.findByRole("link", {
      name: "Debit Card Dispute Handling SOP.docx · v2",
    });
    expect(link).toHaveAttribute("href", "/knowledge/documents?document=doc-1&version=2");
    expect(screen.getByRole("list", { name: "Served document versions" })).toBeInTheDocument();
  });

  it("names the served versions a GRAPH hop rested on, beside its graph link", async () => {
    // POST_DEMO #13: the server used to project `served_document_versions` for
    // search reads only, and the live box served 62 graph reads to zero search
    // reads — so no answer on stage could say which version it rested on. The
    // graph arm now carries the same binding; this pins that the drawer shows
    // BOTH exits rather than trading one for the other.
    const graphRead: AgentEvidenceEnvelopeWire = {
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: {
            entity_id: null,
            graph_path: body.evidence[0]!.locator!.graph_path!,
            served_document_versions: [
              {
                document_id: "doc-9",
                version_seq: 4,
                content_hash: "b".repeat(64),
                display_name: "graph-customer-mer-cus-001.negraph.json",
              },
            ],
          },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => graphRead) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("link", { name: "Open in Graph" })).toBeInTheDocument();
    const version = screen.getByRole("link", {
      name: "graph-customer-mer-cus-001.negraph.json · v4",
    });
    expect(version).toHaveAttribute("href", "/knowledge/documents?document=doc-9&version=4");
  });

  it("renders no version chrome when the server sent no served-version binding", async () => {
    const unbound: AgentEvidenceEnvelopeWire = {
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: { kind: "knowledge_search", chunk_ids: ["doc-1"] },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => unbound) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Evidence available")).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "Served document versions" }),
    ).not.toBeInTheDocument();
  });
});

describe("the served row anchor renders as the row's identity", () => {
  const anchor = "9f".repeat(32);
  const verified: AgentEvidenceEnvelopeWire = {
    ...body,
    evidence: [
      {
        ...body.evidence[0]!,
        locator: { kind: "verified_t3", row_anchor: anchor, receipt_hash: "c".repeat(64) },
      },
    ],
  };

  it("draws the SERVED anchor — short form on screen, full value in the title and copy", async () => {
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => verified) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Row identity")).toBeInTheDocument();
    // The short form is the served value's own first 8 chars, and the full
    // served value rides in the title — displayed, never recomputed.
    expect(screen.getByText(anchor.slice(0, 8))).toHaveAttribute("title", anchor);
    expect(screen.getByRole("button", { name: "Copy row anchor" })).toBeInTheDocument();
  });

  it("a withheld record's anchor never renders, and the empty arm draws none", async () => {
    const withheld: AgentEvidenceEnvelopeWire = {
      ...verified,
      evidence: [
        { ...verified.evidence[0]!, state: "withheld", message: "Evidence withheld" },
        {
          ...verified.evidence[0]!,
          evidence_id: "e2",
          message: "No matching rows",
          locator: { kind: "verified_t3_empty", receipt_hash: "c".repeat(64) },
        },
      ],
    };
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => withheld) } as never}
          agentId="a"
          runId="r"
          view="evidence"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Evidence withheld")).toBeInTheDocument();
    expect(screen.queryByText("Row identity")).not.toBeInTheDocument();
    expect(screen.queryByText(anchor.slice(0, 8))).not.toBeInTheDocument();
  });
});

describe("a row that cannot be opened is not a control", () => {
  // The underlying cause is a backend gap — a locator kind with no parser arm —
  // tracked elsewhere. These pin how it READS, which is the part a reader meets.
  const unresolved: AgentEvidenceEnvelopeWire = {
    ...body,
    // No claims: this fixture is about the evidence row itself, and a claim
    // button in the same drawer would blur what "no control on the row" means.
    claims: [],
    evidence: [
      {
        evidence_id: "e",
        state: "locator_unavailable",
        message: "Evidence locator unavailable",
        assurance: "unavailable",
        locator: null,
      },
    ],
  };

  function draw(envelope: AgentEvidenceEnvelopeWire, view: "evidence" | "governance" = "evidence") {
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => envelope) } as never}
          agentId="a"
          runId="r"
          view={view}
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );
  }

  it("says once, in plain words, that the pointer did not resolve — and offers no button", async () => {
    draw(unresolved);
    const row = await screen.findByText(/could not resolve where it came from/);
    expect(row.tagName).toBe("P");
    // The row used to be a button labelled with the server literal, and a badge
    // reading "Unavailable" sat beside it. Both are gone: one sentence, no
    // control, and no second spelling of the same absence.
    expect(screen.queryByText("Evidence locator unavailable")).toBeNull();
    expect(screen.queryByText("Not established")).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
    const buttons = screen.getAllByRole("button").map((b) => b.textContent);
    expect(buttons).toEqual(["Close", "Export governed JSON"]);
  });

  it("never claims the evidence is missing — only that the way back to it is", async () => {
    draw(unresolved);
    const row = await screen.findByText(/could not resolve where it came from/);
    expect(row).toHaveTextContent("This Run recorded a piece of evidence here");
    expect(row).toHaveTextContent("nothing to open");
  });

  it("keeps a state this build has never seen non-interactive, in the server's own words", async () => {
    draw({
      ...unresolved,
      evidence: [
        {
          ...unresolved.evidence[0]!,
          state: "quarantined" as never,
          message: "Evidence quarantined",
        },
      ],
    });
    const row = await screen.findByText("Evidence quarantined");
    expect(row.tagName).toBe("P");
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Close",
      "Export governed JSON",
    ]);
  });

  it("an available row is still a button, still badged", async () => {
    draw(body);
    expect(await screen.findByRole("button", { name: "Evidence available" })).toBeInTheDocument();
    expect(screen.getByText("Extracted")).toBeInTheDocument();
  });

  it("badges a WITHHELD row, which cannot be opened — the badge is not part of the button", async () => {
    // The badge used to live inside the openable branch, and "openable" is
    // `state === "available"`, so the Withheld badge was unreachable on an
    // evidence row while `assuranceVocabulary.test.ts` asserted it. The row
    // rendered as a bare sentence that reads like a gap in the record. It is
    // not one: the server HAS this and this read did not get it, and the server
    // projects the row with `assurance="unavailable"`, so only the state can
    // carry that distinction. WHY it was not released is not the badge's to
    // say — five conditions reach `state="withheld"` and only one of them is a
    // decision about access (assuranceVocabulary.ts, rule 2).
    draw({
      ...unresolved,
      evidence: [
        {
          ...unresolved.evidence[0]!,
          state: "withheld",
          message: "Evidence withheld — access changed",
        },
      ],
    });
    const row = await screen.findByText("Evidence withheld — access changed");
    expect(row.tagName).toBe("P");
    expect(screen.getByText("Withheld")).toHaveAttribute(
      "title",
      expect.stringContaining("did not release it on this read"),
    );
    // Withheld is a record the server holds, never an empty one — the row must
    // not also wear the badge that means this console established nothing.
    expect(screen.queryByText("Not established")).toBeNull();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Close",
      "Export governed JSON",
    ]);
  });

  it("admits a hop review state it does not know instead of printing the raw token", async () => {
    const hop = body.evidence[0]!.locator!.graph_path![0]!;
    draw({
      ...body,
      evidence: [
        {
          ...body.evidence[0]!,
          locator: { graph_path: [{ ...hop, review_state: "escalated" as never }] },
        },
      ],
    });
    const list = await screen.findByRole("list", { name: "Graph hop review states" });
    expect(list).toHaveTextContent("Not recognised");
    expect(list).not.toHaveTextContent("escalated");
    expect(within(list).getByText("Not recognised")).toHaveAttribute(
      "title",
      expect.stringContaining("escalated"),
    );
  });
});

describe("the governance panel says what resolved, and names every lookup that did not", () => {
  function drawGovernance(governance: AgentEvidenceEnvelopeWire["governance"]) {
    render(
      <MemoryRouter>
        <EvidenceDrawer
          client={{ getAgentRunEvidence: vi.fn(async () => ({ ...body, governance })) } as never}
          agentId="a"
          runId="r"
          view="governance"
          focusedId={null}
          onFocus={() => {}}
          onClose={() => {}}
          trigger={createRef()}
        />
      </MemoryRouter>,
    );
  }

  it("replaces four <dd>Unavailable</dd> rows with the facts held and one line naming the gaps", async () => {
    drawGovernance({
      ...body.governance,
      status: "partial",
      delegated_user_label: "Owner",
      tool_decision: "approval_required",
      tool_tier: 2,
      unavailable_reasons: [
        "service_identity_join_unavailable",
        "credential_owner_join_unavailable",
        "scope_join_unavailable",
        "policy_join_unavailable",
      ],
    });
    expect(
      await screen.findByText(
        "Some governance lookups for this Run did not resolve; those are named below.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("Approval required first")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Did not resolve: agent service, credential owner, effective scopes, policy environment.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Unavailable")).toBeNull();
    expect(screen.queryByText("approval_required")).toBeNull();
    expect(screen.queryByText("Governance partial")).toBeNull();
  });

  it("names the gap when `partial` fires on a join this pane does not display", async () => {
    // ⚠️ THE P1 THIS ROUND. All FOUR displayed facts joined; the scope and
    // policy joins did not, so `project_governance` set `partial`. The gap line
    // used to be derived from the four fields on screen, and with none of them
    // null it did not render at all — leaving the reader told part of the
    // record was missing, with nothing named, on the pane that exists to
    // account for the record.
    drawGovernance({
      ...body.governance,
      status: "partial",
      delegated_user_label: "Dana Whitfield",
      agent_service_identity: "svc-disputes-agent",
      connector_credential_owner_label: "Payments Ops",
      tool_decision: "allow",
      tool_tier: 1,
      unavailable_reasons: ["scope_join_unavailable", "policy_join_unavailable"],
    });
    expect(await screen.findByText("Dana Whitfield")).toBeInTheDocument();
    expect(screen.getByText("svc-disputes-agent")).toBeInTheDocument();
    expect(screen.getByText("Payments Ops")).toBeInTheDocument();
    expect(screen.getByText("Allowed")).toBeInTheDocument();
    expect(
      screen.getByText("Did not resolve: effective scopes, policy environment."),
    ).toBeInTheDocument();
    // And the sentence above them no longer says the rest were never recorded.
    expect(screen.queryByText(/never recorded/)).toBeNull();
  });

  it("names the half of the tool join that is absent, not the half on screen", async () => {
    // `tool_decision_join_unavailable` fires when EITHER field is None. The
    // decision is drawn here; naming the whole token would contradict the row.
    drawGovernance({
      ...body.governance,
      status: "partial",
      delegated_user_label: "Owner",
      agent_service_identity: "svc",
      connector_credential_owner_label: "Ops",
      tool_decision: "deny",
      tool_tier: null,
      unavailable_reasons: ["tool_decision_join_unavailable"],
    });
    expect(await screen.findByText("Denied")).toBeInTheDocument();
    expect(screen.getByText("Did not resolve: tool tier.")).toBeInTheDocument();
  });

  it("names the missing receipt, then names the unresolved joins — never in the same breath", async () => {
    // The fixture: no receipt AND no join resolved, the case that used to be
    // summed up as "No governance facts are on file for this Run." That sentence
    // was a statement about the SERVER'S records, and `unavailable` is not one —
    // it is set from the audit receipts alone. The panel now says the two things
    // separately, and the gap line is no longer suppressed here, because it is
    // no longer a second spelling of the opening line. It names all SIX joins
    // now, including the three this pane does not draw a row for.
    drawGovernance(body.governance);
    expect(
      await screen.findByText(
        "No audit receipt could be tied to this Run, so nothing here can be checked against the audit log.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Did not resolve: delegated user, agent service, credential owner, effective scopes, policy environment, tool decision, tool tier.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Governance details unavailable.")).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("never says no facts are on file directly above the facts", async () => {
    // ⚠️ THE REGRESSION THIS FILE MISSED. `governance_projection` sets
    // `status = "unavailable" if not valid` from the audit receipts ALONE, and
    // that branch takes precedence over every join reason — so all four
    // producer-authored facts arrive on an `unavailable` envelope, exactly as
    // below. The old fixture paired that status with four nulls, so the <dl>
    // was empty and the contradiction could not appear. It appears here.
    drawGovernance({
      ...body.governance,
      status: "unavailable",
      delegated_user_label: "Dana Whitfield",
      agent_service_identity: "svc-disputes-agent",
      connector_credential_owner_label: "Payments Ops",
      tool_decision: "deny",
      tool_tier: 3,
      unavailable_reasons: [
        "scope_join_unavailable",
        "policy_join_unavailable",
        "audit_receipt_join_unavailable",
      ],
    });
    expect(await screen.findByText("Dana Whitfield")).toBeInTheDocument();
    expect(screen.getByText("svc-disputes-agent")).toBeInTheDocument();
    expect(screen.getByText("Payments Ops")).toBeInTheDocument();
    expect(screen.getByText("Denied")).toBeInTheDocument();
    // The sentence above them says nothing about whether they exist.
    expect(screen.queryByText(/No governance facts/)).toBeNull();
    // The gap line names the two joins that did not resolve, and leaves the
    // receipt to the opening sentence — one fact, said once.
    expect(
      screen.getByText("Did not resolve: effective scopes, policy environment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Did not resolve:.*receipt/)).toBeNull();
    expect(
      screen.getByText(
        "No audit receipt could be tied to this Run, so nothing here can be checked against the audit log.",
      ),
    ).toBeInTheDocument();
  });

  it("admits a governance status this build does not know, by name", async () => {
    drawGovernance({ ...body.governance, status: "degraded" as never });
    expect(await screen.findByText(/does not know the governance status/)).toHaveTextContent(
      "degraded",
    );
  });
});
