// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE DEFENSIBLE UNIT (review round 1, analyst persona).
//
// The analyst's deliverable is not the answer on screen; it is a paragraph in a
// document that survives being challenged, with something the challenger can open
// independently. The console had no way to get one out: no export, no per-turn
// permalink, clipboard support in exactly two places and only for hash strings.
// The evidence would be retyped, and retyped evidence drifts from what the system
// actually said.
//
// This emits ONE block of plain text containing exactly what the console can
// vouch for:
//   - the answer text, verbatim as it was streamed;
//   - every citation with its document, source system and source ref;
//   - the chat session id and any structural outcome/run trace the terminal
//     frame actually served.
//
// NO DEAD DEEP LINK (round-3 blocker). This block used to emit, per citation,
//     open: {origin}/knowledge?citation={source_ref}
// — the exact URL the console's own source documents as a dead end: on this
// deployment it renders ANCHORED 1, NODES RETURNED 0, an empty graph pane and a
// DETAIL pane with nothing selectable, because the graph holds entity nodes and a
// corpus document that was never entity-extracted has none. /search moved PROVE
// off that URL in round 2 for that reason; the export was not moved with it. So
// the one artifact built specifically to survive being challenged shipped a link
// that renders an empty screen for the person challenging it — who reasonably
// concludes the citation was invented.
//
// There is no addressable per-record route on this console to point at instead,
// and inventing one would be worse. The block therefore emits the identifiers
// that DO resolve — the chunk id the governed retrieve takes, and the document
// URI — and says in one line where the record is opened. A reader can act on
// that; they cannot act on a URL that renders blank.
//
// WHAT IT DELIBERATELY DOES NOT CLAIM. The ordinary done frame carries no
// per-turn configuration hash, so sampling the current Posture hash would
// mislabel a later reading as the configuration that produced this answer. No
// such line is emitted. Likewise, no trace link is invented from the unrelated
// chat session id: only a server-authored provenance trace is named.

import { useState } from "react";
import { classifyAnswer } from "./answerFrames";
import type { UiMessage } from "./useAgentStream";

/** Build the copyable evidence block from the exact message being rendered. */
function buildEvidenceBlock(message: UiMessage, origin: string): string {
  // THE SAME derivation the answer surface uses. The block was otherwise the
  // strongest artifact in the product and it opened with the model's internal
  // monologue — paste that into a challenge review and you have shipped the
  // reasoning trace as evidence. See chat/answerFrames.ts.
  const { answer, frames } = classifyAnswer(message.text);
  const lines: string[] = [];
  lines.push("NEUTRON ENTERPRISE — GOVERNED ANSWER");
  lines.push("");
  lines.push(answer === "" ? "(the answer carried no text)" : answer);
  lines.push("");
  if (frames.length > 0) {
    lines.push(
      `WARNING: this turn emitted ${frames.length} tool call${
        frames.length === 1 ? "" : "s"
      } as text that the runtime never dispatched. The turn did not complete.`,
    );
    lines.push("");
  }
  if (message.citations.length === 0) {
    lines.push("CITATIONS: none. This answer cited no document.");
  } else {
    lines.push(`CITATIONS (${message.citations.length}):`);
    message.citations.forEach((c, i) => {
      lines.push(`  [${i + 1}] ${c.document_id} · ${c.source_system}`);
      lines.push(`      source ref: ${c.source_ref}`);
      lines.push(`      chunk id:   ${c.chunk_id}`);
      // ONLY a URL the server itself resolved. `source_url` is a server-side
      // enrichment and is absent whenever the API cannot resolve a deep link;
      // the console never constructs one.
      if (c.source_url) lines.push(`      open:       ${c.source_url}`);
    });
    lines.push("");
    lines.push(
      `  To read a cited record back: open ${origin}/search, run the query, and open the record`,
    );
    lines.push(
      "  from the result. It is served by the governed POST /v1/brain/retrieve for the chunk id",
    );
    lines.push(
      "  above, under the reader's own key — a reader without permission gets nothing, which is",
    );
    lines.push("  the point.");
  }
  lines.push("");
  lines.push("PROVENANCE");
  lines.push(`  answered by: POST /v1/chat/sessions/{id}/messages`);
  lines.push(`  chat session: ${message.sessionId ?? "not recorded"}`);
  lines.push(`  requested model: ${message.requestedModel ?? "not recorded"}`);
  if (message.provenance) {
    lines.push(`  structural outcome: ${message.provenance.kind}`);
  } else {
    lines.push("  structural outcome: none served for this answer");
  }
  lines.push(
    `  run trace: ${message.traceId ?? message.provenance?.trace?.traceId ?? "not served"}`,
  );
  lines.push(`  copied at: ${new Date().toISOString()}`);
  lines.push("");
  lines.push(
    "Results were trimmed to the signed-in identity server-side. This console never receives an ACL and never re-ranks.",
  );
  return lines.join("\n");
}

export function AnswerExport({ message }: { message: UiMessage }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const [block, setBlock] = useState<string | null>(null);
  const reasoningObserved = classifyAnswer(message.text).reasoning !== "";

  async function copy() {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    const text = buildEvidenceBlock(message, origin);
    setBlock(text);
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      // Clipboard unavailable (insecure context / permission): show the block so
      // it can still be selected by hand rather than silently doing nothing.
      setState("failed");
    }
  }

  // A copy control labelled "WITH CITATIONS" offered on an answer that has none
  // promises something the block cannot deliver. With zero citations the control
  // says what it will actually produce.
  const label =
    message.citations.length > 0 ? "COPY ANSWER WITH CITATIONS" : "COPY ANSWER — NO CITATIONS";
  return (
    <div className="ne-answerexport">
      <button type="button" className="ne-ask__quiet" onClick={() => void copy()}>
        {label}
      </button>
      {state === "copied" ? (
        <span className="ne-answerexport__note" role="status">
          copied — answer text, {message.citations.length} citation
          {message.citations.length === 1 ? "" : "s"} with their source refs, and served structural
          provenance.
          {reasoningObserved
            ? " Internal reasoning was received and is not displayed or included."
            : ""}
        </span>
      ) : null}
      {state === "failed" && block ? (
        <>
          <span className="ne-answerexport__note" role="status">
            the clipboard is not available here — select the block below
          </span>
          <pre className="ne-answerexport__block">{block}</pre>
        </>
      ) : null}
    </div>
  );
}
