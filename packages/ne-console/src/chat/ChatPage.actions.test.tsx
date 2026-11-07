// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// OWNER-GENERATION FENCING, at the callback seam.
//
// Every action this screen hands downwards is captured while one owner (client
// + served principal) is current, and may be invoked after the owner has been
// replaced — a handler already in flight, a retained closure in a child that
// has not re-rendered yet. None of them may act on the old owner's behalf, and
// none of them may act on the NEW owner's behalf either: a click Ada made is
// not a request Bob authorised.

import { act, render } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { Citation } from "../api/types";
import type { AgentStream, UiMessage } from "./useAgentStream";

const state = vi.hoisted(() => ({
  client: undefined as ApiClient | undefined,
  principal: {
    status: "ok" as const,
    principal: { id: "user:ada", groups: [] as string[] },
  },
  generation: "owner:ada",
  stream: undefined as AgentStream | undefined,
  input: undefined as Record<string, unknown> | undefined,
  messages: undefined as Record<string, unknown> | undefined,
}));

vi.mock("../auth/useApiClient", () => ({ useApiClient: () => state.client }));
vi.mock("../auth/PrincipalContext", () => ({
  principalOwnerReady: (owner: { principal: { status: string; principal: unknown } }) =>
    owner.principal.status === "ok" && owner.principal.principal !== null,
  usePrincipalOwner: () => ({
    client: state.client,
    principal: state.principal,
    generation: state.generation,
  }),
  usePrincipal: () => state.principal,
}));
vi.mock("../auth/useSurfaceAccess", () => ({
  useSurfaceAccess: () => ({ access: () => "unknown", refused: () => false }),
}));
vi.mock("../models/RunModelPicker", async () => {
  const React = await import("react");
  return {
    RunModelPicker: ({ onReadinessChange }: { onReadinessChange?: (ready: boolean) => void }) => {
      React.useLayoutEffect(() => onReadinessChange?.(true), [onReadinessChange]);
      return null;
    },
  };
});
vi.mock("./useAgentStream", () => ({ useAgentStream: () => state.stream }));
vi.mock("./MessageInput", async () => {
  const React = await import("react");
  return {
    MessageInput: (props: Record<string, unknown>) => {
      state.input = props;
      return React.createElement(React.Fragment, null, props.controls as React.ReactNode);
    },
  };
});
vi.mock("./MessageList", () => ({
  MessageList: (props: Record<string, unknown>) => {
    state.messages = props;
    return null;
  },
}));

import { ChatPage } from "./ChatPage";

function apiClient() {
  return {
    chatCapabilities: vi.fn(async () => ({ grounded: null })),
    signalThumbs: vi.fn(async () => ({ event_id: "thumb" })),
    signalWrongSource: vi.fn(async () => ({ event_id: "source" })),
  } as unknown as ApiClient;
}

function stream(): AgentStream {
  return {
    messages: [
      {
        id: "answer",
        role: "assistant",
        text: "served",
        citations: [],
        streaming: false,
        tools: [],
        sessionId: "ada-conversation",
        messageId: "ada-attachment",
        prompt: "ADA RETRY",
      },
    ],
    busy: false,
    refused: false,
    send: vi.fn(async () => "ada-conversation"),
    stop: vi.fn(),
    startedAt: null,
    lastSignalAt: null,
    loadThread: vi.fn(),
  };
}

function view() {
  return (
    <MemoryRouter initialEntries={["/agents/agt-ask"]}>
      <ChatPage />
    </MemoryRouter>
  );
}

beforeEach(() => {
  state.client = undefined;
  state.principal = { status: "ok", principal: { id: "user:ada", groups: [] } };
  state.generation = "owner:ada";
  state.stream = undefined;
  state.input = undefined;
  state.messages = undefined;
});

it.each([
  ["principal changes on the same client", "principal"],
  ["client changes for the same principal", "client"],
] as const)("makes every retained Ask action inert when the %s", async (_label, changed) => {
  const adaClient = apiClient();
  const adaStream = stream();
  state.client = adaClient;
  state.stream = adaStream;
  const rendered = render(view());
  await act(async () => undefined);
  const retainedInput = state.input;
  const retainedMessages = state.messages;
  expect(retainedInput).toBeDefined();
  expect(retainedMessages).toBeDefined();

  const bobClient = changed === "client" ? apiClient() : adaClient;
  const bobStream = stream();
  state.client = bobClient;
  if (changed === "principal") {
    state.principal = { status: "ok", principal: { id: "user:bob", groups: [] } };
  }
  state.generation = `owner:bob-${changed}`;
  state.stream = bobStream;
  rendered.rerender(view());

  // The SELECTION effect legitimately clears the thread for the new owner, so
  // the baseline is taken after the handover: what is measured below is what
  // the RETAINED callbacks did, not what the render did.
  const adaLoads = vi.mocked(adaStream.loadThread).mock.calls.length;
  const bobLoads = vi.mocked(bobStream.loadThread).mock.calls.length;

  const message = adaStream.messages[0] as UiMessage;
  const citation = { chunk_id: "ada-source" } as Citation;
  await act(async () => {
    (retainedInput?.onSubmit as (prompt: string) => void)("ADA ASK");
    (retainedMessages?.onRetry as (prompt: string) => void)("ADA RETRY");
    await (retainedMessages?.onThumbs as (value: UiMessage, up: boolean) => Promise<void>)(
      message,
      true,
    );
    await (retainedMessages?.onFlagSource as (value: UiMessage, source: Citation) => Promise<void>)(
      message,
      citation,
    );
    (retainedMessages?.onViewSource as (value: Citation) => void)(citation);
  });

  expect(adaStream.send).not.toHaveBeenCalled();
  expect(adaClient.signalThumbs).not.toHaveBeenCalled();
  expect(adaClient.signalWrongSource).not.toHaveBeenCalled();
  expect(bobStream.send).not.toHaveBeenCalled();
  expect(bobStream.stop).not.toHaveBeenCalled();
  expect(bobClient.signalThumbs).not.toHaveBeenCalled();
  expect(bobClient.signalWrongSource).not.toHaveBeenCalled();
  expect(vi.mocked(adaStream.loadThread).mock.calls).toHaveLength(adaLoads);
  expect(vi.mocked(bobStream.loadThread).mock.calls).toHaveLength(bobLoads);
  // A retained citation cannot open a pane on the new owner's screen either.
  expect(document.querySelector(".ne-detail")).toBeNull();
});
