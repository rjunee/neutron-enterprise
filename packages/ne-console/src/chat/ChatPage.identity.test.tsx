// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NO OLD-OWNER BYTE SURVIVES A CHANGE OF OWNER.
//
// The owner is client + served principal, and either half may change under a
// mounted thread. Every byte this screen is holding at that moment belongs to
// the owner that was served it — the answer in the thread, the half-typed
// question in the composer, the source record in the docked pane — and none of
// it may still be on screen for the next owner. Nor may a read that was ALREADY
// IN FLIGHT for the old owner land afterwards and paint their bytes into the
// new owner's session: a served answer is not the only leak, a late one is.
//
// (The thread's own history list is the workspace sidebar's, and is covered by
// the sidebar's tests; this file exercises the thread ChatPage renders.)

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Citation, RetrievalResult, StreamEvent } from "../api/types";

const hookState = vi.hoisted(() => ({
  client: undefined as ApiClient | undefined,
  principal: {
    status: "ok" as const,
    principal: { id: "user:ada", groups: [] as string[] },
  },
  generation: "owner:ada",
}));

vi.mock("../auth/useApiClient", () => ({ useApiClient: () => hookState.client }));
vi.mock("../auth/PrincipalContext", () => ({
  principalOwnerReady: (owner: { principal: { status: string; principal: unknown } }) =>
    owner.principal.status === "ok" && owner.principal.principal !== null,
  usePrincipalOwner: () => ({
    client: hookState.client,
    principal: hookState.principal,
    generation: hookState.generation,
  }),
  usePrincipal: () => hookState.principal,
}));
vi.mock("../auth/useSurfaceAccess", () => ({
  useSurfaceAccess: () => ({ access: () => "unknown", refused: () => false }),
}));

import { ChatPage } from "./ChatPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const catalog = {
  surface: "ask" as const,
  task: "reason",
  effective_default: "private-default",
  choices: [
    {
      model: "private-default",
      provider: "vllm",
      lane: "private-vllm" as const,
      evidence: "probe",
      observed_at: "2026-08-21T12:00:00Z",
    },
  ],
};

const CITATIONS: Citation[] = [
  {
    chunk_id: "ada-chunk",
    document_id: "ada-doc",
    source_system: "sharepoint",
    source_ref: "ada/ref",
    source_url: "https://sources.invalid/ada",
  },
  {
    chunk_id: "ada-late-chunk",
    document_id: "ada-late-doc",
    source_system: "sharepoint",
    source_ref: "ada/late-ref",
  },
];

const ADA_RECORD: RetrievalResult = {
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
  graph_paths: [
    {
      hops: [
        {
          subject: {
            entity_class: "Document",
            entity_key: "ada-doc",
            display: "Ada document",
            assurance: "source",
            source_ref: "ada/ref",
          },
          edge_kind: "mentions",
          obj: {
            entity_class: "Party",
            entity_key: "party-1",
            display: "Party 1",
            assurance: "source",
            source_ref: "another/ref",
          },
          source_system: "sharepoint",
          source_ref: "edge/ref",
          assurance: "source",
        },
      ],
    },
  ],
  provisional_source_count: 0,
} as unknown as RetrievalResult;

/** A client whose whole chat surface is the two calls a turn actually makes.
 * The turn streams a citations frame and its terminal, so the thread, the
 * citation chips and the docked source pane are all real render paths. */
function chatClient(options: {
  answer: () => string;
  retrieve: (chunkIds: string[]) => Promise<RetrievalResult>;
}): ApiClient {
  return {
    chatCapabilities: vi.fn(async () => ({ grounded: null })),
    modelChoices: vi.fn(async () => catalog),
    createSession: vi.fn(async () => ({
      session_id: "conversation-1",
      agent_name: "console-chat",
    })),
    sendMessage: vi.fn(
      async (
        _sessionId: string,
        _prompt: string,
        onEvent: (event: StreamEvent) => void,
      ): Promise<void> => {
        onEvent({ type: "citations", citations: CITATIONS } as StreamEvent);
        onEvent({
          type: "done",
          session_id: "conversation-1",
          output: options.answer(),
        } as StreamEvent);
      },
    ),
    retrieve: vi.fn((chunkIds: string[]) => options.retrieve(chunkIds)),
  } as unknown as ApiClient;
}

function view() {
  return (
    <MemoryRouter initialEntries={["/agents/agt-ask"]}>
      <ChatPage />
    </MemoryRouter>
  );
}

async function askOnce(prompt: string) {
  await screen.findByRole("combobox", { name: "Run model" });
  await userEvent.type(screen.getByPlaceholderText("Ask a question"), prompt);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

beforeEach(() => {
  hookState.client = undefined;
  hookState.principal = { status: "ok", principal: { id: "user:ada", groups: [] } };
  hookState.generation = "owner:ada";
});

it.each([
  ["the client changes for the same principal", "client"],
  ["the principal changes on one client", "principal"],
] as const)("withdraws every served Ask/Source byte when %s", async (_label, changed) => {
  const lateRetrieve = deferred<RetrievalResult>();
  let served = "ADA-ANSWER-SENTINEL";
  const retrieve = (chunkIds: string[]) =>
    chunkIds.includes("ada-late-chunk") ? lateRetrieve.promise : Promise.resolve(ADA_RECORD);
  const adaClient = chatClient({ answer: () => served, retrieve });
  hookState.client = adaClient;
  const rendered = render(view());

  await askOnce("ADA-QUESTION-SENTINEL");
  expect(await screen.findByText("ADA-ANSWER-SENTINEL")).toBeInTheDocument();

  await userEvent.type(screen.getByPlaceholderText("Ask a question"), "ADA-DRAFT-SENTINEL");
  await userEvent.click(screen.getByLabelText("View source for citation 1"));
  expect(await screen.findByText("ADA-SOURCE-BODY-SENTINEL")).toBeInTheDocument();
  expect(screen.getByText("ADA-SOURCE-METADATA-SENTINEL")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open in source system" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "View in Graph" })).toBeInTheDocument();

  // A second citation whose governed retrieve is STILL OPEN at the handover.
  await userEvent.click(screen.getByRole("button", { name: "Close" }));
  await userEvent.click(screen.getByLabelText("View source for citation 2"));

  served = "BOB-ANSWER-SENTINEL";
  const bobClient =
    changed === "client" ? chatClient({ answer: () => served, retrieve }) : adaClient;
  hookState.client = bobClient;
  if (changed === "principal") {
    hookState.principal = { status: "ok", principal: { id: "user:bob", groups: [] } };
  }
  hookState.generation = `owner:bob-${changed}`;
  rendered.rerender(view());

  // Synchronously, in the render that observes the change — not after a fetch.
  expect(screen.queryByText("ADA-QUESTION-SENTINEL")).not.toBeInTheDocument();
  expect(screen.queryByText("ADA-ANSWER-SENTINEL")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("ADA-DRAFT-SENTINEL")).not.toBeInTheDocument();
  expect(screen.queryByText("ADA-SOURCE-BODY-SENTINEL")).not.toBeInTheDocument();
  expect(screen.queryByText("ADA-SOURCE-METADATA-SENTINEL")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open in source system" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "View in Graph" })).not.toBeInTheDocument();
  expect(screen.getByText("Ask a question about your connected sources.")).toBeInTheDocument();

  // The old owner's in-flight read lands late. It has nowhere to land.
  lateRetrieve.resolve({
    chunks: [
      {
        chunk: {
          chunk_id: "ada-late-chunk",
          document_id: "ada-late-doc",
          text: "ADA-LATE-SOURCE-SENTINEL",
          metadata: { owner: "ADA-LATE-METADATA-SENTINEL" },
        },
        score: 1,
      },
    ],
    citations: [],
    graph_paths: [],
    provisional_source_count: 0,
  } as unknown as RetrievalResult);

  await askOnce("BOB-QUESTION");
  await waitFor(() => expect(screen.getByText("BOB-ANSWER-SENTINEL")).toBeInTheDocument());
  expect(document.body.textContent).not.toContain("ADA-");
});
