// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE COMPOSER IS ONE CARD. The question takes the card's top; everything about
// HOW the question gets answered — the served model, the grounding segment, the
// server's caps — sits in the card's own bordered footer with the single
// primary Ask. Nothing about answering floats outside the card, which is what
// stopped the grounding control being homeless in the chrome above the thread.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageInput } from "./MessageInput";

function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = Array.from(css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g")));
  expect(matches.length, `missing ${selector} rule`).toBeGreaterThan(0);
  return matches.at(-1)?.[1] ?? "";
}

describe("the composer card", () => {
  it("puts the question above the card's own control footer", () => {
    render(
      <MessageInput
        busy={false}
        value=""
        onChange={() => undefined}
        onSubmit={() => undefined}
        onStop={() => undefined}
        modelReady
        controls={<select aria-label="Run model" />}
        capsNote="8 steps · 2 min"
      />,
    );

    const textarea = screen.getByLabelText("Ask a question");
    const form = textarea.closest("form");
    expect(form).toHaveClass("ag-composer__card");
    const footer = form?.querySelector(".ag-composer__ctl");
    // Two children and nothing else: the question, then the row about it.
    expect(Array.from(form?.children ?? [])).toEqual([textarea, footer]);

    const picker = screen.getByRole("combobox", { name: "Run model" });
    const send = screen.getByRole("button", { name: "Ask" });
    expect(footer).toContainElement(picker);
    expect(footer).toContainElement(screen.getByText("8 steps · 2 min"));
    expect(footer).toContainElement(send);
    // The served controls lead the row; the one primary closes it.
    const row = Array.from(footer?.children ?? []);
    expect(row.at(0)).toBe(picker);
    expect(row.at(-1)).toBe(send);
  });

  it("stays legible but starts nothing while the served agent is unavailable", () => {
    const onSubmit = vi.fn();
    render(
      <MessageInput
        busy={false}
        value="a question already typed"
        onChange={() => undefined}
        onSubmit={onSubmit}
        onStop={() => undefined}
        modelReady
        controls={null}
        disabled
        placeholder="Ask Contracts Analyst…"
      />,
    );

    // Disabled rather than hidden: the reader can still read what they were
    // doing, and the placeholder names the agent this thread is with.
    const box = screen.getByLabelText("Ask Contracts Analyst…");
    expect(box).toBeDisabled();
    expect(box).toHaveValue("a question already typed");
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    // No served caps means no caps line — never a guess at one.
    expect(document.querySelector(".ag-caps")).toBeNull();

    // A governed turn is never started by anything but a deliberate press, so
    // even a submit that reaches the form finds the guard.
    fireEvent.submit(box.closest("form") as HTMLFormElement);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  // THE SILENT LOCK. `submit()` returns early on `!modelReady` and on
  // `disabled`, and neither used to put a word on the screen: the reader typed,
  // pressed Enter, and the surface swallowed it. These three pin that a refusal
  // to send is now always accompanied by the owning screen's own reason — and
  // that the reason never appears when the composer would in fact send, which
  // would be a stated fault that is not happening.
  describe("a composer that will not send says why", () => {
    function renderWith(props: Partial<Parameters<typeof MessageInput>[0]>) {
      return render(
        <MessageInput
          busy={false}
          value="a question already typed"
          onChange={() => undefined}
          onSubmit={() => undefined}
          onStop={() => undefined}
          modelReady
          controls={null}
          {...props}
        />,
      );
    }

    it("states the reason beside the dead send control, as a live region", () => {
      renderWith({ modelReady: false, blockedReason: "No model is ready for this run yet." });

      const reason = screen.getByText("No model is ready for this run yet.");
      expect(reason).toHaveAttribute("role", "status");
      expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
      // In the control footer, with the send button it explains — not somewhere
      // else on the page the reader has to go and find.
      const footer = document.querySelector(".ag-composer__ctl");
      expect(footer).toContainElement(reason);
      // The single primary still closes the row.
      expect(Array.from(footer?.children ?? []).at(-1)).toBe(
        screen.getByRole("button", { name: "Ask" }),
      );
    });

    it("states it for a disabled composer too — the other silent arm", () => {
      renderWith({ disabled: true, blockedReason: "This agent's run envelope hasn't loaded." });

      expect(screen.getByText("This agent's run envelope hasn't loaded.")).toHaveAttribute(
        "role",
        "status",
      );
    });

    it("says NOTHING when the composer would send, and nothing while a turn runs", () => {
      const ready = renderWith({ blockedReason: "should never be shown" });
      expect(screen.queryByText("should never be shown")).toBeNull();
      ready.unmount();

      // Busy is not blocked: the control is Stop, and a reason beside it would
      // be stating a fault about a turn that is running normally.
      renderWith({ busy: true, disabled: true, blockedReason: "should never be shown" });
      expect(screen.queryByText("should never be shown")).toBeNull();
    });
  });

  it("bounds the question to a prose measure and wraps its control row", () => {
    // The composer's own layout moved to the workspace stylesheet with the
    // card; chat.css no longer styles anything this component renders.
    const css = readFileSync(resolve(__dirname, "../agents/agents.css"), "utf8");

    const card = rule(css, ".ag-composer__card");
    expect(card).toContain("max-width: 76ch");
    expect(card).toContain("margin-inline: auto");

    const text = rule(css, ".ag-composer__text");
    expect(text).toContain("width: 100%");
    expect(text).toContain("resize: none");

    // The footer WRAPS: model, grounding and caps drop to a second line at a
    // narrow width instead of pushing the card into a horizontal scroll.
    const footer = rule(css, ".ag-composer__ctl");
    expect(footer).toContain("display: flex");
    expect(footer).toContain("flex-wrap: wrap");

    expect(rule(css, ".ag-composer__send")).toContain("margin-left: auto");
  });
});
