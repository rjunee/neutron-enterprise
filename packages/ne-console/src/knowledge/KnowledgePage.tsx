// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { Navigate, useLocation } from "react-router";
import { parseEntityUrn } from "./entityUrn";

const DOCUMENTS_PATH = "/knowledge/documents";
const GRAPH_PATH = "/knowledge/graph";
const ONTOLOGY_PATH = "/knowledge/ontology";

/**
 * Translate the retired Library contract without interpreting opaque graph
 * anchors or citations. Cloning URLSearchParams retains repeated values and
 * unrelated parameters in arrival order; only the retired `tab` is consumed.
 */
function legacyKnowledgeHref(search: string): string {
  const params = new URLSearchParams(search);
  const suppliedTab = params.get("tab");
  const tab =
    suppliedTab === "documents" ||
    suppliedTab === "entities" ||
    suppliedTab === "graph" ||
    suppliedTab === "reference"
      ? suppliedTab
      : null;
  params.delete("tab");

  const hasGraphContext =
    params.getAll("anchor").some((value) => value !== "") ||
    params.getAll("citation").some((value) => value !== "");
  const entity = params.get("entity");

  let path = DOCUMENTS_PATH;
  if (tab === "reference") {
    path = ONTOLOGY_PATH;
  } else if (tab === "entities" || (tab === null && entity !== null && !hasGraphContext)) {
    path = GRAPH_PATH;
    params.set("mode", "record");
    if (entity !== null) params.set("entity", parseEntityUrn(entity) ?? entity);
  } else if (tab === "graph" || (tab === null && hasGraphContext)) {
    path = GRAPH_PATH;
    params.set("mode", "graph");
  }

  const query = params.toString();
  return query === "" ? path : `${path}?${query}`;
}

/** The sole production owner of the retired `/library` and `/knowledge` family. */
export function KnowledgePage() {
  const location = useLocation();
  return <Navigate to={legacyKnowledgeHref(location.search)} replace />;
}
