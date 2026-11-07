// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — "View in Library (when the citation resolves to a graph entity)".
//
// The only evidence the wire carries for that is the traversal the server
// returned with the retrieval: an entity on a traversed relation whose reference
// is this citation's own. A link offered on anything looser is a link that opens
// an empty screen for the person following it, who then reasonably concludes the
// citation was invented.
//
// Pure, and apart from the pane, so the rule is testable without a DOM.

import type { Citation, RetrievalGraphPathWire, RetrievalResult } from "../api/types";

/** The rule itself, over the traversal and a bare source_ref — ONE
 * implementation, shared with Search's result pane, which gates the same link
 * on the same evidence (it used to offer the link unconditionally, and every
 * connector-record ref landed on an empty graph). */
export function sourceRefResolvesToEntity(
  paths: readonly RetrievalGraphPathWire[],
  sourceRef: string,
): boolean {
  return paths.some((path) =>
    path.hops.some(
      (hop) => hop.subject.source_ref === sourceRef || hop.obj.source_ref === sourceRef,
    ),
  );
}

export function resolvesToEntity(result: RetrievalResult, citation: Citation): boolean {
  return sourceRefResolvesToEntity(result.graph_paths, citation.source_ref);
}
