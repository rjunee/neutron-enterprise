// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { AgentFrame } from "./AgentFrame";
import { materialAnnouncement } from "./types";
import type { AgentTranscriptModel } from "./types";
import "./agentHarness.css";

export function AgentTranscript({ transcript }: { transcript: AgentTranscriptModel }) {
  // Preserve the supplied order exactly. Each adapter owns its explicit order
  // basis; a browser-time sort would manufacture a cross-source chronology.
  const announcement = transcript.frames.reduce<string | null>(
    (latest, frame) => materialAnnouncement(frame) ?? latest,
    null,
  );

  return (
    <section
      className="ne-agent-transcript"
      aria-label={transcript.label}
      aria-busy={transcript.busy}
    >
      <p className="ne-visually-hidden" aria-live="polite" aria-atomic="true">
        {announcement}
      </p>
      <ol className="ne-agent-transcript__frames" aria-label={`${transcript.label} timeline`}>
        {transcript.frames.map((frame) => (
          <AgentFrame key={frame.key} frame={frame} />
        ))}
      </ol>
    </section>
  );
}
