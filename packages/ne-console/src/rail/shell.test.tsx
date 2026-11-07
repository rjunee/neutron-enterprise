// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.3 / §2.6 — the top bar and the global keyboard contract, through the real
// App so the providers, the router and the redirects are the production ones.
//
// The claim under test is the one §1.2 makes by deletion: GLOBAL STATE HAS
// EXACTLY ONE HOME NOW. The chain verdict, the attested tally, the air-gap state
// and the configuration hash were four separate pieces of permanent rail chrome
// on every route; they are one chip that is quiet when healthy and one popover
// when it is not.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { AuthStub, makeAuthValue } from "../test/authStub";
import {
  AUDIT_EVENTS,
  PRINCIPAL_APPROVER,
  PRINCIPAL_OPS_STEWARD,
  RUNTIME_CONFIG,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

const SHELL_CSS = readFileSync(resolve(__dirname, "..", "styles", "shell.css"), "utf-8");

function ownedApp(token: string, name?: string) {
  const auth = makeAuthValue();
  return (
    <AuthStub
      value={{
        user: {
          ...auth.user!,
          access_token: token,
          profile: { ...auth.user!.profile, name },
        } as typeof auth.user,
      }}
    >
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>
  );
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  window.history.pushState({}, "", "/");
  localStorage.clear();
});

describe("the top bar (§1.3)", () => {
  it("is quiet when healthy: a dot and the word Operational, no figure and no alarm", async () => {
    renderAt("/");
    const chip = await screen.findByRole("button", { name: /Operational/ });
    expect(chip.className).not.toContain("ne-status__chip--fault");
  });

  it("does not announce a break the verdict has not confirmed — it says it is re-checking", async () => {
    // The chip rests on the LATCHED verdict (audit/chainVerdict.ts), not on one
    // read's raw `valid`: the server has a reproduced read-during-append race,
    // and the console's own polling is one of the things appending. So at the
    // production poll interval ONE disagreeing read shows this, and it is
    // neither of the two lies available — not "Operational", which would vouch
    // for a chain nothing has confirmed, and not a break at an entry, which is
    // the accusation /audit's strip is simultaneously declining to make.
    //
    // NAMING A CONFIRMED BREAK, and the popover row's deep link at its entry,
    // need a poll driven read by read: rail/StatusChip.test.tsx.
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: AUDIT_EVENTS,
          verification: {
            valid: false,
            checked: 4812,
            first_break_seq: 4812,
            first_break_event_id: "evt-4812",
            reason: "hash mismatch",
            detail: "chain broken",
          },
        }),
      ),
    );
    renderAt("/");
    const chip = await screen.findByRole("button", { name: "Re-checking the audit chain" });
    expect(chip.className).not.toContain("ne-status__chip--fault");
    await userEvent.click(chip);
    const popover = screen.getByRole("group", { name: "System status" });
    const chainRow = within(popover).getByText("Audit chain").closest("a");
    expect(chainRow).toHaveAttribute("href", "/audit");
  });

  it("a FAILED audit poll faults the chip and reads Unavailable", async () => {
    // The three-way split postureAbsence documents, applied to the audit row:
    // an outage is not an authorization boundary.
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderAt("/");
    const chip = await screen.findByRole("button", { name: "Audit unavailable" });
    expect(chip.className).toContain("ne-status__chip--fault");
    expect(screen.queryByRole("button", { name: /Operational/ })).toBeNull();
    await userEvent.click(chip);
    const popover = screen.getByRole("group", { name: "System status" });
    const row = within(popover).getByText("Audit chain").closest("a");
    await waitFor(() => expect(row?.textContent).toContain("Unavailable"));
    expect(row?.textContent).not.toContain("Not visible to your role");
  });

  it("a read still IN FLIGHT reads Loading… — never 'Not visible to your role'", async () => {
    server.use(http.get("*/api/v1/audit/events", () => new Promise<never>(() => {})));
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: /Operational/ }));
    const popover = screen.getByRole("group", { name: "System status" });
    const row = within(popover).getByText("Audit chain").closest("a");
    expect(row?.textContent).toContain("Loading…");
    expect(row?.textContent).not.toContain("Not visible to your role");
  });

  it("CONTROL: the server's own 403 is the one absence that IS a role boundary", async () => {
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: /Operational/ }));
    const popover = screen.getByRole("group", { name: "System status" });
    const row = within(popover).getByText("Audit chain").closest("a");
    await waitFor(() => expect(row?.textContent).toContain("Not visible to your role"));
  });

  it("puts the chain, the controls, the network and the configuration in ONE popover", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: /Operational/ }));
    const popover = screen.getByRole("group", { name: "System status" });
    for (const label of ["Audit chain", "Controls", "Network", "Configuration"]) {
      expect(within(popover).getByText(label)).toBeInTheDocument();
    }
    // The configuration hash is TRUNCATED with a copy affordance; the full value
    // lives in `title` and the clipboard (§2.7).
    expect(within(popover).getByLabelText("Copy configuration hash")).toBeInTheDocument();
  });

  it("offers the account menu: identity, theme, density, guided tour, sign out — in that order", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Account" }));
    const menu = screen.getByRole("menu", { name: "Account" });
    expect(within(menu).getByText(/^Signed in as /)).toBeInTheDocument();
    expect(within(menu).getByRole("radio", { name: "Dark" })).toBeInTheDocument();
    expect(within(menu).getByRole("radio", { name: "Comfortable" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Guided tour" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });

  it("never combines client B's token identity with client A's settled principal", async () => {
    const subject = "c21456b9-c8dc-4458-b393-bc3230db0081";
    const groupA = "group-client-a-only";
    const groupB = "group-client-b-only";
    const delayedB = deferredResponse();
    const calls: string[] = [];
    server.use(
      http.get("*/api/v1/me", ({ request }) => {
        const authorization = request.headers.get("authorization") ?? "";
        calls.push(authorization);
        return authorization.endsWith("client-a")
          ? HttpResponse.json({ id: subject, groups: [groupA] })
          : delayedB.promise;
      }),
    );

    window.history.pushState({}, "", "/");
    const view = render(ownedApp("client-a", "Token A"));
    await userEvent.click(await screen.findByRole("button", { name: "Account" }));
    expect(await screen.findByText("Signed in as Token A")).toBeInTheDocument();
    await userEvent.click(await screen.findByText("Identity details"));
    expect(screen.getByText(groupA)).toBeVisible();

    view.rerender(ownedApp("client-b", "Token B"));
    expect(screen.getByText("Signed in as Token B")).toBeInTheDocument();
    expect(screen.queryByText(subject)).toBeNull();
    expect(screen.queryByText(groupA)).toBeNull();
    expect(document.querySelector(".ne-rail__slotpending")).not.toBeNull();
    await waitFor(() => expect(calls).toContain("Bearer client-b"));

    await act(async () => {
      delayedB.resolve(HttpResponse.json({ id: subject, groups: [groupB] }));
    });
    await userEvent.click(await screen.findByText("Identity details"));
    expect(screen.getByText(subject)).toBeVisible();
    expect(screen.getByText(groupB)).toBeVisible();
    expect(screen.queryByText(groupA)).toBeNull();
  });

  it("suppresses client A's late principal completion throughout client B's loading cycle", async () => {
    const subject = "c21456b9-c8dc-4458-b393-bc3230db0081";
    const lateGroupA = "group-late-client-a-only";
    const groupB = "group-client-b-settled";
    const delayedA = deferredResponse();
    const delayedB = deferredResponse();
    const calls: string[] = [];
    server.use(
      http.get("*/api/v1/me", ({ request }) => {
        const authorization = request.headers.get("authorization") ?? "";
        calls.push(authorization);
        return authorization.endsWith("client-a") ? delayedA.promise : delayedB.promise;
      }),
    );

    window.history.pushState({}, "", "/");
    const view = render(ownedApp("client-a", "Token A"));
    await userEvent.click(await screen.findByRole("button", { name: "Account" }));
    await waitFor(() => expect(calls).toContain("Bearer client-a"));

    view.rerender(ownedApp("client-b", "Token B"));
    expect(screen.getByText("Signed in as Token B")).toBeInTheDocument();
    await waitFor(() => expect(calls).toContain("Bearer client-b"));

    await act(async () => {
      delayedA.resolve(HttpResponse.json({ id: subject, groups: [lateGroupA] }));
    });
    expect(screen.getByText("Signed in as Token B")).toBeInTheDocument();
    expect(screen.queryByText(subject)).toBeNull();
    expect(screen.queryByText(lateGroupA)).toBeNull();
    expect(document.querySelector(".ne-rail__slotpending")).not.toBeNull();

    await act(async () => {
      delayedB.resolve(HttpResponse.json({ id: subject, groups: [groupB] }));
    });
    await userEvent.click(await screen.findByText("Identity details"));
    expect(screen.getByText(groupB)).toBeVisible();
    expect(screen.queryByText(lateGroupA)).toBeNull();
  });

  it("applies the density choice product-wide, from one attribute", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Account" }));
    await userEvent.click(screen.getByRole("radio", { name: "Comfortable" }));
    // A table never decides its own row height: the height arrives through the
    // cascade, so the account menu and Settings cannot disagree about it.
    await waitFor(() => expect(document.documentElement.dataset.density).toBe("comfortable"));
  });
});

describe("the command palette and the keyboard contract (§2.6, §3)", () => {
  it("opens from the top-bar search field and from ⌘K, and Escape closes it", async () => {
    renderAt("/");
    const search = await screen.findByRole("button", { name: /Search or jump to/ });
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.click(search);
    expect(screen.getByRole("dialog", { name: "Search or jump to" })).toBeInTheDocument();
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    expect(search.closest(".ne-shell__col")).toHaveAttribute("inert");
    await userEvent.tab();
    expect(input).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(input).toHaveFocus();
    search.focus();
    expect(input).toHaveFocus();
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(input).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Search or jump to" })).toBeNull();
    expect(search.closest(".ne-shell__col")).not.toHaveAttribute("inert");
    expect(search).toHaveFocus();

    const account = screen.getByRole("button", { name: "Account" });
    account.focus();
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("dialog", { name: "Search or jump to" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(account).toHaveFocus();
  });

  it("restores focus after an action but ignores a detached palette invoker", async () => {
    const { container } = renderAt("/");
    const search = await screen.findByRole("button", { name: /Search or jump to/ });
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.click(search);
    await userEvent.type(
      screen.getByRole("combobox", { name: "Search or jump to" }),
      "switch theme",
    );
    await userEvent.click(screen.getByRole("option", { name: /Switch theme/ }));
    expect(screen.queryByRole("dialog", { name: "Search or jump to" })).toBeNull();
    expect(search).toHaveFocus();

    await userEvent.click(search);
    await userEvent.click(container.querySelector<HTMLElement>(".ne-palette__scrim")!);
    expect(screen.queryByRole("dialog", { name: "Search or jump to" })).toBeNull();
    expect(search).toHaveFocus();

    await userEvent.click(search);
    search.remove();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Search or jump to" })).toBeNull();
    expect(document.body).toHaveFocus();
  });

  it("refuses a bare uuid4 outright and routes only the kind-named approval form", async () => {
    // §3 rank 1 through the real shell. The runtime mints approval request ids,
    // run ids, conversation ids and job ids from the same `str(uuid.uuid4())`,
    // so a bare canonical UUID names four id spaces and identifies none. The
    // palette must not pick one: it used to read every such string as a request
    // and send the operator to Approvals, top result, labelled, with nothing on
    // screen to say a kind had been inferred.
    //
    // The approver is the principal on purpose — the rail GRANTS Approvals
    // here, so a row onto that screen is genuinely available to leak and its
    // absence below is the grammar refusing to guess, not a hidden destination.
    const requestId = "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411";
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    renderAt("/");
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    expect(document.querySelector('.ne-rail__nav a[href="/approvals"]')).not.toBeNull();
    const before = `${window.location.pathname}${window.location.search}`;

    await userEvent.keyboard("{Meta>}k{/Meta}");
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    const dialog = screen.getByRole("dialog", { name: "Search or jump to" });

    // NON-ENUMERATING: the bare id puts no instance of any kind on screen.
    await userEvent.type(input, requestId);
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
    for (const kind of ["Approval request", "Run", "Conversation", "Agent", "Trace"]) {
      expect(within(dialog).queryByText(kind), kind).toBeNull();
    }

    // NON-ACTIONABLE: choosing a result unconditionally closes the palette, so
    // a dialog still standing after Enter is proof there was nothing to act on
    // — and the address bar is where it was.
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("dialog", { name: "Search or jump to" })).toBeInTheDocument();
    expect(`${window.location.pathname}${window.location.search}`).toBe(before);

    // THE CONTROL, same session and the SAME id: name the kind and the request
    // is still one row, still labelled with its type, and Enter still lands on
    // it. Without this the three assertions above would read identically
    // against a palette that had simply stopped resolving typed ids at all.
    await userEvent.clear(input);
    await userEvent.type(input, `approval ${requestId}`);
    const options = screen.getAllByRole("option");
    // A grammar match is ALWAYS the top result, labelled with its type.
    expect(within(options[0]!).getByText("Approval request")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        `/approvals?request=${encodeURIComponent(requestId)}`,
      ),
    );
  });

  it("answers a no-match with a typed-id form that works and the Search screen, never the query", async () => {
    // §3's no-match line is the WHOLE screen after a query resolves nothing —
    // including a pasted bare uuid4, which rank 1 refuses on purpose because
    // that shape names five id spaces and identifies none. Failing closed is
    // right; failing closed with no way out is not, because nothing tells the
    // operator the kind-named form exists. So the line owes three things at
    // once, and each is asserted on its own below so dropping any one fails.
    const requestId = "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411";
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    renderAt("/");
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());

    await userEvent.keyboard("{Meta>}k{/Meta}");
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    const dialog = screen.getByRole("dialog", { name: "Search or jump to" });
    const emptyLine = () =>
      dialog.querySelector<HTMLElement>(".ne-palette__empty")?.textContent ?? "";

    await userEvent.type(input, "zzzzqqq");
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
    const onWords = emptyLine();

    await userEvent.clear(input);
    await userEvent.type(input, requestId);
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
    const onPastedId = emptyLine();

    // STATIC. Two queries with nothing in common, one identical line — so the
    // id is not quoted back. A message built around the pasted string would
    // re-assert in prose the "we know what this is" claim rank 1 just refused.
    expect(onPastedId).toBe(onWords);

    // IT STILL SAYS WHERE CONTENT SEARCH LIVES. The grammar hint is added to
    // that guidance, not traded for it.
    expect(onPastedId).toContain("Content search runs from the Search screen.");

    // IT NAMES A FORM THE PALETTE ACTUALLY RESOLVES. Lift the form out of the
    // rendered line without assuming which word it uses, put the id that just
    // failed into its placeholder, type THAT, and the request comes back as one
    // row labelled with its kind. A hint is only help if the string it teaches
    // parses: §3's own history is four palette rows naming capabilities that
    // nothing on the other end ever read.
    const taught = /([a-z][a-z-]*) <request-id>/.exec(onPastedId);
    expect(taught, onPastedId).not.toBeNull();
    await userEvent.clear(input);
    await userEvent.type(input, taught![0].replace("<request-id>", requestId));
    const options = within(dialog).getAllByRole("option");
    expect(within(options[0]!).getByText("Approval request")).toBeInTheDocument();
  });

  it("derives the canonical Verified Queries destination from the rail index", async () => {
    renderAt("/");
    await userEvent.keyboard("{Meta>}k{/Meta}");
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    await userEvent.type(input, "verified queries");
    const destination = screen.getByRole("option", { name: /Execution › Verified Queries/ });
    await userEvent.click(destination);
    await waitFor(() => expect(window.location.pathname).toBe("/verified-queries"));
    expect(screen.getByRole("heading", { name: "Verified Queries", level: 1 })).toBeInTheDocument();
  });

  it("keeps unserved Access out of navigation and the palette while its direct route stays truthful", async () => {
    renderAt("/access");
    expect(await screen.findByRole("heading", { level: 1, name: "Access" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "People, their groups and what each opens appear here once this deployment serves them.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    expect(document.querySelector('.ne-rail__nav a[href="/access"]')).toBeNull();

    await userEvent.keyboard("{Meta>}k{/Meta}");
    const dialog = screen.getByRole("dialog", { name: "Search or jump to" });
    await userEvent.type(screen.getByRole("combobox", { name: "Search or jump to" }), "access");
    expect(within(dialog).queryByRole("option", { name: /Access/ })).toBeNull();
  });

  it("mounts the one Agents rail/palette destination with title, H1 and honest empty state", async () => {
    server.use(
      http.get("*/api/v1/agents", () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    renderAt("/");
    expect(await screen.findByRole("link", { name: "Agents" })).toHaveAttribute("href", "/agents");
    await userEvent.keyboard("{Meta>}k{/Meta}");
    await userEvent.type(screen.getByRole("combobox", { name: "Search or jump to" }), "agents");
    const options = screen.getAllByRole("option", { name: /Execution › Agents/ });
    expect(options).toHaveLength(1);
    await userEvent.click(options[0]!);
    await waitFor(() => expect(window.location.pathname).toBe("/agents"));
    expect(await screen.findByRole("heading", { level: 1, name: "Agents" })).toBeInTheDocument();
    expect(screen.getByText("No agents are registered in this deployment.")).toBeInTheDocument();
    expect(document.title).toBe("Agents · Neutron Enterprise");
  });

  it("`g` then `p` goes to Posture", async () => {
    renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    await userEvent.keyboard("gp");
    await waitFor(() => expect(window.location.pathname).toBe("/posture"));
  });

  it("routes `g g` and `g l` to the canonical workspaces and visibly focuses the shell main", async () => {
    server.use(
      http.get("*/api/v1/graph/entities", () =>
        HttpResponse.json({ entities: [], next_cursor: null }),
      ),
      http.get("*/api/v1/documents", () =>
        HttpResponse.json({
          documents: [],
          next_cursor: null,
          document_types: [],
          source_systems: [],
        }),
      ),
    );
    renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    await userEvent.keyboard("gg");
    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/graph"));
    expect(document.getElementById("ne-main")).toHaveFocus();
    await userEvent.keyboard("gl");
    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/documents"));
    expect(document.getElementById("ne-main")).toHaveFocus();
    expect(SHELL_CSS).toMatch(
      /\.ne-shell__stage:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--cherenkov\);[^}]*outline-offset:\s*-2px;/s,
    );
  });

  it("binds the production Graph route to the replacement principal before issuing its reads", async () => {
    const bob = deferredResponse();
    let bobEntityReads = 0;
    let bobReadsForAliceEntity = 0;
    server.use(
      http.get("*/api/v1/me", ({ request }) =>
        request.headers.get("authorization")?.endsWith("alice-token")
          ? HttpResponse.json(PRINCIPAL_OPS_STEWARD)
          : bob.promise,
      ),
      http.get("*/api/v1/graph/entities", ({ request }) => {
        const isBob = request.headers.get("authorization")?.endsWith("bob-token") === true;
        if (isBob) bobEntityReads += 1;
        return HttpResponse.json({
          entities: [
            {
              key: isBob ? "acct/b" : "acct/a",
              entity_class: "Account",
              display_name: isBob ? "Bob visible entity" : "Alice private entity",
              source_system: "crm",
              assurance: "source",
            },
          ],
          next_cursor: null,
        });
      }),
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        const isBob = request.headers.get("authorization")?.endsWith("bob-token") === true;
        const requestedKey = new URL(request.url).searchParams.get("entity");
        if (isBob && requestedKey === "acct/a") bobReadsForAliceEntity += 1;
        const key = requestedKey ?? (isBob ? "acct/b" : "acct/a");
        return HttpResponse.json({
          nodes: [
            {
              key,
              entity_class: "Account",
              display_name: isBob ? "Bob private record" : "Alice private record",
              source_system: "crm",
              source_ref: `crm/${key}`,
              properties: { private_owner: isBob ? "bob" : "alice" },
              assurance: "source",
            },
          ],
          edges: [],
          truncated: false,
        });
      }),
    );

    window.history.pushState(
      {},
      "",
      "/knowledge/graph?keep=one&entity=acct%2Fa&mode=record&depth=4&anchor=old&citation=old-ref&q=alice&class=Account&source=crm&assurance=source&cursor=old&keep=two",
    );
    const view = render(ownedApp("alice-token"));
    expect(await screen.findByText("Alice private record")).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /Alice private entity/ })).toBeInTheDocument();

    view.rerender(ownedApp("bob-token"));
    expect(screen.queryByText("Alice private record")).toBeNull();
    expect(screen.queryByRole("option", { name: /Alice private entity/ })).toBeNull();
    expect(
      screen
        .getAllByRole("status")
        .some((status) => status.textContent?.includes("the verified Graph owner")),
    ).toBe(true);
    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        "/knowledge/graph?keep=one&keep=two",
      ),
    );
    expect(bobEntityReads).toBe(0);
    expect(bobReadsForAliceEntity).toBe(0);

    bob.resolve(HttpResponse.json(PRINCIPAL_APPROVER));
    expect(await screen.findByRole("option", { name: /Bob visible entity/ })).toBeInTheDocument();
    expect(bobEntityReads).toBe(1);
    expect(bobReadsForAliceEntity).toBe(0);
    expect(screen.queryByText("Alice private record")).toBeNull();
    expect(screen.queryByRole("option", { name: /Alice private entity/ })).toBeNull();
    // The URL sync can land after Bob's entity renders (observed in CI), so await it
    // the same way the cleared-URL assertion above does.
    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}`).toBe(
        "/knowledge/graph?keep=one&keep=two&entity=acct%2Fb&mode=graph&depth=1",
      ),
    );
  });

  it("`g a` DOES NOTHING for a key the server refuses approvals to — and its granted `g u` still goes", async () => {
    // The shortcut agrees with the rail (§2.6): a destination the rail does
    // not show is not reachable by chord either. PRINCIPAL_OPS_STEWARD is
    // refused "approvals" and granted "ops".
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_OPS_STEWARD)));
    renderAt("/");
    // Wait for the verdict — the in-flight state is pending, not refused.
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    expect(document.querySelector('.ne-rail__nav a[href="/approvals"]')).toBeNull();
    await userEvent.keyboard("ga");
    expect(window.location.pathname).toBe("/");
    // CONTROL, same key, same session: the chord is not dead — the server
    // granted "ops", so `g u` (Audit log) navigates. An over-broad "no
    // shortcuts for entitled principals" fails here.
    await userEvent.keyboard("gu");
    await waitFor(() => expect(window.location.pathname).toBe("/audit"));
  });

  it("`g a` goes to Approvals for the approver the server grants it to", async () => {
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    renderAt("/");
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.keyboard("ga");
    await waitFor(() => expect(window.location.pathname).toBe("/approvals"));
  });

  it("the palette omits a refused destination AND its actions — and offers the granted ones", async () => {
    // §3 rank 2/3: the palette indexes the SAME filtered sections the rail
    // renders. For the approver, "Audit log" (refused "ops") must not be
    // offered as a destination, and "Export audit log" — a second door onto
    // the same screen — must not be offered as an action.
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    renderAt("/");
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.keyboard("{Meta>}k{/Meta}");
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    await userEvent.type(input, "audit");
    expect(screen.queryByText("Audit log")).toBeNull();
    expect(screen.queryByText("Export audit log")).toBeNull();
    // CONTROL, same key, same palette: its GRANTED destination is offered, so
    // the omission above is the verdict, not a dead palette.
    await userEvent.clear(input);
    await userEvent.type(input, "approvals");
    expect(screen.getByRole("option", { name: /Approvals/ })).toBeInTheDocument();
  });

  it("reads only the current owner's v2 recents and rebuilds them from the live registry", async () => {
    localStorage.setItem(
      "ne-palette-recents:v2:amber%40example.com",
      JSON.stringify([
        {
          id: "dest:/audit",
          label: "STALE audit label",
          kind: "Destination",
          group: "recent",
          to: "/audit",
        },
        {
          id: "dest:/library",
          label: "Workspace › Library",
          kind: "Destination",
          group: "recent",
          to: "/library",
        },
        {
          id: "dest:/approvals",
          label: "STALE approvals label",
          kind: "Destination",
          group: "recent",
          to: "/approvals",
        },
        {
          id: "act:Export audit log",
          label: "Export audit log",
          kind: "Action",
          group: "recent",
          to: "/audit",
          command: "export-audit",
        },
      ]),
    );
    localStorage.setItem(
      "ne-palette-recents:v1",
      JSON.stringify([
        {
          id: "act:Switch theme",
          label: "Legacy ownerless theme row",
          kind: "Action",
          group: "recent",
          to: null,
          command: "switch-theme",
        },
      ]),
    );
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    renderAt("/");
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());

    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.queryByText("STALE audit label")).toBeNull();
    expect(screen.queryByText("Workspace › Library")).toBeNull();
    expect(screen.queryByText("STALE approvals label")).toBeNull();
    expect(screen.queryByText("Export audit log")).toBeNull();
    expect(screen.queryByText("Switch theme")).toBeNull();
    expect(screen.getByRole("option", { name: /Governance › Approvals/ })).toBeInTheDocument();
  });

  it("reconciles open recents on a same-mounted principal change", async () => {
    localStorage.setItem(
      "ne-palette-recents:v2:oscar%40example.com",
      JSON.stringify([
        {
          id: "dest:/audit",
          label: "Forged audit label",
          kind: "Action",
          group: "action",
          to: "javascript:alert(1)",
        },
        {
          id: "act:Switch theme",
          label: "Forged action label",
          kind: "Destination",
          group: "destination",
          to: "/audit",
        },
      ]),
    );
    localStorage.setItem(
      "ne-palette-recents:v2:amber%40example.com",
      JSON.stringify([
        {
          id: "dest:/approvals",
          label: "Forged approvals label",
          kind: "Action",
          group: "action",
          to: "https://attacker.invalid",
        },
        {
          id: "act:Switch theme",
          label: "Forged action label",
          kind: "Destination",
          group: "destination",
          to: "/audit",
        },
      ]),
    );
    server.use(
      http.get("*/api/v1/me", ({ request }) =>
        HttpResponse.json(
          request.headers.get("authorization")?.endsWith("ops-token")
            ? PRINCIPAL_OPS_STEWARD
            : PRINCIPAL_APPROVER,
        ),
      ),
    );
    window.history.pushState({}, "", "/");
    const view = render(ownedApp("ops-token"));
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("option", { name: /Governance › Audit log/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Governance › Approvals/ })).toBeNull();
    expect(screen.getByRole("option", { name: /Switch theme/ })).toBeInTheDocument();
    expect(screen.queryByText(/Forged/)).toBeNull();

    view.rerender(ownedApp("approver-token"));
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /Governance › Approvals/ })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("option", { name: /Governance › Audit log/ })).toBeNull();
    expect(screen.getByRole("option", { name: /Switch theme/ })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Search or jump to" })).toHaveFocus();
  });

  it("drops a prior owner's live query and typed recents before the next principal settles", async () => {
    const aliceTrace = "a2bf6cea2f1d4a779c315f0e2b8d4411";
    const bobApproval = "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411";
    const unattributedProposal = `sha256:${"a".repeat(64)}`;
    localStorage.setItem(
      "ne-palette-recents:v2:oscar%40example.com",
      JSON.stringify([
        {
          id: `id:trace:${aliceTrace}`,
          label: aliceTrace,
          kind: "Trace",
          group: "recent",
          to: `/trace/${aliceTrace}`,
        },
      ]),
    );
    localStorage.setItem(
      "ne-palette-recents:v2:amber%40example.com",
      JSON.stringify([
        {
          id: `id:approval:${bobApproval}`,
          label: bobApproval,
          kind: "Approval request",
          group: "recent",
          to: `/approvals?request=${bobApproval}`,
        },
      ]),
    );
    localStorage.setItem(
      "ne-palette-recents:v1",
      JSON.stringify([
        {
          id: `id:proposal:${unattributedProposal}`,
          label: unattributedProposal,
          kind: "Proposal",
          group: "recent",
          to: `/improvement?proposal=${unattributedProposal}`,
        },
      ]),
    );
    let resolveBob!: (response: Response) => void;
    const bob = new Promise<Response>((resolve) => {
      resolveBob = resolve;
    });
    server.use(
      http.get("*/api/v1/me", ({ request }) =>
        request.headers.get("authorization")?.endsWith("ops-token")
          ? HttpResponse.json(PRINCIPAL_OPS_STEWARD)
          : bob,
      ),
    );

    window.history.pushState({}, "", "/");
    const view = render(ownedApp("ops-token"));
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    await userEvent.keyboard("{Meta>}k{/Meta}");
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    expect(screen.getByRole("option", { name: new RegExp(aliceTrace) })).toBeInTheDocument();
    await userEvent.type(input, aliceTrace);
    expect(input).toHaveValue(aliceTrace);

    view.rerender(ownedApp("approver-token"));
    expect(screen.getByRole("combobox", { name: "Search or jump to" })).toHaveValue("");
    expect(screen.queryByText(aliceTrace)).toBeNull();
    expect(screen.queryByText(unattributedProposal)).toBeNull();

    resolveBob(HttpResponse.json(PRINCIPAL_APPROVER));
    expect(
      await screen.findByRole("option", { name: new RegExp(bobApproval) }),
    ).toBeInTheDocument();
    expect(screen.queryByText(aliceTrace)).toBeNull();
    expect(screen.queryByText(unattributedProposal)).toBeNull();
  });

  it("keeps a cold-open palette empty during a new client's stale-principal window", async () => {
    const aliceTrace = "a2bf6cea2f1d4a779c315f0e2b8d4411";
    const bobApproval = "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411";
    localStorage.setItem(
      "ne-palette-recents:v2:oscar%40example.com",
      JSON.stringify([
        {
          id: `id:trace:${aliceTrace}`,
          label: aliceTrace,
          kind: "Trace",
          group: "recent",
          to: `/trace/${aliceTrace}`,
        },
      ]),
    );
    localStorage.setItem(
      "ne-palette-recents:v2:amber%40example.com",
      JSON.stringify([
        {
          id: `id:approval:${bobApproval}`,
          label: bobApproval,
          kind: "Approval request",
          group: "recent",
          to: `/approvals?request=${bobApproval}`,
        },
      ]),
    );
    let resolveBob!: (response: Response) => void;
    const bob = new Promise<Response>((resolve) => {
      resolveBob = resolve;
    });
    server.use(
      http.get("*/api/v1/me", ({ request }) =>
        request.headers.get("authorization")?.endsWith("ops-token")
          ? HttpResponse.json(PRINCIPAL_OPS_STEWARD)
          : bob,
      ),
    );

    window.history.pushState({}, "", "/");
    const view = render(ownedApp("ops-token"));
    await waitFor(() => expect(document.querySelector(".ne-rail__slotpending")).toBeNull());
    view.rerender(ownedApp("approver-token"));
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("combobox", { name: "Search or jump to" })).toHaveValue("");
    expect(screen.queryByText(aliceTrace)).toBeNull();
    expect(screen.queryByRole("option")).toBeNull();

    resolveBob(HttpResponse.json(PRINCIPAL_APPROVER));
    expect(
      await screen.findByRole("option", { name: new RegExp(bobApproval) }),
    ).toBeInTheDocument();
    expect(screen.queryByText(aliceTrace)).toBeNull();
  });

  it("gives every collapsed destination a keyboard-visible tooltip", async () => {
    renderAt("/");
    const toggle = await screen.findByRole("button", { name: "Collapse navigation" });
    await userEvent.click(toggle);
    toggle.blur();
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "Neutron Enterprise" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "Home" })).toHaveFocus();
    await userEvent.tab();
    const documents = screen.getByRole("link", { name: "Documents" });
    expect(documents).toHaveFocus();
    expect(documents).not.toHaveAttribute("title");
    const tooltip = screen.getByRole("tooltip", { name: "Documents" });
    expect(documents).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip.parentElement?.parentElement).toBe(document.body);
    expect(SHELL_CSS).toMatch(
      /\.ne-rail__tooltip-hit\s*\{[^}]*position:\s*fixed;[^}]*pointer-events:\s*auto;[^}]*padding-left:\s*var\(--space-8\);/s,
    );
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Expand navigation" }));
    documents.focus();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(documents).toHaveAttribute("title", "Documents");
  });

  it("keeps a focus-owned compact tooltip through pointer and viewport movement", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Collapse navigation" }));
    const documents = screen.getByRole("link", { name: "Documents" });
    let rect = {
      x: 8,
      y: 80,
      left: 8,
      right: 48,
      top: 80,
      bottom: 104,
      width: 40,
      height: 24,
      toJSON: () => ({}),
    };
    documents.getBoundingClientRect = () => rect;
    documents.focus();
    const tooltip = await screen.findByRole("tooltip", { name: "Documents" });
    const hitRegion = tooltip.parentElement!;
    expect(documents).toHaveAttribute("aria-describedby", tooltip.id);

    await userEvent.hover(documents);
    await userEvent.unhover(documents);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.getByRole("tooltip", { name: "Documents" })).toBeInTheDocument();

    await userEvent.hover(hitRegion);
    await userEvent.unhover(hitRegion);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.getByRole("tooltip", { name: "Documents" })).toBeInTheDocument();

    rect = { ...rect, right: 64, top: 120, bottom: 144 };
    window.dispatchEvent(new Event("scroll"));
    await waitFor(() => expect(hitRegion).toHaveStyle({ left: "64px", top: "132px" }));
    expect(documents).toHaveAttribute("aria-describedby", tooltip.id);

    rect = { ...rect, right: 72, top: 160, bottom: 184 };
    window.dispatchEvent(new Event("resize"));
    await waitFor(() => expect(hitRegion).toHaveStyle({ left: "72px", top: "172px" }));
    expect(documents).toHaveAttribute("aria-describedby", tooltip.id);

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(documents).toHaveFocus();

    documents.blur();
    documents.focus();
    expect(await screen.findByRole("tooltip", { name: "Documents" })).toBeInTheDocument();
    documents.blur();
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });

  it("retains mixed tooltip ownership until both focus and pointer release", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Collapse navigation" }));
    const documents = screen.getByRole("link", { name: "Documents" });
    await userEvent.hover(documents);
    documents.focus();
    const tooltip = await screen.findByRole("tooltip", { name: "Documents" });
    documents.blur();
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.getByRole("tooltip", { name: "Documents" })).toBeInTheDocument();
    expect(documents).toHaveAttribute("aria-describedby", tooltip.id);

    await userEvent.unhover(documents);
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    expect(documents).not.toHaveAttribute("aria-describedby");

    await userEvent.hover(documents);
    expect(screen.getByRole("tooltip", { name: "Documents" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(documents).not.toHaveAttribute("aria-describedby");
  });

  it("arbitrates mixed focus and pointer ownership to one compact portal", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Collapse navigation" }));
    const graph = screen.getByRole("link", { name: "Graph" });
    const posture = screen.getByRole("link", { name: "Posture" });
    graph.focus();
    const graphTooltip = await screen.findByRole("tooltip", { name: "Graph" });
    expect(graph).toHaveAttribute("aria-describedby", graphTooltip.id);

    window.dispatchEvent(new Event("scroll"));
    await waitFor(() => expect(screen.getAllByRole("tooltip")).toHaveLength(1));
    expect(screen.getByRole("tooltip", { name: "Graph" })).toBeInTheDocument();

    await userEvent.hover(posture);
    const postureTooltip = screen.getByRole("tooltip", { name: "Posture" });
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    expect(posture).toHaveAttribute("aria-describedby", postureTooltip.id);
    expect(graph).not.toHaveAttribute("aria-describedby");

    window.dispatchEvent(new Event("scroll"));
    const restoredGraphTooltip = await screen.findByRole("tooltip", { name: "Graph" });
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    expect(graph).toHaveAttribute("aria-describedby", restoredGraphTooltip.id);
    expect(posture).not.toHaveAttribute("aria-describedby");
  });

  it("unconditionally dismisses the entire compact tooltip ownership stack", async () => {
    renderAt("/posture");
    await userEvent.click(await screen.findByRole("button", { name: "Collapse navigation" }));
    const graph = screen.getByRole("link", { name: "Graph" });
    const posture = screen.getByRole("link", { name: "Posture" });

    await userEvent.hover(graph);
    posture.focus();
    expect(await screen.findByRole("tooltip", { name: "Posture" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(graph).not.toHaveAttribute("aria-describedby");
    expect(posture).not.toHaveAttribute("aria-describedby");

    await userEvent.unhover(graph);
    posture.blur();
    await userEvent.hover(graph);
    posture.focus();
    expect(await screen.findByRole("tooltip", { name: "Posture" })).toBeInTheDocument();
    await userEvent.click(posture);
    expect(window.location.pathname).toBe("/posture");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(graph).not.toHaveAttribute("aria-describedby");
    expect(posture).not.toHaveAttribute("aria-describedby");
  });

  it("keeps the compact Settings tooltip in the viewport and dismisses it on scroll", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 320 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 390 });
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: "Collapse navigation" }));
    const rail = screen.getByRole("navigation", { name: "Primary" });
    const settings = screen.getByRole("link", { name: "Settings" });
    const railRect = {
      x: 0,
      y: 0,
      left: 0,
      right: 56,
      top: 0,
      bottom: 390,
      width: 56,
      height: 390,
      toJSON: () => ({}),
    };
    const rect = {
      x: 8,
      y: 360,
      left: 8,
      right: 48,
      top: 360,
      bottom: 384,
      width: 40,
      height: 24,
      toJSON: () => ({}),
    };
    rail.getBoundingClientRect = () => railRect;
    settings.getBoundingClientRect = () => rect;
    await userEvent.hover(settings);
    const tooltip = screen.getByRole("tooltip", { name: "Settings" });
    expect(tooltip.parentElement).toHaveStyle({ left: "56px", top: "372px" });
    expect(Number.parseFloat(tooltip.parentElement!.style.left)).toBeGreaterThanOrEqual(
      railRect.right,
    );
    expect(tooltip.parentElement?.parentElement).toBe(document.body);
    expect(settings).toHaveAttribute("aria-describedby", tooltip.id);
    window.dispatchEvent(new Event("scroll"));
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });

  it("NO single-letter shortcut shadows typing in an input", async () => {
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: /Search or jump to/ }));
    const input = screen.getByRole("combobox", { name: "Search or jump to" });
    // "gp" typed into a field is two characters, not a navigation.
    await userEvent.type(input, "gp");
    expect(input).toHaveValue("gp");
    expect(window.location.pathname).toBe("/");
  });
});

describe("route renames ship with redirects (§1.2)", () => {
  it.each([
    // /chat -> /ask -> the Agent workspace. Both hops are `replace`, and the
    // second is the 2026-08-27 consolidation: Ask is the Chat tab of the Agent
    // it always ran. The oldest bookmark in the console still lands somewhere
    // real, which is the whole point of keeping both hops.
    ["/chat", "/agents/agt_01k3meridiananalyst000000000"],
    ["/ask", "/agents/agt_01k3meridiananalyst000000000"],
    ["/knowledge", "/knowledge/documents"],
    ["/connect", "/sources"],
    ["/flywheel", "/improvement"],
    ["/evals", "/evaluations"],
    ["/kpi", "/insights"],
    ["/steward", "/improvement"],
  ])("%s lands on %s", async (from, to) => {
    // Every one of these paths is somebody's bookmark.
    renderAt(from);
    await waitFor(() => expect(window.location.pathname).toBe(to));
  });
});
