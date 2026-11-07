// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter, useNavigate } from "react-router";
import { describe, expect, it } from "vitest";
import { RoutePresentation } from "./RoutePresentation";

function Harness() {
  const navigate = useNavigate();
  return (
    <main id="ne-main" tabIndex={-1}>
      <RoutePresentation />
      <button type="button" onClick={() => void navigate("/knowledge/graph?mode=record")}>
        Change path
      </button>
      <button type="button" onClick={() => void navigate("?mode=graph&q=next")}>
        Change query
      </button>
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => void navigate(1)}>
        Forward
      </button>
    </main>
  );
}

describe("RoutePresentation", () => {
  it("derives title and grouped breadcrumb from the exact live destination registry", () => {
    render(
      <MemoryRouter initialEntries={["/verified-queries"]}>
        <Harness />
      </MemoryRouter>,
    );
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
      "Execution/Verified Queries",
    );
    expect(document.title).toBe("Verified Queries · Neutron Enterprise");
  });

  it("keeps Agent, Conversation and Run deep links under the one Agents presentation", () => {
    for (const path of [
      "/agents",
      "/agents/meridian",
      "/agents/meridian/conversations/conversation-1",
      "/agents/meridian/runs/run-1",
    ]) {
      const view = render(
        <MemoryRouter initialEntries={[path]}>
          <Harness />
        </MemoryRouter>,
      );
      expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent(
        "Execution/Agents",
      );
      expect(document.title).toBe("Agents · Neutron Enterprise");
      view.unmount();
    }
  });

  it("never focuses main on the initial mount, but layout-focuses pathname and history arrivals, never query-only state", async () => {
    render(
      <MemoryRouter initialEntries={["/knowledge/documents?document=one"]}>
        <Harness />
      </MemoryRouter>,
    );
    const main = document.getElementById("ne-main")!;
    // A plain mount (page load / reload) is not a navigation: it must never
    // steal focus, or the cyan focus-visible outline paints around the main
    // pane before the user has interacted with anything.
    expect(main).not.toHaveFocus();

    act(() => main.blur());
    screen.getByRole("button", { name: "Change query" }).click();
    await waitFor(() => expect(document.title).toBe("Documents · Neutron Enterprise"));
    expect(main).not.toHaveFocus();

    screen.getByRole("button", { name: "Change path" }).click();
    await waitFor(() => expect(main).toHaveFocus());
    // The title is written by a DIFFERENT effect than the focus move, so it
    // must be waited on separately - asserting it synchronously after the
    // focus wait races that effect (~1-in-N flake, reproduced 2026-08-27).
    await waitFor(() => expect(document.title).toBe("Graph · Neutron Enterprise"));

    act(() => main.blur());
    screen.getByRole("button", { name: "Back" }).click();
    await waitFor(() => expect(main).toHaveFocus());
    await waitFor(() => expect(document.title).toBe("Documents · Neutron Enterprise"));

    act(() => main.blur());
    screen.getByRole("button", { name: "Forward" }).click();
    await waitFor(() => expect(main).toHaveFocus());
    await waitFor(() => expect(document.title).toBe("Graph · Neutron Enterprise"));
  });
});
