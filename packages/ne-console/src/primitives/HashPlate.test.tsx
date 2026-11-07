// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// HashPlate proof (§1.2 hash typography): first-6/last-6 at full text with a
// dimmed middle; collapsed → "…"; hover/focus reveals the full value + a copy
// affordance that writes the FULL hash to the clipboard; the unattested/absent
// state renders the honest hollow placard (§0 law 5), never a fabricated hash.

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HashPlate } from "./HashPlate";

const VALUE = "abcdef" + "0".repeat(52) + "123456"; // 64 chars, head=abcdef tail=123456

// A plain spy for navigator.clipboard.writeText — asserted on directly (avoids
// @testing-library/user-event's setup() installing its own clipboard stub).
let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
    writable: true,
  });
});

describe("HashPlate", () => {
  it("renders first-6 + last-6 at full text with a dimmed middle span", () => {
    const { container } = render(<HashPlate value={VALUE} />);
    expect(screen.getByText("abcdef")).toBeInTheDocument();
    expect(screen.getByText("123456")).toBeInTheDocument();
    const mid = container.querySelector(".ne-hashplate__mid");
    expect(mid).not.toBeNull();
    expect(mid?.textContent).toBe("0".repeat(52));
  });

  it("collapsed renders an ellipsis middle instead of the raw chars", () => {
    const { container } = render(<HashPlate value={VALUE} collapsed />);
    expect(container.querySelector(".ne-hashplate__mid")?.textContent).toBe("…");
  });

  it("reveals the full hash and a copy button on focus, and copies the FULL value", async () => {
    render(<HashPlate value={VALUE} />);
    // Popover hidden until hover/focus.
    expect(screen.queryByLabelText("copy full hash")).not.toBeInTheDocument();

    fireEvent.focus(screen.getByRole("button", { name: `hash ${VALUE}` }));
    const copy = await screen.findByLabelText("copy full hash");
    // The full value is revealed in the popover.
    expect(screen.getByRole("status")).toHaveTextContent(VALUE);

    fireEvent.click(copy);
    expect(writeText).toHaveBeenCalledWith(VALUE);
  });

  it("renders the honest hollow UNATTESTED placard when unattested (never a fake hash)", () => {
    render(<HashPlate plate unattested />);
    expect(screen.getByText("UNATTESTED")).toBeInTheDocument();
    expect(screen.getByTitle("awaiting GET /v1/posture")).toBeInTheDocument();
  });

  it("renders the hollow placard when no value is supplied", () => {
    render(<HashPlate />);
    expect(screen.getByText("UNATTESTED")).toBeInTheDocument();
  });
});
