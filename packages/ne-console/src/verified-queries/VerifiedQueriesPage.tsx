// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { Link, useSearchParams } from "react-router";
import { useApiClient } from "../auth/useApiClient";
import { usePrincipal } from "../auth/PrincipalContext";
import { ListScreen } from "../primitives/PageHeader";
import { T3Browser } from "./T3Browser";
import { VerifiedQueriesWorkspace } from "./VerifiedQueriesWorkspace";
import {
  browseClassFrom,
  browseRecordFrom,
  selectedTemplateFrom,
  viewFrom,
  withBrowseState,
  withSelectedTemplate,
} from "./route";
import { verifiedQueriesOwnerId } from "./verifiedQueriesState";
import "./verifiedQueries.css";

const AUTHORING_REASON =
  "Authoring is not enabled: templates enter this catalog only through steward review.";

export function VerifiedQueriesPage() {
  const client = useApiClient();
  const principal = usePrincipal();
  const [params, setParams] = useSearchParams();
  const selected = selectedTemplateFrom(params);
  const view = viewFrom(params);

  return (
    <div className="ne-verified-queries">
      <ListScreen
        title="Verified Queries"
        action={
          <span className="ne-verified-queries__create">
            <button
              type="button"
              className="ne-btn ne-btn--primary"
              disabled
              aria-describedby="ne-query-authoring-reason"
            >
              Create query
            </button>
            <span id="ne-query-authoring-reason">{AUTHORING_REASON}</span>
          </span>
        }
      >
        <div className="ne-verified-queries__views" role="group" aria-label="Verified store view">
          <button
            type="button"
            className={view === "queries" ? "is-selected" : ""}
            aria-pressed={view === "queries"}
            onClick={() => setParams(withBrowseState(params, { view: "queries" }))}
          >
            Approved queries
          </button>
          <button
            type="button"
            className={view === "browse" ? "is-selected" : ""}
            aria-pressed={view === "browse"}
            onClick={() => setParams(withBrowseState(params, { view: "browse" }))}
          >
            Browse records
          </button>
        </div>
        <p className="ne-verified-queries__signpost">
          This page is the window onto the <strong>Tier-3 verified store</strong> — a hash-verified,
          versioned release (FEMA disaster declarations, Reg E disputes, auto-loan servicing) that
          only these pre-approved queries can read; every returned row carries a citation receipt so
          the claim can be re-proved later. The freely traversable operational graph mirrored from
          connected systems lives on the <Link to="/graph">Graph</Link> and{" "}
          <Link to="/ontology">Ontology</Link> pages.
        </p>
        {principal.status === "loading" ? (
          <p role="status">Loading verified query identity…</p>
        ) : principal.status === "error" || principal.principal === null ? (
          <p role="alert">Verified queries are unavailable until identity can be confirmed.</p>
        ) : view === "browse" ? (
          <T3Browser
            client={client}
            selectedClass={browseClassFrom(params)}
            selectedRecord={browseRecordFrom(params)}
            onSelectClass={(className) =>
              setParams(withBrowseState(params, { view: "browse", cls: className, record: null }))
            }
            onSelectRecord={(recordIri) =>
              setParams(withBrowseState(params, { view: "browse", record: recordIri }))
            }
          />
        ) : (
          <VerifiedQueriesWorkspace
            key={verifiedQueriesOwnerId(client, principal.principal.id)}
            client={client}
            ownerId={verifiedQueriesOwnerId(client, principal.principal.id)}
            selectedTemplateId={selected}
            onSelectTemplate={(templateId) => setParams(withSelectedTemplate(params, templateId))}
          />
        )}
      </ListScreen>
    </div>
  );
}
