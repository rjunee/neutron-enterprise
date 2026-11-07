// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-MODEL-ROUTING — the Routing tab.
//
// C7 IS THE PROPERTY UNDER TEST, and here it is load-bearing rather than
// decorative: which models a task may be repointed to is a server judgement
// over the boot model set, the gateway's budget arithmetic, the tier table and
// the live egress posture. Every assertion below is therefore about the console
// obeying the server's `selectable`, `reason`, `action` and `tier` — and the
// bidirectional controls prove it is obeying rather than coinciding: the SAME
// payload with `selectable` flipped produces the opposite menu.
//
// The other property: a PENDING change must not render as live. A console that
// showed the requested model as "serving now" would be asserting a route the
// server is not using.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ModelRoutingView, TaskRoutingView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { RoutingTab } from "./RoutingTab";

const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

const CPU_TARGET = {
  model: "aux-model",
  lane: "private-vllm",
  action: "write.internal.model.routing.repoint",
  tier: 2,
  selectable: true,
  reason: null,
};
const FRONTIER_TARGET = {
  model: "frontier-best",
  lane: "frontier-api",
  action: "write.external.model.routing.repoint",
  tier: 2,
  selectable: true,
  reason: null,
};
// A target the SERVER marked unselectable, carrying the server's own sentence.
// The reason is a real one `refusal_for_target` still emits (the air-gap arm, so
// the target sits on the frontier lane and rides the EXTERNAL action). It used
// to quote the input-budget floor, which NE-CTX-IN-FORCE deleted — and a fixture
// quoting a message the server can no longer produce is a test pinned to
// nothing.
const TINY_REFUSED = {
  model: "tiny-model",
  lane: "frontier-api",
  action: "write.external.model.routing.repoint",
  tier: 2,
  selectable: false,
  reason:
    "model 'tiny-model' is on the frontier lane and the air gap is engaged; " +
    "the egress guard would refuse every call on this route. To restore " +
    "frontier routing, FIRST lift the air gap, THEN repoint this task",
};

function routing(overrides: Partial<TaskRoutingView> = {}): ModelRoutingView {
  return {
    tasks: [
      {
        task: "agent",
        model: "private-default",
        lane: "private-vllm",
        available: true,
        unavailable_reason: null,
        default_model: "private-default",
        source: "default",
        changed_by: null,
        approved_by: null,
        changed_at: null,
        pending_change: null,
        targets: [CPU_TARGET, FRONTIER_TARGET, TINY_REFUSED],
        ...overrides,
      },
    ],
  };
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/models?tab=routing"]}>
        <RoutingTab />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

async function openAgentRow(user: ReturnType<typeof userEvent.setup>) {
  const row = await screen.findByText("agent");
  await user.click(row);
}

describe("NE-MODEL-ROUTING — the Routing tab", () => {
  beforeEach(() => {
    stub.client = { modelRouting: () => Promise.resolve(routing()) };
  });

  it("renders the configured available route and the deployment default beside it", async () => {
    const user = setup();
    await openAgentRow(user);
    expect(await screen.findByText("Shipped with")).toBeInTheDocument();
    // Two facts, deliberately both: the running model and what shipped — and
    // the source says which of the two is in force.
    expect(screen.getAllByText("private-default").length).toBe(3);
    expect(screen.getAllByText("Deployment default").length).toBe(2);
  });

  it("retains an unavailable route without claiming it is serving and renders the server reason", async () => {
    stub.client = {
      modelRouting: () =>
        Promise.resolve(
          routing({
            available: false,
            unavailable_reason: "private-default is stopped by the inference operator",
          }),
        ),
    };
    const user = setup();
    expect(await screen.findByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.queryByText("SERVING")).toBeNull();
    await openAgentRow(user);
    expect(
      await screen.findByText("private-default is stopped by the inference operator"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Configured route").length).toBeGreaterThan(0);
  });

  it("offers ONLY the targets the server marked selectable", async () => {
    const user = setup();
    await openAgentRow(user);
    const select = (await screen.findByLabelText("New model")) as HTMLSelectElement;
    const offered = Array.from(select.options).map((option) => option.value);
    expect(offered).toEqual(["aux-model", "frontier-best"]);
    expect(offered).not.toContain("tiny-model");
  });

  it("does NOT offer a target the server refused, and shows the server's reason instead", async () => {
    const user = setup();
    await openAgentRow(user);
    // The refusal is EXPLAINED, verbatim — a missing option is never silent.
    expect(await screen.findByText(/FIRST lift the air gap, THEN repoint/)).toBeInTheDocument();
  });

  it("CONTROL — the same payload with selectable flipped offers the previously refused target", async () => {
    // The bidirectional half: nothing about this console decides that a
    // small-window model is unroutable. Flip the SERVER's flag and the menu
    // flips with it.
    stub.client = {
      modelRouting: () =>
        Promise.resolve(
          routing({
            targets: [CPU_TARGET, { ...TINY_REFUSED, selectable: true, reason: null }],
          }),
        ),
    };
    const user = setup();
    await openAgentRow(user);
    const select = (await screen.findByLabelText("New model")) as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "aux-model",
      "tiny-model",
    ]);
  });

  it("names the consequence before the click, and names it differently for a frontier target", async () => {
    const user = setup();
    await openAgentRow(user);
    // In-VPC target: the prompt stays inside.
    expect(await screen.findByText(/stays in-VPC/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("New model"), "frontier-best");
    expect(
      await screen.findByText(/prompts for this task will leave the deployment/),
    ).toBeInTheDocument();
  });

  it("submits the task and the model ALIAS, and nothing else", async () => {
    const setModelRoute = vi.fn(() =>
      Promise.resolve({ status: "pending" as const, request_id: "req-1" }),
    );
    stub.client = { modelRouting: () => Promise.resolve(routing()), setModelRoute };
    const user = setup();
    await openAgentRow(user);
    await user.click(await screen.findByRole("button", { name: "Request change" }));
    expect(setModelRoute).toHaveBeenCalledWith("agent", "aux-model");
  });

  it("a PENDING change does not render as the serving model", async () => {
    // The server answers pending and keeps reporting the OLD model — which is
    // the truth, because the route has not changed. The console must not
    // anticipate it.
    const setModelRoute = vi.fn(() =>
      Promise.resolve({ status: "pending" as const, request_id: "req-1" }),
    );
    stub.client = { modelRouting: () => Promise.resolve(routing()), setModelRoute };
    const user = setup();
    await openAgentRow(user);
    await user.click(await screen.findByRole("button", { name: "Request change" }));
    expect((await screen.findAllByText("Deployment default")).length).toBe(2);
    expect(screen.queryByText("Governed change")).toBeNull();
  });

  it("renders the server's refusal verbatim on a 409, and invents none of its own", async () => {
    const setModelRoute = vi.fn(() =>
      Promise.reject(
        // Another real `refusal_for_target` sentence (the no-op arm), chosen so
        // it cannot collide with the catalog row's own refusal above.
        new ApiError(
          "conflict",
          409,
          "task 'agent' already routes to 'private-default'; nothing to change",
        ),
      ),
    );
    stub.client = { modelRouting: () => Promise.resolve(routing()), setModelRoute };
    const user = setup();
    await openAgentRow(user);
    await user.click(await screen.findByRole("button", { name: "Request change" }));
    expect(
      await screen.findByText(/already routes to 'private-default'; nothing to change/),
    ).toBeInTheDocument();
  });

  it("says so IN ITS OWN VOICE when a 409 carries no reason at all", async () => {
    // The same refusal with no served `detail` — a proxy answering 409 with no
    // JSON body. The console's §2.4 sentence used to land here, in the slot
    // that renders as the server's refusal, so the screen attributed "Someone
    // changed this first." to a server that said nothing.
    const setModelRoute = vi.fn(() => Promise.reject(new ApiError("conflict", 409)));
    stub.client = { modelRouting: () => Promise.resolve(routing()), setModelRoute };
    const user = setup();
    await openAgentRow(user);
    await user.click(await screen.findByRole("button", { name: "Request change" }));
    expect(
      await screen.findByText("The route wasn’t changed; the server stated no reason."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Someone changed this first.")).toBeNull();
  });

  it("states a 403 the gate gave no reason for, and writes none for it", async () => {
    const setModelRoute = vi.fn(() => Promise.reject(new ApiError("forbidden", 403)));
    stub.client = { modelRouting: () => Promise.resolve(routing()), setModelRoute };
    const user = setup();
    await openAgentRow(user);
    await user.click(await screen.findByRole("button", { name: "Request change" }));
    expect(await screen.findByText("Denied by policy.")).toBeInTheDocument();
    expect(screen.queryByText("Your role doesn’t include this action.")).toBeNull();
  });

  it("renders the SERVER's refusal on a read, never a console-decided one", async () => {
    stub.client = {
      modelRouting: () => Promise.reject(new ApiError("forbidden", 403, "not authorized")),
    };
    setup();
    expect(await screen.findByText(/model routing/i)).toBeInTheDocument();
  });

  it("shows who requested and who approved a governed change", async () => {
    stub.client = {
      modelRouting: () =>
        Promise.resolve(
          routing({
            model: "aux-model",
            source: "override",
            changed_by: "user:ops-olga",
            approved_by: "user:approver-ada",
            changed_at: "2026-08-05T12:00:00Z",
          }),
        ),
    };
    const user = setup();
    await openAgentRow(user);
    expect(await screen.findByText("user:ops-olga")).toBeInTheDocument();
    expect(screen.getByText("user:approver-ada")).toBeInTheDocument();
    expect(screen.getAllByText("Governed change").length).toBe(2);
  });

  it("offers no change at all when the server marks every target unselectable", async () => {
    stub.client = {
      modelRouting: () =>
        Promise.resolve(
          routing({
            targets: [
              { ...CPU_TARGET, selectable: false, reason: "already routes there" },
              TINY_REFUSED,
            ],
          }),
        ),
    };
    const user = setup();
    await openAgentRow(user);
    expect(
      await screen.findByText("No other model on this deployment can serve this task."),
    ).toBeInTheDocument();
    // Absent, not disabled — the console offers no affordance for a change the
    // server would refuse.
    expect(screen.queryByRole("button", { name: "Request change" })).toBeNull();
  });
});
