// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9 — SOURCES, proved against the served fixture.
//
// The assertions that matter here are IDENTITY assertions. A count assertion
// passes for the wrong rows: "3 rows" is true of a filter that selected three
// rows nobody asked for, and "1 row" is true of a filter that selected the wrong
// one. So every sort and every filter below is checked by which sources are on
// screen and in what order.
//
// The other load-bearing pair is refused-vs-unreachable. A 403 must render the
// SERVER's refusal; a 503 must never render a sentence about this key's role,
// because that sentence is a false statement about authorization.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";
import type { ConnectStatusView } from "../api/types";
import { PrincipalProvider, usePrincipal } from "../auth/PrincipalContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { AuthStub } from "../test/authStub";
import {
  AUDIT_EVENTS,
  CONNECT_STATUS_EMPTY,
  PRINCIPAL_APPROVER,
  VERIFICATION_OK,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { ConnectPage } from "./ConnectPage";

const STATUS_URL = "*/api/v1/connect/status";
const AUDIT_URL = "*/api/v1/audit/events";
const SYNC_URL = "*/api/v1/connect/:source/sync";
const AUTHORITY_URL = "*/api/v1/connect/authority/attempts";
const AUTHORITY_RETRY_URL = "*/api/v1/connect/authority/attempts/:attemptId/retry";

beforeEach(() => {
  server.use(http.get(AUTHORITY_URL, () => HttpResponse.json({ attempts: [] })));
});

function renderPage(withPrincipalProbe = false) {
  return render(
    <AuthStub>
      <MemoryRouter>
        <PrincipalProvider>
          {withPrincipalProbe ? (
            <span data-testid="principal-status" hidden>
              <PrincipalStatus />
            </span>
          ) : null}
          <AuditPollProvider>
            <ConnectPage />
          </AuditPollProvider>
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
}

function PrincipalStatus() {
  return usePrincipal().status;
}

function serve(payload: ConnectStatusView) {
  server.use(http.get(STATUS_URL, () => HttpResponse.json(payload)));
}

/** Same tree as `renderPage`, but with a fast audit poll so a test can drive a
 * genuine poll-tick (latestSeq change) without a real 5-second wait. */
function renderPageFastPoll() {
  return render(
    <AuthStub>
      <MemoryRouter>
        <PrincipalProvider>
          <AuditPollProvider intervalMs={15}>
            <ConnectPage />
          </AuditPollProvider>
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
}

/** The rendered window, BY ROW IDENTITY, in render order. */
function sourceIds(): string[] {
  const table = screen.getByRole("table", { name: "Sources" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

/** The row for one source, BY ITS OWN CELL — "jira" is also a Kind, and a
 * by-text click would be ambiguous between the two columns. */
function sourceRow(id: string): HTMLElement {
  const table = screen.getByRole("table", { name: "Sources" });
  const row = within(table)
    .getAllByRole("row")
    .slice(1)
    .find((r) => within(r).getAllByRole("cell")[0]?.textContent === id);
  if (row === undefined) throw new Error(`no row for source ${id}`);
  return row;
}

function reasonRows(): string[] {
  const table = screen.getByRole("table", { name: "Quarantine reasons" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

/** A never-checked lane: every nullable field absent at once — no sync cycle
 * has ever run, which is a different fact from "checked and found nothing". */
const NEVER_CHECKED: ConnectStatusView = {
  connectors: [
    {
      id: "zendesk",
      kind: "zendesk",
      posture: "live",
      cursor_age_s: null,
      last_checked_age_s: null,
      acl_mirror: null,
      last_sync_seq: null,
      quarantined_count: null,
      sync_allowed: false,
    },
  ],
  quarantine_total: 0,
  quarantine_reasons: [],
};

/** SPEC §12 item 9's live-box shape: the cron ran two minutes ago and found
 * nothing new — the newest record is a day old. Both true at once. */
const QUIET_SOURCE: ConnectStatusView = {
  connectors: [
    {
      id: "salesforce",
      kind: "salesforce",
      posture: "live",
      cursor_age_s: 90_000,
      last_checked_age_s: 120,
      acl_mirror: "ok",
      last_sync_seq: 51,
      quarantined_count: 0,
      sync_allowed: true,
    },
  ],
  quarantine_total: 0,
  quarantine_reasons: [],
};

/** The old-live demo shape captured on 2026-08-19: both source-age facts are
 * fourteen days old, while the independent permissions-mirror fact says the
 * last recorded check passed. The UI must not let that historical ACL result
 * read as a current source-health verdict. */
const OLD_LIVE_STALE_SOURCE: ConnectStatusView = {
  connectors: [
    {
      id: "m365-sharepoint",
      kind: "m365",
      posture: "live",
      cursor_age_s: 14 * 24 * 60 * 60,
      last_checked_age_s: 14 * 24 * 60 * 60,
      acl_mirror: "ok",
      last_sync_seq: 23_090,
      quarantined_count: 0,
      sync_allowed: true,
    },
  ],
  quarantine_total: 0,
  quarantine_reasons: [],
};

/** The post-cutover demo posture: the governed M365 population is retained as
 * a historical snapshot, while the local CSV lane remains live/runnable. The
 * deliberately inconsistent snapshot capability bit proves the console gates
 * on posture too, rather than trusting one stale boolean. */
const FROZEN_M365_WITH_LIVE_CSV: ConnectStatusView = {
  connectors: [
    {
      id: "m365",
      kind: "m365",
      posture: "snapshot",
      cursor_age_s: 14 * 24 * 60 * 60,
      last_checked_age_s: 14 * 24 * 60 * 60,
      acl_mirror: "ok",
      last_sync_seq: 23_090,
      quarantined_count: 0,
      sync_allowed: true,
    },
    {
      id: "csv",
      kind: "csv",
      posture: "live",
      cursor_age_s: 90,
      last_checked_age_s: 30,
      acl_mirror: "ok",
      last_sync_seq: 23_091,
      quarantined_count: 0,
      sync_allowed: true,
    },
  ],
  quarantine_total: 0,
  quarantine_reasons: [],
};

describe("Sources (§9)", () => {
  it("waits for the principal answer and never asks refused ops drill-downs", async () => {
    let resolvePrincipal!: (response: Response) => void;
    const principal = new Promise<Response>((resolve) => {
      resolvePrincipal = resolve;
    });
    let quarantineHits = 0;
    let authorityHits = 0;
    server.use(
      http.get("*/api/v1/me", () => principal),
      http.get("*/api/v1/connect/quarantine", () => {
        quarantineHits += 1;
        return HttpResponse.json({ detail: "not authorized" }, { status: 403 });
      }),
      http.get(AUTHORITY_URL, () => {
        authorityHits += 1;
        return HttpResponse.json({ detail: "not authorized" }, { status: 403 });
      }),
    );

    renderPage(true);
    await screen.findByRole("heading", { level: 1, name: "Sources" });
    expect(quarantineHits).toBe(0);
    expect(authorityHits).toBe(0);

    resolvePrincipal(HttpResponse.json(PRINCIPAL_APPROVER));
    await waitFor(() => expect(screen.getByTestId("principal-status")).toHaveTextContent("ok"));
    expect(quarantineHits).toBe(0);
    expect(authorityHits).toBe(0);
    expect(screen.queryByRole("table", { name: "Quarantined records" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Authority recovery" })).toBeNull();
  });

  it("renders current authority recovery metadata and sends an opaque zero-body retry", async () => {
    const attempt = {
      attempt_id: "attempt-current",
      source_kind: "m365-sharepoint",
      source_binding: "m365",
      scope_hash: "a".repeat(64),
      identity_hash: "b".repeat(64),
      stage: "deletion_validated",
      source_revision: "etag-current",
      contract_epoch: 4,
      prior_artifact_id: null,
      receipt_id: null,
      receipt_hash: null,
    };
    let pending = true;
    let retryBody = "not-called";
    let releaseRetry = () => {};
    const retryGate = new Promise<void>((resolve) => {
      releaseRetry = resolve;
    });
    server.use(
      http.get(AUTHORITY_URL, () => HttpResponse.json({ attempts: pending ? [attempt] : [] })),
      http.post(AUTHORITY_RETRY_URL, async ({ request, params }) => {
        expect(params.attemptId).toBe("attempt-current");
        retryBody = await request.text();
        await retryGate;
        pending = false;
        return HttpResponse.json(attempt, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    const table = await screen.findByRole("table", { name: "Authority recovery attempts" });
    expect(await within(table).findByText("Deletion validated")).toBeInTheDocument();
    expect(within(table).getByText("etag-current")).toBeInTheDocument();
    await user.click(
      within(table).getByRole("button", {
        name: "Retry authority attempt: attempt-current",
      }),
    );

    // Currency is re-established from the server after the action. The old
    // button cannot survive as a stale second action, and source/receipt data
    // never rides the POST body.
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Retry authority attempt: attempt-current" }),
      ).toBeNull(),
    );
    releaseRetry();
    await screen.findByText("No authority retries pending.");
    expect(
      screen.queryByRole("button", { name: "Retry authority attempt: attempt-current" }),
    ).toBeNull();
    expect(retryBody).toBe("");
  });

  it("does not fabricate an empty authority queue when the server refuses it", async () => {
    server.use(
      http.get(AUTHORITY_URL, () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    renderPage();
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Authority recovery" })).toBeNull(),
    );
    expect(screen.queryByText("No authority retries pending.")).toBeNull();
  });

  it("shows loading only on the genuine first load, and keeps a stale authority row up through a poll-driven background refetch", async () => {
    const attempt = {
      attempt_id: "attempt-poll-1",
      source_kind: "m365-sharepoint",
      source_binding: "m365",
      scope_hash: "a".repeat(64),
      identity_hash: "b".repeat(64),
      stage: "claimed" as const,
      source_revision: "etag-poll-1",
      contract_epoch: 1,
      prior_artifact_id: null,
      receipt_id: null,
      receipt_hash: null,
    };
    let authorityCalls = 0;
    let releaseSecond = () => {};
    server.use(
      http.get(AUTHORITY_URL, () => {
        authorityCalls += 1;
        if (authorityCalls === 1) {
          return HttpResponse.json({ attempts: [attempt] });
        }
        // The poll-driven BACKGROUND refetch: held open so the test can prove
        // the first response's row is still on screen while this one is
        // in flight, before it ever resolves.
        return new Promise<Response>((resolve) => {
          releaseSecond = () => resolve(HttpResponse.json({ attempts: [attempt] }));
        });
      }),
    );

    renderPageFastPoll();

    // The genuine FIRST load shows the loading state (skeleton rows + the
    // visually-hidden announcement) before the row is up.
    await waitFor(() =>
      expect(screen.getAllByRole("status").map((n) => n.textContent)).toContain(
        "Loading authority recovery attempts",
      ),
    );
    const table = await screen.findByRole("table", { name: "Authority recovery attempts" });
    expect(await within(table).findByText("etag-poll-1")).toBeInTheDocument();

    // A later audit-poll tick surfaces a fresh event — the same trigger a real
    // background refresh runs on (useAuthorityAttempts reloads on latestSeq).
    const newEvent = {
      ...AUDIT_EVENTS[2]!,
      id: 4,
      seq: 4,
      event_id: "evt-poll-4",
      event_type: "connector.sync",
      action: "sync",
    };
    server.use(
      http.get(AUDIT_URL, () =>
        HttpResponse.json({ events: [newEvent], verification: VERIFICATION_OK }),
      ),
    );

    await waitFor(() => expect(authorityCalls).toBeGreaterThanOrEqual(2));
    // The background refetch is IN FLIGHT right now (held open above). The
    // stale row must still be on screen — no transient empty/loading blank —
    // and the table must not have flipped back to its loading announcement.
    expect(within(table).getByText("etag-poll-1")).toBeInTheDocument();
    expect(screen.queryByText("No authority retries pending.")).toBeNull();
    expect(screen.queryAllByRole("status").map((n) => n.textContent)).not.toContain(
      "Loading authority recovery attempts",
    );

    releaseSecond();
    // The new response replaces the old one once it lands; the row survives
    // because the server kept serving it, not because nothing ever changed.
    await waitFor(() => expect(within(table).getByText("etag-poll-1")).toBeInTheDocument());
  });

  it("control: retry still clears the queue immediately, unlike the now-silent background refetch above", async () => {
    const attempt = {
      attempt_id: "attempt-retry-control",
      source_kind: "jira",
      source_binding: "jira",
      scope_hash: "a".repeat(64),
      identity_hash: "b".repeat(64),
      stage: "claimed" as const,
      source_revision: "etag-retry-control",
      contract_epoch: 1,
      prior_artifact_id: null,
      receipt_id: null,
      receipt_hash: null,
    };
    let pending = true;
    let releaseRetry = () => {};
    const retryGate = new Promise<void>((resolve) => {
      releaseRetry = resolve;
    });
    server.use(
      http.get(AUTHORITY_URL, () => HttpResponse.json({ attempts: pending ? [attempt] : [] })),
      http.post(AUTHORITY_RETRY_URL, async () => {
        await retryGate;
        pending = false;
        return HttpResponse.json(attempt, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    const table = await screen.findByRole("table", { name: "Authority recovery attempts" });
    // The read is deferred until /v1/me settles (role-gated read races), so the
    // first row arrives asynchronously — find, don't get.
    expect(await within(table).findByText("etag-retry-control")).toBeInTheDocument();

    await user.click(
      within(table).getByRole("button", {
        name: "Retry authority attempt: attempt-retry-control",
      }),
    );
    // Retry clears the queue IMMEDIATELY, before the server ever answers —
    // deliberately unlike the poll-driven background refetch, which must NOT
    // blank the table. This is the control the fix above must not touch.
    await waitFor(() => expect(within(table).queryByText("etag-retry-control")).toBeNull());

    releaseRetry();
    await screen.findByText("No authority retries pending.");
  });

  it("renders every served connector as a row, and no lane the server did not send", async () => {
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));
    // The eight hardcoded canonical lanes are gone: only what was served.
    expect([...sourceIds()].sort()).toEqual(["debezium", "jira", "m365"]);
    expect(screen.queryByText("Slack")).not.toBeInTheDocument();
    expect(screen.queryByText("Salesforce")).not.toBeInTheDocument();
  });

  it("defaults to Source A–Z", async () => {
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));
    // Served order is m365, debezium, jira — the rendered order is alphabetical.
    // Scoped to the Sources table: the quarantined-records table below also
    // carries a Source column.
    expect(sourceIds()).toEqual(["debezium", "jira", "m365"]);
    const sourcesTable = screen.getByRole("table", { name: "Sources" });
    expect(within(sourcesTable).getByRole("columnheader", { name: "Source" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("shows Sync now only from the server capability and sends no request body", async () => {
    let body = "not-called";
    server.use(
      http.post(SYNC_URL, async ({ request, params }) => {
        body = await request.text();
        return HttpResponse.json(
          {
            request_id: "00000000-0000-4000-8000-000000000002",
            source: String(params.source),
            status: "succeeded",
            created_at: "2026-08-19T12:00:00Z",
            expires_at: "2026-08-19T12:05:00Z",
            counts: {
              mode: "incremental",
              records_seen: 4,
              written: 2,
              skipped_unchanged: 2,
              invalid: 0,
              quarantined: 0,
            },
            error_class: null,
          },
          { status: 202 },
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "Sync now: m365" });

    // debezium's server-authored capability is false. The client does not infer
    // visibility from a local role name.
    expect(screen.queryByRole("button", { name: /debezium/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Sync now: m365" }));

    const counts = "4 seen · 2 written · 2 unchanged · 0 invalid · 0 quarantined";
    const row = sourceRow("m365");
    const done = await within(row).findByRole("button", {
      name: `Synced: m365. ${counts}`,
    });
    expect(body).toBe("");
    expect(done).toBeInTheDocument();
    expect(within(row).getByRole("status")).toHaveTextContent(counts);
    expect(within(row).getByText(counts)).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders frozen M365 truth and leaves only the live CSV lane keyboard-runnable", async () => {
    const requested: string[] = [];
    serve(FROZEN_M365_WITH_LIVE_CSV);
    server.use(
      http.post(SYNC_URL, ({ params }) => {
        requested.push(String(params.source));
        return HttpResponse.json(
          {
            request_id: "00000000-0000-4000-8000-000000000003",
            source: String(params.source),
            status: "succeeded",
            created_at: "2026-08-19T12:00:00Z",
            expires_at: "2026-08-19T12:05:00Z",
            counts: {
              mode: "incremental",
              records_seen: 1,
              written: 0,
              skipped_unchanged: 1,
              invalid: 0,
              quarantined: 0,
            },
            error_class: null,
          },
          { status: 202 },
        );
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toEqual(["csv", "m365"]));

    const snapshot = sourceRow("m365");
    expect(within(snapshot).getByLabelText("Frozen snapshot")).toBeInTheDocument();
    expect(within(snapshot).getAllByRole("cell")[2]!.textContent).toBe("14 d ago");
    expect(within(snapshot).queryByRole("button", { name: /sync/i })).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Sync now:/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Sync now: csv" })).toBeInTheDocument();

    // The table's keyboard action for a snapshot opens only the read pane. It
    // cannot reach a hidden POST affordance through Enter or row activation.
    snapshot.focus();
    await user.keyboard("{Enter}");
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByLabelText("Frozen snapshot")).toBeInTheDocument();
    expect(within(pane).getByText("Last checked")).toBeInTheDocument();
    expect(requested).toEqual([]);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // The control case stays executable from the keyboard: only the live CSV
    // lane reaches the governed zero-body sync request.
    const csvSync = screen.getByRole("button", { name: "Sync now: csv" });
    csvSync.focus();
    expect(document.activeElement).toBe(csvSync);
    await user.keyboard(" ");
    await waitFor(() => expect(requested).toEqual(["csv"]));
  });

  it("renders an authoritative POST refusal as a refused action", async () => {
    server.use(
      http.post(SYNC_URL, () =>
        HttpResponse.json({ detail: "sync request revoked" }, { status: 403 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Sync now: jira" }));
    expect(await screen.findByRole("button", { name: "Sync refused: jira" })).toBeInTheDocument();
  });

  it("sorts Last checked newest-first on the first click, and reverses on the second", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.click(screen.getByRole("button", { name: "Last checked" }));
    // Run ages: m365 15 s, debezium 22 s, jira 610 s.
    expect(sourceIds()).toEqual(["m365", "debezium", "jira"]);
    expect(screen.getByRole("columnheader", { name: "Last checked" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );

    await user.click(screen.getByRole("button", { name: "Last checked" }));
    expect(sourceIds()).toEqual(["jira", "debezium", "m365"]);
  });

  it("sorts Newest record on the RECORD age — a different order than Last checked", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.click(screen.getByRole("button", { name: "Newest record" }));
    // Record ages: debezium 22 s, m365 340 s, jira 9,200 s — NOT the run-age
    // order above, so a column sorting on the wrong field fails by identity.
    expect(sourceIds()).toEqual(["debezium", "m365", "jira"]);
    expect(screen.getByRole("columnheader", { name: "Newest record" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("sorts Quarantined largest-first, and Kind A–Z", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.click(screen.getByRole("button", { name: "Quarantined" }));
    // jira 2, debezium 1, m365 0.
    expect(sourceIds()).toEqual(["jira", "debezium", "m365"]);

    await user.click(screen.getByRole("button", { name: "Kind" }));
    // cdc, jira, m365.
    expect(sourceIds()).toEqual(["debezium", "jira", "m365"]);
  });

  it("leaves Permissions check unsortable — §9 names five sortable columns", async () => {
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));
    const header = screen.getByRole("columnheader", { name: "Permissions check" });
    expect(within(header).queryByRole("button")).toBeNull();
    // The five §9 names, all sortable — scoped to the Sources table (the
    // quarantined-records table below has its own Source header).
    const sourcesTable = screen.getByRole("table", { name: "Sources" });
    for (const name of ["Source", "Kind", "Last checked", "Newest record", "Quarantined"]) {
      expect(within(sourcesTable).getByRole("button", { name })).toBeInTheDocument();
    }
    // The collapsed label is GONE: "Last sync" claimed run-time while showing
    // record age (SPEC §12 item 9) — neither fact may wear it again.
    expect(screen.queryByText("Last sync")).not.toBeInTheDocument();
  });

  it("renders a quiet source checked-recently with an old newest record — two facts, two names", async () => {
    serve(QUIET_SOURCE);
    renderPage();
    await waitFor(() => expect(sourceIds()).toEqual(["salesforce"]));

    // The demo failure mode this pins (SPEC §12 item 9): the presenter says
    // "it polls every fifteen minutes"; the screen must AGREE — checked 2 min
    // ago — while separately saying the newest record is a day old.
    const row = sourceRow("salesforce");
    const cells = within(row).getAllByRole("cell");
    expect(cells[2]!.textContent).toBe("2 min ago");
    expect(cells[3]!.textContent).toBe("1 d ago");

    // The pane carries the same two facts absolutely, each under its own name.
    const user = userEvent.setup();
    await user.click(row);
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("Last checked")).toBeInTheDocument();
    expect(within(pane).getByText("Newest record")).toBeInTheDocument();
  });

  it("binds a passing permissions mirror to the old check instead of presenting current source health", async () => {
    serve(OLD_LIVE_STALE_SOURCE);
    renderPage();
    await waitFor(() => expect(sourceIds()).toEqual(["m365-sharepoint"]));

    const row = sourceRow("m365-sharepoint");
    const cells = within(row).getAllByRole("cell");
    expect(cells[2]!.textContent).toBe("14 d ago");
    expect(cells[3]!.textContent).toBe("14 d ago");
    expect(within(cells[4]!).getByLabelText("OK — at last check")).toBeInTheDocument();
    expect(within(row).queryByLabelText("DEGRADED")).toBeNull();

    const user = userEvent.setup();
    await user.click(row);
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByLabelText("OK — at last check")).toBeInTheDocument();
  });

  it("narrows to the chosen kind — asserted by which sources remain", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.selectOptions(screen.getByLabelText("Kind"), "jira");
    expect(sourceIds()).toEqual(["jira"]);
    expect(screen.getByText("1 sources")).toBeInTheDocument();
    // The dismissible chip says WHY the list is short (§2.2).
    await user.click(screen.getByRole("button", { name: "Clear Kind: jira" }));
    expect(sourceIds()).toEqual(["debezium", "jira", "m365"]);
    expect(screen.getByText("3 sources")).toBeInTheDocument();
  });

  it("search-within narrows by identity, and clears on Esc", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    const box = screen.getByLabelText("Filter sources…");
    await user.type(box, "deb");
    expect(sourceIds()).toEqual(["debezium"]);
    await user.type(box, "{Escape}");
    expect(sourceIds()).toEqual(["debezium", "jira", "m365"]);
  });

  it("renders the filtered-to-nothing state with a way back", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.type(screen.getByLabelText("Filter sources…"), "nothing-matches-this");
    expect(screen.getByText("No matching sources.")).toBeInTheDocument();
    // NOT "No sources connected." — three sources are connected.
    expect(screen.queryByText("No sources connected.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show all 3" }));
    expect(sourceIds()).toEqual(["debezium", "jira", "m365"]);
  });

  it("renders the absent fields as §9 names them, with the reason in the title", async () => {
    serve(NEVER_CHECKED);
    renderPage();
    await waitFor(() => expect(sourceIds()).toEqual(["zendesk"]));

    const row = within(screen.getByRole("table", { name: "Sources" })).getAllByRole("row")[1]!;
    const cells = within(row).getAllByRole("cell");
    // Never checked is WORDS, not a dash: a lane no cycle has ever run for must
    // be unmistakable next to one that checked seconds ago and found nothing.
    expect(cells[2]!.textContent).toBe("Never checked");
    expect(
      within(cells[2]!).getByTitle("No sync cycle has run for this source"),
    ).toBeInTheDocument();
    expect(cells[3]!.textContent).toBe("—");
    expect(within(cells[3]!).getByTitle("No record cursor recorded")).toBeInTheDocument();
    expect(cells[4]!.textContent).toBe("—");
    expect(within(cells[4]!).getByTitle("No sync recorded yet")).toBeInTheDocument();
    expect(cells[5]!.textContent).toBe("—");
    expect(within(cells[5]!).getByTitle("Quarantine store unreadable")).toBeInTheDocument();
    // A never-checked lane is not a zero and not a green tick.
    expect(screen.queryByText("OK")).not.toBeInTheDocument();
  });

  it("announces the load without a spinner paragraph", () => {
    renderPage();
    const announcements = screen.getAllByRole("status").map((n) => n.textContent);
    expect(announcements).toContain("Loading sources");
  });

  it("renders the empty screen with §9's copy when nothing is connected", async () => {
    serve(CONNECT_STATUS_EMPTY);
    renderPage();
    expect(await screen.findByText("No sources connected.")).toBeInTheDocument();
    // An unreadable quarantine store is UNAVAILABLE, never zero.
    expect(screen.getByText("Quarantine — total unavailable")).toBeInTheDocument();
    expect(screen.getByTitle("The quarantine store could not be read")).toBeInTheDocument();
    expect(screen.getByText("Nothing in quarantine.")).toBeInTheDocument();
  });

  it("renders the error state on an unreachable read — and NEVER a role boundary", async () => {
    server.use(http.get(STATUS_URL, () => new HttpResponse(null, { status: 503 })));
    renderPage();
    expect(await screen.findByText(/Couldn.t load sources\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // The bug this test exists for: an unreachable read is not a refusal, and
    // saying it is states something false about this key's authorization.
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
  });

  it("retries the same read from the error state", async () => {
    const user = userEvent.setup();
    server.use(http.get(STATUS_URL, () => new HttpResponse(null, { status: 503 })));
    renderPage();
    await screen.findByText(/Couldn.t load sources\./);

    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(sourceIds()).toHaveLength(3));
  });

  it("renders the SERVER's refusal on a 403, not a client-side denial", async () => {
    server.use(http.get(STATUS_URL, () => new HttpResponse(null, { status: 403 })));
    renderPage();
    expect(
      await screen.findByText(/Your role doesn.t include sources\. Contact your administrator/),
    ).toBeInTheDocument();
    // Refused is not an outage.
    expect(screen.queryByText(/Couldn.t load sources\./)).not.toBeInTheDocument();
    // The title row survives, so the page that refused is still named.
    expect(screen.getByRole("heading", { name: "Sources" })).toBeInTheDocument();
  });

  it("opens the detail pane on a row, absolute, with the degraded line §9 gives", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    // The TABLE cell carries the qualifier too, not just the pane: the grid is
    // the surface a reader scans, so an unqualified DEGRADED there would be the
    // stale-verdict-as-current claim in the most-read place.
    expect(
      within(within(sourceRow("jira")).getAllByRole("cell")[4]!).getByLabelText(
        "DEGRADED \u2014 at last check",
      ),
    ).toBeInTheDocument();

    await user.click(sourceRow("jira"));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("Permissions check")).toBeInTheDocument();
    expect(within(pane).getByText("DEGRADED")).toBeInTheDocument();
    // The word alone would read as a CURRENT verdict; the pill must carry the
    // same "at last check" qualifier the OK branch does.
    expect(within(pane).getByLabelText("DEGRADED — at last check")).toBeInTheDocument();
    expect(
      within(pane).getByText(
        "The permissions mirror failed its last check. Results from this source are withheld until it passes.",
      ),
    ).toBeInTheDocument();
    // The sync record links into the ledger for a key the server has not refused.
    const link = within(pane).getByRole("link", { name: "Sync record: entry 39" });
    expect(link).toHaveAttribute("href", "/audit?seq=39");
    // The relative form is the TABLE's; the pane gives the instant.
    expect(within(pane).queryByText(/ago$/)).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("renders the sync record as plain text when the server has refused this key the ledger", async () => {
    const user = userEvent.setup();
    server.use(http.get(AUDIT_URL, () => new HttpResponse(null, { status: 403 })));
    renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));

    await user.click(sourceRow("jira"));
    const pane = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(pane).getByText("Sync record: entry 39")).toBeInTheDocument(),
    );
    expect(within(pane).queryByRole("link")).toBeNull();
  });

  it("omits the sync record when there is none, rather than inventing an entry", async () => {
    const user = userEvent.setup();
    serve(NEVER_CHECKED);
    renderPage();
    await waitFor(() => expect(sourceIds()).toEqual(["zendesk"]));

    await user.click(sourceRow("zendesk"));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).queryByText(/Sync record/)).toBeNull();
    expect(within(pane).getByText("Never checked")).toBeInTheDocument();
  });

  it("orders the quarantine ledger count-descending, under the served total", async () => {
    renderPage();
    await screen.findByRole("table", { name: "Quarantine reasons" });
    expect(await screen.findByText("Quarantine — 3 items")).toBeInTheDocument();
    // schema-validation 2, unknown-table 1.
    await waitFor(() => expect(reasonRows()).toEqual(["schema-validation", "unknown-table"]));
  });

  it("does not claim an empty quarantine when items are parked with no reasons", async () => {
    serve({ connectors: [], quarantine_total: 3, quarantine_reasons: [] });
    renderPage();
    expect(await screen.findByText("Quarantine — 3 items")).toBeInTheDocument();
    expect(screen.getByText("No reasons recorded.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing in quarantine.")).not.toBeInTheDocument();
  });

  // --- NE-CONNECT-CSV: the ops-gated quarantined-records drill-down ---------

  it("renders the quarantined records themselves, and a click opens the row, the field and the offending value", async () => {
    const user = userEvent.setup();
    renderPage();
    const records = await screen.findByRole("table", { name: "Quarantined records" });
    // The rows are the served parks, by identity.
    const ids = within(records)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
    expect(ids).toEqual(["RNW-1007", "row-13", "orders-77"]);

    await user.click(within(records).getByText("RNW-1007"));
    const pane = await screen.findByRole("dialog");
    // The validator's own reason, verbatim — field, offending value, constraint.
    expect(
      within(pane).getByText(
        "$.contract_end: 'ASAP' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
      ),
    ).toBeInTheDocument();
    // The record as received: the sheet's own words.
    expect(within(pane).getByText("Meridian Logistics Ltd")).toBeInTheDocument();
    expect(within(pane).getByText("58000")).toBeInTheDocument();
    // No truncation note when nothing was truncated.
    expect(within(pane).queryByText(/truncated/)).toBeNull();
  });

  it("renders no records table when the drill-down is refused — categories only, nothing fabricated", async () => {
    server.use(
      http.get("*/api/v1/connect/quarantine", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    renderPage();
    await screen.findByRole("table", { name: "Quarantine reasons" });
    await waitFor(() => expect(reasonRows()).toEqual(["schema-validation", "unknown-table"]));
    expect(screen.queryByRole("table", { name: "Quarantined records" })).toBeNull();
    // Not one byte of parked content on a refused drill-down.
    expect(screen.queryByText(/RNW-1007/)).toBeNull();
    expect(screen.queryByText(/ASAP/)).toBeNull();
  });

  it("puts no repo internals on screen", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(sourceIds()).toHaveLength(3));
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/NE_[A-Z]/);
    expect(text).not.toMatch(/NE-\d/);
    expect(text).not.toMatch(/\/v1\//);
    expect(text).not.toMatch(/GET |POST /);
    expect(container.innerHTML).not.toMatch(/packages\//);
  });
});
