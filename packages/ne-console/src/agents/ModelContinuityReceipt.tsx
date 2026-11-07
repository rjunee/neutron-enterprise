// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { AgentRunEventWire } from "../api/types";
import { HashChip } from "../primitives/HashChip";

const FACTS = [
  ["safe_boundary", "Safe boundary"],
  ["actor", "Actor"],
  ["model_policy_id", "Model policy"],
  ["before_requested_model", "Before requested model"],
  ["before_served_model", "Before served model"],
  ["after_requested_model", "After requested model"],
  ["after_authorized_model", "Authorized target model"],
  ["after_authorized_lane", "Authorized target lane"],
  ["context_snapshot_hash", "Context snapshot hash"],
  ["context_fit", "Context-window fit"],
  ["estimated_input_tokens", "Estimated input tokens"],
  ["target_input_budget", "Target input budget"],
  ["compaction", "Compaction"],
  ["cache_impact", "Prompt-cache impact"],
  ["authorization_receipt_hash", "Authorization receipt hash"],
  ["before_config_hash", "Before config hash"],
  ["config_hash", "Committed config hash"],
] as const;

export function ModelContinuityReceipt({ event }: { event: AgentRunEventWire }) {
  if (event.event_kind !== "model.changed") return null;
  const metadata = new Map(event.metadata.map((field) => [field.key, field.value]));
  return (
    <section
      className="ne-model-continuity-receipt"
      aria-label={`Model continuity receipt ${event.event_id}`}
    >
      <h3>Model continuity receipt</h3>
      <p>
        Server-authored model-change evidence for this Conversation. Cache impact is distinct from
        Conversation context continuity.
      </p>
      <dl>
        {FACTS.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            {/* This row ALREADY knew which facts are digests -- it selected the
                mono face on `key.includes("hash")` -- and then painted all 64
                characters anyway. §2.7 (primitives/HashChip) is a written rule:
                truncate to 8 with a copy affordance, full value in `title` and
                clipboard. The label names WHICH hash so a receipt carrying more
                than one does not render a column of identical buttons.
                `Unavailable` is preserved verbatim: a fact the server did not
                send must not become a chip over an empty string. The guard is on the
                VALUE's type, not the key alone -- this metadata map carries
                string | number | boolean, and tsc caught that a numeric field whose
                name contains "hash" would otherwise be chipped as if it were a digest. */}
            <dd className={key.includes("hash") ? "ne-mono" : undefined}>
              {key.includes("hash") && typeof metadata.get(key) === "string" ? (
                <HashChip
                  hash={metadata.get(key) as string}
                  label={`Copy ${label.toLowerCase()}`}
                />
              ) : (
                (metadata.get(key) ?? "Unavailable")
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
