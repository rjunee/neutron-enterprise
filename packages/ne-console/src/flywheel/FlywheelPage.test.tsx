// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT, as rendered.
//
// The measured starting point for this rebuild was zero sortable columns in the
// entire product and a steward surface built out of cards. So the assertions
// below are about the things whose absence was the defect — a real table with
// the stated default sort, filters that narrow the ROWS BY IDENTITY (a count
// assertion passes for the wrong rows), a live count, a pane that can decide.
//
// And about the things whose PRESENCE would be the defect:
//   * a role-boundary sentence standing in for a read that was LOADING or that
//     FAILED. A refusal is a 403 and nothing else; saying "your role" about an
//     outage is a false statement about someone's authorization.
//   * proposal-carried markup rendered as anything but text.
//   * a client-supplied actor on any decision.
//   * a decided proposal leaving the queue by a local splice.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { SkillLibraryRow } from "../api/types";
import { absoluteTime } from "../primitives/format";
import { AuthStub } from "../test/authStub";
import {
  INERT_ATTACK,
  STEWARD_DIGEST,
  STEWARD_PROPOSALS,
  STEWARD_PROPOSAL_SKILL,
  STEWARD_SKILLS,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { FlywheelPage } from "./FlywheelPage";

function renderPage(path = "/improvement") {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <FlywheelPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** ROW IDENTITY, not row count: the Lane cell names which proposal is on screen,
 * in the order the table renders it. */
function laneOrder(): string[] {
  const table = screen.getByRole("table", { name: "Proposals" });
  return [...table.querySelectorAll<HTMLElement>("tbody tr.ne-tr")].map(
    (row) => row.querySelectorAll("td")[0]?.textContent ?? "",
  );
}

function skillNames(): string[] {
  const table = screen.getByRole("table", { name: "Skill library" });
  return [...table.querySelectorAll<HTMLElement>("tbody tr.ne-tr")].map(
    (row) => row.querySelectorAll("td")[0]?.textContent ?? "",
  );
}

const REFUSAL = "Your role doesn’t include Improvement. Contact your administrator for access.";

// ===========================================================================
// QUEUE — the table standard
// ===========================================================================

describe("§11 queue — the table", () => {
  it("defaults to Created descending, which is not the served order", async () => {
    renderPage();
    await screen.findByText("Retrieval");
    // Served: content · policy · retrieval. Created desc: retrieval (Jul 6
    // 18:15) · content (Jul 6 09:00) · policy (Jul 5 14:30). A default sort that
    // did nothing would read the served order.
    expect(laneOrder()).toEqual(["Retrieval", "Content", "Policy"]);
    const created = screen.getByRole("columnheader", { name: /Created/ });
    expect(created).toHaveAttribute("aria-sort", "descending");
  });

  it("makes exactly the six columns §11 names sortable, and marks the active one", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");

    for (const header of ["Lane", "Title", "Confidence", "Priority", "Created", "Status"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${header}$`) })).toBeInTheDocument();
    }
    // Kind is a served enum with no order a reader would agree on.
    expect(screen.queryByRole("button", { name: /^Kind$/ })).toBeNull();

    await user.click(screen.getByRole("button", { name: /^Confidence$/ }));
    expect(screen.getByRole("columnheader", { name: /Confidence/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(laneOrder()).toEqual(["Policy", "Content", "Retrieval"]);

    await user.click(screen.getByRole("button", { name: /^Confidence$/ }));
    expect(laneOrder()).toEqual(["Retrieval", "Content", "Policy"]);
  });

  it("sorts Priority by severity, not alphabetically", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByRole("button", { name: /^Priority$/ }));
    // Content and Policy are "high", Retrieval is "medium". Sorted as plain
    // strings, "high" lands after "medium" and Retrieval would come FIRST.
    const rows = laneOrder();
    expect(rows[2]).toBe("Retrieval");
    expect([rows[0], rows[1]].sort()).toEqual(["Content", "Policy"]);
  });

  it("the lane filter narrows the RENDERED ROWS, and the count describes them", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    expect(screen.getByText("3 proposals")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Lane"), "policy");

    expect(laneOrder()).toEqual(["Policy"]);
    expect(screen.getByText("1 proposals")).toBeInTheDocument();
    // The reason the list is short stands next to the list.
    expect(screen.getByRole("button", { name: "Clear Lane: Policy" })).toBeInTheDocument();
  });

  it("the status filter narrows the RENDERED ROWS", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.selectOptions(screen.getByLabelText("Status"), "approved");
    // Only prop-policy-2 is approved on the wire.
    expect(laneOrder()).toEqual(["Policy"]);
  });

  it("search-within narrows the RENDERED ROWS", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.type(screen.getByLabelText("Filter proposals…"), "9c31f0a2");
    expect(laneOrder()).toEqual(["Retrieval"]);
  });

  it("an empty queue renders §11's copy", async () => {
    server.use(http.get("*/api/v1/steward/proposals", () => HttpResponse.json({ proposals: [] })));
    renderPage();
    expect(await screen.findByText("No open proposals.")).toBeInTheDocument();
    expect(screen.getByText("0 proposals")).toBeInTheDocument();
  });

  it("a filtered-to-nothing queue says so and offers the whole window back", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.selectOptions(screen.getByLabelText("Lane"), "ontology");
    expect(await screen.findByText("No matching proposals.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show all 3" })).toBeInTheDocument();
  });
});

// ===========================================================================
// THE FOUR STATES — and the line between a refusal and an outage
// ===========================================================================

describe("§11 queue — refused is not unreachable", () => {
  it("renders the SERVER's refusal on a 403", async () => {
    server.use(
      http.get("*/api/v1/steward/proposals", () =>
        HttpResponse.json({ detail: "not a steward" }, { status: 403 }),
      ),
    );
    renderPage();
    expect(await screen.findByText(REFUSAL)).toBeInTheDocument();
    // A refused read never produces a confident count.
    expect(screen.queryByText(/proposals$/)).toBeNull();
  });

  it("an unreachable read renders the load error and NEVER the role sentence", async () => {
    server.use(http.get("*/api/v1/steward/proposals", () => HttpResponse.error()));
    renderPage();
    expect(await screen.findByText("Couldn’t load proposals.")).toBeInTheDocument();
    expect(screen.queryByText(REFUSAL)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("a 500 is an outage, not a role boundary", async () => {
    server.use(
      http.get("*/api/v1/steward/proposals", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderPage();
    expect(await screen.findByText("Couldn’t load proposals.")).toBeInTheDocument();
    expect(screen.queryByText(REFUSAL)).toBeNull();
  });

  it("a read still in flight announces loading and claims nothing about the role", () => {
    server.use(http.get("*/api/v1/steward/proposals", () => new Promise<never>(() => {})));
    renderPage();
    expect(screen.getByText("Loading proposals")).toBeInTheDocument();
    expect(screen.queryByText(REFUSAL)).toBeNull();
    expect(screen.queryByText("No open proposals.")).toBeNull();
  });
});

// ===========================================================================
// THE PANE — what it shows, and what it submits
// ===========================================================================

describe("§11 pane — the proposal", () => {
  it("opens on a row click and shows the fields §11 lists", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));

    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("cluster-content-1")).toBeInTheDocument();
    expect(within(pane).getByText("0.82")).toBeInTheDocument();
    expect(within(pane).getByText("2 events · 3 signals")).toBeInTheDocument();
    expect(pane.querySelector(".ne-fly__diff")?.textContent).toMatch(/Proposal review/);
  });

  it("renders proposal-carried markup as text and never as markup", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));

    const pane = screen.getByRole("dialog");
    expect(pane.textContent).toContain(INERT_ATTACK);
    expect(pane.querySelector("img")).toBeNull();
    expect(pane.querySelector("script")).toBeNull();
    expect(within(pane).queryByRole("heading", { name: "not-a-heading" })).toBeNull();
    expect((window as unknown as { __ne_pwned?: boolean }).__ne_pwned).toBeUndefined();
  });

  it("Approve submits with the bearer and NO client-supplied identity, and the row leaves via the server", async () => {
    let auth: string | null = null;
    let body: Record<string, unknown> = {};
    let listed = 0;
    server.use(
      http.get("*/api/v1/steward/proposals", () => {
        listed += 1;
        // The SECOND read is the server's — the decided row is simply not in it.
        const rows = listed === 1 ? STEWARD_PROPOSALS : STEWARD_PROPOSALS.slice(1);
        return HttpResponse.json({ proposals: rows });
      }),
      http.post("*/api/v1/steward/proposals/:id/approve", async ({ request }) => {
        auth = request.headers.get("authorization");
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review_id: "rev-1",
          proposal_id: "prop-content-1",
          action: "approve",
          from_status: "proposed",
          to_status: "approved",
          actor: "alice@example.com",
          surface: "console",
          session_id: "s",
          rejection_code: null,
          note: null,
          applied_ref: null,
          config_hash: "cfg-1",
          created_at: "2026-07-07T10:00:00Z",
        });
      }),
    );

    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(laneOrder()).toEqual(["Retrieval", "Policy"]));
    expect(auth).toBe("Bearer test-access-token");
    expect(Object.keys(body)).toEqual(["note"]);
    expect(body).not.toHaveProperty("actor");
    expect(body).not.toHaveProperty("principal");
    expect(body).not.toHaveProperty("surface");
    // The review event is the only account of who decided and when.
    expect(screen.getByText(/approve by alice@example.com at/)).toBeInTheDocument();
  });

  it("Reject sends the typed reason code the steward chose", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/reject", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review_id: "r",
          proposal_id: "prop-content-1",
          action: "reject",
          from_status: "proposed",
          to_status: "rejected",
          actor: "alice@example.com",
          surface: "console",
          session_id: "s",
          rejection_code: "unsafe",
          note: "",
          applied_ref: null,
          config_hash: "cfg-1",
          created_at: "2026-07-07T10:00:00Z",
        });
      }),
    );

    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.selectOptions(screen.getByLabelText("Reason"), "unsafe");
    await user.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(body).toHaveProperty("code", "unsafe"));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Rejected")).toBeInTheDocument();
    expect(within(pane).queryByLabelText("PENDING")).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("a proposal that leaves the settled queue elsewhere is no longer actionable", async () => {
    let listed = 0;
    server.use(
      http.get("*/api/v1/steward/proposals", () => {
        listed += 1;
        return HttpResponse.json({ proposals: listed === 1 ? STEWARD_PROPOSALS : [] });
      }),
      http.post("*/api/v1/steward/proposals/:id/approve", () =>
        HttpResponse.json({ detail: "already decided" }, { status: 409 }),
      ),
    );

    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));
    await user.click(screen.getByRole("button", { name: "Approve" }));

    const pane = screen.getByRole("dialog");
    expect(
      await within(pane).findByText("Already decided by another steward."),
    ).toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("Record apply is offered only on an approved proposal and will not submit without a reference", async () => {
    const user = renderPage();
    await screen.findByText("Retrieval");

    // A proposed row offers Approve and Reject, never Record apply: the server
    // rejects proposed→applied, so the console does not offer the path.
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));
    expect(screen.queryByRole("button", { name: "Record apply" })).toBeNull();
    await user.keyboard("{Escape}");

    await user.click(screen.getByText("[flywheel:policy] proposal 2b6a9d0c1e4f7788"));
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Record apply" }));
    expect(screen.getByRole("button", { name: "Record apply" })).toBeDisabled();

    await user.type(screen.getByLabelText("Applied reference"), "https://example.test/pr/9");
    expect(screen.getByRole("button", { name: "Record apply" })).toBeEnabled();
  });

  it("a failed submission renders §11's copy plus the server's own sentence", async () => {
    server.use(
      http.post("*/api/v1/steward/proposals/:id/approve", () =>
        HttpResponse.json({ detail: "already decided" }, { status: 409 }),
      ),
    );
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("[flywheel:content] proposal 8f19c2a41b0d7e33"));
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Couldn’t submit. Try again.")).toBeInTheDocument();
    expect(screen.getByText("already decided")).toBeInTheDocument();
  });

  it("a ?proposal= deep link opens the pane on that row", async () => {
    renderPage("/improvement?tab=queue&proposal=prop-retrieval-3");
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("cluster-retrieval-3")).toBeInTheDocument();
  });
});

// ===========================================================================
// THE SKILL CANDIDATE — untrusted text, and the visibility decision
// ===========================================================================

describe("§11 pane — a skill candidate", () => {
  function withSkill() {
    server.use(
      http.get("*/api/v1/steward/proposals", () =>
        HttpResponse.json({ proposals: [STEWARD_PROPOSAL_SKILL] }),
      ),
    );
  }

  it("renders the candidate field by field with §11's warning line, verbatim", async () => {
    withSkill();
    const user = renderPage();
    await screen.findByText("[flywheel:skills] proposal sig9");
    await user.click(screen.getByText("[flywheel:skills] proposal sig9"));

    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("deploy-runbook")).toBeInTheDocument();
    expect(
      within(pane).getByText("Repeat the standard deploy-and-verify workflow"),
    ).toBeInTheDocument();
    expect(within(pane).getByText("deploy the service")).toBeInTheDocument();
    expect(within(pane).getByText("grp:platform")).toBeInTheDocument();
    expect(within(pane).getByText("forged from session sess-1 on 2026-07-07")).toBeInTheDocument();
    expect(within(pane).getByText("Skill text")).toBeInTheDocument();
    expect(
      within(pane).getByText(
        "⚠ On apply this text is added to every agent that matches it. Review it as untrusted input.",
      ),
    ).toBeInTheDocument();
    // The body is the text an apply injects — it renders inert.
    expect(pane.querySelector(".ne-fly__skilltext")?.textContent).toContain(INERT_ATTACK);
    expect(pane.querySelector("img")).toBeNull();
  });

  it("Apply requires the visibility decision — labels XOR everyone, never both, never neither", async () => {
    withSkill();
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/apply-skill", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review: {
            review_id: "r",
            proposal_id: "prop-skill-9",
            action: "apply",
            from_status: "approved",
            to_status: "applied",
            actor: "alice@example.com",
            surface: "console",
            session_id: "s",
            rejection_code: null,
            note: "",
            applied_ref: "skill:deploy-runbook@v1",
            config_hash: "cfg-1",
            created_at: "2026-07-07T10:00:00Z",
          },
          name: "deploy-runbook",
          version: 1,
          content_hash: "d",
          acl_labels: ["grp:platform"],
        });
      }),
    );

    const user = renderPage();
    await screen.findByText("[flywheel:skills] proposal sig9");
    await user.click(screen.getByText("[flywheel:skills] proposal sig9"));
    await user.click(screen.getByRole("button", { name: "Apply" }));

    // The suggested label arrives checked, so the form starts satisfiable…
    expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
    // …and clearing it with the labels radio still selected leaves NEITHER.
    await user.click(screen.getByRole("checkbox", { name: "grp:platform" }));
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();

    await user.click(screen.getByRole("radio", { name: "Visible to everyone" }));
    expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(body).toHaveProperty("global_visibility", true));
    expect(body).toHaveProperty("acl_labels", []);
    // Nothing was edited, so no re-validation is asked for.
    expect(body).not.toHaveProperty("edits");
  });

  it("an edited description rides the apply as an edit delta", async () => {
    withSkill();
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/apply-skill", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review: {
            review_id: "r",
            proposal_id: "prop-skill-9",
            action: "apply",
            from_status: "approved",
            to_status: "applied",
            actor: "alice@example.com",
            surface: "console",
            session_id: "s",
            rejection_code: null,
            note: "",
            applied_ref: "x",
            config_hash: "cfg-1",
            created_at: "2026-07-07T10:00:00Z",
          },
          name: "deploy-runbook",
          version: 1,
          content_hash: "d",
          acl_labels: ["grp:platform"],
        });
      }),
    );

    const user = renderPage();
    await screen.findByText("[flywheel:skills] proposal sig9");
    await user.click(screen.getByText("[flywheel:skills] proposal sig9"));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.clear(screen.getByLabelText("Description"));
    await user.type(screen.getByLabelText("Description"), "Deploy and verify");
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(body).toHaveProperty("edits"));
    expect(body.edits).toEqual({ description: "Deploy and verify" });
  });
});

// ===========================================================================
// SKILLS TAB
// ===========================================================================

describe("§11 skills", () => {
  it("defaults to Name A–Z and makes only the four columns §11 names sortable", async () => {
    renderPage("/improvement?tab=skills");
    await screen.findByText("quarterly-close");
    expect(skillNames()).toEqual(["draft-skill", "quarterly-close"]);
    expect(screen.getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    for (const header of ["Name", "Status", "Fired", "Last fired"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${header}$`) })).toBeInTheDocument();
    }
    for (const header of ["Source", "Description", "Labels", "Version"]) {
      expect(screen.queryByRole("button", { name: new RegExp(`^${header}$`) })).toBeNull();
    }
  });

  it("a never-fired skill renders the em dash and no invented time", async () => {
    renderPage("/improvement?tab=skills");
    const table = await screen.findByRole("table", { name: "Skill library" });
    const draft = [...table.querySelectorAll<HTMLElement>("tbody tr.ne-tr")].find((row) =>
      row.textContent?.includes("draft-skill"),
    );
    expect(draft?.querySelectorAll("td")[7]?.textContent).toBe("—");
  });

  it("does not turn a version-less empty label set into global visibility", async () => {
    const global: SkillLibraryRow = {
      ...STEWARD_SKILLS[0]!,
      name: "global-skill",
      current_version: 1,
      acl_labels: [],
    };
    server.use(
      http.get("*/api/v1/steward/skills", () =>
        HttpResponse.json({ skills: [...STEWARD_SKILLS, global] }),
      ),
    );
    renderPage("/improvement?tab=skills");
    const table = await screen.findByRole("table", { name: "Skill library" });
    const rows = [...table.querySelectorAll<HTMLElement>("tbody tr.ne-tr")];
    const draft = rows.find((row) => row.textContent?.includes("draft-skill"));
    const visible = rows.find((row) => row.textContent?.includes("global-skill"));
    expect(draft).toHaveTextContent("No version");
    expect(draft).not.toHaveTextContent("Everyone");
    expect(visible).toHaveTextContent("Everyone");
  });

  it("a tampered row carries the word, not just a colour", async () => {
    const tampered: SkillLibraryRow = { ...STEWARD_SKILLS[0]!, tampered: true };
    server.use(
      http.get("*/api/v1/steward/skills", () => HttpResponse.json({ skills: [tampered] })),
    );
    const user = renderPage("/improvement?tab=skills");
    await screen.findByText("quarterly-close");
    expect(screen.getByText("TAMPERED")).toBeInTheDocument();

    await user.click(screen.getByText("quarterly-close"));
    expect(
      screen.getByText("Stored content no longer matches its recorded hash."),
    ).toBeInTheDocument();
  });

  it("the row menu retires an active skill, and only with a reason", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/skills/:name/retire", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ name: "quarterly-close", decision: "retired" });
      }),
    );
    const user = renderPage("/improvement?tab=skills");
    await screen.findByText("quarterly-close");

    await user.click(screen.getByRole("button", { name: "Actions for quarterly-close" }));
    await user.click(screen.getByRole("menuitem", { name: "Retire…" }));

    expect(screen.getByRole("button", { name: "Retire" })).toBeDisabled();
    await user.type(screen.getByLabelText("Why?"), "superseded");
    await user.click(screen.getByRole("button", { name: "Retire" }));

    await waitFor(() => expect(body).toEqual({ reason: "superseded" }));
  });

  it("a quarantined name offers no lifecycle move", async () => {
    renderPage("/improvement?tab=skills");
    await screen.findByText("draft-skill");
    expect(screen.queryByRole("button", { name: "Actions for draft-skill" })).toBeNull();
  });

  it("an empty library renders §11's copy", async () => {
    server.use(http.get("*/api/v1/steward/skills", () => HttpResponse.json({ skills: [] })));
    renderPage("/improvement?tab=skills");
    expect(await screen.findByText("No skills in the library.")).toBeInTheDocument();
  });

  it("distinguishes a refusal from an outage", async () => {
    server.use(
      http.get("*/api/v1/steward/skills", () =>
        HttpResponse.json({ detail: "no" }, { status: 403 }),
      ),
    );
    const { unmount } = render(
      <AuthStub>
        <MemoryRouter initialEntries={["/improvement?tab=skills"]}>
          <FlywheelPage />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByText(REFUSAL)).toBeInTheDocument();
    unmount();

    server.use(http.get("*/api/v1/steward/skills", () => HttpResponse.error()));
    renderPage("/improvement?tab=skills");
    expect(await screen.findByText("Couldn’t load skills.")).toBeInTheDocument();
    expect(screen.queryByText(REFUSAL)).toBeNull();
  });
});

// ===========================================================================
// DIGEST TAB
// ===========================================================================

describe("§11 digest", () => {
  it("renders the week's totals, the per-lane table and the latency line", async () => {
    renderPage("/improvement?tab=digest");
    await screen.findByText("Week totals");

    const tiles = document.querySelectorAll(".ne-fly__tile");
    expect([...tiles].map((t) => t.textContent)).toEqual([
      "Proposed8",
      "Approved4",
      "Rejected2",
      "Applied3",
    ]);

    const lanes = await screen.findByRole("table", { name: "By lane" });
    expect(within(lanes).getByText("Entity resolution")).toBeInTheDocument();

    // 5400 s → 1.5 h, 86400 s → 24.0 h, 259200 s → 72.0 h.
    expect(
      await screen.findByText("Median wait 1.5 h · p90 24.0 h · 3 waiting · oldest 72.0 h"),
    ).toBeInTheDocument();
  });

  it("an unmeasured latency renders the em dash, never a zero", async () => {
    server.use(
      http.get("*/api/v1/steward/latency", () =>
        HttpResponse.json({
          median_wait_s: null,
          p90_wait_s: null,
          decided_count: 0,
          pending_count: 0,
          oldest_pending_age_s: null,
          generated_at: "2026-07-07T10:00:00Z",
        }),
      ),
    );
    renderPage("/improvement?tab=digest");
    expect(
      await screen.findByText("Median wait — · p90 — · 0 waiting · oldest —"),
    ).toBeInTheDocument();
  });

  it("a week with no activity renders §11's copy", async () => {
    server.use(
      http.get("*/api/v1/steward/digest", () =>
        HttpResponse.json({
          week_start: "2026-06-22",
          week_end: "2026-06-28",
          generated_at: "2026-07-07T10:00:00Z",
          totals: { proposed: 0, approved: 0, rejected: 0, applied: 0 },
          by_lane: [],
        }),
      ),
    );
    renderPage("/improvement?tab=digest");
    expect(await screen.findByText("No steward activity this week.")).toBeInTheDocument();
  });

  it("previous week asks the server for the week before the one it just described", async () => {
    const asked: string[] = [];
    server.use(
      http.get("*/api/v1/steward/digest", ({ request }) => {
        const week = new URL(request.url).searchParams.get("week_start");
        asked.push(week ?? "");
        return HttpResponse.json({
          week_start: week ?? "2026-06-29",
          week_end: "2026-07-05",
          generated_at: "2026-07-07T10:00:00Z",
          totals: { proposed: 0, approved: 0, rejected: 0, applied: 0 },
          by_lane: [],
        });
      }),
    );
    const user = renderPage("/improvement?tab=digest");
    await screen.findByText("Week totals");
    await user.click(screen.getByRole("button", { name: "‹ previous week" }));
    await waitFor(() => expect(asked).toEqual(["", "2026-06-22"]));
  });

  it("a latency outage does not blank the week's counts, and claims nothing about the role", async () => {
    server.use(http.get("*/api/v1/steward/latency", () => HttpResponse.error()));
    renderPage("/improvement?tab=digest");
    expect(await screen.findByText("Couldn’t load review latency.")).toBeInTheDocument();
    expect(screen.getByText("Week totals")).toBeInTheDocument();
    expect(document.querySelectorAll(".ne-fly__tile")).toHaveLength(4);
    expect(screen.queryByText(REFUSAL)).toBeNull();
  });
});

// ===========================================================================
// THE SHELL
// ===========================================================================

describe("§11 shell", () => {
  it("is a title row and three tabs — no purpose line, no hero", async () => {
    renderPage();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Improvement");
    const tabs = screen.getByRole("navigation", { name: "Improvement" });
    expect([...tabs.querySelectorAll("a")].map((a) => a.textContent)).toEqual([
      "Queue",
      "Skills",
      "Digest",
    ]);
    expect(within(tabs).getByText("Queue")).toHaveAttribute("aria-current", "page");
    await screen.findByText("Retrieval");
    // The deleted hero.
    expect(screen.queryByRole("img", { name: /improvement loop/i })).toBeNull();
  });

  it("names no route, no source path and no internal id on screen", async () => {
    renderPage();
    await screen.findByText("Retrieval");
    const chrome = [
      ...document.querySelectorAll<HTMLElement>(
        ".ne-titlerow, .ne-toolbar, .ne-th, .ne-state, .ne-fly__tabs",
      ),
    ]
      .map((node) => node.textContent ?? "")
      .join(" ");
    expect(chrome).not.toMatch(/\/v1\//);
    expect(chrome).not.toMatch(/NE-[0-9]/);
    expect(chrome).not.toMatch(/GET |POST /);
    expect(chrome).not.toMatch(/\.tsx?\b/);
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — a proposal the server did not disclose
// ===========================================================================

describe("§11 + 015A0-ENFORCEMENT — an undisclosed proposal", () => {
  /** The withheld branch as the server sends it: id, safe status, time. */
  const WITHHELD = {
    disclosure_status: "content_withheld",
    proposal_id: "prop-withheld-7",
    status: "proposed",
    created_at: "2026-07-06T19:00:00Z",
  };

  function serveWithWithheld() {
    server.use(
      http.get("*/api/v1/steward/proposals", () =>
        HttpResponse.json({ proposals: [...STEWARD_PROPOSALS, WITHHELD] }),
      ),
    );
  }

  it("renders the withheld row with the literal label and no protected cell", async () => {
    serveWithWithheld();
    renderPage();
    await screen.findByText("Retrieval");
    const table = screen.getByRole("table", { name: "Proposals" });
    const row = within(table).getByText("Content withheld").closest("tr");
    expect(row).not.toBeNull();
    // Lane, Title, Kind, Confidence, Priority, Created, Status. Everything the
    // server withheld is the absent mark; the two it kept are rendered.
    const cells = [...row!.querySelectorAll("td")].map((cell) => cell.textContent ?? "");
    expect(cells.slice(0, 5)).toEqual(["—", "Content withheld", "—", "—", "—"]);
    expect(cells[6]).toContain("PENDING");
  });

  it("opens a pane with no diff, no evidence and no decision control", async () => {
    serveWithWithheld();
    const user = renderPage();
    await screen.findByText("Retrieval");
    await user.click(screen.getByText("Content withheld"));

    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("prop-withheld-7")).toBeVisible();
    expect(
      within(pane).getByText(
        "This proposal’s source access changed. Its content and every action on it are withheld.",
      ),
    ).toBeVisible();
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Reject" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Record apply" })).toBeNull();
    expect(within(pane).queryByText("Evidence ids")).toBeNull();
  });

  it("withholds a row that states NO disclosure status, complete though it looks", async () => {
    const unstated = { ...STEWARD_PROPOSALS[0]!, disclosure_status: undefined };
    server.use(
      http.get("*/api/v1/steward/proposals", () => HttpResponse.json({ proposals: [unstated] })),
    );
    renderPage();
    expect(await screen.findByText("Content withheld")).toBeVisible();
    expect(screen.queryByText(STEWARD_PROPOSALS[0]!.artifact.title)).toBeNull();
  });

  it("exports the withheld row as withheld rather than dereferencing its artifact", async () => {
    serveWithWithheld();
    const user = renderPage();
    await screen.findByText("Retrieval");
    let csv = "";
    const blob = vi
      .spyOn(URL, "createObjectURL")
      .mockImplementation((value: Blob | MediaSource) => {
        void (value as Blob).text().then((text) => {
          csv = text;
        });
        return "blob:csv";
      });
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(csv).not.toBe(""));
    // The exact exported row: the withheld literal in Title, the safe status
    // and time, and an empty cell for every protected column. No artifact
    // title, lane, kind, confidence, priority or cluster id crosses the export.
    expect(csv.split(/\r?\n/)).toContain(
      `,Content withheld,,,,${absoluteTime(WITHHELD.created_at)},proposed,`,
    );
    blob.mockRestore();
    revoke.mockRestore();
  });
});

describe("§11 + 015A0-ENFORCEMENT — the principal-visible digest", () => {
  it("says the totals are this principal's view, not the deployment's", async () => {
    server.use(
      http.get("*/api/v1/steward/digest", () =>
        HttpResponse.json({ ...STEWARD_DIGEST, scope: "principal_visible" }),
      ),
    );
    renderPage("/improvement?tab=digest");
    expect(
      await screen.findByText("Counts what you can currently see, not the whole deployment."),
    ).toBeVisible();
  });

  it("drops a Skill card that rides a principal-visible envelope", async () => {
    server.use(
      http.get("*/api/v1/steward/digest", () =>
        HttpResponse.json({
          ...STEWARD_DIGEST,
          scope: "principal_visible",
          skills: {
            generated_at: "2026-07-06T00:00:00Z",
            unknown_tools: [],
            failures: [],
            near_dups: [],
            overlaps: [],
            retirements: [],
            stale_proposals: [],
          },
        }),
      ),
    );
    renderPage("/improvement?tab=digest");
    await screen.findByText("Counts what you can currently see, not the whole deployment.");
    // The Gardener report is one GLOBAL fold; a scoped envelope may not carry it.
    expect(screen.queryByText("Skill health")).toBeNull();
  });
});
