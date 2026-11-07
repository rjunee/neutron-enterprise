// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// A DELIBERATELY TINY Markdown-subset renderer for one input only: a governed
// Skill's authored procedure body (## headings, ordered/unordered lists,
// paragraphs). It renders to REACT ELEMENTS — never `dangerouslySetInnerHTML`,
// never a string of HTML — so the Skill author's text is displayed as text and
// can carry no markup into the DOM. That is the whole reason this exists instead
// of a general Markdown library: the console has no raw-HTML sink anywhere, and
// the Capabilities panel is not the place to introduce the first one. Inline
// emphasis is intentionally NOT interpreted; a stray `*` renders as itself.

import type { ReactElement } from "react";

type Block =
  | { kind: "heading"; text: string }
  | { kind: "ordered"; items: string[] }
  | { kind: "unordered"; items: string[] }
  | { kind: "paragraph"; text: string };

/** Group the body's lines into headings, list runs and paragraphs. */
function parseSkillBody(body: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    }
  };
  for (const raw of body.split("\n")) {
    const line = raw.trimEnd();
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    const ordered = /^\d+\.\s+(.*)$/.exec(line);
    const unordered = /^[-*]\s+(.*)$/.exec(line);
    if (line.trim() === "") {
      flushParagraph();
      continue;
    }
    if (heading) {
      flushParagraph();
      blocks.push({ kind: "heading", text: heading[1]! });
      continue;
    }
    if (ordered) {
      flushParagraph();
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "ordered") last.items.push(ordered[1]!);
      else blocks.push({ kind: "ordered", items: [ordered[1]!] });
      continue;
    }
    if (unordered) {
      flushParagraph();
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "unordered") last.items.push(unordered[1]!);
      else blocks.push({ kind: "unordered", items: [unordered[1]!] });
      continue;
    }
    paragraph.push(line.trim());
  }
  flushParagraph();
  return blocks;
}

export function SkillBody({ body }: { body: string }): ReactElement {
  const blocks = parseSkillBody(body);
  return (
    <div className="ne-cap-skill__body">
      {blocks.map((block, index) => {
        if (block.kind === "heading") {
          return (
            <h4 key={index} className="ne-cap-skill__body-h">
              {block.text}
            </h4>
          );
        }
        if (block.kind === "ordered") {
          return (
            <ol key={index} className="ne-cap-skill__body-list">
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{item}</li>
              ))}
            </ol>
          );
        }
        if (block.kind === "unordered") {
          return (
            <ul key={index} className="ne-cap-skill__body-list">
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{item}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={index} className="ne-cap-skill__body-p">
            {block.text}
          </p>
        );
      })}
    </div>
  );
}
