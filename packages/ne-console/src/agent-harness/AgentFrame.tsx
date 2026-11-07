// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useId } from "react";
import type { AgentFrame as AgentFrameModel, AgentFrameStatus } from "./types";

const STATUS: Record<AgentFrameStatus, { glyph: string; label: string }> = {
  neutral: { glyph: "○", label: "Recorded" },
  active: { glyph: "→", label: "Active" },
  pending: { glyph: "Ⅱ", label: "Pending" },
  allowed: { glyph: "✓", label: "Allowed" },
  denied: { glyph: "×", label: "Denied" },
  complete: { glyph: "✓", label: "Complete" },
  failed: { glyph: "!", label: "Failed" },
  refused: { glyph: "—", label: "Refused" },
};

const KIND_LABEL: Record<AgentFrameModel["kind"], string> = {
  prompt: "Prompt",
  answer: "Answer",
  "tool-decision": "Tool",
  "governance-checkpoint": "Checkpoint",
  citations: "Evidence",
  "model-call": "Model call",
  "context-change": "Context change",
  retrieval: "Retrieval",
  guardrail: "Guardrail",
  workflow: "Workflow",
  terminal: "Run",
  unknown: "Unclassified event",
};

const SCHEMA_DEPTH = 5;
const SCHEMA_WIDTH = 24;

function schemaKey(key: string): string {
  return /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(key) ? key : "[redacted key]";
}

/** Describe shape without ever coercing or serializing a payload value. */
function schemaLines(raw: unknown): string[] {
  const seen = new WeakSet<object>();
  const visit = (value: unknown, depth: number): string[] => {
    if (value === null) return ["null"];
    if (typeof value !== "object") return [typeof value];
    if (seen.has(value)) return ["object (recursive)"];
    if (depth >= SCHEMA_DEPTH) return [Array.isArray(value) ? "array (…)" : "object (…)"];
    seen.add(value);

    if (Array.isArray(value)) {
      const lines = ["array"];
      value.slice(0, SCHEMA_WIDTH).forEach((entry, index) => {
        const [head, ...tail] = visit(entry, depth + 1);
        lines.push(`  [${index}]: ${head}`);
        lines.push(...tail.map((line) => `    ${line}`));
      });
      if (value.length > SCHEMA_WIDTH) lines.push("  […]");
      return lines;
    }

    const lines = ["object"];
    let descriptors: Record<string, PropertyDescriptor>;
    try {
      descriptors = Object.getOwnPropertyDescriptors(value);
    } catch {
      return ["object (unavailable)"];
    }
    const entries = Object.entries(descriptors).slice(0, SCHEMA_WIDTH);
    for (const [key, descriptor] of entries) {
      const child = "value" in descriptor ? visit(descriptor.value, depth + 1) : ["accessor"];
      const [head, ...tail] = child;
      lines.push(`  ${schemaKey(key)}: ${head}`);
      lines.push(...tail.map((line) => `    ${line}`));
    }
    if (Object.keys(descriptors).length > SCHEMA_WIDTH) lines.push("  […]");
    return lines;
  };
  return ["Payload values withheld.", "Schema:", ...visit(raw, 0)];
}

export function AgentFrame({ frame }: { frame: AgentFrameModel }) {
  const titleId = useId();
  const state = STATUS[frame.status];
  const facts = frame.facts ?? [];
  const links = frame.links ?? [];
  const hasDetails = facts.length > 0 || links.length > 0 || frame.kind === "unknown";

  return (
    <li
      className={`ne-agent-frame ne-agent-frame--${frame.status}`}
      data-frame-key={frame.key}
      data-frame-source={frame.source}
      data-order-basis={frame.orderBasis}
    >
      <article aria-labelledby={titleId}>
        <div className="ne-agent-frame__rail" aria-hidden="true">
          <span>{state.glyph}</span>
        </div>
        <div className="ne-agent-frame__content">
          <header className="ne-agent-frame__header">
            <div>
              <span className="ne-agent-frame__kind">{KIND_LABEL[frame.kind]}</span>
              <h3 id={titleId}>{frame.title}</h3>
            </div>
            <span className="ne-agent-frame__state">
              <span aria-hidden="true">{state.glyph}</span>
              {state.label}
            </span>
          </header>

          {frame.body === undefined ? null : <p className="ne-agent-frame__body">{frame.body}</p>}

          {frame.kind === "tool-decision" ? (
            <p className="ne-agent-frame__identity">
              <span>Tool</span> <code>{frame.tool}</code>
            </p>
          ) : null}

          {frame.kind === "citations" ? (
            <ol className="ne-agent-frame__citations" aria-label="Citations">
              {frame.citations.map((citation) => (
                <li key={citation.key}>
                  {citation.href ? <a href={citation.href}>{citation.label}</a> : citation.label}
                </li>
              ))}
            </ol>
          ) : null}

          {hasDetails ? (
            <details className="ne-agent-frame__details">
              <summary>Details</summary>
              {facts.length > 0 ? (
                <dl>
                  {facts.map((fact) => (
                    <div key={fact.label}>
                      <dt>{fact.label}</dt>
                      <dd className={fact.mono ? "ne-agent-frame__mono" : undefined}>
                        {fact.value === null ? "—" : String(fact.value)}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {links.length > 0 ? (
                <ul className="ne-agent-frame__links">
                  {links.map((link) => (
                    <li key={`${link.label}:${link.href}`}>
                      <a href={link.href}>{link.label}</a>
                    </li>
                  ))}
                </ul>
              ) : null}
              {frame.kind === "unknown" ? (
                <pre className="ne-agent-frame__raw">{schemaLines(frame.raw).join("\n")}</pre>
              ) : null}
            </details>
          ) : null}
        </div>
      </article>
    </li>
  );
}
