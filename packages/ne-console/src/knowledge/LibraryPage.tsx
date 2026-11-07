// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY. The knowledge record surface, and the landing target when
// someone follows a citation.
//
// Four tabs — Documents, Entities, Graph, Reference — over §1.4's list-screen shell: a
// title row, then content. There is no purpose sentence, no hero, no annunciator
// and no page foot; the screen this replaces opened with a paragraph explaining
// itself to someone who had just clicked "View in Library" on purpose.
//
// EVERY PIECE OF STATE THAT NAMES AN OBJECT LIVES IN THE URL. Which tab, which
// entity, which anchor: all four params are read here and passed down, so a
// citation from Ask, a relation link inside the Relations table, a verified
// answer's citation cell and the browser's back button all produce the same
// screen — and nothing has to sync one copy of the state onto another.
//
// `?entity=` accepts either the bare key or the `urn:ne:entity:<key>` form that
// verified answers mint, because both are in circulation and a link that half
// works is worse than either.

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import { ListScreen } from "../primitives/PageHeader";
import { EntitiesTab } from "./EntitiesTab";
import { DocumentsTab } from "./DocumentsTab";
import { GraphTab } from "./GraphTab";
import { ReferenceTab } from "./ReferenceTab";
import { parseEntityUrn } from "./entityUrn";
import "./library.css";

const TABS = [
  { id: "documents", label: "Documents" },
  { id: "entities", label: "Entities" },
  { id: "graph", label: "Graph" },
  { id: "reference", label: "Reference" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function isTabId(value: string | null): value is TabId {
  return value !== null && TABS.some((tab) => tab.id === value);
}

export function LibraryPage() {
  const [params, setParams] = useSearchParams();

  const entityParam = params.get("entity");
  const entityKey = entityParam === null ? null : (parseEntityUrn(entityParam) ?? entityParam);
  const anchorKeys = useMemo(() => params.getAll("anchor"), [params]);
  const anchorCitations = useMemo(() => params.getAll("citation"), [params]);
  const documentId = params.get("document");

  const tabParam = params.get("tab");
  // A link that anchors the graph opens the graph, even without naming the tab —
  // Search's "View in Library" sends a citation anchor and nothing else.
  const tab: TabId = isTabId(tabParam)
    ? tabParam
    : anchorKeys.length > 0 || anchorCitations.length > 0
      ? "graph"
      : "entities";

  const openDocument = useCallback(
    (id: string) => {
      const next = new URLSearchParams(params);
      next.set("tab", "documents");
      next.set("document", id);
      setParams(next);
    },
    [params, setParams],
  );

  const openEntity = useCallback(
    (key: string) => {
      const next = new URLSearchParams(params);
      next.set("tab", "entities");
      next.set("entity", key);
      setParams(next);
    },
    [params, setParams],
  );

  const openInGraph = useCallback(
    (key: string) => {
      const next = new URLSearchParams(params);
      next.set("tab", "graph");
      next.delete("anchor");
      next.delete("citation");
      next.append("anchor", key);
      setParams(next);
    },
    [params, setParams],
  );

  const selectTab = useCallback(
    (id: TabId) => {
      const next = new URLSearchParams(params);
      next.set("tab", id);
      setParams(next);
    },
    [params, setParams],
  );

  return (
    <div className="ne-library">
      <ListScreen
        title="Library"
        tabs={
          <div role="tablist" aria-label="Library sections" className="ne-library__tabs">
            {TABS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                role="tab"
                id={`ne-library-tab-${entry.id}`}
                aria-selected={tab === entry.id}
                aria-controls={`ne-library-panel-${entry.id}`}
                className={`ne-library__tabbtn${tab === entry.id ? " ne-library__tabbtn--on" : ""}`}
                onClick={() => selectTab(entry.id)}
              >
                {entry.label}
              </button>
            ))}
          </div>
        }
      >
        <div
          role="tabpanel"
          id={`ne-library-panel-${tab}`}
          aria-labelledby={`ne-library-tab-${tab}`}
        >
          {tab === "documents" ? (
            <DocumentsTab documentId={documentId} onOpenDocument={openDocument} />
          ) : tab === "entities" ? (
            <EntitiesTab
              entityKey={entityKey}
              onOpenEntity={openEntity}
              onOpenInGraph={openInGraph}
            />
          ) : tab === "graph" ? (
            <GraphTab
              anchorKeys={anchorKeys}
              anchorCitations={anchorCitations}
              onAnchor={openInGraph}
            />
          ) : (
            <ReferenceTab />
          )}
        </div>
      </ListScreen>
    </div>
  );
}
