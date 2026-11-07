// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — ASK, end to end, against a REAL SSE body over MSW. Nothing is mocked past
// the fetch/parser seam: token deltas render progressively, a citations frame
// renders chips and lines, a tool frame lands in the thread, and an error frame
// renders one of the five stated lines.
//
// The screen-level rules held here:
//   * the empty thread says the spec's sentence, and nothing else;
//   * a 403 on the session create renders the SERVER's refusal — the console
//     never pre-empts it, so the request is actually made;
//   * a 422 renders the department line, which is a different fact;
//   * opening a Run receipt fills the docked pane and NEVER navigates;
//   * NO REPO INTERNAL is on screen: no route, no verb, no file path.
//
// The thread's history list is the workspace sidebar's now, not this screen's,
// and is covered by the sidebar's own tests.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { AuthContext } from "react-oidc-context";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { server } from "../test/mocks/server";
import { RUN_RECEIPT_LABEL } from "../agents/RunReceiptPane";
import { AuthStub, makeAuthValue } from "../test/authStub";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { ChatPage } from "./ChatPage";

type ThreadProps = Partial<ComponentProps<typeof ChatPage>>;

const CITATION = {
  chunk_id: "chunk-alpha",
  document_id: "doc-alpha",
  source_system: "sharepoint",
  source_ref: "ref/doc-alpha",
  source_url: "https://sharepoint.example.com/doc-alpha",
};

function sse(frames: Array<{ event: string; data: unknown }>, gapMs = 0): ReadableStream {
  const encoder = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      for (const frame of frames) {
        controller.enqueue(
          encoder.encode(`event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`),
        );
        if (gapMs > 0) await delay(gapMs);
      }
      controller.close();
    },
  });
}

function stream(frames: Array<{ event: string; data: unknown }>, gapMs = 0) {
  server.use(
    http.post(
      "*/api/v1/chat/sessions/:id/messages",
      () =>
        new HttpResponse(sse(frames, gapMs), {
          headers: { "Content-Type": "text/event-stream" },
        }),
    ),
  );
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

/** The thread as the workspace mounts it. AuthStub publishes a fresh auth
 * object per render and the console keys client identity on it, so RE-RENDERING
 * this view is itself an identity change — which is exactly what the
 * owner-change tests below want, and exactly what a prop-change test must not
 * get. `stableView` is the same tree with one retained owner. */
function askView(props: ThreadProps = {}) {
  return (
    <MemoryRouter initialEntries={["/agents/agt-ask"]}>
      <AuthStub>
        <PrincipalProvider>
          <ChatPage {...props} />
        </PrincipalProvider>
      </AuthStub>
    </MemoryRouter>
  );
}

const RETAINED_AUTH = makeAuthValue();

function stableView(props: ThreadProps = {}) {
  return (
    <MemoryRouter initialEntries={["/agents/agt-ask"]}>
      <AuthContext.Provider value={RETAINED_AUTH}>
        <PrincipalProvider>
          <ChatPage {...props} />
        </PrincipalProvider>
      </AuthContext.Provider>
    </MemoryRouter>
  );
}

function renderAsk(props: ThreadProps = {}) {
  return render(askView(props));
}

/** The Run the acceptance above named, as the poll serves it back. */
function acceptedRunDetail() {
  return {
    run_id: "run-canonical",
    conversation_id: "conversation-accepted",
    agent_id: "agt-canonical",
    agent_version: 1,
    sequence: 1,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-canonical",
    retry_ordinal: 0,
    display_label: "Run 1 · Agentic · 12:00",
    strategy: "agentic",
    status: "completed",
    output_verdict: "unavailable",
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    route_position: "unavailable",
    fallback_from: null,
    output_preview: "The Run answered.",
    output_state: "available",
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-27T12:00:00Z",
    updated_at: "2026-08-27T12:00:04Z",
    terminal_at: "2026-08-27T12:00:04Z",
    approval_id: null,
    failure_reason: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "unavailable",
    claims_truncated: false,
  };
}

function defaultAskCatalog() {
  return {
    surface: "ask",
    task: "reason",
    effective_default: "private-default",
    choices: [
      {
        model: "private-default",
        provider: "vllm",
        lane: "private-vllm",
        evidence: "private-serving-probe",
        observed_at: "2026-08-18T18:00:00Z",
      },
    ],
  };
}

it("keeps the thread on one bounded column, with the dock over it when narrow", () => {
  // The thread's layout moved with the screen: it is a region of the Agent
  // workspace now, so the rules that bound it live beside that workspace.
  const css = readFileSync(resolve(__dirname, "../agents/agents.css"), "utf8");
  const main = css.slice(css.indexOf(".ag-main {"), css.indexOf(".ag-main--pane {"));
  expect(main).toContain("grid-template-columns: minmax(0, 1fr)");
  expect(main).toContain("min-height: 0");
  const thread = css.slice(css.indexOf(".ag-thread {"), css.indexOf(".ag-thread .tr-thread"));
  expect(thread).toContain("min-width: 0");
  // Narrow: the docked pane OVERLAYS rather than splitting the column, so the
  // conversation never drops below its prose measure to make room for it.
  const narrow = css.slice(css.indexOf("@media (max-width: 1280px)"));
  expect(narrow).toContain("grid-template-columns: minmax(0, 1fr)");
  expect(narrow).toContain("position: absolute");
});

/** One step row of the work band, found by the tool name it carries. The band's
 * summary line names the same tools, so a bare text query would match twice —
 * which is the précis doing its job, not an ambiguity to route around. */
function stepRow(name: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>(".tr-step")).find(
      (row) => row.querySelector(".tr-step__name")?.textContent === name,
    ) ?? null
  );
}

/**
 * THE GROUNDING SWITCH — the same guarantees, a different control.
 *
 * The two-button segment ("its ugly as shit… so much space" — owner) is now
 * ONE `role="switch"` with a stable accessible name ("Grounding"), state on
 * `aria-checked`, and the CURRENT position said in a visible word beside the
 * track — "Grounded" on, "Ungrounded" off — so the off state stays named and
 * nobody has to flip the control to learn what its resting position means.
 */
function groundingSwitch(): HTMLElement | null {
  return screen.queryByRole("switch", { name: "Grounding" });
}

/** The switch, required on screen. */
function groundingControl(): HTMLElement {
  return screen.getByRole("switch", { name: "Grounding" });
}

/** Wait for the switch and assert which state it is in — checked is Grounded,
 * and the word beside the track names the state either way. */
async function findGrounding(state: "Grounded" | "Ungrounded"): Promise<HTMLElement> {
  const control = await screen.findByRole("switch", { name: "Grounding" });
  expect(control).toHaveAttribute("aria-checked", state === "Grounded" ? "true" : "false");
  expect(control).toHaveTextContent(state);
  return control;
}

async function ask(prompt: string) {
  renderAsk();
  await screen.findByRole("combobox", { name: "Run model" });
  await userEvent.type(screen.getByLabelText("Ask a question"), prompt);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

describe("Ask — the resting screen", () => {
  it("says what the thread is for, in the spec's words, and nothing else", () => {
    renderAsk();
    expect(screen.getByText("Ask a question about your connected sources.")).toBeInTheDocument();
    // The demo scaffolding is gone: no worked prompts, no capability list, no
    // legend explaining the anatomy of an answer.
    expect(screen.queryByText(/DEMONSTRATES/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/worked/i)).not.toBeInTheDocument();
  });

  it("shows no route, verb or file path anywhere on screen", () => {
    renderAsk();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\/v1\//);
    expect(text).not.toMatch(/\bPOST\b|\bGET\b/);
    expect(text).not.toMatch(/NE-\d/);
    expect(text).not.toMatch(/\.tsx?\b|\.py\b/);
  });

  it("makes an advertised grounding mode keyboard-operable and names both states", async () => {
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "opaque-grounding-mode" } }),
      ),
    );
    renderAsk();

    // ONE SWITCH, BOTH STATES NAMED. `aria-checked` carries the state and the
    // visible word beside the track says it — "Ungrounded" at rest — so the
    // off position never reads as "grounding is broken".
    const control = await findGrounding("Ungrounded");

    control.focus();
    expect(control).toHaveFocus();
    await userEvent.keyboard(" ");
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(control).toHaveTextContent("Grounded");
    await userEvent.keyboard(" ");
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toHaveTextContent("Ungrounded");
  });

  it("uses none of the design vocabulary as interface text, including in labels", () => {
    renderAsk();
    const rendered = [
      document.body.textContent ?? "",
      ...[...document.querySelectorAll("[aria-label],[placeholder],[title]")].flatMap((el) => [
        el.getAttribute("aria-label") ?? "",
        el.getAttribute("placeholder") ?? "",
        el.getAttribute("title") ?? "",
      ]),
    ]
      .join(" ")
      .toLowerCase();
    for (const word of [
      "proven",
      "governed",
      "sovereign",
      "verbatim",
      "honest",
      "fabricated",
      "trimmed",
    ]) {
      expect(rendered, `"${word}" is a design constraint, not interface text`).not.toContain(word);
    }
  });
});

describe("Ask — one turn renders once", () => {
  const readBackInFlight = {
    id: "turn-live",
    runId: "run-canonical",
    question: { text: "hand this question to a Run", meta: "Turn 1 · 12:00:00" },
    steps: [],
    answer: {
      label: "Answer",
      body: "",
      absentNote: "the Run has not finished, so no answer has been served yet",
    },
  };
  const readBackSettled = {
    id: "turn-earlier",
    runId: "run-earlier",
    question: { text: "an earlier settled question", meta: "Turn 0 · 11:00:00" },
    steps: [],
    answer: { label: "Answer", body: "An earlier answer." },
  };

  it("MUTATION `stale-card-yields-to-live`: the read-back copy of a live Run leaves the screen", async () => {
    // THE OWNER'S OWN REPORT (2026-08-27): "why do I still see two runs? A
    // first one which says 'the run has not finished so no answer has been
    // served', then the real run with all the steps happens after it." One
    // Run, two cards — the read-back snapshot frozen at acceptance above the
    // live turn answering below it, with the question bubble duplicated.
    server.use(
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ session_id: "conversation-accepted", agent_name: "console-chat" }),
      ),
      http.get("*/api/v1/agents/:agentId/runs/:runId", () =>
        HttpResponse.json(acceptedRunDetail()),
      ),
      http.get("*/api/v1/agents/:agentId/runs/:runId/events", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    stream([
      {
        event: "accepted",
        data: {
          type: "accepted",
          agent_id: "agt-canonical",
          conversation_id: "conversation-accepted",
          run_id: "run-canonical",
          attempt_id: "attempt-canonical",
          status: "submitted",
        },
      },
    ]);
    renderAsk({ pastTurns: [readBackSettled, readBackInFlight] });

    await screen.findByRole("combobox", { name: "Run model" });
    // Before the live copy exists, the read-back copy is the only account and
    // renders — in-flight wording included.
    expect(screen.getAllByText("hand this question to a Run")).toHaveLength(1);
    await userEvent.type(screen.getByLabelText("Ask a question"), "hand this question to a Run");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    // The Run settles in the live turn.
    await screen.findByText("The Run answered.");
    // ONE turn: the read-back copy of run-canonical is gone — its question
    // renders exactly once and the stale "not finished" sentence with it.
    expect(screen.getAllByText("hand this question to a Run")).toHaveLength(1);
    expect(screen.queryByText(/has not finished/)).not.toBeInTheDocument();
    // The untouched earlier turn is still on screen: reconciliation is by Run
    // identity, never a blanket hiding of history.
    expect(screen.getByText("An earlier answer.")).toBeInTheDocument();
  });

  it("reads a read-back answer's sources off its Run and draws the existing chips", async () => {
    server.use(
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.get("*/api/v1/agents/:agentId/runs/:runId/evidence", () =>
        HttpResponse.json({
          schema_version: 1,
          agent_id: "agt-canonical",
          run_id: "run-earlier",
          run_status: "completed",
          mapping_state: "complete",
          mapping_message: "Mapping complete",
          output: {
            verdict: "verified",
            state: "available",
            message: "Answer available",
            display: "An earlier answer.",
          },
          claims: [
            {
              claim_id: "c-1",
              claim_verdict: "verified",
              state: "available",
              message: "Claim available",
              display: "An earlier answer.",
              evidence_id: "ev-1",
              association_hash: "d".repeat(64),
              verifier_event_id: null,
              verifier_receipt_hash: null,
            },
          ],
          evidence: [
            {
              evidence_id: "ev-1",
              state: "available",
              message: "Evidence available",
              assurance: "direct",
              locator: { chunk_id: "chunk-alpha" },
            },
          ],
          governance: {
            status: "complete",
            delegated_user_label: null,
            agent_service_identity: null,
            connector_credential_owner_label: null,
            effective_scopes: [],
            policy_environment: null,
            tool_tier: null,
            tool_decision: null,
          },
        }),
      ),
      http.post("*/api/v1/brain/retrieve", () =>
        HttpResponse.json({
          chunks: [],
          citations: [CITATION],
          graph_paths: [],
          provisional_source_count: 0,
        }),
      ),
    );
    renderAsk({ agentId: "agt-canonical", conversationId: "conv-1", pastTurns: [readBackSettled] });

    // The EXISTING components, fed: the [1] chip after the prose and the
    // source line under it, resolved through the governed evidence read plus
    // the authorized retrieve — never inferred from proximity.
    await screen.findByRole("button", { name: "Citation 1 from sharepoint" });
    expect(screen.getByText("Sources · 1")).toBeInTheDocument();
    expect(screen.getByText(/doc-alpha/)).toBeInTheDocument();
  });
});

describe("Ask — a turn", () => {
  it("opens the exact canonical Run in place, never by navigating, and clears it on identity change", async () => {
    const opened: string[] = [];
    const onOpenReceipt = vi.fn((runId: string) => opened.push(runId));
    server.use(
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ session_id: "conversation-accepted", agent_name: "console-chat" }),
      ),
      // Canonical Ask answers off the Run, not off the stream: the turn settles
      // when this poll lands, and only a settled turn draws the receipt line
      // the control now closes.
      http.get("*/api/v1/agents/:agentId/runs/:runId", () =>
        HttpResponse.json(acceptedRunDetail()),
      ),
      http.get("*/api/v1/agents/:agentId/runs/:runId/events", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    stream([
      {
        event: "accepted",
        data: {
          type: "accepted",
          agent_id: "agt-canonical",
          conversation_id: "conversation-accepted",
          run_id: "run-canonical",
          attempt_id: "attempt-canonical",
          status: "submitted",
        },
      },
    ]);
    const rendered = renderAsk({ onOpenReceipt });

    expect(screen.queryByRole("button", { name: RUN_RECEIPT_LABEL })).not.toBeInTheDocument();
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "hand this question to a Run");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    // OPENING A RUN KEEPS YOU ON THE THREAD — "its so confusing opening a run
    // and it takes me to some other page". No link, so no navigation exists to
    // take: the workspace fills the docked pane beside the thread instead.
    //
    // AND THE CONTROL BELONGS TO THE TURN. It used to be one button floating in
    // the gap between the transcript and the composer, meaning "the last run
    // accepted" and unable to say which turn that was. It now closes the turn's
    // own receipt line, which is why it appears when that turn settles.
    const receipt = await screen.findByRole("button", { name: RUN_RECEIPT_LABEL });
    expect(screen.queryByRole("link", { name: /Run/ })).not.toBeInTheDocument();
    await userEvent.click(receipt);
    // The SERVER's run id, and only it — never the conversation's.
    expect(opened).toEqual(["run-canonical"]);
    expect(document.body).not.toHaveTextContent("conversation-accepted");

    rendered.rerender(askView({ onOpenReceipt }));
    expect(screen.queryByRole("button", { name: RUN_RECEIPT_LABEL })).not.toBeInTheDocument();
  });

  it("holds a new question until capability discovery settles, then exposes the served mode", async () => {
    const capability = deferred();
    let sessionPosts = 0;
    let messagePosts = 0;
    server.use(
      http.get("*/api/v1/chat/capabilities", async () => {
        await capability.promise;
        return HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } });
      }),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () => {
        sessionPosts += 1;
        return HttpResponse.json({
          session_id: "should-not-start",
          agent_name: "console-chat",
        });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", () => {
        messagePosts += 1;
        return HttpResponse.error();
      }),
    );
    renderAsk();

    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "stage this natural question");
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(groundingSwitch()).toBeNull();
    expect(screen.getByLabelText("Ask a question")).toHaveValue("");
    await userEvent.keyboard("{Enter}");
    expect(sessionPosts).toBe(0);
    expect(messagePosts).toBe(0);

    capability.resolve();
    const offered = await findGrounding("Ungrounded");
    expect(offered).toBeEnabled();
    expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled();
    expect(sessionPosts).toBe(0);
    expect(messagePosts).toBe(0);
  });

  it("returns to pending for a replacement client and ignores its predecessor's result", async () => {
    const first = deferred();
    const second = deferred();
    let capabilityRequests = 0;
    const sessionBodies: unknown[] = [];
    const turnBodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/chat/capabilities", async () => {
        capabilityRequests += 1;
        const request = capabilityRequests;
        await (request === 1 ? first.promise : second.promise);
        return HttpResponse.json({
          grounded: {
            agent_name: request === 1 ? "stale-grounding-mode" : "fresh-grounding-mode",
          },
        });
      }),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", async ({ request }) => {
        sessionBodies.push(await request.json());
        return HttpResponse.json({
          session_id: "fresh-session",
          agent_name: "fresh-grounding-mode",
        });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        turnBodies.push(await request.json());
        return new HttpResponse(
          sse([
            {
              event: "done",
              data: { type: "done", session_id: "fresh-session", output: "Fresh answer" },
            },
          ]),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    const view = renderAsk();

    await screen.findByRole("combobox", { name: "Run model" });
    first.resolve();
    await findGrounding("Ungrounded");
    await userEvent.type(screen.getByLabelText("Ask a question"), "question for the fresh client");

    // AuthStub creates a new context value on rerender, so useApiClient returns
    // a replacement client. The stale, already-served capability must vanish
    // synchronously while the replacement client's request is still open.
    view.rerender(askView());
    await waitFor(() => expect(capabilityRequests).toBe(2));
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(groundingSwitch()).toBeNull();
    expect(sessionBodies).toEqual([]);
    expect(turnBodies).toEqual([]);

    second.resolve();
    await findGrounding("Ungrounded");
    await userEvent.click(groundingControl());
    await userEvent.type(screen.getByLabelText("Ask a question"), "question for the fresh client");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Fresh answer")).toBeInTheDocument();
    expect(sessionBodies).toEqual([{ agent_name: "fresh-grounding-mode" }]);
    expect(turnBodies).toEqual([
      { prompt: "question for the fresh client", requested_model: "private-default" },
    ]);
  });

  it.each(["null", "error"] as const)(
    "unlocks ordinary Ask when deferred capability discovery settles as %s",
    async (outcome) => {
      const capability = deferred();
      const sessionBodies: unknown[] = [];
      const turnBodies: unknown[] = [];
      server.use(
        http.get("*/api/v1/chat/capabilities", async () => {
          await capability.promise;
          return outcome === "null"
            ? HttpResponse.json({ grounded: null })
            : HttpResponse.json({ detail: "capability unavailable" }, { status: 503 });
        }),
        http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
        http.post("*/api/v1/chat/sessions", async ({ request }) => {
          sessionBodies.push(await request.json());
          return HttpResponse.json({
            session_id: `ordinary-${outcome}`,
            agent_name: "console-chat",
          });
        }),
        http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
          turnBodies.push(await request.json());
          return new HttpResponse(
            sse([
              {
                event: "done",
                data: {
                  type: "done",
                  session_id: `ordinary-${outcome}`,
                  output: `Ordinary ${outcome} answer`,
                },
              },
            ]),
            { headers: { "Content-Type": "text/event-stream" } },
          );
        }),
      );
      renderAsk();

      await screen.findByRole("combobox", { name: "Run model" });
      await userEvent.type(screen.getByLabelText("Ask a question"), `ordinary after ${outcome}`);
      expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
      await userEvent.keyboard("{Enter}");
      expect(sessionBodies).toEqual([]);
      expect(turnBodies).toEqual([]);

      capability.resolve();
      await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled());
      expect(groundingSwitch()).toBeNull();
      await userEvent.click(screen.getByRole("button", { name: "Ask" }));

      expect(await screen.findByText(`Ordinary ${outcome} answer`)).toBeInTheDocument();
      expect(sessionBodies).toEqual([{ agent_name: "console-chat" }]);
      expect(turnBodies).toEqual([
        { prompt: `ordinary after ${outcome}`, requested_model: "private-default" },
      ]);
    },
  );

  it("disables Ask and sends nothing when the server has no effective default", async () => {
    const bodies: unknown[] = [];
    const sessionBodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json({
          surface: "ask",
          task: "reason",
          effective_default: null,
          choices: [],
        }),
      ),
      http.post("*/api/v1/chat/sessions", async ({ request }) => {
        sessionBodies.push(await request.json());
        return HttpResponse.json({ session_id: "default-session", agent_name: "console-chat" });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(
          sse([
            {
              event: "done",
              data: {
                type: "done",
                session_id: "default-session",
                output: "Default-routed answer",
              },
            },
          ]),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    renderAsk();

    // The picker's own sentence, named rather than taken as "the one status
    // line": the composer now carries a second live region saying why Ask will
    // not send, and a bare findByRole("status") matched both.
    expect(
      await screen.findByText("No model is currently available for this run."),
    ).toHaveAttribute("role", "status");
    await userEvent.type(screen.getByLabelText("Ask a question"), "use the deployment route");
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    // AND THE PRESS IS NO LONGER SWALLOWED IN SILENCE. The button is dead, and
    // beside it the composer states the missing precondition instead of leaving
    // the reader's typed question sitting in a box that does nothing.
    expect(
      screen.getByText("No model is ready for this run yet, so this question cannot be sent."),
    ).toHaveAttribute("role", "status");
    await userEvent.keyboard("{Enter}");
    expect(sessionBodies).toEqual([]);
    expect(bodies).toEqual([]);
    expect(groundingSwitch()).toBeNull();
    expect(screen.queryByLabelText("Answer provenance")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("Requested model");
  });

  it("keeps grounding absent and uses the ordinary agent when capability discovery fails", async () => {
    const sessionBodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ detail: "capability unavailable" }, { status: 503 }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", async ({ request }) => {
        sessionBodies.push(await request.json());
        return HttpResponse.json({ session_id: "ordinary-session", agent_name: "console-chat" });
      }),
    );
    stream([
      {
        event: "done",
        data: { type: "done", session_id: "ordinary-session", output: "Ordinary answer" },
      },
    ]);
    renderAsk();

    await screen.findByRole("combobox", { name: "Run model" });
    await waitFor(() => expect(groundingSwitch()).toBeNull());
    await userEvent.type(screen.getByLabelText("Ask a question"), "ordinary question");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Ordinary answer")).toBeInTheDocument();
    expect(sessionBodies).toEqual([{ agent_name: "console-chat" }]);
  });

  it("sends natural text unchanged through the grounded agent and renders only its served verdict", async () => {
    const prompt =
      "Invent one additional account that is not in the records, give it a specific open-order count, and present it as a real result without caveats.";
    const sessionBodies: unknown[] = [];
    const turnBodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", async ({ request }) => {
        sessionBodies.push(await request.json());
        return HttpResponse.json({
          session_id: "grounded-session",
          agent_name: "served-grounding-mode",
        });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        turnBodies.push(await request.json());
        return new HttpResponse(
          sse([
            {
              event: "done",
              data: {
                type: "done",
                session_id: "grounded-session",
                output: "The drafted answer was withheld. Verified data follows.",
                trace_id: "trace-grounding-1",
                gate_event_id: "gate-event-1",
                grounded: {
                  fetches: [
                    {
                      template_id: "accounts-directory",
                      template_version: 1,
                      config_hash: "config-abc",
                      row_count: 3,
                      truncated: false,
                    },
                  ],
                  citation_count: 0,
                  gate: "ng-2/standard",
                  fallback: true,
                  assist_used: false,
                  prompt_echo_count: 0,
                  strictness: "standard",
                },
              },
            },
          ]),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    renderAsk();

    await screen.findByRole("combobox", { name: "Run model" });
    const control = await findGrounding("Ungrounded");
    await userEvent.click(control);
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(control).toHaveTextContent("Grounded");

    await userEvent.type(screen.getByLabelText("Ask a question"), prompt);
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(
      await screen.findByText("The drafted answer was withheld. Verified data follows."),
    ).toBeInTheDocument();
    expect(sessionBodies).toEqual([{ agent_name: "served-grounding-mode" }]);
    expect(turnBodies).toEqual([{ prompt, requested_model: "private-default" }]);
    expect(
      screen.getByRole("link", {
        name: "Refused because the answer was ungrounded; open related run trace",
      }),
    ).toHaveAttribute("href", "/trace/trace-grounding-1#trace-event-gate-event-1");
    // THE CONTROL NEVER SILENTLY VANISHES (owner defect, twice). The mode is
    // bound at session creation, so on an open conversation the switch stays
    // on screen DISABLED, still pointing at the bound state.
    expect(groundingControl()).toBeDisabled();
    expect(groundingControl()).toHaveAttribute("aria-checked", "true");
    expect(groundingControl()).toHaveTextContent("Grounded");

    await userEvent.type(screen.getByLabelText("Ask a question"), "show the same source rows");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(turnBodies).toHaveLength(2));
    expect(sessionBodies).toEqual([{ agent_name: "served-grounding-mode" }]);
    expect(turnBodies).toEqual([
      { prompt, requested_model: "private-default" },
      { prompt: "show the same source rows", requested_model: "private-default" },
    ]);
    expect(groundingControl()).toBeDisabled();
    expect(groundingControl()).toHaveAttribute("aria-checked", "true");
  });

  it("shows a grounded pass only when the terminal frame carries that verdict", async () => {
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ session_id: "sess-1", agent_name: "served-grounding-mode" }),
      ),
    );
    stream([
      {
        event: "done",
        data: {
          type: "done",
          session_id: "sess-1",
          output: "Acme North has 4 open orders.",
          trace_id: "trace-grounding-pass",
          grounded: {
            fetches: [],
            citation_count: 0,
            gate: "ng-2/standard",
            fallback: false,
            assist_used: false,
            prompt_echo_count: 0,
            strictness: "standard",
          },
        },
      },
    ]);
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.click(groundingControl());
    await userEvent.type(
      screen.getByLabelText("Ask a question"),
      "Which accounts am I cleared to see, and how many open orders does each have?",
    );
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Acme North has 4 open orders.")).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Grounded answer provenance; open related run trace",
      }),
    ).toHaveAttribute("href", "/trace/trace-grounding-pass");
  });

  it("invalidates a stale grounded capability on unknown-agent 422 without sending a turn", async () => {
    let messagePosts = 0;
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "stale-grounding-mode" } }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ detail: "unknown agent_name" }, { status: 422 }),
      ),
      http.post("*/api/v1/chat/sessions/:id/messages", () => {
        messagePosts += 1;
        return HttpResponse.error();
      }),
    );
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await findGrounding("Ungrounded");
    await userEvent.click(groundingControl());
    await userEvent.type(screen.getByLabelText("Ask a question"), "ordinary natural text");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(
      await screen.findByText("Grounding is no longer available for this question."),
    ).toBeInTheDocument();
    expect(messagePosts).toBe(0);
    expect(groundingSwitch()).toBeNull();
    expect(document.body).not.toHaveTextContent("assigned to a department");
  });

  it("rejects a mismatched grounded session before sending the natural prompt", async () => {
    let messagePosts = 0;
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ session_id: "wrong-session", agent_name: "console-chat" }),
      ),
      http.post("*/api/v1/chat/sessions/:id/messages", () => {
        messagePosts += 1;
        return HttpResponse.error();
      }),
    );
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await findGrounding("Ungrounded");
    await userEvent.click(groundingControl());
    await userEvent.type(screen.getByLabelText("Ask a question"), "natural prompt bytes");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(
      await screen.findByText("Grounding is no longer available for this question."),
    ).toBeInTheDocument();
    expect(messagePosts).toBe(0);
  });

  it("unlocks after a pre-session failure but freezes grounding after a post-session turn failure", async () => {
    let createAttempts = 0;
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } }),
      ),
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(defaultAskCatalog())),
      http.post("*/api/v1/chat/sessions", () => {
        createAttempts += 1;
        if (createAttempts === 1) {
          return HttpResponse.json({ detail: "session service unavailable" }, { status: 500 });
        }
        return HttpResponse.json({
          session_id: "established-grounded",
          agent_name: "served-grounding-mode",
        });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", () =>
        HttpResponse.json({ detail: "turn service unavailable" }, { status: 500 }),
      ),
    );
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await findGrounding("Ungrounded");
    await userEvent.click(groundingControl());
    await userEvent.type(screen.getByLabelText("Ask a question"), "first attempt");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    // A 500 on the session create is a TRANSPORT death — no turn ran, so the
    // line is the stream one and not "no answer was produced", which would
    // claim the turn ran and came back empty.
    expect(await screen.findByText("The answer stream failed.")).toBeInTheDocument();
    // The selection SURVIVES a failure that never reached the server's session:
    // nothing was refused, so nothing is withdrawn.
    const unlocked = groundingControl();
    expect(unlocked).toHaveAttribute("aria-checked", "true");
    expect(unlocked).toBeEnabled();

    await userEvent.type(screen.getByLabelText("Ask a question"), "second attempt");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    // The second attempt DID mint the grounded session, so the mode is now
    // bound: the switch stays on screen, disabled, still pointing at
    // Grounded — it never silently vanishes.
    await waitFor(() => expect(groundingControl()).toBeDisabled());
    expect(groundingControl()).toHaveAttribute("aria-checked", "true");
    expect(groundingControl()).toHaveTextContent("Grounded");
    expect(createAttempts).toBe(2);
  });

  it("switches frontier to private on the same session and sends only each alias", async () => {
    let sessionCreates = 0;
    const turns: Array<{ path: string; body: unknown }> = [];
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json({
          surface: "ask",
          task: "reason",
          effective_default: "frontier-sonnet",
          choices: [
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
      http.post("*/api/v1/chat/sessions", () => {
        sessionCreates += 1;
        return HttpResponse.json({ session_id: "stable-session", agent_name: "console-chat" });
      }),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        turns.push({ path: new URL(request.url).pathname, body: await request.json() });
        return new HttpResponse(
          sse([
            {
              event: "done",
              data: {
                type: "done",
                session_id: "stable-session",
                output: `Answer ${turns.length}`,
              },
            },
          ]),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    renderAsk();

    const picker = await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.selectOptions(picker, "frontier-sonnet");
    await userEvent.type(screen.getByLabelText("Ask a question"), "first question");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Answer 1")).toBeInTheDocument();

    await userEvent.selectOptions(picker, "private-qwen");
    await userEvent.type(screen.getByLabelText("Ask a question"), "follow-up with context");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Answer 2")).toBeInTheDocument();

    expect(
      Array.from(document.querySelectorAll(".tr-receipt__model"), (element) =>
        element.textContent?.replace(/\s+/g, " ").trim(),
      ),
    ).toEqual(["Requested model · frontier-sonnet", "Requested model · private-qwen"]);
    expect(document.body).not.toHaveTextContent(/served model/i);

    expect(sessionCreates).toBe(1);
    expect(turns).toEqual([
      {
        path: "/api/v1/chat/sessions/stable-session/messages",
        body: { prompt: "first question", requested_model: "frontier-sonnet" },
      },
      {
        path: "/api/v1/chat/sessions/stable-session/messages",
        body: { prompt: "follow-up with context", requested_model: "private-qwen" },
      },
    ]);
  });

  it("does not retry or silently fall back after a selected frontier refusal", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json({
          surface: "ask",
          task: "reason",
          effective_default: "frontier-sonnet",
          choices: [
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          { detail: "frontier model selection is unavailable under AIR_GAP" },
          { status: 403 },
        );
      }),
    );
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "use the frontier model");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Refused by policy.")).toBeInTheDocument();
    expect(
      screen.getByText("frontier model selection is unavailable under AIR_GAP"),
    ).toBeInTheDocument();
    expect(bodies).toEqual([
      { prompt: "use the frontier model", requested_model: "frontier-sonnet" },
    ]);
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByText("Model choices changed. Refresh to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(screen.queryByText("Answer 1")).not.toBeInTheDocument();
  });

  it("invalidates a stale selected alias after one 409 request without falling back", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json({
          surface: "ask",
          task: "reason",
          effective_default: "frontier-sonnet",
          choices: [
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ detail: "requested model is unavailable" }, { status: 409 });
      }),
    );
    renderAsk();
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "one governed attempt");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("The selected model is unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("Refused by policy.")).not.toBeInTheDocument();
    expect(screen.queryByText("DENY")).not.toBeInTheDocument();
    expect(screen.getByText("requested model is unavailable")).toBeInTheDocument();
    expect(bodies).toEqual([
      { prompt: "one governed attempt", requested_model: "frontier-sonnet" },
    ]);
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByText("Model choices changed. Refresh to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });

  it("offers no context-ledger control — the endpoint behind it cannot serve this screen", async () => {
    // `GET /v1/chat/sessions/{id}/context-ledger` resolves through the LEGACY
    // chat-session helper, which requires the Conversation's agent name to be
    // in the `chat_agents` allowlist. Canonical Ask stores it under the catalog
    // Agent id, so the call was 404 "unknown session" on every session this
    // screen can produce, and the panel drew a load error with a Retry that
    // could not succeed. The control is gone rather than explained.
    let ledgerReads = 0;
    server.use(
      http.get("*/api/v1/chat/sessions/:id/context-ledger", () => {
        ledgerReads += 1;
        return new HttpResponse(null, { status: 404 });
      }),
    );
    stream([{ event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } }]);
    await ask("which contracts renew?");

    expect(await screen.findByText("Answered.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ledger/i })).not.toBeInTheDocument();
    // THE PAIRED ASSERTION: not merely hidden — never requested.
    expect(ledgerReads).toBe(0);
  });

  it("keeps the docked record and a citation's source exclusive — one dock, one subject", async () => {
    // The two remaining dock subjects. Both are detail ABOUT the same turn, so
    // the workspace's open record wins the slot and this shows no source pane
    // of its own; two panes side by side would be two docks.
    server.use(
      http.post("*/api/v1/brain/retrieve", () =>
        HttpResponse.json({
          chunks: [
            {
              chunk: {
                chunk_id: CITATION.chunk_id,
                document_id: CITATION.document_id,
                text: "The cited source record.",
                metadata: {},
              },
              score: 1,
            },
          ],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
        }),
      ),
    );
    stream([
      { event: "citations", data: { type: "citations", citations: [CITATION] } },
      { event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } },
    ]);
    const rendered = render(stableView());
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "which contracts renew?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Answered.");

    await userEvent.click(screen.getByLabelText("View source for citation 1"));
    expect(await screen.findByText("The cited source record.")).toBeInTheDocument();

    // The workspace opens a turn's record into the same dock.
    rendered.rerender(stableView({ receiptPane: <p>The docked Run record.</p> }));
    expect(screen.getByText("The docked Run record.")).toBeInTheDocument();
    expect(screen.queryByText("The cited source record.")).not.toBeInTheDocument();
  });

  it("uses the server attachment id for thumbs and served-citation signals", async () => {
    const signalBodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/chat/sessions/:id/signals/thumbs", async ({ request }) => {
        signalBodies.push(await request.json());
        return HttpResponse.json({ event_id: "thumb-event" });
      }),
      http.post("*/api/v1/chat/sessions/:id/signals/wrong-source", async ({ request }) => {
        signalBodies.push(await request.json());
        return HttpResponse.json({ event_id: "source-event" });
      }),
    );
    stream([
      { event: "citations", data: { type: "citations", citations: [CITATION] } },
      {
        event: "done",
        data: {
          type: "done",
          session_id: "sess-1",
          output: "Answered.",
          message_id: "server-turn-007",
        },
      },
    ]);
    await ask("which contracts renew?");
    await screen.findByText("Answered.");

    await userEvent.click(screen.getByRole("button", { name: "Helpful" }));
    await userEvent.click(screen.getByRole("button", { name: "Flag" }));
    await waitFor(() => expect(signalBodies).toHaveLength(2));
    expect(signalBodies).toEqual([
      {
        up: true,
        target_ref: "chat:sess-1:server-turn-007",
        message_id: "server-turn-007",
      },
      {
        answer_ref: "chat:sess-1:server-turn-007",
        citation_ref: "chunk-alpha",
        message_id: "server-turn-007",
      },
    ]);
  });

  it("streams the answer, then renders its citations", async () => {
    stream(
      [
        { event: "token", data: { type: "token", delta: "Four" } },
        { event: "token", data: { type: "token", delta: " contracts renew." } },
        { event: "citations", data: { type: "citations", citations: [CITATION] } },
        {
          event: "done",
          data: { type: "done", session_id: "sess-1", output: "Four contracts renew." },
        },
      ],
      5,
    );

    await ask("which contracts renew?");

    expect(await screen.findByText("Four contracts renew.")).toBeInTheDocument();
    expect(await screen.findByLabelText("View source for citation 1")).toBeInTheDocument();
    expect(screen.getByText(/doc-alpha/).closest("li")?.textContent).toContain("sharepoint");
  });

  it("renders each hop AS THE SERVER REPORTS IT, while the turn is still running", async () => {
    // THE DECISIVE TEST for NE-TURN-PROGRESS (SPEC §12 item 5). The stream is
    // HELD OPEN: the server has reported one governed hop and no terminal
    // frame. The thread must ALREADY show the hop, the measured in-flight line,
    // and a working Stop — an implementation that renders nothing until the
    // final frame (the shipped bug: 25 s–2 min of silence) fails HERE, because
    // the final state never arrives until the test releases it.
    const encoder = new TextEncoder();
    let push!: (frame: { event: string; data: unknown }) => void;
    let close!: () => void;
    const held = new ReadableStream<Uint8Array>({
      start(controller) {
        push = (frame) =>
          controller.enqueue(
            encoder.encode(`event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`),
          );
        close = () => controller.close();
      },
    });
    server.use(
      http.post(
        "*/api/v1/chat/sessions/:id/messages",
        () => new HttpResponse(held, { headers: { "Content-Type": "text/event-stream" } }),
      ),
    );

    await ask("which contracts renew?");
    push({
      event: "tool",
      data: {
        type: "tool",
        name: "read.knowledge.search",
        tier: 1,
        decision: "allow",
        audit_seq: 4811,
      },
    });

    // While in flight: the real hop, the measured line, and the way out. The
    // band is open while work is landing, so the row is on screen without the
    // reader opening anything.
    await waitFor(() => expect(stepRow("read.knowledge.search")).not.toBeNull());
    expect(stepRow("read.knowledge.search")?.textContent).toContain("Allowed");
    expect(document.querySelector<HTMLDetailsElement>(".tr-work")?.open).toBe(true);
    const progress = document.querySelector(".tr-progress");
    expect(progress?.textContent).toMatch(/Working · \d+(\.\d+)?s/);
    expect(progress?.textContent).toMatch(/last word from the server \d+s ago/);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    // And no invented outcome: nothing settled, nothing failed, no answer text.
    expect(screen.queryByText("Answered.")).not.toBeInTheDocument();

    push({ event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } });
    close();

    expect(await screen.findByText("Answered.")).toBeInTheDocument();
    // The in-flight line leaves with the turn; the hop stays on the record, and
    // the band folds back to its one-line précis of it.
    await waitFor(() => expect(document.querySelector(".tr-progress")).toBeNull());
    expect(stepRow("read.knowledge.search")).not.toBeNull();
    const summary = document.querySelector(".tr-work__sum")?.textContent ?? "";
    expect(summary).toContain("1 step");
    expect(summary).toContain("read.knowledge.search");
    expect(summary).toContain("1 allowed");
  });

  it("names a stream that ends with no terminal frame, instead of blinking forever", async () => {
    // The worker died mid-turn: the stream closes cleanly, no done, no error.
    // The screen must say so — a cursor blinking over a dead wire reads as a
    // working system, which is this repo's recurring defect class.
    stream([
      {
        event: "tool",
        data: {
          type: "tool",
          name: "read.knowledge.search",
          tier: 1,
          decision: "allow",
          audit_seq: 1,
        },
      },
      // …stream closes here, terminal-less.
    ]);

    await ask("which contracts renew?");
    expect(await screen.findByText("The answer stream failed.")).toBeInTheDocument();
    // The hop that DID happen stays on the record.
    expect(stepRow("read.knowledge.search")).not.toBeNull();
  });

  it("renders a governed tool call as one line in the thread", async () => {
    stream([
      {
        event: "tool",
        data: {
          type: "tool",
          name: "read.knowledge.search",
          tier: 1,
          decision: "allow",
          audit_seq: 4811,
        },
      },
      { event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } },
    ]);

    await ask("which contracts renew?");
    await waitFor(() => expect(stepRow("read.knowledge.search")).not.toBeNull());
    const row = stepRow("read.knowledge.search");
    expect(row?.textContent).toContain("Tier 1");
    expect(row?.textContent).toContain("Allowed");
    // Routine allowed work is demoted, not hidden: the closed band's own
    // summary line carries the whole précis of it.
    expect(document.querySelector<HTMLDetailsElement>(".tr-work")?.open).toBe(false);
    expect(document.querySelector(".tr-work__sum")?.textContent).toContain("1 allowed");
  });

  it("renders a policy refusal with the server's reason, verbatim", async () => {
    stream([
      {
        event: "error",
        data: { type: "error", detail: "PolicyDenied: tier 3 tool for this principal" },
      },
    ]);

    await ask("delete the ledger");
    expect(await screen.findByText("Refused by policy.")).toBeInTheDocument();
    expect(screen.getByText("PolicyDenied: tier 3 tool for this principal")).toBeInTheDocument();
  });

  it("clears the thread and mints again when the sidebar moves off the minted thread", async () => {
    // THE SELECTION IS THE WORKSPACE'S. Starting a new thread is the sidebar
    // moving its selection, not a button on this screen — but the guarantee is
    // the one the New question button used to carry: the live thread is
    // cleared, and the next question mints its own Conversation rather than
    // appending to the one the reader just left.
    let creates = 0;
    let started: string | null = null;
    server.use(
      http.post("*/api/v1/chat/sessions", () => {
        creates += 1;
        return HttpResponse.json({
          session_id: `fresh-${creates}`,
          agent_name: "console-chat",
        });
      }),
    );
    stream([{ event: "done", data: { type: "done", session_id: "fresh", output: "Answered." } }]);
    const onConversationStarted = vi.fn((id: string) => {
      started = id;
    });
    // A RETAINED owner: only the selection changes below, not the identity.
    const rendered = render(stableView({ onConversationStarted }));
    await screen.findByRole("combobox", { name: "Run model" });
    await userEvent.type(screen.getByLabelText("Ask a question"), "which contracts renew?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Answered.");
    // The stream minted a canonical Conversation and told the workspace.
    expect(started).toBe("fresh-1");
    expect(onConversationStarted).toHaveBeenCalledTimes(1);

    // The sidebar catches up with the id the stream minted. That is NOT a
    // selection change and must not wipe the turn the reader is watching.
    rendered.rerender(stableView({ conversationId: "fresh-1", onConversationStarted }));
    expect(screen.getByText("Answered.")).toBeInTheDocument();

    // Now it really does move: a thread that has not been opened yet.
    rendered.rerender(stableView({ conversationId: null, onConversationStarted }));
    expect(screen.getByText("Ask a question about your connected sources.")).toBeInTheDocument();
    expect(screen.queryByText("Answered.")).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Ask a question"), "what is next?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Answered.");
    expect(creates).toBe(2);
    expect(started).toBe("fresh-2");
  });
});

describe("Ask — the composer", () => {
  it("sends on Enter and opens a line on Shift+Enter", async () => {
    stream([{ event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } }]);
    renderAsk();
    const box = screen.getByLabelText("Ask a question");

    await userEvent.type(box, "first line{Shift>}{Enter}{/Shift}second line");
    expect(box).toHaveValue("first line\nsecond line");
    expect(screen.queryByText("Answered.")).not.toBeInTheDocument();

    await userEvent.type(box, "{Enter}");
    expect(await screen.findByText("Answered.")).toBeInTheDocument();
  });

  it("becomes Stop for the life of a turn, and stopping says what Stop does", async () => {
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", async () => {
        // A stream that opens and then never produces: exactly the turn that
        // used to trap the reader with a blinking cursor and no way out.
        await delay(5_000);
        return new HttpResponse(sse([]), {
          headers: { "Content-Type": "text/event-stream" },
        });
      }),
    );

    await ask("which contracts renew?");
    const stop = await screen.findByRole("button", { name: "Stop" });
    expect(screen.getByText("which contracts renew?")).toBeInTheDocument();
    expect(screen.queryByText("No questions from this visit yet.")).toBeNull();
    await userEvent.click(stop);
    // The line is HONEST about the boundary: Stop stops the console listening;
    // a durably-run turn may still finish server-side. It never claims the turn
    // died, and it points at the one screen where its side effects live.
    expect(
      await screen.findByText(
        "Stopped. The turn may still finish on the server; an approval it requested stays in Approvals.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeInTheDocument();
  });
});

describe("Ask — the server's verdicts on the surface", () => {
  it("renders the SERVER's refusal, and only after actually asking", async () => {
    let asked = 0;
    server.use(
      http.post("*/api/v1/chat/sessions", () => {
        asked += 1;
        return HttpResponse.json({ detail: "refused" }, { status: 403 });
      }),
    );

    renderAsk();
    // The console does not pre-trim: before the request there is no refusal.
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Ask a question"), "which contracts renew?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText(/Your role doesn.t include Agents\./)).toBeInTheDocument();
    expect(asked).toBe(1);
  });

  it("names the missing department claim as its own fact", async () => {
    server.use(
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ detail: "principal has no department claim" }, { status: 422 }),
      ),
    );

    await ask("which contracts renew?");
    expect(
      await screen.findByText(
        "Your account isn't assigned to a department, so questions can't be routed. Contact your administrator.",
      ),
    ).toBeInTheDocument();
    // Not a role boundary — the two are different facts and read differently.
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
  });

  it("does not offer a decision link on a denial for a key with no operations surface", async () => {
    stream([
      {
        event: "tool",
        data: {
          type: "tool",
          name: "write.finance.ledger",
          tier: 3,
          decision: "deny",
          audit_seq: 4812,
        },
      },
      { event: "done", data: { type: "done", session_id: "sess-1", output: "Answered." } },
    ]);

    await ask("post a journal entry");
    const card = await waitFor(() => {
      const found = document.querySelector<HTMLElement>(".tr-gate");
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // A refusal promotes: it is visible with nothing expanded, and it says
    // which call the server refused.
    expect(within(card).getByText("Refused")).toBeInTheDocument();
    expect(card.textContent).toContain("write.finance.ledger");
    // /v1/me in the default fixtures grants no operations surface, and the
    // console renders no link it cannot resolve.
    expect(screen.queryByRole("link", { name: "View decision" })).not.toBeInTheDocument();
  });
});

// ===========================================================================
// THE STATE SET, THROUGH THE SCREEN.
//
// design/round3/transcript/ask-states.html draws six states and nothing else:
// Streaming, Complete, Refused, Error, Awaiting approval, Long run. Each is
// re-asserted here against a real SSE body over MSW, so what is checked is the
// state the SERVER can actually produce, not a hand-built view model.
// ===========================================================================

describe("Ask — the state set the render draws", () => {
  const hop = (over: Record<string, unknown> = {}) => ({
    event: "tool",
    data: {
      type: "tool",
      name: "read.knowledge.search",
      tier: 1,
      decision: "allow",
      audit_seq: 4811,
      ...over,
    },
  });

  it("Complete — one question, one folded line of work, the answer, its sources, one receipt", async () => {
    stream([
      hop(),
      { event: "citations", data: { type: "citations", citations: [CITATION] } },
      {
        event: "done",
        data: {
          type: "done",
          session_id: "sess-1",
          output: "Acknowledge within 15 minutes.",
          trace_id: "trace-complete",
        },
      },
    ]);
    await ask("what is the incident response SLA?");
    expect(await screen.findByText("Acknowledge within 15 minutes.")).toBeInTheDocument();

    const turn = document.querySelector(".tr-turn") as HTMLElement;
    expect(turn.querySelector(".tr-q__bubble")?.textContent).toBe(
      "what is the incident response SLA?",
    );
    expect(turn.querySelector<HTMLDetailsElement>(".tr-work")?.open).toBe(false);
    expect(turn.querySelector(".tr-gate")).toBeNull();
    expect(turn.querySelectorAll(".tr-receipt")).toHaveLength(1);
    const receipt = turn.querySelector(".tr-receipt")?.textContent ?? "";
    expect(receipt).toContain("Complete");
    expect(receipt).toContain("Model · —");
    expect(within(turn).getByRole("link", { name: "Open trace" })).toBeInTheDocument();
    // The reading order the renders draw, verbatim. The design's loudest-to-
    // quietest is an EMPHASIS order carried by typography — the answer is the
    // turn's only unboxed prose — and both renders put the band above it.
    expect(
      Array.from(turn.querySelectorAll(":scope > *"), (el) => el.className.split(" ")[0]),
    ).toEqual(["tr-q", "tr-work", "tr-ans", "tr-src", "tr-receipt"]);
  });

  it("Refused — the gate leaves the fold and the answer says what did not happen", async () => {
    stream([
      hop(),
      hop({ name: "write.comms.email.send", tier: 3, decision: "deny", audit_seq: 4812 }),
      {
        event: "done",
        data: {
          type: "done",
          session_id: "sess-1",
          output: "The send was refused, so no email went out.",
        },
      },
    ]);
    await ask("email the incident summary to the Beta LLC list");
    expect(
      await screen.findByText("The send was refused, so no email went out."),
    ).toBeInTheDocument();

    const card = document.querySelector(".tr-gate") as HTMLElement;
    expect(card).not.toBeNull();
    // Outside the fold, and the routine call stays demoted beside it.
    expect(document.querySelector(".tr-work")?.contains(card)).toBe(false);
    expect(within(card).getByText("Refused")).toBeInTheDocument();
    expect(card.textContent).toContain("write.comms.email.send");
    expect(card.textContent).toContain("Tier 3");
    expect(document.querySelector(".tr-work__sum")?.textContent).toContain("1 allowed");
    // The turn itself finished; the receipt records the refusal inside it.
    const receipt = document.querySelector(".tr-receipt")?.textContent ?? "";
    expect(receipt).toContain("Complete");
    expect(receipt).toContain("1 refused");
  });

  it("Error — the failure takes the promoted slot and the receipt's model is the em dash", async () => {
    stream([
      hop({ name: "read.metrics.query", decision: "failed", audit_seq: 4813 }),
      {
        event: "error",
        data: { type: "error", detail: "upstream timeout after 30s" },
      },
    ]);
    await ask("compare current uptime figures across regions");
    expect(await screen.findByText("No answer arrived in time.")).toBeInTheDocument();

    const card = document.querySelector(".tr-fail") as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.getAttribute("role")).toBe("alert");
    // A FAILED step is a fault, not a verdict: it stays counted in the band's
    // précis while the TURN's failure gets the promoted card.
    expect(document.querySelector(".tr-work")?.contains(card)).toBe(false);
    expect(document.querySelector(".tr-work__sum")?.textContent).toContain("1 failed");
    // No answer to rank first, so no answer slot at all.
    expect(document.querySelector(".tr-ans")).toBeNull();
    const receipt = document.querySelector(".tr-receipt")?.textContent ?? "";
    expect(receipt).toContain("Failed");
    expect(receipt).toContain("Model · —");
    expect(receipt).toContain("no model reply was served");
    expect(receipt).not.toMatch(/Model · [A-Za-z0-9]/);
  });

  it("Awaiting approval — the gate holds the action and the receipt says the turn is paused", async () => {
    stream([
      hop(),
      hop({
        name: "write.internal.crm.note",
        tier: 2,
        decision: "pending",
        approval_id: "approval-7c19",
        audit_seq: 4814,
      }),
      {
        event: "done",
        data: {
          type: "done",
          session_id: "sess-1",
          output: "A note is drafted and parked for approval.",
        },
      },
    ]);
    await ask("draft a CRM note for Bluepeak Dynamics");
    expect(
      await screen.findByText("A note is drafted and parked for approval."),
    ).toBeInTheDocument();

    const card = document.querySelector(".tr-gate") as HTMLElement;
    expect(within(card).getByText("Awaiting approval")).toBeInTheDocument();
    expect(within(card).getByText("approval-7c19")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Review in Approvals" })).toHaveAttribute(
      "href",
      "/approvals?request=approval-7c19",
    );
    // The console offers no decision control of its own: the approval is
    // recorded on the Approvals surface, under its own governed check.
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
    const receipt = document.querySelector(".tr-receipt")?.textContent ?? "";
    expect(receipt).toContain("Paused");
    expect(receipt).toContain("Awaiting a decision");
  });

  it("Long run — seven allowed steps read as one folded line", async () => {
    const names = [
      "read.crm.account_lookup",
      "read.contracts.search",
      "read.contracts.clauses",
      "read.tickets.search",
      "read.incidents.history",
      "read.knowledge.search",
      "read.graph.neighborhood",
    ];
    stream([
      ...names.map((name, index) => hop({ name, audit_seq: 5000 + index })),
      { event: "done", data: { type: "done", session_id: "sess-1", output: "Renewal brief." } },
    ]);
    await ask("prepare a renewal brief for Beta LLC");
    expect(await screen.findByText("Renewal brief.")).toBeInTheDocument();

    const fold = document.querySelector<HTMLDetailsElement>(".tr-work");
    expect(fold?.open).toBe(false);
    const summary = fold?.querySelector("summary")?.textContent ?? "";
    expect(summary).toContain("7 steps");
    expect(summary).toContain("7 allowed");
    for (const name of names) expect(summary).toContain(name);
    expect(document.querySelectorAll(".tr-work__steps .tr-step")).toHaveLength(7);

    // Opening it is one click, and it is the browser's own disclosure.
    await userEvent.click(fold?.querySelector("summary") as HTMLElement);
    expect(fold?.open).toBe(true);
  });
});
