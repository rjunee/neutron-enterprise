// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { User } from "oidc-client-ts";
import { useRef, useState } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { GraphNodeWire, SubgraphWire } from "../../api/types";
import { AuthStub } from "../../test/authStub";
import { server } from "../../test/mocks/server";
import { GraphQuickDrawer } from "./GraphQuickDrawer";

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
const DRAWER_RECORD: SubgraphWire = {
  nodes: [
    {
      ...node(ENTITY, "Acme Corp"),
      properties: { one: 1, two: 2, three: 3, four: 4, five: 5 },
    },
    ...Array.from({ length: 6 }, (_, index) =>
      node(`related/${index}`, `Related ${index}`, "Person"),
    ),
  ],
  edges: Array.from({ length: 6 }, (_, index) => ({
    key: `edge-${index}`,
    edge_kind: `REL_${index}`,
    from_key: index % 2 === 0 ? ENTITY : `related/${index}`,
    to_key: index % 2 === 0 ? `related/${index}` : ENTITY,
    source_system: "crm",
    source_ref: `crm/edge-${index}`,
    assurance: "source",
  })),
  truncated: true,
};

const entityHref = vi.fn(
  (key: string, mode: "graph" | "record") =>
    `/knowledge/graph?entity=${encodeURIComponent(key)}&mode=${mode}`,
);

function renderDrawer(onClose = vi.fn(), returnFocusTo?: HTMLElement | null) {
  return render(
    <AuthStub>
      <MemoryRouter>
        <GraphQuickDrawer
          entityKey={ENTITY}
          entityHref={entityHref}
          onClose={onClose}
          returnFocusTo={returnFocusTo}
        />
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("GraphQuickDrawer", () => {
  it("owns an independent one-hop read and labels preview and server truncation separately", async () => {
    const seen: URL[] = [];
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        seen.push(new URL(request.url));
        return HttpResponse.json(DRAWER_RECORD);
      }),
    );
    renderDrawer();

    // The docked pane is an <aside>, not a modal dialog (screens/graph.html:
    // "docked, not a route change" — it never covers the canvas and never
    // traps focus the way a true dialog would).
    const pane = await screen.findByRole("complementary", { name: "Acme Corp" });
    expect(seen[0]?.searchParams.get("entity")).toBe(ENTITY);
    expect(seen[0]?.searchParams.get("hops")).toBe("1");
    expect(within(pane).getByText("Showing 4 of 5 properties in quick view.")).toBeInTheDocument();
    expect(
      within(pane).getByText("Showing 5 of 6 relationships in quick view."),
    ).toBeInTheDocument();
    expect(
      within(pane).getByText(
        "This neighborhood was capped by the server. More visible relationships may exist.",
      ),
    ).toBeInTheDocument();
    expect(within(pane).queryByText("REL_5")).toBeNull();
    expect(within(pane).getByRole("link", { name: "Related 0" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=related%2F0&mode=graph",
    );
    expect(within(pane).getByRole("link", { name: "Open full record" })).toHaveAttribute(
      "href",
      "/knowledge/graph?entity=acct%2Facme%3Fregion%3Dwest&mode=record",
    );
  });

  it.each([
    [403, /Your role doesn.t include the knowledge graph/],
    [404, "The knowledge graph isn't enabled on this deployment."],
    [500, "Couldn’t load the entity record."],
  ])("keeps drawer %i truth distinct", async (status, message) => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", () =>
        HttpResponse.json({ detail: "read failed" }, { status }),
      ),
    );
    renderDrawer();
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("closes with Escape and returns focus to the latest opener", async () => {
    server.use(
      http.get("*/api/v1/graph/neighborhood", ({ request }) => {
        const entity = new URL(request.url).searchParams.get("entity") ?? "";
        return HttpResponse.json({
          ...DRAWER_RECORD,
          nodes: [
            node(entity, entity === "first" ? "First record" : "Second record"),
            ...DRAWER_RECORD.nodes.slice(1),
          ],
        });
      }),
    );

    function Harness() {
      const [selected, setSelected] = useState<string | null>(null);
      const firstRef = useRef<HTMLButtonElement>(null);
      const secondRef = useRef<HTMLButtonElement>(null);
      const opener = selected === "first" ? firstRef.current : secondRef.current;
      return (
        <AuthStub>
          <MemoryRouter>
            <button ref={firstRef} type="button" onClick={() => setSelected("first")}>
              Open first
            </button>
            <button ref={secondRef} type="button" onClick={() => setSelected("second")}>
              Open second
            </button>
            {selected === null ? null : (
              <GraphQuickDrawer
                entityKey={selected}
                entityHref={entityHref}
                returnFocusTo={opener}
                onClose={() => setSelected(null)}
              />
            )}
          </MemoryRouter>
        </AuthStub>
      );
    }

    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Open first" }));
    await screen.findByRole("complementary", { name: "First record" });
    await userEvent.click(screen.getByRole("button", { name: "Open second" }));
    await screen.findByRole("complementary", { name: "Second record" });
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Open second" })).toHaveFocus());
  });

  it("does not render an already-served drawer after its principal and entity change", async () => {
    let releaseSecond: (() => void) | undefined;
    server.use(
      http.get("*/api/v1/graph/neighborhood", async ({ request }) => {
        const entity = new URL(request.url).searchParams.get("entity") ?? "";
        if (request.headers.get("authorization") === "Bearer token-b") {
          await new Promise<void>((resolve) => {
            releaseSecond = resolve;
          });
        }
        return HttpResponse.json({ ...DRAWER_RECORD, nodes: [node(entity, `${entity} result`)] });
      }),
    );
    const firstUser = { access_token: "token-a", profile: { sub: "a" } } as User;
    const secondUser = { access_token: "token-b", profile: { sub: "b" } } as User;
    const view = render(
      <AuthStub value={{ user: firstUser }}>
        <MemoryRouter>
          <GraphQuickDrawer entityKey="first" entityHref={entityHref} onClose={() => undefined} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(await screen.findByRole("complementary", { name: "first result" })).toBeInTheDocument();

    entityHref.mockClear();
    view.rerender(
      <AuthStub value={{ user: secondUser }}>
        <MemoryRouter>
          <GraphQuickDrawer entityKey="second" entityHref={entityHref} onClose={() => undefined} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("first result")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(entityHref).not.toHaveBeenCalledWith("first", expect.anything());
    await waitFor(() => expect(releaseSecond).toBeDefined());
    releaseSecond?.();
    expect(await screen.findByRole("complementary", { name: "second result" })).toBeInTheDocument();
  });

  it("hides an already-served same-entity drawer when the client and principal change", async () => {
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
          ...DRAWER_RECORD,
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
          <GraphQuickDrawer entityKey="same" entityHref={firstHref} onClose={() => undefined} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(
      await screen.findByRole("complementary", { name: "First principal" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Close" })).toHaveFocus());

    firstHref.mockClear();
    view.rerender(
      <AuthStub value={{ user: secondUser }}>
        <MemoryRouter>
          <GraphQuickDrawer entityKey="same" entityHref={secondHref} onClose={() => undefined} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("First principal")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open full record" })).toHaveAttribute(
      "href",
      "/second/same/record",
    );
    expect(document.querySelector('a[href^="/first/"]')).toBeNull();
    await waitFor(() => expect(releaseSecond).toBeDefined());
    releaseSecond?.();
    expect(
      await screen.findByRole("complementary", { name: "Second principal" }),
    ).toBeInTheDocument();
  });

  it("is the floor's own docked column on wide layouts and a shadowed overlay only at the narrow breakpoint", () => {
    // The pane's own visual vocabulary (head/body/foot, no motion) lives here…
    const recordCss = readFileSync(resolve(__dirname, "graphRecord.css"), "utf8");
    expect(recordCss).toMatch(/\.ne-record\s*{[^}]*display: flex/s);
    expect(recordCss).not.toMatch(/animation:|transition:/);

    // …its POSITION in the floor (in-flow column vs. narrow overlay) belongs
    // to graphWorkspace.css, the floor grid's own file (CR-B9).
    const workspaceCss = readFileSync(resolve(__dirname, "graphWorkspace.css"), "utf8");
    expect(workspaceCss).toMatch(/\.ne-graphwork \.ne-record\s*{[^}]*border-left/s);
    expect(workspaceCss).toMatch(
      /@media \(max-width: 64rem\)[\s\S]*\.ne-graphwork \.ne-record\s*{[^}]*position: absolute[^}]*width: var\(--detail-width\)[^}]*box-shadow: var\(--shadow-pane\)/s,
    );
    expect(workspaceCss).not.toMatch(/animation:|transition:/);
  });
});
