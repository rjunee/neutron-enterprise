// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { VerifiedTemplateWire } from "../api/types";
import { parseEntityUrn } from "../knowledge/entityUrn";
import { graphEntityHref } from "../knowledge/graph/graphLinks";

export interface VerifiedCitationLink {
  readonly immutableId: string;
  readonly entityKey: string | null;
  readonly humanLabel: string;
  readonly href: string | null;
}

export function currentEntityHref(entityKey: string): string {
  return graphEntityHref(entityKey, { mode: "record" });
}

export function verifiedCitationLink(
  citation: string,
  cells: readonly string[],
  columns: readonly string[],
  template: Pick<VerifiedTemplateWire, "label_var">,
): VerifiedCitationLink {
  const entityKey = parseEntityUrn(citation);
  const labelIndex = template.label_var == null ? -1 : columns.indexOf(template.label_var);
  const servedLabel = labelIndex < 0 ? null : (cells[labelIndex]?.trim() ?? "");
  const humanLabel =
    servedLabel === null || servedLabel === "" ? (entityKey ?? citation) : servedLabel;
  return {
    immutableId: citation,
    entityKey,
    humanLabel,
    href: entityKey === null ? null : currentEntityHref(entityKey),
  };
}
