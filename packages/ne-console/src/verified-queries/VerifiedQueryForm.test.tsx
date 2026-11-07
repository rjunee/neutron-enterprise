// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { VerifiedTemplateWire } from "../api/types";
import { VerifiedQueryForm } from "./VerifiedQueryForm";

const scalar = { type: "string", pattern: "^mer-cus-[0-9]{3}$", maxLength: 11 };
const LIST_TEMPLATE = {
  template_id: "autoloans-for-customer",
  version: 1,
  title: "Auto loans for one or more validated customer ids.",
  tool_name: "read.kg.autoloans_for_customer",
  args_schema: {
    type: "object",
    additionalProperties: false,
    required: ["q_customer_id"],
    properties: {
      q_customer_id: {
        title: "Q Customer Id",
        anyOf: [scalar, { type: "array", items: scalar, minItems: 1, maxItems: 24 }],
      },
    },
  },
  columns: ["loan"],
  citation_var: "loan",
  label_var: null,
  max_rows: 20,
} as VerifiedTemplateWire;

describe("VerifiedQueryForm list-arg input", () => {
  it("posts a single value as the scalar branch", async () => {
    const onRun = vi.fn();
    render(<VerifiedQueryForm template={LIST_TEMPLATE} running={false} onRun={onRun} />);
    await userEvent.type(screen.getByLabelText("Q Customer Id value 1"), "mer-cus-001");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ args: { q_customer_id: "mer-cus-001" } }),
    );
  });

  it("adds a bounded second value and posts the list", async () => {
    const onRun = vi.fn();
    render(<VerifiedQueryForm template={LIST_TEMPLATE} running={false} onRun={onRun} />);
    await userEvent.type(screen.getByLabelText("Q Customer Id value 1"), "mer-cus-001");
    await userEvent.click(screen.getByRole("button", { name: /Add another value/ }));
    await userEvent.type(screen.getByLabelText("Q Customer Id value 2"), "mer-cus-002");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ args: { q_customer_id: ["mer-cus-001", "mer-cus-002"] } }),
    );
  });

  it("refuses a duplicate member without calling onRun", async () => {
    const onRun = vi.fn();
    render(<VerifiedQueryForm template={LIST_TEMPLATE} running={false} onRun={onRun} />);
    await userEvent.type(screen.getByLabelText("Q Customer Id value 1"), "mer-cus-001");
    await userEvent.click(screen.getByRole("button", { name: /Add another value/ }));
    await userEvent.type(screen.getByLabelText("Q Customer Id value 2"), "mer-cus-001");
    await userEvent.click(screen.getByRole("button", { name: "Run verified query" }));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/distinct/);
  });
});
