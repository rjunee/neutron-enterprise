// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { VerifiedInvokeWire, VerifiedTemplateWire, VerifiedTemplatesWire } from "../api/types";
import { HashChip } from "../primitives/HashChip";
import { Empty, LoadError, Refused } from "../primitives/states";
import type { SortState } from "../primitives/tableSort";
import { Toolbar } from "../primitives/Toolbar";
import { VerifiedQueryForm } from "./VerifiedQueryForm";
import { VerifiedResultTable } from "./VerifiedResultTable";
import {
  catalogMatchesHumanDescription,
  classifyVerifiedFailure,
  ownsRequest,
  templateShortLabel,
} from "./verifiedQueriesState";
import type {
  OwnedRequestId,
  VerifiedFailure,
  VerifiedInvokeRequestSnapshot,
} from "./verifiedQueriesState";

type CatalogState =
  | { phase: "loading" }
  | { phase: "ok"; value: VerifiedTemplatesWire }
  | { phase: "refused" }
  | { phase: "error" };

type InvokeState =
  | { phase: "idle"; templateKey: string }
  | { phase: "loading"; templateKey: string; request: VerifiedInvokeRequestSnapshot }
  | {
      phase: "failure";
      templateKey: string;
      request: VerifiedInvokeRequestSnapshot;
      failure: VerifiedFailure;
    }
  | {
      phase: "ok";
      templateKey: string;
      request: VerifiedInvokeRequestSnapshot;
      result: VerifiedInvokeWire;
    };

function keyOf(template: Pick<VerifiedTemplateWire, "template_id" | "version">): string {
  return `${template.template_id}@${template.version}`;
}

export function VerifiedQueriesWorkspace({
  client,
  ownerId,
  selectedTemplateId,
  onSelectTemplate,
}: {
  client: ApiClient;
  ownerId: string;
  selectedTemplateId: string | null;
  onSelectTemplate: (templateId: string) => void;
}) {
  const [catalog, setCatalog] = useState<CatalogState>({ phase: "loading" });
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [catalogSearch, setCatalogSearch] = useState("");
  const [invoke, setInvoke] = useState<InvokeState>({ phase: "idle", templateKey: "" });
  const [resultFilter, setResultFilter] = useState("");
  const [resultSort, setResultSort] = useState<SortState | null>(null);
  const catalogSequence = useRef(0);
  const invokeSequence = useRef(0);
  const activeCatalog = useRef<OwnedRequestId | null>(null);
  const activeInvoke = useRef<OwnedRequestId | null>(null);

  useEffect(() => {
    const request = { owner: ownerId, sequence: ++catalogSequence.current };
    activeCatalog.current = request;
    void client.verifiedTemplates().then(
      (value) => {
        if (ownsRequest(activeCatalog.current, request)) setCatalog({ phase: "ok", value });
      },
      (error: unknown) => {
        if (!ownsRequest(activeCatalog.current, request)) return;
        const failure = classifyVerifiedFailure(error);
        setCatalog({ phase: failure.kind === "refused" ? "refused" : "error" });
      },
    );
    return () => {
      activeCatalog.current = null;
    };
  }, [catalogAttempt, client, ownerId]);

  const templates = useMemo(
    () => (catalog.phase === "ok" ? catalog.value.templates : []),
    [catalog],
  );
  const shown = useMemo(
    () => templates.filter((template) => catalogMatchesHumanDescription(template, catalogSearch)),
    [catalogSearch, templates],
  );
  const selected = useMemo(
    () => templates.find((template) => template.template_id === selectedTemplateId) ?? null,
    [selectedTemplateId, templates],
  );
  const selectedKey = selected === null ? "" : keyOf(selected);
  const visibleInvoke =
    invoke.templateKey === selectedKey
      ? invoke
      : { phase: "idle" as const, templateKey: selectedKey };

  useEffect(() => {
    if (visibleInvoke.phase === "ok") {
      document.getElementById("ne-verified-results-heading")?.focus();
    }
  }, [visibleInvoke.phase]);

  const execute = useCallback(
    (template: VerifiedTemplateWire, request: VerifiedInvokeRequestSnapshot) => {
      const templateKey = keyOf(template);
      const identity = { owner: ownerId, sequence: ++invokeSequence.current };
      activeInvoke.current = identity;
      setInvoke({ phase: "loading", templateKey, request });
      setResultFilter("");
      setResultSort(null);
      void client
        .verifiedInvoke({
          template_id: request.template_id,
          version: request.version,
          args: { ...request.args },
        })
        .then(
          (result) => {
            if (ownsRequest(activeInvoke.current, identity)) {
              setInvoke({ phase: "ok", templateKey, request, result });
            }
          },
          (error: unknown) => {
            if (ownsRequest(activeInvoke.current, identity)) {
              setInvoke({
                phase: "failure",
                templateKey,
                request,
                failure: classifyVerifiedFailure(error),
              });
            }
          },
        );
    },
    [client, ownerId],
  );

  if (catalog.phase === "loading") return <p role="status">Loading verified queries…</p>;
  if (catalog.phase === "refused") {
    return (
      <div role="alert">
        <Refused thing="verified queries" page="Execution" />
      </div>
    );
  }
  if (catalog.phase === "error") {
    return (
      <LoadError
        things="verified queries"
        onRetry={() => {
          setCatalog({ phase: "loading" });
          setCatalogAttempt((value) => value + 1);
        }}
      />
    );
  }
  if (!catalog.value.enabled) {
    return <Empty message="Verified queries are not enabled in this deployment." />;
  }

  return (
    <div className="ne-verified-queries__layout">
      <aside className="ne-verified-queries__catalog" aria-label="Verified query catalog">
        <Toolbar
          search={{
            value: catalogSearch,
            onChange: setCatalogSearch,
            placeholder: "Search query names and descriptions…",
          }}
          count={`${shown.length} queries`}
        />
        {templates.length === 0 ? (
          <Empty message="No verified queries are registered in this deployment." />
        ) : shown.length === 0 ? (
          <Empty message="No query descriptions match this search." />
        ) : (
          <ul className="ne-verified-queries__catalog-list">
            {shown.map((template) => (
              <li key={keyOf(template)}>
                <button
                  type="button"
                  className={template.template_id === selectedTemplateId ? "is-selected" : ""}
                  aria-pressed={template.template_id === selectedTemplateId}
                  onClick={() => {
                    activeInvoke.current = null;
                    setInvoke({ phase: "idle", templateKey: keyOf(template) });
                    onSelectTemplate(template.template_id);
                  }}
                >
                  <span>{templateShortLabel(template.template_id)}</span>
                  <small>Version {template.version}</small>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>
      <section className="ne-verified-queries__workspace" aria-label="Verified query workspace">
        {selectedTemplateId === null ? (
          <Empty message="Select an approved query to inspect its parameters and output before running it." />
        ) : selected === null ? (
          <Empty message="This selected query is not available in the current catalog." />
        ) : (
          <>
            <section className="ne-verified-queries__query" aria-labelledby="ne-selected-query">
              <h2 id="ne-selected-query">{templateShortLabel(selected.template_id)}</h2>
              <p className="ne-verified-queries__description">{selected.title}</p>
              <p>
                This approved query runs deterministically on the server, applies your permissions,
                and replay-checks the structured result without an LLM composing the answer.
              </p>
              <dl className="ne-verified-queries__summary">
                <div>
                  <dt>Version</dt>
                  <dd>{selected.version}</dd>
                </div>
                <div>
                  <dt>Parameters</dt>
                  <dd>{Object.keys(selected.args_schema.properties ?? {}).length}</dd>
                </div>
                <div>
                  <dt>Output columns</dt>
                  <dd>{selected.columns.join(", ")}</dd>
                </div>
                <div>
                  <dt>Row limit</dt>
                  <dd>{selected.max_rows ?? "Server default"}</dd>
                </div>
                {selected.graph_classes === undefined ||
                selected.graph_classes.length === 0 ? null : (
                  <div>
                    <dt>Reads ACL graphs</dt>
                    <dd>{selected.graph_classes.join(", ")}</dd>
                  </div>
                )}
              </dl>
              <details>
                <summary>Technical provenance</summary>
                <dl>
                  <div>
                    <dt>Template id</dt>
                    <dd>
                      <code>{selected.template_id}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Tool</dt>
                    <dd>
                      <code>{selected.tool_name}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Registry SHA-256</dt>
                    <dd>
                      {catalog.value.registry_hash ? (
                        <HashChip
                          hash={catalog.value.registry_hash}
                          label="Copy registry SHA-256"
                        />
                      ) : (
                        "Unavailable"
                      )}
                    </dd>
                  </div>
                </dl>
              </details>
              <VerifiedQueryForm
                key={selectedKey}
                template={selected}
                running={visibleInvoke.phase === "loading"}
                onRun={(request) => execute(selected, request)}
              />
            </section>
            {visibleInvoke.phase === "loading" ? (
              <p role="status">Running verified query…</p>
            ) : null}
            {visibleInvoke.phase === "failure" && visibleInvoke.failure.kind === "refused" ? (
              <div role="alert">
                <Refused thing="this verified query" page="Execution" />
              </div>
            ) : null}
            {visibleInvoke.phase === "failure" && visibleInvoke.failure.kind === "invalid" ? (
              <p role="alert">Arguments were refused. No query was executed.</p>
            ) : null}
            {visibleInvoke.phase === "failure" && visibleInvoke.failure.kind === "error" ? (
              <div>
                <LoadError
                  things="the verified result"
                  onRetry={() => execute(selected, visibleInvoke.request)}
                />
                {visibleInvoke.failure.detail === null ? null : (
                  <p>{visibleInvoke.failure.detail}</p>
                )}
              </div>
            ) : null}
            {visibleInvoke.phase === "ok" ? (
              <VerifiedResultTable
                result={visibleInvoke.result}
                template={selected}
                filter={resultFilter}
                sort={resultSort}
                onFilter={setResultFilter}
                onSort={setResultSort}
              />
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
