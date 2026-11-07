// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ANSWER'S LIGHT-MARKDOWN SUBSET — the parse, plus the render's two hard
// rules: nothing outside the subset is interpreted, and no HTML path exists.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { proseBlocks, renderInline } from "./prose";

describe("proseBlocks", () => {
  it("splits paragraphs on blank lines and opens a list after a single newline", () => {
    // The live corpus's exact shape: "…are:\n- item" with no blank line.
    expect(proseBlocks("The eligible loans are:\n- loan one\n- loan two\n\nNext steps.")).toEqual([
      { kind: "paragraph", text: "The eligible loans are:" },
      { kind: "list", ordered: false, items: ["loan one", "loan two"] },
      { kind: "paragraph", text: "Next steps." },
    ]);
  });

  it("reads numbered lines as an ordered list and # lines as headings", () => {
    expect(proseBlocks("### Summary\n1. first\n2. second")).toEqual([
      { kind: "heading", text: "Summary" },
      { kind: "list", ordered: true, items: ["first", "second"] },
    ]);
  });

  it("treats prose that merely contains digits or dashes as prose", () => {
    expect(proseBlocks("Total due 2026-08-20 is 1,240.")).toEqual([
      { kind: "paragraph", text: "Total due 2026-08-20 is 1,240." },
    ]);
  });
});

describe("renderInline", () => {
  it("MUTATION `bold-is-typography`: **text** renders as <strong>, with no literal asterisks", () => {
    const { container } = render(<p>{renderInline("credit **mer-loan-001** is due", "t")}</p>);
    expect(container.querySelector("strong")?.textContent).toBe("mer-loan-001");
    expect(container.textContent).toBe("credit mer-loan-001 is due");
  });

  it("renders backtick code in the data face and leaves single * and _ literal", () => {
    const { container } = render(
      <p>{renderInline("call `read.kg.disputes` on 3 * 4 rows of mer_loan_001", "t")}</p>,
    );
    expect(container.querySelector("code")?.textContent).toBe("read.kg.disputes");
    expect(container.textContent).toContain("3 * 4");
    expect(container.textContent).toContain("mer_loan_001");
  });

  it("never interprets HTML — angle brackets stay text", () => {
    const { container } = render(<p>{renderInline("a <script>alert(1)</script> b", "t")}</p>);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });
});
