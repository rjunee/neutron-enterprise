// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { User } from "oidc-client-ts";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { GraphNodeWire, GraphPathsWire } from "../../api/types";
import { AuthStub } from "../../test/authStub";
import { server } from "../../test/mocks/server";
import { GraphPathsFinder } from "./GraphPathsFinder";

function node(key: string, name: string): GraphNodeWire {
  return {
    key,
    entity_class: "Account",
    display_name: name,
    source_system: "crm",
    source_ref: `crm/${key}`,
    properties: {},
    assurance: "source",
  };
}

const PATHS: GraphPathsWire = {
  paths: [
    {
      nodes: [node("acct/a", "Alpha"), node("acct/b", "Beta"), node("acct/c", "Gamma")],
      edges: [
        {
          key: "edge-2",
          edge_kind: "SUPPLIES",
          from_key: "acct/b",
          to_key: "acct/c",
          source_system: "crm",
          source_ref: "crm/edge-2",
          assurance: "source",
        },
        {
          key: "edge-1",
          edge_kind: "OWNS",
          from_key: "acct/a",
          to_key: "acct/b",
          source_system: "crm",
          source_ref: "crm/edge-1",
          assurance: "confirmed",
        },
      ],
    },
  ],
  truncated: true,
};

const entityHref = vi.fn(
  (key: string, mode: "graph" | "record") =>
    `/knowledge/graph?entity=${encodeURIComponent(key)}&mode=${mode}`,
);

function View({
  from = "acct/a",
  user,
  href = entityHref,
}: {
  from?: string;
  user?: User;
  href?: typeof entityHref;
}) {
  return (
    <AuthStub value={user === undefined ? undefined : { user }}>
      <MemoryRouter>
        <GraphPathsFinder from={from} entityHref={href} />
      </MemoryRouter>
    </AuthStub>
  );
}

async function openAndSubmit(target = "acct/c") {
  await userEvent.click(screen.getByRole("button", { name: "Paths to another entity…" }));
  await userEvent.type(screen.getByRole("textbox", { name: "Entity key" }), target);
  await userEvent.click(screen.getByRole("button", { name: "Find paths" }));
}

describe("GraphPathsFinder", () => {
  it("requests the bounded path contract and preserves served edge order, direction, and labels", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(PATHS);
      }),
    );
    render(<View />);
    await openAndSubmit();

    const edgeList = await screen.findByRole("list", { name: "Path relationships" });
    expect(body).toEqual({ from: "acct/a", to: "acct/c", max_hops: 4, limit: 20 });
    expect(
      within(edgeList)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Beta — SUPPLIES →  SUPPLIES, directed to Gamma",
      "Alpha — OWNS →  OWNS, directed to Beta",
    ]);
    expect(within(edgeList).getByRole("link", { name: "Alpha" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=acct%2Fa&mode=record",
    );
    expect(within(edgeList).getByRole("link", { name: "Gamma" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=acct%2Fc&mode=record",
    );
    expect(
      screen.getByText(
        "These path results were capped by the server. More visible paths may exist.",
      ),
    ).toHaveAttribute("role", "status");
  });

  it("sends an opaque destination exactly, including leading and trailing whitespace", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ paths: [], truncated: false });
      }),
    );
    render(<View />);
    await userEvent.click(screen.getByRole("button", { name: "Paths to another entity…" }));
    const input = screen.getByRole("textbox", { name: "Entity key" });
    await userEvent.type(input, "  opaque/key  ");
    await userEvent.click(screen.getByRole("button", { name: "Find paths" }));
    await screen.findByText("No visible path was returned.");
    expect(body).toMatchObject({ from: "acct/a", to: "  opaque/key  " });
  });

  it.each([
    [403, /Your role doesn.t include graph paths/],
    [404, "The knowledge graph isn't enabled on this deployment."],
    [500, "Couldn’t load paths."],
  ])("keeps path %i truth distinct", async (status, message) => {
    server.use(
      http.post("*/api/v1/graph/paths", () =>
        HttpResponse.json({ detail: "read failed" }, { status }),
      ),
    );
    render(<View />);
    await openAndSubmit();
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("renders a truthful empty and retries a broken path read", async () => {
    let reads = 0;
    server.use(
      http.post("*/api/v1/graph/paths", () => {
        reads += 1;
        return reads === 1
          ? HttpResponse.json({ detail: "boom" }, { status: 500 })
          : HttpResponse.json({ paths: [], truncated: false });
      }),
    );
    render(<View />);
    await openAndSubmit("acct/none");
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No visible path was returned.")).toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it("does not let a late result from the previous source overwrite the current path", async () => {
    let releaseOld: (() => void) | undefined;
    let markOldStarted: (() => void) | undefined;
    const oldGate = new Promise<void>((resolve) => {
      releaseOld = resolve;
    });
    const oldStarted = new Promise<void>((resolve) => {
      markOldStarted = resolve;
    });
    server.use(
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        const body = (await request.json()) as { from: string };
        if (body.from === "old") {
          markOldStarted?.();
          await oldGate;
          return HttpResponse.json({
            paths: [{ nodes: [node("old", "Old result")], edges: [] }],
            truncated: false,
          });
        }
        return HttpResponse.json({
          paths: [{ nodes: [node("new", "New result")], edges: [] }],
          truncated: false,
        });
      }),
    );
    const rendered = render(<View from="old" />);
    await openAndSubmit("target");
    await oldStarted;

    entityHref.mockClear();
    rendered.rerender(<View from="new" />);
    expect(screen.queryByText("Old result")).toBeNull();
    expect(screen.getByRole("button", { name: "Paths to another entity…" })).toBeInTheDocument();
    expect(entityHref).not.toHaveBeenCalledWith("old", "record");
    await openAndSubmit("target");
    expect(await screen.findByRole("link", { name: "New result" })).toBeInTheDocument();
    releaseOld?.();
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(screen.getByRole("link", { name: "New result" })).toBeInTheDocument();
    expect(screen.queryByText("Old result")).toBeNull();
  });

  it("hides already-served same-source paths when the client and principal change", async () => {
    let releaseSecond: (() => void) | undefined;
    server.use(
      http.post("*/api/v1/graph/paths", async ({ request }) => {
        const secondPrincipal = request.headers.get("authorization") === "Bearer token-b";
        if (secondPrincipal) {
          await new Promise<void>((resolve) => {
            releaseSecond = resolve;
          });
        }
        return HttpResponse.json({
          paths: [
            {
              nodes: [
                node("same", secondPrincipal ? "Second principal path" : "First principal path"),
              ],
              edges: [],
            },
          ],
          truncated: false,
        });
      }),
    );
    const firstUser = { access_token: "token-a", profile: { sub: "a" } } as User;
    const secondUser = { access_token: "token-b", profile: { sub: "b" } } as User;
    const firstHref = vi.fn(entityHref);
    const secondHref = vi.fn(entityHref);
    const rendered = render(<View from="same" user={firstUser} href={firstHref} />);
    await openAndSubmit("target");
    expect(await screen.findByRole("link", { name: "First principal path" })).toBeInTheDocument();

    firstHref.mockClear();
    rendered.rerender(<View from="same" user={secondUser} href={secondHref} />);
    expect(screen.queryByText("First principal path")).toBeNull();
    expect(screen.getByRole("button", { name: "Paths to another entity…" })).toBeInTheDocument();
    expect(firstHref).not.toHaveBeenCalled();
    await openAndSubmit("target");
    await waitFor(() => expect(releaseSecond).toBeDefined());
    releaseSecond?.();
    expect(await screen.findByRole("link", { name: "Second principal path" })).toBeInTheDocument();
  });
});
