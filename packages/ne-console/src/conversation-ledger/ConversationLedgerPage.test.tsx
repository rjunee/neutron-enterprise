// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { User } from "oidc-client-ts";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthStub, TEST_ACCESS_TOKEN } from "../test/authStub";
import { server } from "../test/mocks/server";
import type {
  ConversationLedgerView,
  ConversationTurnView,
  ProtectedBindingView,
  ProtectedContentStatus,
} from "./types";
import { ConversationLedgerContent, ConversationLedgerPage } from "./ConversationLedgerPage";

const QUESTION = "QUESTION-BODY-SENTINEL";
const ANSWER = "ANSWER-BODY-SENTINEL";

function binding(status: ProtectedContentStatus, marker: string): ProtectedBindingView {
  return {
    status,
    protected_ref: "protected-" + marker,
    protected_hash: marker.repeat(64),
  };
}

function turn(
  sessionId: string,
  principalId: string,
  contentState: ConversationTurnView["content_state"],
  inputStatus: ProtectedContentStatus,
  terminalStatus: ProtectedContentStatus,
): ConversationTurnView {
  const common = {
    session_id: sessionId,
    agent_name: "grounded-analyst",
    department: "finance",
    seq: 7,
    principal_id: principalId,
    trace_id: "trace-" + sessionId,
    attribution: "exact" as const,
    transport_generation: "agent-transport-v1" as const,
    input: binding(inputStatus, "a"),
    terminal: binding(terminalStatus, "b"),
    created_at: "2026-08-18T17:00:00Z",
  };
  return contentState === "available"
    ? {
        ...common,
        content_state: "available",
        messages: [
          { role: "user", content: QUESTION },
          { role: "assistant", content: ANSWER },
        ],
      }
    : { ...common, content_state: contentState };
}

const LEDGER: ConversationLedgerView = {
  turns: [
    turn("session-available", "user:available", "available", "source_free", "source_free"),
    turn(
      "session-withheld",
      "user:withheld",
      "content_withheld",
      "source_bound_container",
      "source_free",
    ),
    turn(
      "session-legacy",
      "user:legacy",
      "legacy_unavailable",
      "legacy_unavailable",
      "legacy_unavailable",
    ),
  ],
  returned_turns: 3,
  truncated: true,
  retention: "until_governed_reset",
};

function ownedLedger(principal: string, body: string): ConversationLedgerView {
  const row = turn("session-" + principal, principal, "available", "source_free", "source_free");
  if (row.content_state !== "available") throw new Error("available fixture expected");
  row.messages = [
    { role: "user", content: body + "-QUESTION" },
    { role: "assistant", content: body + "-ANSWER" },
  ];
  return {
    turns: [row],
    returned_turns: 1,
    truncated: false,
    retention: "until_governed_reset",
  };
}

function user(token: string, subject: string): User {
  return { access_token: token, profile: { sub: subject } } as User;
}

function view(authUser: User = user(TEST_ACCESS_TOKEN, "alice@example.com")) {
  return (
    <AuthStub value={{ user: authUser }}>
      <MemoryRouter initialEntries={["/conversation-ledger"]}>
        <ConversationLedgerPage />
      </MemoryRouter>
    </AuthStub>
  );
}

describe("Conversation ledger", () => {
  it("renders only the server-projected available body plus exact safe transport metadata", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", ({ request }) => {
        expect(request.headers.get("authorization")).toBe(`Bearer ${TEST_ACCESS_TOKEN}`);
        return HttpResponse.json(LEDGER);
      }),
    );
    render(view());

    expect(await screen.findByRole("heading", { name: "Conversation ledger" })).toBeVisible();
    expect(screen.getByText("Retained until governed reset")).toBeVisible();
    expect(screen.getByText("Showing 3 whole turns · response capped")).toBeVisible();
    expect(screen.getByText("user:available")).toBeVisible();
    expect(screen.getByRole("link", { name: "trace-session-available" })).toHaveAttribute(
      "href",
      "/trace/trace-session-available",
    );
    const body = screen.getByLabelText("Projected messages for session session-available turn 7");
    expect(within(body).getByText(QUESTION)).toBeVisible();
    expect(within(body).getByText(ANSWER)).toBeVisible();
    // THE STORED ENUMS NEVER REACH THE SCREEN. Each one is drawn as the
    // sentence it means; the raw value stays in the tooltip for an operator.
    expect(screen.getAllByText("Protected transport, v1")).toHaveLength(3);
    expect(screen.getAllByText("Holds source material")).toHaveLength(1);
    expect(screen.getAllByText("No readable body")).toHaveLength(2);
    expect(screen.getAllByText("Couldn’t be shown")).toHaveLength(1);
    expect(screen.getByText("Shown in full")).toBeVisible();
    expect(screen.getByText("Withheld")).toBeVisible();
    expect(screen.getAllByText("No source material")).toHaveLength(3);
    for (const stored of ["agent-transport-v1", "source_bound_container", "legacy_unavailable"]) {
      expect(screen.queryByText(stored)).toBeNull();
    }
    expect(screen.getByText("Holds source material")).toHaveAttribute(
      "title",
      expect.stringContaining("source's own authorization"),
    );
    // The ref is an opaque row id, not a digest, so it stays whole. The hash is
    // a digest, so §2.7 truncates it — proved in its own describe below.
    expect(screen.getAllByText("protected-a")).toHaveLength(3);
    expect(screen.getAllByText("b".repeat(8))).toHaveLength(3);
  });

  it("keeps source-bound and legacy rows opaque even if a hostile object carries raw bytes", () => {
    const hostile = structuredClone(LEDGER) as unknown as {
      turns: Array<Record<string, unknown>>;
      returned_turns: number;
      truncated: boolean;
      retention: "until_governed_reset";
    };
    const withheld = hostile.turns[1];
    const legacy = hostile.turns[2];
    if (withheld === undefined || legacy === undefined) throw new Error("shell fixtures expected");
    withheld.messages = [{ role: "assistant", content: "SOURCE-BOUND-PAYLOAD-SENTINEL" }];
    withheld.payload = "SOURCE-BOUND-RAW-SENTINEL";
    legacy.prompt = "RETIRED-PROMPT-SENTINEL";
    legacy.output = "RETIRED-OUTPUT-SENTINEL";

    render(
      <MemoryRouter>
        <ConversationLedgerContent
          ledger={hostile as unknown as ConversationLedgerView}
          onRefresh={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/carries source material, and reading it needs/)).toBeVisible();
    expect(screen.getByText(/no protected body could be established for this turn/)).toBeVisible();
    expect(document.body.textContent).not.toContain("SOURCE-BOUND-PAYLOAD-SENTINEL");
    expect(document.body.textContent).not.toContain("SOURCE-BOUND-RAW-SENTINEL");
    expect(document.body.textContent).not.toContain("RETIRED-PROMPT-SENTINEL");
    expect(document.body.textContent).not.toContain("RETIRED-OUTPUT-SENTINEL");
  });

  it("clears a prior identity's body before the replacement read settles", async () => {
    let releaseBob!: () => void;
    const bobPending = new Promise<void>((resolve) => {
      releaseBob = resolve;
    });
    server.use(
      http.get("*/api/v1/chat/conversations", async ({ request }) => {
        if (request.headers.get("authorization") === "Bearer token-b") await bobPending;
        return HttpResponse.json(
          request.headers.get("authorization") === "Bearer token-b"
            ? ownedLedger("bob", "BOB")
            : ownedLedger("alice", "ALICE-PRIVATE"),
        );
      }),
    );
    const rendered = render(view(user("token-a", "alice")));
    expect(await screen.findByText("ALICE-PRIVATE-ANSWER")).toBeVisible();

    rendered.rerender(view(user("token-b", "bob")));
    expect(screen.queryByText("ALICE-PRIVATE-ANSWER")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading conversation turns");

    releaseBob();
    expect(await screen.findByText("BOB-ANSWER")).toBeVisible();
    expect(document.body.textContent).not.toContain("ALICE-PRIVATE");
  });

  it("clears at refresh start and prevents an older response restoring the ledger", async () => {
    let aliceReads = 0;
    let releaseStale!: (response: Response) => void;
    server.use(
      http.get("*/api/v1/chat/conversations", ({ request }) => {
        if (request.headers.get("authorization") === "Bearer token-a") {
          aliceReads += 1;
          if (aliceReads === 2) {
            return new Promise<Response>((resolve) => {
              releaseStale = resolve;
            });
          }
          return HttpResponse.json(ownedLedger("alice", "ALICE-PRIVATE"));
        }
        return HttpResponse.json(ownedLedger("bob", "BOB"));
      }),
    );
    const rendered = render(view(user("token-a", "alice")));
    expect(await screen.findByText("ALICE-PRIVATE-ANSWER")).toBeVisible();

    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.queryByText("ALICE-PRIVATE-ANSWER")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading conversation turns");

    rendered.rerender(view(user("token-b", "bob")));
    expect(await screen.findByText("BOB-ANSWER")).toBeVisible();
    await act(async () => {
      releaseStale(HttpResponse.json(ownedLedger("alice", "STALE-ALICE")));
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(document.body.textContent).not.toContain("STALE-ALICE");
    expect(screen.getByText("BOB-ANSWER")).toBeVisible();
  });

  it("renders a 403 as a refusal and retries a transient read failure", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    render(view());
    expect(await screen.findByText(/doesn.t include conversation disclosures/i)).toBeVisible();

    let reads = 0;
    server.use(
      http.get("*/api/v1/chat/conversations", () => {
        reads += 1;
        return reads === 1
          ? HttpResponse.json({ detail: "temporarily unavailable" }, { status: 503 })
          : HttpResponse.json(LEDGER);
      }),
    );
    render(view());
    await userEvent.setup().click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("user:available")).toBeVisible());
  });

  it("shows neutral cutover unavailability for a legacy envelope without revealing it", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({
          turns: [{ messages: [{ content: "LEGACY-PRIVATE-SENTINEL" }] }],
          total_turns: 1,
          returned_turns: 1,
          truncated: false,
          retention: "until_governed_reset",
          response_byte_limit: 1_000_000,
        }),
      ),
    );
    render(view());
    expect(await screen.findByText(/Nothing to show here yet/)).toBeVisible();
    expect(document.body.textContent).not.toContain("LEGACY-PRIVATE-SENTINEL");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});

describe("what this page says when it has nothing to show", () => {
  it("tells the reader what the page WILL list and why it cannot list it yet", async () => {
    // THE CONFUSION THIS REPLACES: this is the only rail entry with the word
    // "ledger" in it, so a reader hunting the monitored ANSWER history lands
    // here, and the old line ("Protected transport cutover not completed or
    // unavailable.") named a migration step rather than answering them. They
    // read the silence as "the agents answered nothing".
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({
          turns: [],
          total_turns: 0,
          returned_turns: 0,
          truncated: false,
          retention: "until_governed_reset",
          response_byte_limit: 1_000_000,
        }),
      ),
    );
    render(view());

    const said = await screen.findByText(/Nothing to show here yet/);
    expect(said).toHaveTextContent("each question put to an agent and the answer it gave");
    expect(said).toHaveTextContent("has not switched over yet");
    // ...and it never implies it counted anything.
    expect(said).toHaveTextContent("not a history that is empty");
    expect(said).not.toHaveTextContent("cutover");
    // The other ledger stays one click away, which is what the reader wanted.
    expect(screen.getByRole("link", { name: "Monitored answer history" })).toBeInTheDocument();
  });

  it("says an empty protected ledger is empty, without explaining the machinery", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({
          turns: [],
          returned_turns: 0,
          truncated: false,
          retention: "until_governed_reset",
        }),
      ),
    );
    render(view());

    expect(
      await screen.findByText(
        "No conversation turns retained yet — they appear here as agents are used.",
      ),
    ).toBeVisible();
  });
});

describe("a turn whose protected body could not be established", () => {
  /**
   * `legacy_unavailable` HAS FOUR CAUSES AND THE WIRE NAMES NONE OF THEM.
   *
   * The server projects this status from the legacy migration's stored
   * classification AND from three failures to open the protected row — the row
   * is missing, its envelope will not open, or another principal owns it — all
   * caught in one `except` (`ne-api`, `routers/chat.py:651`, whose own comment
   * calls a "missing/tampered/foreign protected row … one unavailable content
   * item"), with a parameterised server test seeding each damage and asserting
   * this same status.
   *
   * The copy this pins therefore has to hold in both directions at once:
   * asserting the turn predates protected storage reports a tampered or
   * foreign-owned row as routine housekeeping, while naming tampering cries
   * wolf on the common case. So the assertions below are two-sided — both
   * readings must be offered, and neither may be stated as the fact.
   */
  it("offers both readings of the status and asserts neither as the cause", async () => {
    server.use(http.get("*/api/v1/chat/conversations", () => HttpResponse.json(LEDGER)));
    render(view());

    // The row is waited for by a field the copy cannot move, so what follows
    // fails on the copy rather than on never reaching it.
    const turn = (await screen.findByText("user:legacy")).closest("article") as HTMLElement;

    // Neither cause is stated as the fact — in labels, body copy or tooltips.
    const rendered = [
      document.body.textContent ?? "",
      ...Array.from(document.querySelectorAll("[title]"), (el) => el.getAttribute("title") ?? ""),
    ].join(" ");
    expect(rendered).not.toMatch(
      /no body was (ever )?kept|predates protected storage|Not retained/,
    );
    expect(rendered).not.toMatch(/tamper|corrupt|altered|missing|another principal/i);

    // Both readings are offered instead.
    const said = within(turn).getByText(/no protected body could be established for this turn/);
    expect(said).toHaveTextContent("written before protected storage");
    expect(said).toHaveTextContent("protected copy can no longer be read");
    expect(said).toHaveTextContent("cannot tell the two apart");

    const halves = within(turn).getAllByText("No readable body");
    expect(halves).toHaveLength(2);
    for (const half of halves) {
      expect(half).toHaveAttribute(
        "title",
        expect.stringContaining(
          "does not say whether it was written before protected storage or its protected copy can no longer be read",
        ),
      );
    }
    expect(within(turn).getByText("Couldn’t be shown")).toHaveAttribute(
      "title",
      expect.stringContaining("does not tell the two apart"),
    );

    // Why "nothing was ever kept" was the wrong reading: the row it would have
    // been said about renders a live protected reference and a real 64-hex
    // hash. The hash is now drawn through HashChip, so the served digest is in
    // the chip's `title` rather than painted whole — still served, still this
    // row's, which is the whole point being made here.
    expect(within(turn).getByText("protected-a")).toBeVisible();
    expect(within(turn).getByTitle("b".repeat(64))).toBeVisible();
  });
});

describe("a transport generation this build has never seen", () => {
  /** The ledger fixture exactly as the SERVER sends it, with one field replaced. */
  function serveLedgerWire(transportGeneration: string) {
    const wire = JSON.parse(JSON.stringify(LEDGER)) as { turns: Record<string, unknown>[] };
    for (const row of wire.turns) row.transport_generation = transportGeneration;
    server.use(http.get("*/api/v1/chat/conversations", () => HttpResponse.json(wire)));
  }

  it("reads the ledger when the served generation is the one this build understands", async () => {
    // The control for the test below: what that one asserts is driven by the
    // value on the wire, not by the harness. The test this pair replaced built
    // its input by cloning an already-parsed view and mutating it, so it never
    // reached the client, the parser or the server and passed regardless of all
    // three.
    serveLedgerWire("agent-transport-v1");
    render(view());

    expect(await screen.findByText(QUESTION)).toBeVisible();
    expect(screen.getAllByText("Protected transport, v1")).toHaveLength(3);
  });

  it("fails the whole read on a skewed generation — no half-understood row is drawn", async () => {
    // This screen used to carry a "Not recognised" arm for exactly this case,
    // with a comment promising an operator chasing a version skew could read
    // the raw value off its tooltip. Neither was reachable: `parseTurn` rejects
    // any `transport_generation` but `agent-transport-v1` through
    // `malformed()`, which throws, so the read fails before a turn is drawn.
    // That is the right outcome for a protected ledger — `content_state` is
    // what decides whether a body may be shown — and this pins it as the
    // intended outcome rather than an accident.
    serveLedgerWire("agent-transport-v9");
    render(view());

    const failed = await screen.findByRole("alert");
    expect(failed).toHaveTextContent("Couldn\u2019t load conversation turns.");
    expect(within(failed).getByRole("button", { name: "Retry" })).toBeInTheDocument();

    // No invented label, no raw enum, and no body out of a response this build
    // could not read.
    expect(screen.queryByText("Not recognised")).toBeNull();
    expect(screen.queryByText(/agent-transport-v9/)).toBeNull();
    expect(screen.queryByText(QUESTION)).toBeNull();
    expect(screen.queryByText(ANSWER)).toBeNull();
  });

  // The `MAP[key] ?? fallback` hole: for an object literal,
  // `MAP["constructor"]` is `Object`'s constructor FUNCTION and
  // `MAP["__proto__"]` is `Object.prototype`, both non-nullish, so `??` never
  // fires and a lookup typed to return a term returns neither.
  //
  // This page no longer indexes anything with a wire string. Its three
  // vocabularies are keyed by the view type's own literal unions, and the only
  // values that reach them have been through a parser that tests each one
  // against a CLOSED LIST rather than indexing with it — `"__proto__"` and
  // `"constructor"` are not on any of those lists. So the response is refused
  // whole and no prototype member can be drawn as a word. Checked from the
  // wire, because a lookup checked in isolation proves nothing about what the
  // page is actually handed.
  //
  // ONE POISONED FIELD PER CASE, BECAUSE TWO IN ONE ROW PROVE ONLY THE FIRST
  // CHECK. The version this replaces set `transport_generation = "__proto__"`
  // AND `input.status = "constructor"` on a single row; `parseTurn` refuses the
  // generation before `parseBinding` is ever called, so deleting the closed
  // list of statuses — the half the title is about — left the test green.
  //
  // The status case is poisoned on the CONTENT-WITHHELD row deliberately: it is
  // the only arm that would still admit the row with the closed list gone (the
  // `available` arm demands `source_free` from both halves, the
  // `legacy_unavailable` arm demands `legacy_unavailable` from both). With the
  // check deleted that response parses, the turn is drawn, and the assertions
  // below go red instead of passing on someone else's refusal.
  const POISON: ReadonlyArray<{ name: string; apply: (turns: Record<string, unknown>[]) => void }> =
    [
      {
        name: "a transport generation named after a prototype member",
        apply: (turns) => {
          turns[0]!.transport_generation = "__proto__";
        },
      },
      {
        name: "a binding status named after a prototype member",
        apply: (turns) => {
          (turns[1]!.terminal as Record<string, unknown>).status = "constructor";
        },
      },
    ];

  it.each(POISON)("refuses the whole response over $name", async ({ apply }) => {
    const wire = JSON.parse(JSON.stringify(LEDGER)) as { turns: Record<string, unknown>[] };
    apply(wire.turns);
    server.use(http.get("*/api/v1/chat/conversations", () => HttpResponse.json(wire)));
    render(view());

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load conversation turns.");
    // Whole-response refusal: not one row of it is drawn, including the rows
    // the poisoned field never touched.
    expect(screen.queryByText("user:withheld")).toBeNull();
    expect(screen.queryByText("user:available")).toBeNull();
    expect(screen.queryByText(QUESTION)).toBeNull();
    // ...and no prototype member reached the screen as a word.
    expect(document.body.textContent).not.toMatch(/native code|\[object Object\]|function Object/);
  });
});

/**
 * §2.7 ON THE ONE SCREEN THAT DRAWS TWO DIGESTS PER ROW.
 *
 * `HashChip.tsx`'s header quotes the rule — "Hashes truncate to 8 chars + copy
 * affordance; full value in `title` and clipboard" — and records the shipped
 * console painting the full 64 into a 280px column, "where it overflowed and
 * painted across page content on four routes". This page painted two whole
 * digests per turn, side by side in a two-column grid, and the fixture below
 * draws three turns: six 64-character strings on one screen.
 *
 * The assertions are two-sided on purpose, because truncation has a failure
 * mode in each direction. Showing 64 characters is the overflow the rule
 * exists to stop; showing only 8 and copying only those 8 would LOSE the
 * served value, which is the worse failure — a digest an operator cannot
 * reproduce is not evidence of anything. So: nothing 64-wide is painted, AND
 * the served 64 is still reachable in `title` and out of the clipboard.
 */
describe("the protected digests obey the hash rule", () => {
  const INPUT_HASH = "a".repeat(64);
  const TERMINAL_HASH = "b".repeat(64);

  // A plain spy, per HashPlate.test.tsx: user-event's setup() installs its own
  // clipboard stub, which would make this assert the stub instead of the chip.
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    server.use(http.get("*/api/v1/chat/conversations", () => HttpResponse.json(LEDGER)));
  });

  it("paints eight characters of each digest and no 64-character string anywhere", async () => {
    render(view());
    // Waited for by a field this change cannot move, so a failure below is the
    // digest rendering rather than the row never arriving.
    expect(await screen.findByText("user:available")).toBeVisible();

    // Three turns, two bindings each: six digests, none of them painted whole.
    expect(screen.queryAllByText(INPUT_HASH)).toHaveLength(0);
    expect(screen.queryAllByText(TERMINAL_HASH)).toHaveLength(0);
    expect(screen.getAllByText("a".repeat(8))).toHaveLength(3);
    expect(screen.getAllByText("b".repeat(8))).toHaveLength(3);

    // Nothing 64 characters wide survives anywhere in the rendered text — the
    // check the two queries above would miss if the digest were drawn with the
    // truncation glued on ("aaaa…" + the rest) rather than replaced.
    for (const node of document.querySelectorAll("dd")) {
      expect((node.textContent ?? "").length).toBeLessThan(64);
    }
  });

  it("keeps the SERVED digest in title and copies all 64, not the eight on screen", async () => {
    render(view());
    expect(await screen.findByText("user:available")).toBeVisible();

    // The served value is never lost: it is the chip's title, verbatim.
    expect(screen.getAllByTitle(INPUT_HASH)).toHaveLength(3);
    expect(screen.getAllByTitle(TERMINAL_HASH)).toHaveLength(3);

    // Each copy control says WHICH digest — the two halves of a turn are
    // otherwise indistinguishable to a screen reader.
    const inputCopies = screen.getAllByRole("button", {
      name: "Copy input protected hash " + "a".repeat(8),
    });
    expect(inputCopies).toHaveLength(3);
    expect(
      screen.getAllByRole("button", { name: "Copy terminal protected hash " + "b".repeat(8) }),
    ).toHaveLength(3);

    // And the clipboard gets the whole digest, not the abbreviation.
    fireEvent.click(inputCopies[0]!);
    expect(writeText).toHaveBeenCalledWith(INPUT_HASH);
    expect(writeText).not.toHaveBeenCalledWith("a".repeat(8));
  });
});
