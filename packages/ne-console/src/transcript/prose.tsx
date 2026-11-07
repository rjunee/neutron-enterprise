// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ANSWER'S PROSE SHAPE — the small, closed subset of Markdown the served
// answers actually carry, rendered as typography instead of as its own syntax.
//
// The models on this deployment answer in light Markdown: `**bold**` around
// identifiers, `-`/`1.` lists, an occasional `###` heading and backtick code.
// The answer slot drew all of it VERBATIM, so the main surface of the product
// showed literal asterisks around every loan id. This module maps exactly that
// subset onto the transcript's own elements and NOTHING else, by construction:
//
//   * no HTML ever — the input is treated as text, split and wrapped in
//     React elements; there is no innerHTML path here at all;
//   * no links — a Markdown link target is a navigation this console has not
//     resolved, so link syntax stays literal text (citations are chips, and
//     chips come from the server);
//   * anything outside the subset stays byte-for-byte literal. Rendering less
//     than the author wrote is safe; rendering more than they wrote is not.
//
// Pure and exported for its own tests: the parse is assertable without a DOM.

import type { ReactNode } from "react";

/** One block of the answer, in source order. */
export type ProseBlock =
  | { readonly kind: "paragraph"; readonly text: string }
  | { readonly kind: "heading"; readonly text: string }
  | { readonly kind: "list"; readonly ordered: boolean; readonly items: readonly string[] };

const BULLET = /^[-*•]\s+/;
const NUMBERED = /^\d{1,3}[.)]\s+/;
const HEADING = /^#{1,4}\s+/;

/**
 * Split an answer into blocks. Blank lines separate blocks (the one shaping
 * the old renderer did); within a block, consecutive bullet or numbered lines
 * form a list — model answers routinely open a list after a single newline
 * ("…are:\n- item") — and remaining adjacent lines rejoin into one paragraph.
 */
export function proseBlocks(body: string): ProseBlock[] {
  const blocks: ProseBlock[] = [];
  for (const chunk of body.split(/\n{2,}/)) {
    const lines = chunk
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line !== "");
    let paragraph: string[] = [];
    let list: { ordered: boolean; items: string[] } | null = null;
    const flushParagraph = () => {
      if (paragraph.length === 0) return;
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    };
    const flushList = () => {
      if (list === null) return;
      blocks.push({ kind: "list", ordered: list.ordered, items: list.items });
      list = null;
    };
    for (const line of lines) {
      const bullet = BULLET.test(line);
      const numbered = !bullet && NUMBERED.test(line);
      if (bullet || numbered) {
        flushParagraph();
        if (list === null || list.ordered !== numbered) {
          flushList();
          list = { ordered: numbered, items: [] };
        }
        list.items.push(line.replace(bullet ? BULLET : NUMBERED, ""));
        continue;
      }
      flushList();
      if (HEADING.test(line)) {
        flushParagraph();
        blocks.push({ kind: "heading", text: line.replace(HEADING, "") });
        continue;
      }
      paragraph.push(line);
    }
    flushParagraph();
    flushList();
  }
  return blocks;
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;

/**
 * Inline emphasis: `**bold**` and `` `code` ``, nothing else. Single `*` and
 * `_` stay literal on purpose — `3 * 4` and `mer_loan_001` are answer bytes,
 * not markup, and mis-reading them would silently edit the answer.
 */
export function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let index = 0;
  for (const part of text.split(INLINE)) {
    if (part === "") continue;
    const key = `${keyPrefix}:${index}`;
    index += 1;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      nodes.push(<strong key={key}>{part.slice(2, -2)}</strong>);
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      nodes.push(
        <code className="tr-ans__code" key={key}>
          {part.slice(1, -1)}
        </code>,
      );
    } else {
      nodes.push(part);
    }
  }
  return nodes;
}
