// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §15 — ACCESS on a deployment whose governed client CAN serve §18 B6.
//
// The screen is not built to be finished later: it asks the client whether the
// access reads are servable and renders the full §15 surface when they are.
// These tests drive that answer with a client that serves them, so the branch
// that lights up on merge is exercised now rather than hoped about.
//
// What they hold to account, in order: the stated default sort, sortable
// headers with `aria-sort`, a filter and a search that narrow the RENDERED ROWS
// (asserted by identity, because a count assertion passes for the wrong rows),
// a count that follows those rows, the four states told apart from one another
// — and the two honesty traps this screen carries: a member count that is the
// SERVER's rather than the length of the trimmed list it was shown, and a
// governed write whose pending and denied answers are the server's.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { AuthStub } from "../test/authStub";
import { AccessPage } from "./AccessPage";
import type {
  AccessPrincipalEntry,
  AccessRoleEntry,
  PendingAccessChange,
  Tier3ApproverEntry,
  Tier3ApproversView,
} from "./access";

/** The client the mocked hook hands the screen. Reassigned per test. */
const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

/** SERVED IN AN ORDER THAT IS NEITHER the default sort nor its reverse, so a
 * default sort that does nothing is visible on the first assertion. */
const PRINCIPALS: AccessPrincipalEntry[] = [
  {
    principal_id: "mira@example.com",
    groups: ["ops", "approvers"],
    surfaces: ["Approvals", "Ops"],
    grants: [
      { surface: "Approvals", via_group: "approvers" },
      { surface: "Ops", via_group: "ops" },
    ],
    pending_change: null,
  },
  {
    principal_id: "alice@example.com",
    groups: ["stewards"],
    surfaces: ["Improvement"],
    grants: [{ surface: "Improvement", via_group: "stewards" }],
    pending_change: null,
  },
  // A principal in no group, granted nothing. Its cells are absent, not zero.
  {
    principal_id: "zed@example.com",
    groups: [],
    surfaces: [],
    grants: [],
    pending_change: null,
  },
  // …and one with a governed change parked at the gate (§2.5).
  {
    principal_id: "kai@example.com",
    groups: ["ops"],
    surfaces: ["Ops"],
    grants: [{ surface: "Ops", via_group: "ops" }],
    pending_change: { request_id: "req-41", requested_by: "mira@example.com" },
  },
];

const ROLES: AccessRoleEntry[] = [
  { group: "stewards", surfaces: ["Improvement"], member_count: 4, members: ["alice@example.com"] },
  // THE TRAP: twelve members, and a member list this key may read that holds
  // one. The count column must render the server's twelve.
  { group: "approvers", surfaces: ["Approvals"], member_count: 12, members: ["mira@example.com"] },
  {
    group: "ops",
    surfaces: ["Ops"],
    member_count: 2,
    members: ["kai@example.com", "mira@example.com"],
  },
];

const APPROVERS: Tier3ApproverEntry[] = [
  { principal_id: "mira@example.com", since: "2026-05-02T10:00:00.000Z" },
  { principal_id: "alice@example.com", since: null },
];

const PENDING_TIER3: PendingAccessChange = {
  request_id: "req-tier3-41",
  requested_by: "mira@example.com",
};

interface Serve {
  principals?: AccessPrincipalEntry[];
  roles?: AccessRoleEntry[];
  approvers?: Tier3ApproverEntry[];
  pendingChange?: PendingAccessChange | null;
  tier3?: () => Promise<Tier3ApproversView>;
  principalsFail?: unknown;
  write?: (
    change: unknown,
  ) => Promise<{ status: "applied" | "pending"; request_id: string | null }>;
  /** Omit the write entirely: a deployment that serves the lists read-only. */
  readOnly?: boolean;
}

function serve(over: Serve = {}) {
  const accessPrincipals = vi.fn(() =>
    over.principalsFail !== undefined
      ? Promise.reject(over.principalsFail)
      : Promise.resolve({ principals: over.principals ?? PRINCIPALS }),
  );
  const accessRoles = vi.fn(() => Promise.resolve({ roles: over.roles ?? ROLES }));
  const accessTier3Approvers = vi.fn(() =>
    over.tier3 === undefined
      ? Promise.resolve({
          approvers: over.approvers ?? APPROVERS,
          pending_change: over.pendingChange ?? null,
        })
      : over.tier3(),
  );
  const submitAccessChange = vi.fn(
    over.write ?? (() => Promise.resolve({ status: "applied" as const, request_id: null })),
  );
  stub.client = over.readOnly
    ? { accessPrincipals, accessRoles, accessTier3Approvers }
    : { accessPrincipals, accessRoles, accessTier3Approvers, submitAccessChange };
  return { accessPrincipals, accessRoles, accessTier3Approvers, submitAccessChange };
}

function setup(path = "/access") {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <AccessPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The rendered people, BY THE IDENTITY IN THEIR FIRST CELL — read off the id
 * element rather than the cell text, which also carries the pending sub-line. */
function people(): string[] {
  const table = screen.getByRole("table", { name: "People" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.querySelector(".ne-access__id")?.textContent ?? "");
}

function firstCells(tableName: string): string[] {
  const table = screen.getByRole("table", { name: tableName });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

function tier3Section(): HTMLElement {
  const section = screen.getByRole("heading", { name: "Tier-3 approvers" }).closest("section");
  expect(section).not.toBeNull();
  return section as HTMLElement;
}

async function peopleLoaded(n = PRINCIPALS.length) {
  await waitFor(() => expect(people()).toHaveLength(n));
}

beforeEach(() => {
  stub.client = {};
});

// ===========================================================================

describe("§15 People — the table standard", () => {
  it("renders a REAL table of the served principals, sorted Principal A–Z", async () => {
    serve();
    setup();
    await peopleLoaded();
    expect(people()).toEqual([
      "alice@example.com",
      "kai@example.com",
      "mira@example.com",
      "zed@example.com",
    ]);
  });

  it("states that default sort on the Principal header, and marks no other header sorted", async () => {
    serve();
    setup();
    await peopleLoaded();
    expect(screen.getByRole("columnheader", { name: "Principal" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    for (const name of ["Groups", "Opens"]) {
      expect(screen.getByRole("columnheader", { name })).not.toHaveAttribute("aria-sort");
    }
  });

  it("sorts by a clicked header, reverses on the second click, and keeps absences last", async () => {
    serve();
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByRole("button", { name: "Groups" }));
    // "ops" < "ops approvers" < "stewards"; zed is in NO group, which is an
    // absence, not the alphabetically-first group.
    expect(people()).toEqual([
      "kai@example.com",
      "mira@example.com",
      "alice@example.com",
      "zed@example.com",
    ]);
    await user.click(screen.getByRole("button", { name: "Groups" }));
    // Reversed — and the absent row is STILL last. A descending click that
    // floated it to the top would hide the rows the reader asked to see.
    expect(people()).toEqual([
      "alice@example.com",
      "mira@example.com",
      "kai@example.com",
      "zed@example.com",
    ]);
    expect(screen.getByRole("columnheader", { name: "Groups" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("the surface filter narrows the RENDERED ROWS, and the count follows them", async () => {
    serve();
    const user = setup();
    await peopleLoaded();
    expect(screen.getByText("4 people")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Surface"), "Ops");
    // Identity, not cardinality: a count of two passes for the wrong two rows.
    expect(people()).toEqual(["kai@example.com", "mira@example.com"]);
    expect(screen.getByText("2 people")).toBeInTheDocument();
    // …and the active filter is dismissible, so the reason the list is short is
    // visible beside the list.
    await user.click(screen.getByRole("button", { name: "Clear Surface: Ops" }));
    expect(people()).toHaveLength(4);
    expect(screen.getByText("4 people")).toBeInTheDocument();
  });

  it("the filter keeps only what the SERVER granted — never a group it inferred", async () => {
    serve();
    const user = setup();
    await peopleLoaded();
    await user.selectOptions(screen.getByLabelText("Surface"), "Approvals");
    // kai is in group `ops` and mira is in `ops` AND `approvers`; only mira was
    // granted Approvals by the server, and the console has no table that would
    // let it decide otherwise.
    expect(people()).toEqual(["mira@example.com"]);
    expect(screen.getByText("1 person")).toBeInTheDocument();
  });

  it("the search-within narrows the RENDERED ROWS across principal, groups and opens", async () => {
    serve();
    const user = setup();
    await peopleLoaded();
    await user.type(screen.getByLabelText("Filter people…"), "stewards");
    // A GROUP match, not a principal-name match.
    expect(people()).toEqual(["alice@example.com"]);
    expect(screen.getByText("1 person")).toBeInTheDocument();
  });

  it("counts NOTHING while the read is in flight", async () => {
    serve();
    setup();
    // The skeleton rows already say the read is in flight; "0 people" over them
    // is a number that is wrong on screen.
    expect(screen.queryByText(/\d+ (people|person)/)).not.toBeInTheDocument();
    await peopleLoaded();
    expect(screen.getByText("4 people")).toBeInTheDocument();
  });
});

// ===========================================================================

describe("§15 People — the four states are four different facts", () => {
  it("an empty served list renders §15's own empty sentence", async () => {
    serve({ principals: [] });
    setup();
    expect(await screen.findByText("No principals found.")).toBeInTheDocument();
    expect(screen.getByText("0 people")).toBeInTheDocument();
  });

  it("a REFUSAL renders the SERVER's refusal — and the server was actually asked", async () => {
    const { accessPrincipals } = serve({
      principalsFail: new ApiError("forbidden", 403, "not an access administrator"),
    });
    setup();
    expect(await screen.findByText(/Your role doesn.t include Access/)).toBeInTheDocument();
    // Thin client (C7): the console did not decide this. It asked, and rendered
    // the answer.
    expect(accessPrincipals).toHaveBeenCalled();
    // No table and no count over a read this key was never shown.
    expect(screen.queryByRole("table", { name: "People" })).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+ (people|person)/)).not.toBeInTheDocument();
  });

  it("an UNREACHABLE read says so, and never claims a role boundary", async () => {
    // The bug caught on the status chip, in its other home: a load that failed
    // rendered as a permission the reader does not hold — a false statement
    // about authorization, made by the console, about the server.
    serve({ principalsFail: new ApiError("boom", 500) });
    setup();
    expect(await screen.findByText(/Couldn.t load people\./)).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("Retry re-runs the same read", async () => {
    const { accessPrincipals } = serve({ principalsFail: new ApiError("boom", 500) });
    const user = setup();
    await screen.findByText(/Couldn.t load people\./);
    expect(accessPrincipals).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(accessPrincipals).toHaveBeenCalledTimes(2));
  });
});

// ===========================================================================

describe("§15 People — the row pane", () => {
  it("opens on the row and names the full group list and the per-surface provenance", async () => {
    serve();
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("mira@example.com"));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("ops · approvers")).toBeInTheDocument();
    expect(within(pane).getByText("via group approvers")).toBeInTheDocument();
    expect(within(pane).getByText("via group ops")).toBeInTheDocument();
  });

  it("a governed change parked at the gate is rendered ON THE OBJECT, not as a toast", async () => {
    serve();
    setup();
    await peopleLoaded();
    const table = screen.getByRole("table", { name: "People" });
    const row = within(table)
      .getAllByRole("row")
      .find((r) => r.querySelector(".ne-access__id")?.textContent === "kai@example.com");
    expect(row).toBeDefined();
    expect(
      within(row as HTMLElement).getByText(/Change requested by mira@example\.com/),
    ).toBeInTheDocument();
    // §2.5: THE OLD VALUE REMAINS THE DISPLAYED VALUE. kai holds `ops` today and
    // that is what the row shows — the console never pre-renders an unapplied
    // change.
    expect(within(row as HTMLElement).getByText("ops")).toBeInTheDocument();
  });
});

// ===========================================================================

describe("§15 Roles — the pinned Tier-3 list and the roles table", () => {
  it("pins the Tier-3 approvers above the roles, sorted Principal A–Z", async () => {
    serve();
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    expect(firstCells("Tier-3 approvers")).toEqual(["alice@example.com", "mira@example.com"]);
    expect(screen.getByText("2 approvers")).toBeInTheDocument();
  });

  it("renders no pending evidence when the authoritative wire says there is none", async () => {
    serve({ pendingChange: null });
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    expect(within(tier3Section()).queryByText("PENDING")).not.toBeInTheDocument();
    expect(within(tier3Section()).queryByRole("link", { name: "View request" })).toBeNull();
  });

  it("shows a parked addition without granting the candidate early", async () => {
    serve({ pendingChange: PENDING_TIER3 });
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));

    const section = tier3Section();
    expect(within(section).getByText("PENDING")).toBeInTheDocument();
    expect(within(section).getByText(/Change requested by mira@example\.com/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "View request" })).toHaveAttribute(
      "href",
      "/approvals?request=req-tier3-41",
    );
    // kai is a served candidate, but is not in the served effective list. The
    // pending marker does not turn the parked addition into an early grant.
    expect(firstCells("Tier-3 approvers")).toEqual(["alice@example.com", "mira@example.com"]);
    expect(firstCells("Tier-3 approvers")).not.toContain("kai@example.com");
  });

  it("shows a parked removal while retaining the still-effective approver", async () => {
    serve({
      approvers: [{ principal_id: "mira@example.com", since: "2026-05-02T10:00:00.000Z" }],
      pendingChange: { request_id: "req-tier3-42", requested_by: "alice@example.com" },
    });
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toEqual(["mira@example.com"]));

    const section = tier3Section();
    expect(within(section).getByText(/Change requested by alice@example\.com/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "View request" })).toHaveAttribute(
      "href",
      "/approvals?request=req-tier3-42",
    );
    // The wire does not label the operation. The effective list still contains
    // mira, so the console keeps rendering mira until the server removes her.
    expect(firstCells("Tier-3 approvers")).toEqual(["mira@example.com"]);
  });

  it("an approver the server gave no date renders the em dash, never a made-up one", async () => {
    serve();
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    const table = screen.getByRole("table", { name: "Tier-3 approvers" });
    const alice = within(table)
      .getAllByRole("row")
      .slice(1)
      .find((r) => within(r).getAllByRole("cell")[0]?.textContent === "alice@example.com");
    expect(within(alice as HTMLElement).getAllByRole("cell")[1]?.textContent).toBe("—");
  });

  it("renders the roles, sorted Role A–Z, with the count following the search", async () => {
    serve();
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Roles")).toHaveLength(3));
    expect(firstCells("Roles")).toEqual(["approvers", "ops", "stewards"]);
    expect(screen.getByText("3 roles")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Filter roles…"), "steward");
    expect(firstCells("Roles")).toEqual(["stewards"]);
    expect(screen.getByText("1 role")).toBeInTheDocument();
  });

  it("Members is the SERVER's count, not the length of the list this key was shown", async () => {
    serve();
    setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Roles")).toHaveLength(3));
    const table = screen.getByRole("table", { name: "Roles" });
    const approvers = within(table)
      .getAllByRole("row")
      .slice(1)
      .find((r) => within(r).getAllByRole("cell")[0]?.textContent === "approvers");
    // Twelve, from the server. The member list this key may read holds one, and
    // a console that counted what it was shown would disagree with the
    // deployment about how many people hold the role.
    expect(within(approvers as HTMLElement).getAllByRole("cell")[2]?.textContent).toBe("12");
  });

  it("a role row opens its member list and surface mapping", async () => {
    serve();
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Roles")).toHaveLength(3));
    await user.click(screen.getByRole("cell", { name: "ops" }));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("Ops")).toBeInTheDocument();
    expect(within(pane).getByText(/kai@example\.com/)).toBeInTheDocument();
  });
});

// ===========================================================================

describe("§15 — the governed writes", () => {
  it("submits the role change as a DELTA and re-reads the server", async () => {
    const { submitAccessChange, accessPrincipals } = serve();
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("alice@example.com"));
    await user.click(await screen.findByRole("button", { name: "Change roles…" }));
    await user.click(await screen.findByRole("checkbox", { name: "ops" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await waitFor(() =>
      expect(submitAccessChange).toHaveBeenCalledWith({
        kind: "roles",
        principal_id: "alice@example.com",
        // What CHANGED, in both directions — never the whole list, which would
        // revert anything altered between this read and this submit.
        add_groups: ["ops"],
        remove_groups: [],
      }),
    );
    // §2.5 step 5: the object reflects the server's new state on the next read.
    // The console never flips it locally, so it re-reads.
    await waitFor(() => expect(accessPrincipals).toHaveBeenCalledTimes(2));
  });

  it("never carries one principal's open editor onto another principal's pane", async () => {
    // The pane is one component in one slot; without a per-principal identity
    // React reuses the instance across rows and the ticked groups stay put —
    // alice's memberships under kai's name, one click from being submitted.
    const { submitAccessChange } = serve();
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("alice@example.com"));
    await user.click(await screen.findByRole("button", { name: "Change roles…" }));
    await user.click(await screen.findByRole("checkbox", { name: "ops" }));
    // …now open a different principal without submitting.
    await user.click(screen.getByText("kai@example.com"));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("kai@example.com")).toBeInTheDocument();
    // The editor is closed, so there is nothing carried over to submit.
    expect(within(pane).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: "Request change" })).not.toBeInTheDocument();
    // …and reopening it starts from KAI's served groups, not alice's.
    await user.click(within(pane).getByRole("button", { name: "Change roles…" }));
    expect(await screen.findByRole("checkbox", { name: "ops" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "stewards" })).not.toBeChecked();
    expect(submitAccessChange).not.toHaveBeenCalled();
  });

  it("renders a DENIAL where the submit happened, with the server's reason verbatim", async () => {
    serve({
      write: () =>
        Promise.reject(new ApiError("forbidden", 403, "alice@example.com may not hold ops")),
    });
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("alice@example.com"));
    await user.click(await screen.findByRole("button", { name: "Change roles…" }));
    await user.click(await screen.findByRole("checkbox", { name: "ops" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    const pane = await screen.findByRole("dialog");
    expect(await within(pane).findByText("Denied by policy.")).toBeInTheDocument();
    expect(within(pane).getByText("alice@example.com may not hold ops")).toBeInTheDocument();
  });

  it("states a DENIAL the gate gave no reason for, and writes none for it", async () => {
    // The gate stated nothing (a 403 with no JSON body). "Denied by policy."
    // stands alone; the console's house sentence under it would read as the
    // gate's own account of the denial.
    serve({ write: () => Promise.reject(new ApiError("forbidden", 403)) });
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("alice@example.com"));
    await user.click(await screen.findByRole("button", { name: "Change roles…" }));
    await user.click(await screen.findByRole("checkbox", { name: "ops" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    const pane = await screen.findByRole("dialog");
    expect(await within(pane).findByText("Denied by policy.")).toBeInTheDocument();
    expect(within(pane).queryByText("Your role doesn’t include this action.")).toBeNull();
  });

  it("submits the Tier-3 approver change as a delta of principals", async () => {
    const { submitAccessChange } = serve();
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    await user.click(screen.getByRole("button", { name: "Edit Tier-3 approvers…" }));
    // The checklist offers only principals the server sent, so no submission
    // can name someone this deployment does not have.
    await user.click(await screen.findByRole("checkbox", { name: "kai@example.com" }));
    await user.click(await screen.findByRole("checkbox", { name: "mira@example.com" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await waitFor(() =>
      expect(submitAccessChange).toHaveBeenCalledWith({
        kind: "tier3-approvers",
        add_principals: ["kai@example.com"],
        remove_principals: ["mira@example.com"],
      }),
    );
  });

  it("renders pending only after the authoritative Tier-3 reread serves it", async () => {
    let reads = 0;
    const { accessTier3Approvers, submitAccessChange } = serve({
      tier3: () =>
        Promise.resolve(
          reads++ === 0
            ? { approvers: APPROVERS, pending_change: null }
            : {
                approvers: APPROVERS,
                pending_change: {
                  request_id: "req-served-after-write",
                  requested_by: "served-requester@example.com",
                },
              },
        ),
      // Deliberately a different id: the response proves the write parked, but
      // cannot supply the requester. The object marker must come from the read.
      write: () => Promise.resolve({ status: "pending", request_id: "req-response-only" }),
    });
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    expect(within(tier3Section()).queryByText("PENDING")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit Tier-3 approvers…" }));
    await user.click(await screen.findByRole("checkbox", { name: "kai@example.com" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await waitFor(() => expect(submitAccessChange).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(accessTier3Approvers).toHaveBeenCalledTimes(2));

    const section = tier3Section();
    expect(
      await within(section).findByText(/Change requested by served-requester@example\.com/),
    ).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "View request" })).toHaveAttribute(
      "href",
      "/approvals?request=req-served-after-write",
    );
    expect(section).not.toHaveTextContent("req-response-only");
    expect(firstCells("Tier-3 approvers")).not.toContain("kai@example.com");
  });

  it("keeps Tier-3 membership effective and renders a policy denial verbatim", async () => {
    serve({
      write: () =>
        Promise.reject(new ApiError("forbidden", 403, "mira must remain a Tier-3 approver")),
    });
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    await user.click(screen.getByRole("button", { name: "Edit Tier-3 approvers…" }));
    await user.click(await screen.findByRole("checkbox", { name: "mira@example.com" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));

    const pane = await screen.findByRole("dialog");
    expect(await within(pane).findByText("Denied by policy.")).toBeInTheDocument();
    expect(within(pane).getByText("mira must remain a Tier-3 approver")).toBeInTheDocument();
    expect(firstCells("Tier-3 approvers")).toContain("mira@example.com");
  });

  it("keeps Tier-3 membership effective and renders an expired request as a failed save", async () => {
    serve({
      write: () => Promise.reject(new ApiError("gone", 410, "the governed change request expired")),
    });
    const user = setup("/access?tab=roles");
    await waitFor(() => expect(firstCells("Tier-3 approvers")).toHaveLength(2));
    await user.click(screen.getByRole("button", { name: "Edit Tier-3 approvers…" }));
    await user.click(await screen.findByRole("checkbox", { name: "mira@example.com" }));
    await user.click(screen.getByRole("button", { name: "Request change" }));

    const pane = await screen.findByRole("dialog");
    expect(await within(pane).findByText("Couldn’t save. Try again.")).toBeInTheDocument();
    expect(within(pane).getByText("the governed change request expired")).toBeInTheDocument();
    expect(firstCells("Tier-3 approvers")).toContain("mira@example.com");
  });

  it("a deployment that serves the lists but no write path offers NO write control", async () => {
    serve({ readOnly: true });
    const user = setup();
    await peopleLoaded();
    await user.click(screen.getByText("alice@example.com"));
    await screen.findByRole("dialog");
    expect(screen.queryByRole("button", { name: "Change roles…" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await waitFor(() => expect(firstCells("Roles")).toHaveLength(3));
    expect(
      screen.queryByRole("button", { name: "Edit Tier-3 approvers…" }),
    ).not.toBeInTheDocument();
  });
});
