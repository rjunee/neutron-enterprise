// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { Citation } from "../api/types";

/** An inline [n] chip that opens the shared EvidencePanel for its citation, plus an
 * optional adjacent mono PROVE segment that PULL-TO-PROVEs into /knowledge.
 * PROVE is 1:1 with the citation — the universal Evidence-Thread gesture.
 *
 * On the transcript the chip sits at the END of the answer's prose: the wire
 * carries no span offsets, so a chip placed mid-sentence would be this console
 * guessing which clause the source supports. */
export function CitationChip({
  index,
  citation,
  onSelect,
  onProve,
}: {
  index: number;
  citation: Citation;
  onSelect: (citation: Citation) => void;
  onProve?: (citation: Citation) => void;
}) {
  return (
    <span className="ne-citation-chip-group">
      <button
        type="button"
        className="ne-ask__citechip"
        onClick={() => onSelect(citation)}
        aria-label={`Citation ${index} from ${citation.source_system}`}
      >
        [{index}]
      </button>
      {onProve && (
        <button
          type="button"
          className="ne-citation-chip__prove"
          onClick={() => onProve(citation)}
          aria-label={`Prove citation ${index}`}
        >
          PROVE
        </button>
      )}
    </span>
  );
}
