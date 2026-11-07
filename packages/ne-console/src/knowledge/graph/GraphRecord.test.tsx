// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { User } from "oidc-client-ts";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { GraphNodeWire, SubgraphWire } from "../../api/types";
import { AuthStub } from "../../test/authStub";
import { server } from "../../test/mocks/server";
import { GraphRecord } from "./GraphRecord";

function node(key: string, name: string, entityClass = "Account"): GraphNodeWire {
  return {
    key,
    entity_class: entityClass,
    display_name: name,
    source_system: "crm",
    source_ref: `crm/${key}`,
    properties: {},
    assurance: "source",
  };
}

const ENTITY = "acct/acme?region=west";
const RECORD: SubgraphWire = {
  nodes: [
    {
      ...node(ENTITY, "Acme Corp"),
      source_ref: "llm:0123456789abcdef:sharepoint/acme-7",
      properties: {
        owner: "Dana Ops",
        active: true,
        score: 7,
        context: { region: "west" },
        absent: null,
      },
      assurance: "extracted",
    },
    node("person/dana", "Dana Ops", "Person"),
    node("deal/renewal", "Renewal", "Opportunity"),
  ],
  edges: [
    {
      key: "edge-out",
      edge_kind: "OWNS",
      from_key: ENTITY,
      to_key: "deal/renewal",
      source_system: "crm",
      source_ref: "crm/edge-out",
      assurance: "confirmed",
    },
    {
      key: "edge-in",
      edge_kind: "WORKS_AT",
      from_key: "person/dana",
      to_key: ENTITY,
      source_system: "workday",
      source_ref: "hr/edge-in",
      assurance: "extracted",
    },
  ],
  truncated: true,
};

const entityHref = vi.fn(
  (key: string, mode: "graph" | "record") =>
    `/knowledge/graph?entity=${encodeURIComponent(key)}&mode=${mode}`,
);

function renderRecord(entityKey = ENTITY) {
  return render(
    <AuthStub>
      <MemoryRouter>
        <GraphRecord entityKey={entityKey} entityHref={entityHref} />
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("GraphRecord", () => {
  it("reads one governed hop and renders served truth, direction, provenance, and canonical links", async () => {
    const seen: URL[] = [];
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        seen.push(new URL(request.url));
        return HttpResponse.json(RECORD);
      }),
    );

    renderRecord();

    const heading = await screen.findByRole("heading", { name: "Acme Corp" });
    expect(heading).toHaveFocus();
    expect(seen[0]?.searchParams.get("entity")).toBe(ENTITY);
    expect(seen[0]?.searchParams.get("hops")).toBe("1");
    // The header's own UNCONFIRMED pill (the node's assurance is "extracted")
    // plus the one provisional relation (edge-in) — the record pane's ONE
    // assurance vocabulary (StatePill), not a second prose sentence.
    expect(screen.getAllByText("UNCONFIRMED")).toHaveLength(2);
    expect(screen.getByText("sharepoint/acme-7")).toHaveAttribute(
      "title",
      "llm:0123456789abcdef:sharepoint/acme-7",
    );
    expect(screen.getByText('{"region":"west"}')).toBeInTheDocument();

    const relations = screen.getByRole("heading", { name: "Relationships" }).closest("section");
    expect(relations).not.toBeNull();
    expect(within(relations!).getByText("Outgoing")).toBeInTheDocument();
    expect(within(relations!).getByText("Incoming")).toBeInTheDocument();
    expect(within(relations!).getByText("UNCONFIRMED")).toBeInTheDocument();
    expect(within(relations!).getByRole("link", { name: "Dana Ops" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=person%2Fdana&mode=record",
    );
    expect(screen.getByRole("link", { name: "Explore in graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=acct%2Facme%3Fregion%3Dwest&mode=graph",
    );
    expect(
      screen.getByText(
        "This neighborhood was capped by the server. More visible relationships may exist.",
      ),
    ).toHaveAttribute("role", "status");
    expect(entityHref).toHaveBeenCalledWith("person/dana", "record");
    expect(entityHref).toHaveBeenCalledWith(ENTITY, "graph");
  });

  it.each([
    [403, /Your role doesn.t include the knowledge graph/],
    [404, "The knowledge graph isn't enabled on this deployment."],
    [500, "Couldn’t load the entity record."],
  ])("keeps the %i response meaning distinct", async (status, message) => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "read failed" }, { status }),
      ),
    );
    renderRecord();
    expect(await screen.findByText(message)).toBeInTheDocument();
    if (status !== 403) expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
  });

  it("retries a broken read and does not disclose whether an absent entity exists", async () => {
    let reads = 0;
    server.use(
      http.get("*/api/v1/graph/neighborhood", () => {
        reads += 1;
        return reads === 1
          ? HttpResponse.json({ detail: "boom" }, { status: 500 })
          : HttpResponse.json({ nodes: [], edges: [], truncated: false });
      }),
    );
    renderRecord();
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No visible entity record was returned.")).toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it("discards a late response owned by the previous entity", async () => {
    let releaseFirst: (() => void) | undefined;
    const first = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    server.use(
      http.get("*/api/v1/graph/neighborhood", async ({ request }) => {
        const key = new URL(request.url).searchParams.get("entity");
        if (key === "first") {
          await first;
          return HttpResponse.json({ ...RECORD, nodes: [node("first", "First result")] });
        }
        return HttpResponse.json({ ...RECORD, nodes: [node("second", "Second result")] });
      }),
    );
    const rendered = renderRecord("first");
    await waitFor(() => expect(screen.getByText("Loading…")).toBeInTheDocument());
    rendered.rerender(
      <AuthStub>
        <MemoryRouter>
          <GraphRecord entityKey="second" entityHref={entityHref} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByRole("heading", { name: "Second result" })).toBeInTheDocument();
    releaseFirst?.();
    await waitFor(() => expect(screen.queryByText("First result")).toBeNull());
  });

  it("removes an already-served prior principal/entity before the replacement read settles", async () => {
    let releaseSecond: (() => void) | undefined;
    server.use(
      http.get("*/api/v1/graph/neighborhood", async ({ request }) => {
        const entity = new URL(request.url).searchParams.get("entity") ?? "";
        if (request.headers.get("authorization") === "Bearer token-b") {
          await new Promise<void>((resolve) => {
            releaseSecond = resolve;
          });
        }
        return HttpResponse.json({ ...RECORD, nodes: [node(entity, `${entity} result`)] });
      }),
    );
    const firstUser = { access_token: "token-a", profile: { sub: "a" } } as User;
    const secondUser = { access_token: "token-b", profile: { sub: "b" } } as User;
    const view = render(
      <AuthStub value={{ user: firstUser }}>
        <MemoryRouter>
          <GraphRecord entityKey="first" entityHref={entityHref} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByRole("heading", { name: "first result" })).toBeInTheDocument();

    entityHref.mockClear();
    view.rerender(
      <AuthStub value={{ user: secondUser }}>
        <MemoryRouter>
          <GraphRecord entityKey="second" entityHref={entityHref} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("first result")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(entityHref).not.toHaveBeenCalledWith("first", expect.anything());
    await waitFor(() => expect(releaseSecond).toBeDefined());
    releaseSecond?.();
    expect(await screen.findByRole("heading", { name: "second result" })).toBeInTheDocument();
  });

  it("hides an already-served same-entity record when the client and principal change", async () => {
    let releaseSecond: (() => void) | undefined;
    server.use(
      http.get("*/api/v1/graph/neighborhood", async ({ request }) => {
        const secondPrincipal = request.headers.get("authorization") === "Bearer token-b";
        if (secondPrincipal) {
          await new Promise<void>((resolve) => {
            releaseSecond = resolve;
          });
        }
        return HttpResponse.json({
          ...RECORD,
          nodes: [node("same", secondPrincipal ? "Second principal" : "First principal")],
          edges: [],
          truncated: false,
        });
      }),
    );
    const firstUser = { access_token: "token-a", profile: { sub: "a" } } as User;
    const secondUser = { access_token: "token-b", profile: { sub: "b" } } as User;
    const firstHref = vi.fn((key: string, mode: "graph" | "record") => `/first/${key}/${mode}`);
    const secondHref = vi.fn((key: string, mode: "graph" | "record") => `/second/${key}/${mode}`);
    const view = render(
      <AuthStub value={{ user: firstUser }}>
        <MemoryRouter>
          <GraphRecord entityKey="same" entityHref={firstHref} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByRole("heading", { name: "First principal" })).toBeInTheDocument();

    firstHref.mockClear();
    view.rerender(
      <AuthStub value={{ user: secondUser }}>
        <MemoryRouter>
          <GraphRecord entityKey="same" entityHref={secondHref} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("First principal")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Explore in graph" })).toBeNull();
    expect(document.querySelector('a[href^="/first/"]')).toBeNull();
    await waitFor(() => expect(releaseSecond).toBeDefined());
    releaseSecond?.();
    expect(await screen.findByRole("heading", { name: "Second principal" })).toBeInTheDocument();
  });
});
