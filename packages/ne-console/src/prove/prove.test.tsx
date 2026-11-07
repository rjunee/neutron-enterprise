// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// PULL-TO-PROVE STAYS DELETED. The overlay was an eight-stage modal, seven of
// whose stages said the deployment could not show them. This locks the deletion:
// what the shell still mounts renders its children and nothing else.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProveProvider } from "./ProveContext";
import { ProveOverlay } from "./ProveOverlay";

describe("pull-to-prove", () => {
  it("mounts nothing: the provider is a pass-through and the overlay draws no dialog", () => {
    const { container } = render(
      <ProveProvider>
        <p>work surface</p>
        <ProveOverlay />
      </ProveProvider>,
    );
    expect(screen.getByText("work surface")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container.textContent).toBe("work surface");
    expect(container.querySelector(".ne-prove-scrim")).toBeNull();
  });
});
