// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// One truthful answer-provenance chip. A trace correlation turns the chip into
// a native link; without one it remains text, never a fabricated destination.

import { Link } from "react-router";
import { traceDecisionHref } from "../trace/traceLink";
import type { AnswerProvenance, AnswerProvenanceKind } from "./provenance";

const LABEL: Record<AnswerProvenanceKind, string> = {
  verified: "VERIFIED",
  grounded: "GROUNDED",
  "refused-ungrounded": "REFUSED — ungrounded",
};

const ACCESSIBLE_LABEL: Record<AnswerProvenanceKind, string> = {
  verified: "Verified answer provenance",
  grounded: "Grounded answer provenance",
  "refused-ungrounded": "Refused because the answer was ungrounded",
};

export function ProvenanceChip({ provenance }: { provenance: AnswerProvenance }) {
  const label = LABEL[provenance.kind];
  const className = `ne-ask__provenance ne-ask__provenance--${provenance.kind}`;
  if (provenance.trace) {
    return (
      <Link
        className={className}
        to={traceDecisionHref(provenance.trace)}
        aria-label={`${ACCESSIBLE_LABEL[provenance.kind]}; open related run trace`}
      >
        {label}
      </Link>
    );
  }
  return (
    <span className={className} aria-label={ACCESSIBLE_LABEL[provenance.kind]}>
      {label}
    </span>
  );
}
