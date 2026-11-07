// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { AuthContext } from "react-oidc-context";
import type { AuthContextProps } from "react-oidc-context";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { RuntimeConfigContext } from "./AppBootstrap";
import { dismissTour } from "./demo/tourStorage";
import { makeAuthValue } from "./test/authStub";
import { DEFAULT_ASK_AGENT_ID } from "./api/types";
import { PRINCIPAL, RUNTIME_CONFIG } from "./test/mocks/fixtures";
import { server } from "./test/mocks/server";

function auth(token: string, sub: string, authenticated = true): AuthContextProps {
  return makeAuthValue({
    isAuthenticated: authenticated,
    user: authenticated
      ? ({ access_token: token, profile: { sub } } as AuthContextProps["user"])
      : null,
    signinRedirect: vi.fn(async () => undefined),
  });
}

function app(value: AuthContextProps) {
  return (
    <AuthContext.Provider value={value}>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthContext.Provider>
  );
}

/** Ask's route since 2026-08-27: the workspace of the Agent it always ran.
 * `/ask` still resolves here by redirect (pinned in rail/shell.test.tsx); this
 * suite uses the settled URL because it drives history back/forward and a
 * `<Navigate replace>` hop is not a history entry to return to. */
const ASK_ROUTE = `/agents/${DEFAULT_ASK_AGENT_ID}`;

/** The composer's accessible name is now the AGENT's, because the thread is the
 * screen and the composer says which agent it is talking to
 * (chat/MessageInput.tsx takes its `aria-label` from the placeholder, and
 * ChatPage composes that from the served `agentLabel`). Held as a constant so a
 * label change is one edit, not eight. */
const ASK_COMPOSER = "Ask Meridian Analyst…";

/** The Agent the workspace reads before it can mount its Chat tab. */
const ASK_AGENT = {
  agent_id: DEFAULT_ASK_AGENT_ID,
  version: 4,
  label: "Meridian Analyst",
  description: "Governed Meridian servicing analysis",
  owner_label: "Meridian Servicing Operations",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "agent.meridian-analyst.v2",
  tool_grants: [],
  knowledge_sources: [],
  skills: [],
  budgets: {
    max_steps: 12,
    max_tool_calls: 10,
    max_input_tokens: 24000,
    max_output_tokens: 4000,
  },
  enforced_caps: {
    max_tool_calls: 10,
    max_model_requests: 11,
    wall_clock_seconds: 120,
    max_total_tokens: 28000,
    max_parked_writes: 1,
  },
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: "1".repeat(64),
};

function bearer(request: Request): string | null {
  return request.headers.get("authorization");
}

afterEach(() => {
  window.history.replaceState({}, "", "/");
});

// A WHOLE-APP INTEGRATION TEST ON A UNIT TEST'S CLOCK — that was the defect.
//
// The single case below mounts the entire authenticated console FIVE times and
// drives a streamed Ask turn, a citation open, a logout, a principal switch,
// three history traversals and a remount through it. vitest's default per-test
// budget is 5000ms, and the case did not fit inside it with any margin: on this
// 16-core host under a plain parallel `vitest run` it measured 4.3-5.3s and
// failed about one run in six, always as `Test timed out in 5000ms` and never
// as a failed assertion. It was the suite's slowest case by a wide margin (next
// slowest: 3.7s) and the only one that crossed 5000ms at moderate load.
//
// It is NOT a leak, a race, or an order dependency. It reproduces standalone
// (`vitest run src/App.askIdentity.test.tsx`), and it behaves identically at the
// commit BEFORE the Documents/Graph train — nothing in the product regressed.
// The clock was the wrong size for the work. That matters beyond this file: a
// red CI run that means nothing is worse than a slow one, because it teaches
// everyone to wave red through on the day a real regression lands.
//
// The budget is therefore sized to the work — roughly 6x the case's measured
// cost after the change below (2.2-2.9s in-suite on an unloaded box, 2.4-3.9s
// on a loaded one). Scheduling noise cannot reach it; a genuine hang still
// trips it, in 15s rather than never. If this case grows, re-measure rather
// than raising this again.
vi.setConfig({ testTimeout: 15_000 });

it("keeps the real authenticated Ask route principal-clean across logout, navigation, and remount", async () => {
  dismissTour();
  window.history.replaceState({}, "", ASK_ROUTE);
  let bobCatalogResolve!: (response: Response) => void;
  let bobPhase = false;
  const postSwitchAuthorizations: Array<string | null> = [];
  const observe = (request: Request) => {
    if (bobPhase) postSwitchAuthorizations.push(bearer(request));
  };

  server.use(
    http.get("*/api/v1/agents", ({ request }) => {
      observe(request);
      return HttpResponse.json({ items: [ASK_AGENT], next_cursor: null });
    }),
    http.get("*/api/v1/agents/:agentId", ({ request }) => {
      observe(request);
      return HttpResponse.json(ASK_AGENT);
    }),
    http.get("*/api/v1/agents/:agentId/preflight", ({ request }) => {
      observe(request);
      return HttpResponse.json({
        agent_id: ASK_AGENT.agent_id,
        version: ASK_AGENT.version,
        definition_hash: ASK_AGENT.config_hash,
        available: true,
        availability_reasons: [],
        capabilities: {
          agent_version: ASK_AGENT.version,
          agent_definition_hash: ASK_AGENT.config_hash,
          tools: [],
          connectors: [],
          capability_hash: "c".repeat(64),
        },
        model: {
          task: ASK_AGENT.model_policy_id,
          allowed_strategies: ["agentic"],
          default_strategy: "agentic",
          max_steps: 12,
          max_tool_calls: 10,
          max_input_tokens: 24000,
          max_output_tokens: 4000,
          candidates: [],
          default_model: null,
        },
      });
    }),
    http.get("*/api/v1/me", ({ request }) => {
      observe(request);
      return HttpResponse.json({
        ...PRINCIPAL,
        id: bearer(request) === "Bearer ada-token" ? "user:ada" : "user:bob",
      });
    }),
    http.get("*/api/v1/chat/capabilities", ({ request }) => {
      observe(request);
      return HttpResponse.json({ grounded: null });
    }),
    http.get("*/api/v1/models/choices/ask", ({ request }) => {
      observe(request);
      if (bearer(request) === "Bearer bob-token") {
        return new Promise<Response>((resolve) => {
          bobCatalogResolve = resolve;
        });
      }
      return HttpResponse.json({
        surface: "ask",
        task: "reason",
        effective_default: "ADA-MODEL-SENTINEL",
        choices: [
          {
            model: "ADA-MODEL-SENTINEL",
            provider: "vllm",
            lane: "private-vllm",
            evidence: "ada-private-probe",
            observed_at: "2026-08-21T18:00:00Z",
          },
        ],
      });
    }),
    // THE SHELL-CHROME PROBE. Not the streamed answer and not the model
    // catalog: a principal-scoped read whose text the PERSISTENT frame renders
    // — the workspace sidebar's conversation history, which is chrome, survives
    // route changes, and is re-read per principal. That property is what this
    // case is about, and it needs a probe that is not part of the thread.
    //
    // It replaces the monitored-answer link that used to be pinned to this
    // sidebar's foot and used to carry the sentinel. That link pinned a whole
    // question above the history and is gone (the answer ledger is reached from
    // the conversation ledger now); the history it was covering is the better
    // probe anyway, because it is the surface a principal switch MUST blank and
    // the one a reader would notice if it did not.
    http.get("*/api/v1/agents/:agentId/conversations", ({ request }) => {
      observe(request);
      const ada = bearer(request) === "Bearer ada-token";
      return HttpResponse.json({
        items: [
          {
            conversation_id: ada ? "ada-conversation" : "bob-conversation",
            agent_id: DEFAULT_ASK_AGENT_ID,
            title: ada ? "ADA-CONVERSATION-SENTINEL" : "BOB-CONVERSATION",
            created_at: "2026-08-21T18:00:00Z",
            updated_at: "2026-08-21T18:00:00Z",
            last_sequence: 1,
          },
        ],
        next_cursor: null,
      });
    }),
    http.post("*/api/v1/chat/sessions", ({ request }) => {
      observe(request);
      return HttpResponse.json({ session_id: "ada-session", agent_name: "console-chat" });
    }),
    http.post("*/api/v1/chat/sessions/:id/messages", ({ request, params }) => {
      observe(request);
      if (bearer(request) === "Bearer bob-token" && params.id === "ada-session") {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      const body = [
        'event: citations\ndata: {"type":"citations","citations":[{"chunk_id":"ada-chunk","document_id":"ada-doc","source_system":"sharepoint","source_ref":"ada/ref"}]}',
        'event: done\ndata: {"type":"done","session_id":"ada-session","output":"ADA-ANSWER-SENTINEL","message_id":"ada-turn"}',
        "",
      ].join("\n\n");
      return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
    }),
    http.post("*/api/v1/brain/retrieve", ({ request }) => {
      observe(request);
      return HttpResponse.json({
        chunks: [
          {
            chunk: {
              chunk_id: "ada-chunk",
              document_id: "ada-doc",
              text: "ADA-SOURCE-BODY-SENTINEL",
              metadata: { owner: "ADA-SOURCE-METADATA-SENTINEL" },
            },
            score: 1,
          },
        ],
        citations: [],
        graph_paths: [],
        provisional_source_count: 0,
      });
    }),
  );

  const ada = auth("ada-token", "user:ada");
  const rendered = render(app(ada));
  expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
    "ADA-MODEL-SENTINEL",
  );
  expect(await screen.findByText(/ADA-CONVERSATION-SENTINEL/)).toBeInTheDocument();
  // PASTED, not typed, and the difference is measured. userEvent.type dispatches
  // one keystroke per character and yields to the event loop between them, so
  // this one line re-rendered the whole authenticated shell 17 times and cost
  // ~590ms of the case's budget when the box was quiet — and disproportionately
  // more on a loaded one, because every yield competes with every other worker.
  // Removing it took the case from 4.3-5.3s to 2.1-2.4s at the same load. No
  // assertion here reads the composed text; it only has to be non-empty so that
  // Ask enables. Per-keystroke composer behaviour is chat/MessageInput.test.tsx's
  // job, and it still does it.
  //
  // The composer's accessible name is ASK_COMPOSER, not the literal "Ask a
  // question": the Round 4 composer takes its placeholder from the AGENT it is
  // addressing ("Ask Meridian Analyst…"), which is the whole point of the
  // thread being the agent's screen.
  await userEvent.click(screen.getByLabelText(ASK_COMPOSER));
  await userEvent.paste("show Ada's answer");
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("ADA-ANSWER-SENTINEL")).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText("View source for citation 1"));
  expect(await screen.findByText("ADA-SOURCE-BODY-SENTINEL")).toBeInTheDocument();

  rendered.rerender(app(auth("", "", false)));
  expect(document.body.textContent).not.toContain("ADA-");
  rendered.unmount();

  bobPhase = true;
  window.history.pushState({}, "", ASK_ROUTE);
  const bob = auth("bob-token", "user:bob");
  const bobView = render(app(bob));
  // FIRST PAINT ASSERTS NOTHING ABOUT ADA -- the property this whole test
  // exists for, checked synchronously, before any await can let a render settle.
  expect(document.body.textContent).not.toContain("ADA-");
  // The composer now waits one read longer than it used to: the workspace
  // resolves WHICH Agent this is before mounting its Chat tab, so the model
  // picker's loading state is awaited rather than demanded on the first tick.
  // What it must never do is arrive already holding the previous principal's
  // catalog, which is what the two ADA- assertions around it pin.
  expect(await screen.findByText("Loading model choices…")).toHaveAttribute("role", "status");
  expect(document.body.textContent).not.toContain("ADA-");
  await waitFor(() => expect(bobCatalogResolve).toBeTypeOf("function"));
  bobCatalogResolve(
    HttpResponse.json({
      surface: "ask",
      task: "reason",
      effective_default: "BOB-MODEL",
      choices: [
        {
          model: "BOB-MODEL",
          provider: "vllm",
          lane: "private-vllm",
          evidence: "bob-private-probe",
          observed_at: "2026-08-21T18:01:00Z",
        },
      ],
    }),
  );
  expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue("BOB-MODEL");
  expect(document.body.textContent).not.toContain("ADA-");

  await userEvent.click(screen.getByRole("link", { name: "Search" }));
  expect(await screen.findByRole("heading", { name: "Search" })).toBeInTheDocument();
  await act(async () => {
    window.history.back();
  });
  await waitFor(() => expect(window.location.pathname).toBe(ASK_ROUTE));
  expect(document.body.textContent).not.toContain("ADA-");
  await act(async () => {
    window.history.forward();
  });
  await waitFor(() => expect(window.location.pathname).toBe("/search"));
  await act(async () => {
    window.history.back();
  });
  await waitFor(() => expect(window.location.pathname).toBe(ASK_ROUTE));
  expect(document.body.textContent).not.toContain("ADA-");

  bobView.unmount();
  render(app(bob));
  expect(await screen.findByRole("heading", { name: "Meridian Analyst" })).toBeInTheDocument();
  expect(document.body.textContent).not.toContain("ADA-");
  expect(postSwitchAuthorizations.length).toBeGreaterThan(0);
  expect(new Set(postSwitchAuthorizations)).toEqual(new Set(["Bearer bob-token"]));
});
