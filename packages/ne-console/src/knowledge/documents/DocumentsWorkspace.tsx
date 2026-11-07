// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api/client";
import type { ApiClient } from "../../api/client";
import type {
  DocumentJourneyReadWire,
  DocumentListWire,
  DocumentStageEvidenceWire,
  DocumentStageStatusWire,
  DocumentStageWire,
  DocumentSummaryReadWire,
  DocumentVersionWire,
} from "../../api/types";
import { ABSENT, formatCount } from "../../primitives/format";
import { HashChip } from "../../primitives/HashChip";
import { StatePill } from "../../primitives/StatePill";
import type { StateWord } from "../../primitives/StatePill";
import { Empty, LoadError, Loading, Refused } from "../../primitives/states";
import { FilterSelect, Toolbar, ToggleChip } from "../../primitives/Toolbar";
import { DocumentStagePane } from "./DocumentStagePane";
import { parseDocumentDiff } from "./documentDiff";
import { DOCUMENT_STAGES, DOCUMENT_TYPES } from "./documentRouteState";
import type {
  DocumentRouteState,
  DocumentSelector,
  DocumentStage,
  DocumentType,
} from "./documentRouteState";

type Load<T> =
  | { phase: "idle" }
  | { phase: "loading"; context: object }
  | { phase: "ok"; context: object; value: T }
  | { phase: "refused"; context: object }
  | { phase: "error"; context: object };

function presented<T>(state: Load<T>, context: object): Load<T> {
  return state.phase !== "idle" && state.context === context
    ? state
    : { phase: "loading", context };
}

interface RouteOptions {
  replace?: boolean;
}

/** What the directory read settled on, reported up so the TITLE ROW can carry
 * the count beside the page name. `more` is the server's own cursor, not a
 * guess: a page that has a next cursor has not counted the library, and says so
 * rather than presenting a page size as a total. */
export interface DocumentDirectorySummary {
  loaded: number;
  more: boolean;
}

export interface DocumentsWorkspaceProps {
  client: ApiClient;
  ownerId: string;
  route: DocumentRouteState;
  onRouteChange: (next: DocumentRouteState, options?: RouteOptions) => void;
  /** Called with the settled directory count, or `null` whenever the list is
   * loading, refused or failed — there is no count to report then, and the
   * title row renders nothing rather than a zero. */
  onDirectorySummary?: (summary: DocumentDirectorySummary | null) => void;
}

const STAGE_WORD: Record<DocumentStageStatusWire, StateWord> = {
  waiting: "PENDING",
  active: "SYNCING",
  complete: "OK",
  skipped: "IDLE",
  unavailable: "UNAVAILABLE",
};

/** The served version's standing in its own LINEAGE, read off that version's own
 * history row.
 *
 * This is deliberately not a source-authority verdict. `DocumentVersionWire.status`
 * is lineage-local — `current` means "latest version of this document", not
 * "current-source authorization passed". The journey payload carries no
 * authorization field at all: the API enforces current-source authority as a
 * 404 gate (routers/documents.py) and collapses the three-valued
 * authorized/permanent_withheld/retryable_unknown decision to a bare boolean
 * before responding, so a 200 implies authorization but proves nothing about it.
 * A pill that said "authorization passed" would be stating a fact this screen
 * does not receive, so these words stay inside what the row actually means.
 *
 * `absent` is not a server status. It is the console holding no row for the
 * version it is displaying, and it resolves to UNKNOWN rather than borrowing a
 * verdict from a neighbouring row — the same rule that stops UNPRICED being
 * rendered as $0.00.
 *
 * A Record over the closed wire union, so a fourth server status breaks `tsc`
 * here rather than landing silently in whichever branch an `if` chain ended on. */
const VERSION_LINEAGE: Record<
  DocumentVersionWire["status"] | "absent",
  { word: StateWord; detail: string }
> = {
  current: { word: "ACTIVE", detail: "Latest version in this document’s lineage." },
  superseded: { word: "RETIRED", detail: "Superseded by a later version in this lineage." },
  indexing: { word: "SYNCING", detail: "Indexing; not yet the latest version in this lineage." },
  absent: { word: "UNKNOWN", detail: "Served version is missing from the version history." },
};

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function refused(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

function selectorQuery(
  selector: DocumentSelector,
): { version?: number; as_of?: string } | undefined {
  if (selector.kind === "version") return { version: selector.version };
  if (selector.kind === "as_of") return { as_of: selector.asOf };
  return undefined;
}

/** The reading surface's own measure of what it is showing, counted off the
 * served text this screen is rendering — the journey wire carries no length
 * field, so this is a count of the bytes on screen and nothing more. Empty
 * served text reports the ABSENCE; it never reports "0 words". */
function contentWords(content: string): string {
  const words = content
    .trim()
    .split(/\s+/)
    .filter((word) => word !== "").length;
  if (words === 0) return "no content returned";
  return `${formatCount(words)} word${words === 1 ? "" : "s"}`;
}

/** The served bytes, in reading typography rather than a monospace dump.
 *
 * Blocks split on blank lines; NOTHING inside a block is reflowed, because the
 * served content field is not always prose — a `.negraph.json` structured
 * record reaches this same surface, and re-wrapping it would rewrite the
 * evidence. There is deliberately no markdown parsing here either: a heading
 * rendered from a `#` the console guessed at would be a claim about the
 * document's structure that the wire never made. */
function ContentSurface({ content }: { content: string }) {
  const blocks = content.split(/\n[ \t]*\n/).filter((block) => block.trim() !== "");
  if (blocks.length === 0) {
    return (
      <p className="ne-documents__contentabsent">
        <span className="ne-absent">{ABSENT}</span> The server returned no content for this version.
      </p>
    );
  }
  return (
    <div className="ne-documents__prose">
      {blocks.map((block, index) => (
        <p key={`${index}:${block.slice(0, 32)}`}>{block}</p>
      ))}
    </div>
  );
}

function displayTime(value: string | null): string {
  if (value === null) return "Unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function uniqueDocuments(
  current: readonly DocumentSummaryReadWire[],
  incoming: readonly DocumentSummaryReadWire[],
): DocumentSummaryReadWire[] {
  const seen = new Set(current.map((document) => document.document_id));
  return [...current, ...incoming.filter((document) => !seen.has(document.document_id))];
}

function Pipeline({
  stages,
  selected,
  predecessorVersion,
  onSelect,
  onVersion,
}: {
  stages: readonly DocumentStageWire[];
  selected: DocumentStage | null;
  predecessorVersion: number | null;
  onSelect: (stage: DocumentStage) => void;
  onVersion: (version: number) => void;
}) {
  return (
    <ol className="ne-documents__timeline" aria-label="Document pipeline">
      {stages.map((stage) => (
        <li
          key={stage.stage}
          className={`ne-documents__stage${selected === stage.stage ? " ne-documents__stage--selected" : ""}`}
        >
          <button
            type="button"
            className="ne-documents__stageactivate"
            aria-pressed={selected === stage.stage}
            onClick={() => onSelect(stage.stage)}
          >
            <span className="ne-documents__stagelabel">{stage.stage.toUpperCase()}</span>
            <StatePill word={STAGE_WORD[stage.status]} />
            <span>{displayTime(stage.occurred_at)}</span>
          </button>
          {stage.audit_seq === null ? (
            <span>Audit unavailable</span>
          ) : (
            <Link to={`/audit?seq=${stage.audit_seq}`}>Audit #{stage.audit_seq}</Link>
          )}
          {stage.stage === "supersede" && predecessorVersion !== null ? (
            <button
              type="button"
              className="ne-documents__versionlink"
              onClick={() => onVersion(predecessorVersion)}
            >
              Predecessor v{predecessorVersion}
            </button>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function ComparisonIdentity({
  label,
  version,
  hash,
}: {
  label: "Before" | "After";
  version: number;
  hash: string | null;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <span>v{version}</span>
        {" · "}
        {hash === null ? (
          <span>Content hash unavailable</span>
        ) : (
          <HashChip hash={hash} label={`Copy ${label.toLowerCase()} v${version} content hash`} />
        )}
      </dd>
    </div>
  );
}

function UnavailableComparison({ children }: { children: string }) {
  return (
    <div role="status">
      <StatePill word="UNAVAILABLE" />
      <p>{children}</p>
    </div>
  );
}

function Diff({ journey }: { journey: DocumentJourneyReadWire }) {
  const predecessorVersion = journey.comparison.predecessor_version;
  const predecessor =
    predecessorVersion === null
      ? undefined
      : journey.versions.find((version) => version.version_seq === predecessorVersion);

  switch (journey.comparison.state) {
    case "first_version":
      return predecessorVersion === null ? (
        <p>No predecessor exists for v{journey.selected_version}.</p>
      ) : (
        <UnavailableComparison>
          The server returned conflicting first-version comparison data.
        </UnavailableComparison>
      );
    case "predecessor_hidden":
      return <p>The predecessor is not visible to this identity.</p>;
    case "unchanged": {
      if (predecessorVersion === null) {
        return (
          <UnavailableComparison>
            The server did not identify the version used for this comparison.
          </UnavailableComparison>
        );
      }
      return (
        <>
          <dl className="ne-documents__stagecontext" aria-label="Version comparison evidence">
            <ComparisonIdentity
              label="Before"
              version={predecessorVersion}
              hash={predecessor?.content_hash ?? null}
            />
            <ComparisonIdentity
              label="After"
              version={journey.selected_version}
              hash={journey.selected_content_hash}
            />
          </dl>
          <p>No content changes from the visible predecessor.</p>
        </>
      );
    }
    case "available": {
      if (predecessorVersion === null) {
        return (
          <UnavailableComparison>
            The server did not identify the version used for this comparison.
          </UnavailableComparison>
        );
      }
      if (journey.diff === null || journey.diff === "") {
        return (
          <UnavailableComparison>
            The server did not return the available comparison diff.
          </UnavailableComparison>
        );
      }
      const parsed = parseDocumentDiff(journey.diff);
      return (
        <>
          <dl className="ne-documents__stagecontext" aria-label="Version comparison evidence">
            <ComparisonIdentity
              label="Before"
              version={predecessorVersion}
              hash={predecessor?.content_hash ?? null}
            />
            <ComparisonIdentity
              label="After"
              version={journey.selected_version}
              hash={journey.selected_content_hash}
            />
          </dl>
          <div className="ne-documents__diffoverflow" tabIndex={0} aria-label="Version comparison">
            <table className="ne-documents__diff">
              <thead>
                <tr>
                  <th scope="col">Before</th>
                  <th scope="col">After</th>
                  <th scope="col">Change</th>
                </tr>
              </thead>
              <tbody>
                {parsed.rows.map((row, index) => (
                  <tr key={`${index}:${row.text}`} className={`ne-documents__diffrow--${row.kind}`}>
                    <td>{row.oldLine}</td>
                    <td>{row.newLine}</td>
                    <td>
                      <code>{row.text || " "}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      );
    }
  }
}

function VersionMoment({
  version,
  kind,
}: {
  version: DocumentVersionWire;
  kind: "ingested" | "superseded";
}) {
  const occurredAt = kind === "ingested" ? version.ingested_at : version.superseded_at;
  const auditSeq = kind === "ingested" ? version.ingested_audit_seq : version.superseded_audit_seq;
  if (kind === "superseded" && version.status !== "superseded") {
    if (occurredAt !== null || auditSeq !== null) return <span>Supersession data unavailable</span>;
    return <span>Not superseded</span>;
  }
  return (
    <>
      <span>{displayTime(occurredAt)}</span>
      {" · "}
      {auditSeq === null ? (
        <span>Audit unavailable</span>
      ) : (
        <Link to={`/audit?seq=${auditSeq}`}>
          {kind === "ingested" ? "Ingest" : "Supersede"} audit #{auditSeq}
        </Link>
      )}
    </>
  );
}

function comparisonHeading(journey: DocumentJourneyReadWire): string {
  const predecessor = journey.comparison.predecessor_version;
  if (
    (journey.comparison.state === "available" || journey.comparison.state === "unchanged") &&
    predecessor !== null
  ) {
    return `Version comparison: v${predecessor} → v${journey.selected_version}`;
  }
  return `Version comparison for v${journey.selected_version}`;
}

function VersionHistory({
  journey,
  onVersion,
}: {
  journey: DocumentJourneyReadWire;
  onVersion: (version: number) => void;
}) {
  return (
    <div className="ne-documents__tableoverflow" tabIndex={0} aria-label="Document version history">
      <table className="ne-documents__versions">
        <thead>
          <tr>
            <th scope="col">Version</th>
            <th scope="col">Content hash</th>
            <th scope="col">Ingested</th>
            <th scope="col">Superseded</th>
            <th scope="col">Chunks</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {journey.versions.map((version) => (
            <tr
              key={version.version_seq}
              className={
                version.version_seq === journey.selected_version
                  ? "ne-documents__versionrow--served"
                  : undefined
              }
            >
              <td>
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  aria-current={
                    version.version_seq === journey.selected_version ? "true" : undefined
                  }
                  onClick={() => onVersion(version.version_seq)}
                >
                  v{version.version_seq}
                </button>
              </td>
              <td>
                <HashChip hash={version.content_hash} label={`Copy v${version.version_seq} hash`} />
              </td>
              <td>
                <VersionMoment version={version} kind="ingested" />
              </td>
              <td>
                <VersionMoment version={version} kind="superseded" />
              </td>
              <td>{version.chunk_count.toLocaleString()}</td>
              <td>{version.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DocumentsWorkspace({
  client,
  ownerId,
  route,
  onRouteChange,
  onDirectorySummary,
}: DocumentsWorkspaceProps) {
  const [list, setList] = useState<Load<DocumentListWire>>({ phase: "idle" });
  const [journey, setJourney] = useState<Load<DocumentJourneyReadWire>>({ phase: "idle" });
  const [stage, setStage] = useState<Load<DocumentStageEvidenceWire>>({ phase: "idle" });
  const [listRetry, setListRetry] = useState(0);
  const [journeyRetry, setJourneyRetry] = useState(0);
  const [stageRetry, setStageRetry] = useState(0);
  const asOfSelectorIdentity =
    route.selector.kind === "version"
      ? `version:${route.selector.version}`
      : route.selector.kind === "as_of"
        ? `as_of:${route.selector.asOf}`
        : route.selector.kind;
  const asOfModeContext = useMemo(
    () => ({
      client,
      ownerId,
      document: route.document,
      selector: asOfSelectorIdentity,
    }),
    [asOfSelectorIdentity, client, ownerId, route.document],
  );
  const [asOfEditingContext, setAsOfEditingContext] = useState<object | null>(() =>
    route.selector.kind === "as_of" ? asOfModeContext : null,
  );
  const asOfEditing = route.selector.kind === "as_of" || asOfEditingContext === asOfModeContext;
  const [asOfDraft, setAsOfDraft] = useState<{ context: object; value: string }>(() => ({
    context: asOfModeContext,
    value: route.selector.kind === "as_of" ? route.selector.asOf : "",
  }));
  const visibleAsOfDraft =
    asOfDraft.context === asOfModeContext
      ? asOfDraft.value
      : route.selector.kind === "as_of"
        ? route.selector.asOf
        : "";
  const asOfInput = useRef<HTMLInputElement>(null);
  const [loadingMore, setLoadingMore] = useState<{ context: object | null; active: boolean }>({
    context: null,
    active: false,
  });
  const listRequest = useRef(0);
  const listController = useRef<AbortController | null>(null);

  useLayoutEffect(() => {
    if (asOfEditing) {
      asOfInput.current?.focus();
    } else if (document.activeElement === asOfInput.current) {
      asOfInput.current?.blur();
    }
  }, [asOfEditing]);

  const listQuery = useMemo(
    () => ({
      ...(route.query === "" ? {} : { q: route.query }),
      ...(route.documentType === null ? {} : { type: route.documentType }),
      ...(route.source === "" ? {} : { source: route.source }),
      // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
      // ".negraph.json" connector filename convention; REMOVE when the real
      // `structured` flag lands on the document wire. Omitted (the default)
      // EXCLUDES operational-graph records from the list.
      ...(route.includeStructured ? { structured: true } : {}),
      limit: 25,
    }),
    [route.documentType, route.query, route.source, route.includeStructured],
  );
  const listContext = useMemo(
    () => ({ client, ownerId, listQuery, attempt: listRetry }),
    [client, listQuery, listRetry, ownerId],
  );
  const visibleList = presented(list, listContext);
  const isLoadingMore = loadingMore.context === listContext && loadingMore.active;
  const listRender = useRef({ context: listContext, route });
  useLayoutEffect(() => {
    listRender.current = { context: listContext, route };
  }, [listContext, route]);

  useEffect(() => {
    const request = ++listRequest.current;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    let disposed = false;
    queueMicrotask(() => {
      if (!disposed) {
        setList({ phase: "loading", context: listContext });
        setLoadingMore({ context: listContext, active: false });
      }
    });
    void client.listDocuments(listQuery, controller.signal).then(
      (value) => {
        if (
          !disposed &&
          !controller.signal.aborted &&
          listRequest.current === request &&
          listRender.current.context === listContext
        ) {
          setList({ phase: "ok", context: listContext, value });
          setLoadingMore({ context: listContext, active: false });
        }
      },
      (error: unknown) => {
        if (
          disposed ||
          controller.signal.aborted ||
          listRequest.current !== request ||
          listRender.current.context !== listContext ||
          isAbort(error)
        )
          return;
        setList(
          refused(error)
            ? { phase: "refused", context: listContext }
            : { phase: "error", context: listContext },
        );
      },
    );
    return () => {
      disposed = true;
      listController.current?.abort();
    };
  }, [client, listContext, listQuery, listRetry]);

  useEffect(() => {
    if (
      route.document !== null ||
      visibleList.phase !== "ok" ||
      visibleList.value.documents[0] === undefined
    ) {
      return;
    }
    if (listRender.current.context !== listContext || listRender.current.route !== route) return;
    onRouteChange(
      {
        ...route,
        document: visibleList.value.documents[0].document_id,
        selector: { kind: "latest" },
      },
      { replace: true },
    );
  }, [listContext, onRouteChange, route, visibleList]);

  const journeyContext = useMemo(
    () => ({
      client,
      ownerId,
      document: route.document,
      selector: route.selector,
      attempt: journeyRetry,
    }),
    [client, journeyRetry, ownerId, route.document, route.selector],
  );
  const visibleJourney = useMemo(
    () =>
      route.document === null || route.selector.kind === "invalid"
        ? ({ phase: "idle" } as const)
        : presented(journey, journeyContext),
    [journey, journeyContext, route.document, route.selector.kind],
  );
  const journeyRender = useRef({ context: journeyContext, route });
  useLayoutEffect(() => {
    journeyRender.current = { context: journeyContext, route };
  }, [journeyContext, route]);

  useEffect(() => {
    let disposed = false;
    if (route.document === null || route.selector.kind === "invalid") {
      queueMicrotask(() => {
        if (!disposed) setJourney({ phase: "idle" });
      });
      return () => {
        disposed = true;
      };
    }
    const controller = new AbortController();
    queueMicrotask(() => {
      if (!disposed) {
        setJourney({ phase: "loading", context: journeyContext });
        setStage({ phase: "idle" });
      }
    });
    void client
      .documentJourney(route.document, selectorQuery(route.selector), controller.signal)
      .then(
        (value) => {
          if (
            !disposed &&
            !controller.signal.aborted &&
            journeyRender.current.context === journeyContext
          ) {
            setJourney({ phase: "ok", context: journeyContext, value });
          }
        },
        (error: unknown) => {
          if (
            disposed ||
            controller.signal.aborted ||
            journeyRender.current.context !== journeyContext ||
            isAbort(error)
          )
            return;
          setJourney(
            refused(error)
              ? { phase: "refused", context: journeyContext }
              : { phase: "error", context: journeyContext },
          );
        },
      );
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [client, journeyContext, journeyRetry, route.document, route.selector]);

  const selectedStage =
    route.stage ??
    (visibleJourney.phase === "ok" ? (visibleJourney.value.stages[0]?.stage ?? null) : null);
  const selectedVersion =
    visibleJourney.phase === "ok" ? visibleJourney.value.selected_version : null;
  const stageContext = useMemo(
    () => ({
      client,
      ownerId,
      document: route.document,
      selectedVersion: visibleJourney.phase === "ok" ? visibleJourney.value.selected_version : null,
      selectedStage:
        route.stage ??
        (visibleJourney.phase === "ok" ? (visibleJourney.value.stages[0]?.stage ?? null) : null),
      attempt: stageRetry,
    }),
    [client, ownerId, route.document, route.stage, stageRetry, visibleJourney],
  );
  const visibleStage =
    selectedVersion === null || selectedStage === null
      ? ({ phase: "idle" } as const)
      : presented(stage, stageContext);
  const stageRender = useRef({ context: stageContext, route });
  useLayoutEffect(() => {
    stageRender.current = { context: stageContext, route };
  }, [route, stageContext]);

  useEffect(() => {
    let disposed = false;
    if (route.document === null || selectedVersion === null || selectedStage === null) {
      queueMicrotask(() => {
        if (!disposed) setStage({ phase: "idle" });
      });
      return () => {
        disposed = true;
      };
    }
    const controller = new AbortController();
    queueMicrotask(() => {
      if (!disposed) setStage({ phase: "loading", context: stageContext });
    });
    void client
      .documentStageEvidence(route.document, selectedVersion, selectedStage, controller.signal)
      .then(
        (value) => {
          if (
            !disposed &&
            !controller.signal.aborted &&
            stageRender.current.context === stageContext
          ) {
            setStage({ phase: "ok", context: stageContext, value });
          }
        },
        (error: unknown) => {
          if (
            disposed ||
            controller.signal.aborted ||
            stageRender.current.context !== stageContext ||
            isAbort(error)
          )
            return;
          setStage(
            refused(error)
              ? { phase: "refused", context: stageContext }
              : { phase: "error", context: stageContext },
          );
        },
      );
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [client, route.document, selectedStage, selectedVersion, stageContext, stageRetry]);

  const updateDirectory = useCallback(
    (
      patch: Partial<
        Pick<DocumentRouteState, "query" | "documentType" | "source" | "includeStructured">
      >,
    ) => {
      if (listRender.current.context !== listContext || listRender.current.route !== route) return;
      onRouteChange({
        ...route,
        ...patch,
        document: null,
        selector: { kind: "latest" },
      });
    },
    [listContext, onRouteChange, route],
  );

  const changeFromList = useCallback(
    (next: DocumentRouteState) => {
      if (listRender.current.context !== listContext || listRender.current.route !== route) return;
      onRouteChange(next);
    },
    [listContext, onRouteChange, route],
  );

  const changeFromJourney = useCallback(
    (next: DocumentRouteState) => {
      if (journeyRender.current.context !== journeyContext || journeyRender.current.route !== route)
        return;
      onRouteChange(next);
    },
    [journeyContext, onRouteChange, route],
  );

  const changeFromStage = useCallback(
    (next: DocumentRouteState) => {
      if (stageRender.current.context !== stageContext || stageRender.current.route !== route)
        return;
      onRouteChange(next);
    },
    [onRouteChange, route, stageContext],
  );

  const loadMore = useCallback(() => {
    if (visibleList.phase !== "ok" || visibleList.value.next_cursor === null || isLoadingMore)
      return;
    const request = ++listRequest.current;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    setLoadingMore({ context: listContext, active: true });
    void client
      .listDocuments({ ...listQuery, cursor: visibleList.value.next_cursor }, controller.signal)
      .then(
        (value) => {
          if (
            listRequest.current !== request ||
            controller.signal.aborted ||
            listRender.current.context !== listContext
          )
            return;
          setList({
            phase: "ok",
            context: listContext,
            value: {
              ...value,
              documents: uniqueDocuments(visibleList.value.documents, value.documents),
            },
          });
          setLoadingMore({ context: listContext, active: false });
        },
        (error: unknown) => {
          if (
            listRequest.current !== request ||
            controller.signal.aborted ||
            listRender.current.context !== listContext ||
            isAbort(error)
          )
            return;
          setLoadingMore({ context: listContext, active: false });
          setList(
            refused(error)
              ? { phase: "refused", context: listContext }
              : { phase: "error", context: listContext },
          );
        },
      );
  }, [client, isLoadingMore, listContext, listQuery, visibleList]);

  /* The title row's facts margin is fed from the SETTLED directory read only.
   * While the list is loading, refused or failed there is no count, and the
   * page is told `null` so the margin stays empty instead of reporting a zero
   * that describes nothing anyone read. */
  const settledDocuments = visibleList.phase === "ok" ? visibleList.value.documents.length : null;
  const settledMore = visibleList.phase === "ok" ? visibleList.value.next_cursor !== null : null;
  useEffect(() => {
    if (onDirectorySummary === undefined) return;
    onDirectorySummary(
      settledDocuments === null || settledMore === null
        ? null
        : { loaded: settledDocuments, more: settledMore },
    );
  }, [onDirectorySummary, settledDocuments, settledMore]);

  const documentOptions = visibleList.phase === "ok" ? visibleList.value.documents : [];
  const typeOptions =
    visibleList.phase === "ok"
      ? visibleList.value.document_types
      : (DOCUMENT_TYPES as readonly DocumentType[]);
  const sourceOptions = visibleList.phase === "ok" ? visibleList.value.source_systems : [];
  const selectedVersionRecord =
    visibleJourney.phase === "ok"
      ? visibleJourney.value.versions.find(
          (version) => version.version_seq === visibleJourney.value.selected_version,
        )
      : undefined;
  const servedLineage = VERSION_LINEAGE[selectedVersionRecord?.status ?? "absent"];
  /* INTERIM (PD-DOC-TITLES): the server's own honest count of ".negraph.json"
   * rows in the caller's ACL-visible corpus (not the page) — never computed
   * from the fetched window, which is cursor-paginated and would understate
   * it. Rendered only while settled; no count is shown rather than a guess. */
  const operationalGraphCount =
    visibleList.phase === "ok" ? visibleList.value.operational_graph_count : null;
  const includeGraphRecordsLabel =
    operationalGraphCount === null
      ? "Include operational graph records"
      : `Include operational graph records (${formatCount(operationalGraphCount)})`;
  const selectorValue = asOfEditing
    ? "as_of"
    : route.selector.kind === "version"
      ? `version:${route.selector.version}`
      : "latest";

  return (
    <div className="ne-documents">
      <div className="ne-documents__layout">
        {/* THE DIRECTORY COLUMN. Search, filters and count moved INSIDE the list
         * they act on: a filter row spanning both panes reads as page furniture
         * and left the reader guessing which half it narrowed. */}
        <aside className="ne-documents__directory" aria-label="Document directory">
          <Toolbar
            search={{
              value: route.query,
              onChange: (query) => updateDirectory({ query }),
              placeholder: "Search documents…",
            }}
            filters={
              <>
                <FilterSelect
                  label="Type"
                  value={route.documentType ?? ""}
                  options={[
                    { value: "", label: "All types" },
                    ...typeOptions.map((value) => ({ value, label: value })),
                  ]}
                  onChange={(value) =>
                    updateDirectory({ documentType: value === "" ? null : (value as DocumentType) })
                  }
                />
                <FilterSelect
                  label="Source"
                  value={route.source}
                  options={[
                    { value: "", label: "All sources" },
                    ...sourceOptions.map((value) => ({ value, label: value })),
                  ]}
                  onChange={(source) => updateDirectory({ source })}
                />
                {/*
                 * INTERIM (PD-DOC-TITLES): structured-ness inferred from the
                 * ".negraph.json" connector filename convention; REMOVE when the
                 * real `structured` flag lands on the document wire. Off (the
                 * default) EXCLUDES operational-graph records from the list —
                 * this is an explicit opt-IN to see them, not a narrowing filter.
                 */}
                <ToggleChip
                  label={includeGraphRecordsLabel}
                  pressed={route.includeStructured}
                  onChange={(includeStructured) => updateDirectory({ includeStructured })}
                />
              </>
            }
            count={
              visibleList.phase === "ok"
                ? `${visibleList.value.documents.length} loaded${visibleList.value.next_cursor === null ? "" : "+"}`
                : "Documents"
            }
          />
          <div className="ne-documents__directorybody">
            {visibleList.phase === "loading" ? <Loading things="documents" /> : null}
            {visibleList.phase === "refused" ? (
              <Refused thing="documents" page="Documents" />
            ) : null}
            {visibleList.phase === "error" ? (
              <LoadError things="documents" onRetry={() => setListRetry((value) => value + 1)} />
            ) : null}
            {visibleList.phase === "ok" && documentOptions.length === 0 ? (
              <Empty message="No documents are visible for these filters." />
            ) : null}
            {visibleList.phase === "ok" && documentOptions.length > 0 ? (
              <>
                <ul className="ne-documents__documentlist">
                  {documentOptions.map((document) => (
                    <li key={document.document_id}>
                      <button
                        type="button"
                        className={`ne-documents__document${route.document === document.document_id ? " ne-documents__document--selected" : ""}`}
                        aria-current={route.document === document.document_id ? "true" : undefined}
                        onClick={() =>
                          changeFromList({
                            ...route,
                            document: document.document_id,
                            selector: { kind: "latest" },
                          })
                        }
                      >
                        <strong>{document.display_name}</strong>
                        <span>
                          {document.source_system} · {document.document_type} · v
                          {document.version_seq}
                          {document.indexing ? " · indexing" : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {visibleList.value.next_cursor === null ? null : (
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet ne-documents__more"
                    onClick={loadMore}
                    disabled={isLoadingMore}
                  >
                    {isLoadingMore ? "Loading…" : "Load more"}
                  </button>
                )}
              </>
            ) : null}
          </div>
        </aside>

        <section
          className="ne-documents__workspace"
          role="region"
          aria-label="Document evidence workspace"
        >
          {route.document === null ? (
            <Empty message="Select a visible document." />
          ) : visibleJourney.phase === "loading" || visibleJourney.phase === "idle" ? (
            <Loading things="the document journey" />
          ) : visibleJourney.phase === "refused" ? (
            <Refused thing="this document" page="Documents" />
          ) : visibleJourney.phase === "error" ? (
            <LoadError
              things="the document journey"
              onRetry={() => setJourneyRetry((value) => value + 1)}
            />
          ) : (
            <>
              {/* THE IDENTITY BAND — one strip, not a stack. Name, lineage,
               * served version, its content hash, its audit receipt and the
               * version control, all above the content they describe. */}
              <header className="ne-documents__band">
                <h2 className="ne-documents__bandname">{visibleJourney.value.display_name}</h2>
                <p className="ne-documents__bandmeta">
                  {visibleJourney.value.source_system} · {visibleJourney.value.document_type} ·
                  served v{visibleJourney.value.selected_version}
                </p>
                <span role="status" className="ne-documents__bandpill">
                  <StatePill word={servedLineage.word} detail={servedLineage.detail} />
                </span>
                <span className="ne-documents__bandhash">
                  <span className="ne-documents__bandlabel">SHA-256</span>
                  <HashChip
                    hash={visibleJourney.value.selected_content_hash}
                    label="Copy selected content hash"
                  />
                </span>
                {selectedVersionRecord?.ingested_audit_seq === null ||
                selectedVersionRecord?.ingested_audit_seq === undefined ? (
                  <span className="ne-documents__bandabsent">
                    Selected-version audit unavailable
                  </span>
                ) : (
                  <Link
                    to={`/audit?seq=${selectedVersionRecord.ingested_audit_seq}`}
                    aria-label={`Selected-version evidence · Audit #${selectedVersionRecord.ingested_audit_seq}`}
                  >
                    Audit #{selectedVersionRecord.ingested_audit_seq}
                  </Link>
                )}
                <div className="ne-documents__bandspacer" />
                <div className="ne-documents__selectorbar">
                  <label>
                    Visible version
                    <select
                      value={selectorValue}
                      onChange={(event) => {
                        if (event.target.value === "latest") {
                          setAsOfEditingContext(null);
                          setAsOfDraft({ context: asOfModeContext, value: "" });
                          changeFromJourney({ ...route, selector: { kind: "latest" } });
                        } else if (event.target.value.startsWith("version:")) {
                          setAsOfEditingContext(null);
                          setAsOfDraft({ context: asOfModeContext, value: "" });
                          changeFromJourney({
                            ...route,
                            selector: {
                              kind: "version",
                              version: Number(event.target.value.slice("version:".length)),
                            },
                          });
                        } else if (event.target.value === "as_of") {
                          setAsOfEditingContext(asOfModeContext);
                        }
                      }}
                    >
                      <option value="latest">Latest visible</option>
                      {visibleJourney.value.versions.map((version) => (
                        <option key={version.version_seq} value={`version:${version.version_seq}`}>
                          Version {version.version_seq}
                        </option>
                      ))}
                      <option value="as_of">As-of instant</option>
                    </select>
                  </label>
                  <form
                    className="ne-documents__asof"
                    onSubmit={(event) => {
                      event.preventDefault();
                      const value = String(new FormData(event.currentTarget).get("as_of") ?? "");
                      changeFromJourney({ ...route, selector: { kind: "as_of", asOf: value } });
                    }}
                  >
                    <label>
                      As-of ISO-8601
                      <input
                        ref={asOfInput}
                        name="as_of"
                        type="text"
                        required
                        pattern="\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(?::\\d{2}(?:\\.\\d{1,9})?)?(?:Z|[+-]\\d{2}:\\d{2})"
                        value={visibleAsOfDraft}
                        onChange={(event) =>
                          setAsOfDraft({ context: asOfModeContext, value: event.target.value })
                        }
                        placeholder="2026-08-20T19:00:00Z"
                      />
                    </label>
                    <button type="submit" className="ne-btn ne-btn--quiet">
                      Show as of
                    </button>
                  </form>
                </div>
              </header>

              {/* THE READING SURFACE. The served text is the thing the reader
               * came for, so it leads — before the pipeline that produced it. */}
              <article
                className="ne-documents__content"
                aria-labelledby="ne-document-content-heading"
              >
                <div className="ne-documents__contenthead">
                  <h3 id="ne-document-content-heading">Content</h3>
                  <span className="ne-documents__contentfacts">
                    served v{visibleJourney.value.selected_version} ·{" "}
                    {contentWords(visibleJourney.value.selected_content)}
                  </span>
                </div>
                <div
                  className="ne-documents__contentscroll"
                  tabIndex={0}
                  role="group"
                  aria-label="Served version content"
                >
                  <p className="ne-documents__contentlabel">Document content · as served</p>
                  <ContentSurface content={visibleJourney.value.selected_content} />
                </div>
              </article>

              {/* PROVENANCE — the full evidentiary trail, nothing removed, but
               * standing behind the content it accounts for. */}
              <section
                className="ne-documents__provenance"
                aria-labelledby="ne-document-provenance-heading"
              >
                <div className="ne-documents__provenancehead">
                  <h3 id="ne-document-provenance-heading">Provenance &amp; versions</h3>
                  <span>How the served version reached this state</span>
                </div>

                <section
                  className="ne-documents__section"
                  aria-labelledby="ne-document-pipeline-heading"
                >
                  <div className="ne-documents__sectionhead">
                    <h4 id="ne-document-pipeline-heading">Ingestion pipeline</h4>
                    <span>
                      v{visibleJourney.value.selected_version} ·{" "}
                      {visibleJourney.value.stages.length} stages
                    </span>
                  </div>
                  <Pipeline
                    stages={visibleJourney.value.stages}
                    selected={selectedStage}
                    predecessorVersion={visibleJourney.value.comparison.predecessor_version}
                    onSelect={(nextStage) => changeFromJourney({ ...route, stage: nextStage })}
                    onVersion={(version) =>
                      changeFromJourney({ ...route, selector: { kind: "version", version } })
                    }
                  />
                  {visibleStage.phase === "loading" || visibleStage.phase === "idle" ? (
                    <Loading things="stage evidence" />
                  ) : visibleStage.phase === "refused" ? (
                    <Refused thing="this stage evidence" page="Documents" />
                  ) : visibleStage.phase === "error" ? (
                    <LoadError
                      things="stage evidence"
                      onRetry={() => setStageRetry((value) => value + 1)}
                    />
                  ) : (
                    <DocumentStagePane
                      evidence={visibleStage.value}
                      onSelectVersion={(version) =>
                        changeFromStage({ ...route, selector: { kind: "version", version } })
                      }
                    />
                  )}
                </section>

                <section
                  className="ne-documents__section"
                  aria-labelledby="ne-document-versions-heading"
                >
                  <div className="ne-documents__sectionhead">
                    <h4 id="ne-document-versions-heading">Version history</h4>
                    <span>
                      {visibleJourney.value.versions.length} version
                      {visibleJourney.value.versions.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <VersionHistory
                    journey={visibleJourney.value}
                    onVersion={(version) =>
                      changeFromJourney({ ...route, selector: { kind: "version", version } })
                    }
                  />
                </section>

                {/* THE COMPARISON stays collapsed until asked for. A diff is the
                 * answer to a question ("what changed?"), and a question nobody
                 * asked does not get to outrank the document. */}
                <details className="ne-documents__section ne-documents__compare">
                  <summary className="ne-documents__sectionhead">
                    <h4>{comparisonHeading(visibleJourney.value)}</h4>
                    <span className="ne-documents__comparecue">Compare versions</span>
                  </summary>
                  <div className="ne-documents__comparebody">
                    <Diff journey={visibleJourney.value} />
                  </div>
                </details>
              </section>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

export { DOCUMENT_STAGES };
