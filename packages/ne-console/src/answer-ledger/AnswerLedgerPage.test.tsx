// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import type { AuthContextProps } from "react-oidc-context";
import { describe, expect, it } from "vitest";
import { AuthStub, TEST_ACCESS_TOKEN } from "../test/authStub";
import { server } from "../test/mocks/server";
import { AnswerLedgerPage } from "./AnswerLedgerPage";
import type { AnswerEventView, AnswerLedgerView } from "./types";

/** Narrows one served event to the disclosed branch, failing the test rather
 * than casting when the ledger served a shell. */
function disclosedEvent(ledger: AnswerLedgerView, index: number): AnswerEventView {
  const event = ledger.events[index];
  expect(event).toHaveProperty("answer");
  return event as AnswerEventView;
}

const BASELINE: AnswerLedgerView = {
  question: {
    question_id: "question-1",
    owner_id: "user:ada",
    question: "What changed in operating performance?",
    created_at: "2026-08-18T17:00:00Z",
    reask_schedule: null,
  },
  events: [
    {
      disclosure_status: "available",
      event_id: "event-1",
      occurred_at: "2026-08-18T17:01:00Z",
      answer: "Margin was 14 percent.",
      corpus_stamp: [
        {
          chunk_id: "chunk-1",
          document_id: "quarterly-review",
          content_hash: "a".repeat(64),
          document_version: 1,
          document_href: "/library?tab=documents&document=quarterly-review&version=1",
          document_title: "Quarterly Operating Review",
        },
      ],
      config_hash: "b".repeat(64),
      served_model_id: "private-model-a",
      trace_id: "trace-1",
      trace_href: "/trace/trace-1",
      classification: null,
      root_cause: null,
    },
  ],
};

const CHANGED: AnswerLedgerView = {
  ...BASELINE,
  events: [
    ...BASELINE.events,
    {
      disclosure_status: "available",
      event_id: "event-2",
      occurred_at: "2026-08-18T18:01:00Z",
      answer: "Margin improved to 18 percent.",
      corpus_stamp: [
        {
          chunk_id: "chunk-2",
          document_id: "quarterly-review",
          content_hash: "c".repeat(64),
          document_version: 2,
          document_href: "/library?tab=documents&document=quarterly-review&version=2",
          document_title: "Quarterly Operating Review",
        },
      ],
      config_hash: "d".repeat(64),
      served_model_id: "private-model-b",
      trace_id: "trace-2",
      trace_href: "/trace/trace-2",
      classification: "CHANGED-corpus",
      root_cause: {
        answer_changed: true,
        corpus_changed: true,
        config_changed: true,
        corpus_diff_hrefs: ["/library?tab=documents&document=quarterly-review&version=2"],
        config_diff_href: `/posture?from=${"b".repeat(64)}&to=${"d".repeat(64)}`,
      },
    },
  ],
};

const OLD_LIVE_OWNER_ID = "dcf23920-be80-4f68-975c-2c4aa365c2f6";
const OLD_LIVE_TRACE_ID = "2cec17e3e194aeb98be91cd53c20b637e5bbddc20b63";
const OLD_LIVE_DOCUMENT_ID = "0785d8a62620b66dc4c8fecaedce1673e9e02bb093cffcd84df6eddb8fb8a3a6";
const OLD_LIVE_CHUNK_ID =
  "0785d8a62620b66dc4c8fecaedce1673e9e02bb093cffcd84df6eddb8fb8a3a6:chunk:0001";
const OLD_LIVE_ANSWER = [
  "Based on the served evidence, provisional credit must be issued according to these timelines:",
  "",
  "**Standard tier:** within **10 business days** after customer notice.",
  "",
  "**Expedited tier:** for disputes of **at least $500**, within **5 business days**.",
].join("\n");

const OLD_LIVE_SHAPED: AnswerLedgerView = {
  question: {
    question_id: "question-live-proof",
    owner_id: OLD_LIVE_OWNER_ID,
    question: "When must we issue provisional credit on a debit card dispute?",
    created_at: "2026-08-18T21:12:20Z",
    reask_schedule: null,
  },
  events: [
    {
      disclosure_status: "available",
      event_id: "event-live-proof",
      occurred_at: "2026-08-18T22:14:23Z",
      answer: OLD_LIVE_ANSWER,
      corpus_stamp: [
        {
          chunk_id: OLD_LIVE_CHUNK_ID,
          document_id: OLD_LIVE_DOCUMENT_ID,
          content_hash: "0d91e689b8297dcd23e3484117104170d373f9549809236166232886ab3f37dd",
          document_version: 4,
          document_href: `/library?tab=documents&document=${OLD_LIVE_DOCUMENT_ID}&version=4`,
          document_title: "Debit Card Dispute Handling SOP",
        },
      ],
      config_hash: "0d91e689b8297dcd23e3484117104170d373f9549809236166232886ab3f37dd",
      served_model_id: "frontier-default",
      trace_id: OLD_LIVE_TRACE_ID,
      trace_href: `/trace/${OLD_LIVE_TRACE_ID}`,
      classification: "CHANGED-unexplained",
      root_cause: {
        answer_changed: true,
        corpus_changed: false,
        config_changed: false,
        corpus_diff_hrefs: [],
        config_diff_href: null,
      },
    },
  ],
};

function setup(profileName?: string) {
  const user = userEvent.setup();
  const authValue = profileName
    ? {
        user: {
          access_token: TEST_ACCESS_TOKEN,
          profile: {
            sub: OLD_LIVE_OWNER_ID,
            name: profileName,
            preferred_username: "ops",
          },
        } as AuthContextProps["user"],
      }
    : undefined;
  render(
    <AuthStub value={authValue}>
      <MemoryRouter initialEntries={["/answer-ledger"]}>
        <AnswerLedgerPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

describe("Answer Ledger", () => {
  it("uses a single readable column with a compact horizontal version selector", () => {
    const css = readFileSync(resolve(__dirname, "answer-ledger.css"), "utf8");
    expect(css).toMatch(
      /\.ne-answer-ledger__workspace\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;/s,
    );
    expect(css).toMatch(/\.ne-answer-ledger__detail\s*\{[^}]*width:\s*min\(100%, 78ch\);/s);
    expect(css).toMatch(
      /\.ne-answer-ledger__timeline\s*\{[^}]*display:\s*flex;[^}]*overflow-x:\s*auto;/s,
    );
    expect(css).toMatch(
      /@media \(max-width: 900px\)[\s\S]*\.ne-answer-ledger__question\s*\{[^}]*flex-direction:\s*column;/,
    );
  });

  it("keeps old-live answer and immutable ids exact without making raw syntax or ids primary", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(OLD_LIVE_SHAPED)));
    setup("Ops Pilot");

    expect(await screen.findByRole("heading", { name: "Monitored answer" })).toBeVisible();
    expect(screen.getByRole("heading", { name: OLD_LIVE_SHAPED.question?.question })).toBeVisible();
    expect(screen.getByText("Ops Pilot")).toBeVisible();

    const ownerDisclosure = screen.getByText("Owner ID").closest("details");
    expect(ownerDisclosure).not.toHaveAttribute("open");
    expect(within(ownerDisclosure as HTMLElement).getByText(OLD_LIVE_OWNER_ID)).not.toBeVisible();

    const answer = screen.getByRole("region", { name: "Answer" });
    expect(within(answer).getByText("Standard tier:", { selector: "strong" })).toBeVisible();
    expect(within(answer).getByText("10 business days", { selector: "strong" })).toBeVisible();
    const verbatim = within(answer).getByText("Verbatim answer").closest("details");
    expect(verbatim).not.toHaveAttribute("open");
    const rawAnswer = verbatim?.querySelector("pre");
    expect(rawAnswer?.textContent).toBe(OLD_LIVE_ANSWER);
    expect(rawAnswer).not.toBeVisible();

    expect(screen.getByRole("link", { name: "Open trace" })).toHaveAttribute(
      "href",
      `/trace/${OLD_LIVE_TRACE_ID}`,
    );
    const traceDisclosure = screen.getByText("Trace ID").closest("details");
    expect(traceDisclosure).not.toHaveAttribute("open");
    expect(within(traceDisclosure as HTMLElement).getByText(OLD_LIVE_TRACE_ID)).not.toBeVisible();

    expect(
      screen.getByRole("link", { name: "Debit Card Dispute Handling SOP · Version 4" }),
    ).toHaveAttribute("href", `/library?tab=documents&document=${OLD_LIVE_DOCUMENT_ID}&version=4`);
    const chunkDisclosure = screen.getByText("Technical details").closest("details");
    expect(chunkDisclosure).not.toHaveAttribute("open");
    expect(
      within(chunkDisclosure as HTMLElement).getByText(OLD_LIVE_DOCUMENT_ID),
    ).not.toBeVisible();
    expect(within(chunkDisclosure as HTMLElement).getByText(OLD_LIVE_CHUNK_ID)).not.toBeVisible();
    // The digest is chipped rather than painted at full 64 (§2.7); the exact
    // value stays on the trigger, so nothing is lost.
    expect(
      (chunkDisclosure as HTMLElement).querySelector(".ne-hashplate__trigger"),
    ).toHaveAttribute(
      "aria-label",
      "hash 0d91e689b8297dcd23e3484117104170d373f9549809236166232886ab3f37dd",
    );
  });

  it("chips the served content hash and leaves the two identifiers whole", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(OLD_LIVE_SHAPED)));
    setup("Ops Pilot");
    await screen.findByRole("heading", { name: "Monitored answer" });
    const chunkDisclosure = screen.getByText("Technical details").closest("details") as HTMLElement;
    const digest = "0d91e689b8297dcd23e3484117104170d373f9549809236166232886ab3f37dd";

    // A digest — §2.7 governs it. Nowhere in the row is it painted whole.
    expect(within(chunkDisclosure).queryByText(digest)).toBeNull();
    expect(chunkDisclosure.querySelector(".ne-hashplate__trigger")).toHaveAttribute(
      "aria-label",
      `hash ${digest}`,
    );

    // NOT digests — the strings a reader quotes back to the source system —
    // so every character survives, and neither carries a `title` that only
    // ever repeated the text beside it.
    expect(within(chunkDisclosure).getByText(OLD_LIVE_DOCUMENT_ID)).not.toHaveAttribute("title");
    expect(within(chunkDisclosure).getByText(OLD_LIVE_CHUNK_ID)).not.toHaveAttribute("title");
  });

  it("renders the owner, verbatim baseline, and exact served provenance", async () => {
    server.use(
      http.get("*/api/v1/answer-ledger", ({ request }) => {
        expect(request.headers.get("authorization")).toBe(`Bearer ${TEST_ACCESS_TOKEN}`);
        return HttpResponse.json(BASELINE);
      }),
    );
    setup();

    expect(await screen.findByRole("heading", { name: BASELINE.question?.question })).toBeVisible();
    expect(screen.getByText("Current principal")).toBeVisible();
    const ownerDisclosure = screen.getByText("Owner ID").closest("details");
    expect(within(ownerDisclosure as HTMLElement).getByText("user:ada")).not.toBeVisible();
    expect(
      screen.getByText("Margin was 14 percent.", {
        selector: ".ne-answer-ledger__readable-answer",
      }),
    ).toBeVisible();
    expect(screen.getAllByText("private-model-a")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Open trace" })).toHaveAttribute(
      "href",
      "/trace/trace-1",
    );
    expect(
      screen.getByRole("link", { name: "Quarterly Operating Review · Version 1" }),
    ).toHaveAttribute("href", "/library?tab=documents&document=quarterly-review&version=1");
  });

  it("re-asks manually and renders the server-classified three-column cause pane", async () => {
    server.use(
      http.get("*/api/v1/answer-ledger", () => HttpResponse.json(BASELINE)),
      http.post("*/api/v1/answer-ledger/reask", () => HttpResponse.json(CHANGED)),
    );
    const user = setup();
    await user.click(await screen.findByRole("button", { name: "Re-ask now" }));

    expect(
      await screen.findByText("Margin improved to 18 percent.", {
        selector: ".ne-answer-ledger__readable-answer",
      }),
    ).toBeVisible();
    const cause = screen.getByRole("region", { name: "Root cause" });
    expect(within(cause).getAllByText("Changed")).toHaveLength(3);
    expect(within(cause).getByText("CHANGED-corpus")).toBeVisible();
    expect(within(cause).getByRole("link", { name: "Document diff 1" })).toHaveAttribute(
      "href",
      "/library?tab=documents&document=quarterly-review&version=2",
    );
    expect(
      within(cause).getByRole("link", { name: "Resolved configuration history" }),
    ).toHaveAttribute("href", disclosedEvent(CHANGED, 1).root_cause?.config_diff_href);
  });

  it("pins the one question before any re-ask is available", async () => {
    const empty: AnswerLedgerView = { question: null, events: [] };
    let posted: unknown;
    server.use(
      http.get("*/api/v1/answer-ledger", () => HttpResponse.json(empty)),
      http.post("*/api/v1/answer-ledger/pin", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ...BASELINE, events: [] });
      }),
    );
    const user = setup();
    await user.type(await screen.findByLabelText("Pin one business question"), "What changed?");
    await user.click(screen.getByRole("button", { name: "Pin question" }));

    expect(
      await screen.findByRole("heading", { name: "What changed in operating performance?" }),
    ).toBeVisible();
    expect(posted).toEqual({ question: "What changed?" });
    expect(screen.getByRole("button", { name: "Re-ask now" })).toBeEnabled();
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — a ledger event whose source access changed
// ===========================================================================

describe("015A0-ENFORCEMENT — an undisclosed answer event", () => {
  const WITHHELD: AnswerLedgerView = {
    ...BASELINE,
    events: [
      ...BASELINE.events,
      {
        disclosure_status: "content_withheld",
        event_id: "event-withheld",
        occurred_at: "2026-08-18T19:01:00Z",
      },
    ],
  };

  it("keeps the owned event and its time, and serves none of the answer's bytes", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(WITHHELD)));
    setup();
    expect(await screen.findByText("Answer withheld — access changed.")).toBeVisible();
    // The immutable record that a re-ask happened survives.
    expect(screen.getByRole("list", { name: "Answer timeline" })).toBeVisible();
    // Nothing the sources produced does — neither in the open detail nor in
    // the withheld event's own timeline row, which names no served model.
    expect(screen.queryByText("Margin improved to 18 percent.")).toBeNull();
    expect(screen.queryByRole("region", { name: "Root cause" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Open trace" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Served chunks" })).toBeNull();
    const rows = within(screen.getByRole("list", { name: "Answer timeline" })).getAllByRole(
      "button",
    );
    expect(rows.at(-1)).toHaveTextContent("WITHHELD");
    expect(rows.at(-1)).not.toHaveTextContent("private-model-a");
  });

  it("names the served model's non-disclosure on the withheld row, never a blank", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(WITHHELD)));
    setup();
    await screen.findByText("Answer withheld — access changed.");
    const rows = within(screen.getByRole("list", { name: "Answer timeline" })).getAllByRole(
      "button",
    );
    // One null, one treatment: the row states the withholding on BOTH lines
    // rather than naming it once and rendering an empty span underneath.
    const withheld = rows.at(-1) as HTMLElement;
    expect(withheld).toHaveTextContent("WITHHELD");
    expect(withheld).toHaveTextContent("Served model withheld");
    expect(withheld.querySelector("span")?.textContent).not.toBe("");
    // The words belong to the withheld branch alone — a disclosed row still
    // names the model that actually served it.
    expect(rows[0]).toHaveTextContent("private-model-a");
  });

  it("offers no verbatim disclosure to copy from on a withheld event", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(WITHHELD)));
    setup();
    await screen.findByText("Answer withheld — access changed.");
    expect(screen.queryByText("Verbatim answer")).toBeNull();
  });

  it("withholds an event that states NO disclosure status, complete though it looks", async () => {
    const unstated: AnswerLedgerView = {
      ...BASELINE,
      events: BASELINE.events.map((event) => ({
        ...event,
        disclosure_status: undefined,
      })) as AnswerLedgerView["events"],
    };
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(unstated)));
    setup();
    expect(
      await screen.findByText(
        "Answer withheld — the server stated no disclosure decision for this event.",
      ),
    ).toBeVisible();
    expect(screen.queryByText("Margin was 14 percent.")).toBeNull();
  });

  it("still renders a disclosed event beside a withheld one", async () => {
    server.use(http.get("*/api/v1/answer-ledger", () => HttpResponse.json(BASELINE)));
    setup();
    expect(
      await screen.findByText("Margin was 14 percent.", {
        selector: ".ne-answer-ledger__readable-answer",
      }),
    ).toBeVisible();
  });
});
