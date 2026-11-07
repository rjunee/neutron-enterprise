// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// TWO RUNS, ONE SCREEN — the side-by-side the product had no way to show.
//
// The claim this platform makes about a verified answer is not "it sounds
// right", it is "it is the same governed row every time, and here is the row's
// canonical hash". That claim is only VISIBLE against a counterexample: the
// same question answered twice, one side stable under its served row anchor,
// the other varying run to run. Until this view existed the only way to stage
// that was two browser windows — nothing on one screen could hold two Runs'
// answers, their claim sets and their row identities in one glance.
//
// WHAT THIS DELIBERATELY IS NOT. It is not a diff and not a verifier: nothing
// here compares two values, marks a match, or colours a mismatch — the client
// displays what each Run's own wire served, side by side, and the reader does
// the comparing (C7: the console displays hashes; it never evaluates one). It
// is also not a new projection: every field is the same Run detail and the
// same evidence envelope the receipt and the drawer already read, addressed by
// whatever two (agent, run) pairs the URL names. Any Run of any Agent — there
// is no privileged fixture, no special-cased template, and no id baked in.
//
// THE URL IS THE WHOLE STATE. `?a_agent=…&a_run=…&b_agent=…&b_run=…`
// (runCompareLink.ts spells it once for the link and this reader). A side the
// URL does not name renders a picker fed from the same served catalog and run
// list every other surface reads. The receipt's "Compare with another Run"
// exit arrives with one side filled.

import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router";
import {
  runOutputAbsentNote,
  runSettlementPresentation,
} from "../agent-harness/adapters/runWorkspace";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import { absoluteTime } from "../primitives/format";
import { HashChip } from "../primitives/HashChip";
import { Empty, LoadError, Loading } from "../primitives/states";
import { ROW_ANCHOR_COPY_LABEL, ROW_ANCHOR_GLOSS, ROW_ANCHOR_LABEL } from "../transcript";
import { verifiedRowAnchors } from "./rowAnchors";
import { OutputVerdictPill } from "./RunReceiptPane";
import { RUN_COMPARE_PARAMS, runCompareRefFromSearch, type RunCompareRef } from "./runCompareLink";
import { strategyWord } from "./StrategyBadge";
import type { AgentDetailView, AgentsClient, AgentSummary, RunDetail, RunReceipt } from "./types";
import { useAgentsRead } from "./useAgentsRead";
import "./agents.css";

/** The page's framing line — what the two columns are and what they are not. */
export const RUN_COMPARE_LEAD =
  "Two Runs, side by side, each exactly as its own record serves it: the answer, the claims it makes, and the identity of every verified row it rests on. Nothing here is compared or verified by the console — the records stand next to each other and speak for themselves.";

/** Stated when a Run's evidence read is not available on this deployment or
 * failed — the same absence the evidence drawer states, in the same words. */
export const RUN_COMPARE_EVIDENCE_UNAVAILABLE = "This Run’s governed evidence is unavailable.";

/** Stated when the envelope was read and associated no verified row: a
 * positive fact off the served envelope, not a shrug. */
export const RUN_COMPARE_NO_ANCHORS =
  "This Run’s evidence names no verified rows, so there is no row identity to show.";

export function RunComparePage({
  client,
  identityKey,
}: {
  client: AgentsClient;
  identityKey: string;
}) {
  const [search, setSearch] = useSearchParams();
  const a = runCompareRefFromSearch(search, "a");
  const b = runCompareRefFromSearch(search, "b");

  const setSide = useCallback(
    (side: "a" | "b", ref: RunCompareRef | null) => {
      const next = new URLSearchParams(search);
      const params = RUN_COMPARE_PARAMS[side];
      if (ref === null) {
        next.delete(params.agent);
        next.delete(params.run);
      } else {
        next.set(params.agent, ref.agentId);
        next.set(params.run, ref.runId);
      }
      setSearch(next, { replace: true });
    },
    [search, setSearch],
  );

  return (
    <div className="ag-compare-page">
      <nav className="ag-crumbs" aria-label="Breadcrumb">
        <span>Execution</span>
        <span aria-hidden="true">›</span>
        <Link to="/agents/all">Agents</Link>
        <span aria-hidden="true">›</span>
        <b>Compare Runs</b>
      </nav>
      <h1 className="ag-compare-page__h1">Compare Runs</h1>
      <p className="ag-compare-page__lead">{RUN_COMPARE_LEAD}</p>
      <div className="ag-compare">
        <CompareColumn
          label="Left Run"
          client={client}
          identityKey={identityKey}
          selected={a}
          onSelect={(ref) => setSide("a", ref)}
        />
        <CompareColumn
          label="Right Run"
          client={client}
          identityKey={identityKey}
          selected={b}
          onSelect={(ref) => setSide("b", ref)}
        />
      </div>
    </div>
  );
}

function CompareColumn({
  label,
  client,
  identityKey,
  selected,
  onSelect,
}: {
  label: string;
  client: AgentsClient;
  identityKey: string;
  selected: RunCompareRef | null;
  onSelect: (ref: RunCompareRef | null) => void;
}) {
  return (
    <section className="ag-compare__col" aria-label={label}>
      <header className="ag-compare__colhead">
        <span className="ag-side__label">{label}</span>
        {selected === null ? null : (
          <button type="button" className="ne-btn ne-btn--quiet" onClick={() => onSelect(null)}>
            Change Run
          </button>
        )}
      </header>
      {selected === null ? (
        <RunPicker client={client} identityKey={identityKey} onPick={onSelect} />
      ) : (
        <ComparedRun client={client} identityKey={identityKey} runRef={selected} />
      )}
    </section>
  );
}

/* ------------------------------------------------------------- picker -- */

/** How many catalog pages the picker will walk. The routers page at 25; this
 * is a guard against a pathological cursor loop, not a design size. */
const MAX_CATALOG_PAGES = 8;

/** The served Agent catalog, first pages accumulated in served order. */
async function readAgentCatalog(client: AgentsClient): Promise<AgentSummary[]> {
  const items: AgentSummary[] = [];
  let cursor: string | undefined = undefined;
  for (let page = 0; page < MAX_CATALOG_PAGES; page += 1) {
    const result = await client.listAgents(cursor);
    items.push(...result.items);
    if (result.next_cursor === null) break;
    cursor = result.next_cursor;
  }
  return items;
}

function RunPicker({
  client,
  identityKey,
  onPick,
}: {
  client: AgentsClient;
  identityKey: string;
  onPick: (ref: RunCompareRef) => void;
}) {
  const loadAgents = useCallback(() => readAgentCatalog(client), [client]);
  const [agents, retryAgents] = useAgentsRead<AgentSummary[]>(loadAgents, identityKey);
  const [agentId, setAgentId] = useState<string>("");

  if (agents.phase === "loading") return <Loading things="the Agent catalog" />;
  if (agents.phase === "unavailable") return <Empty message="The Agent catalog isn’t available." />;
  if (agents.phase === "error")
    return <LoadError things="the Agent catalog" onRetry={retryAgents} />;

  return (
    <div className="ag-compare__picker">
      <label className="ag-compare__pickfield">
        <span className="ag-side__label">Agent</span>
        <select
          className="ne-input"
          value={agentId}
          onChange={(event) => setAgentId(event.target.value)}
        >
          <option value="">Choose an Agent…</option>
          {agents.value.map((agent) => (
            <option key={agent.agent_id} value={agent.agent_id}>
              {agent.label} · {strategyWord(agent.default_strategy)}
            </option>
          ))}
        </select>
      </label>
      {agentId === "" ? null : (
        <RunList
          key={agentId}
          client={client}
          identityKey={identityKey}
          agentId={agentId}
          onPick={(runId) => onPick({ agentId, runId })}
        />
      )}
    </div>
  );
}

function RunList({
  client,
  identityKey,
  agentId,
  onPick,
}: {
  client: AgentsClient;
  identityKey: string;
  agentId: string;
  onPick: (runId: string) => void;
}) {
  const load = useCallback(() => client.listAgentRuns(agentId), [agentId, client]);
  const [runs, retry] = useAgentsRead(load, `${identityKey}:${agentId}`);
  if (runs.phase === "loading") return <Loading things="this Agent’s Runs" />;
  if (runs.phase === "unavailable") return <Empty message="This Agent’s Runs aren’t available." />;
  if (runs.phase === "error") return <LoadError things="this Agent’s Runs" onRetry={retry} />;
  if (runs.value.items.length === 0) return <Empty message="No Runs for this Agent yet." />;
  return (
    <ol className="ag-compare__runs" aria-label="Runs">
      {runs.value.items.map((run: RunReceipt) => (
        <li key={run.run_id}>
          <button type="button" className="ag-compare__runpick" onClick={() => onPick(run.run_id)}>
            <span className="ag-compare__runlabel">{run.display_label}</span>
            <span className="ag-compare__runmeta">
              {absoluteTime(run.created_at)}
              {run.output_verdict === null || run.output_verdict === "unavailable"
                ? ""
                : ` · ${run.output_verdict}`}
            </span>
          </button>
        </li>
      ))}
      {runs.value.next_cursor === null ? null : (
        <li className="ag-compare__runmore">Older Runs are on the Agent’s own workspace.</li>
      )}
    </ol>
  );
}

/* ---------------------------------------------------------- one side -- */

interface ComparedRunFacts {
  run: RunDetail;
  agent: AgentDetailView | null;
  /** The evidence envelope, or `null` when the read is not available on this
   * deployment or did not land. Null renders the stated absence — never a
   * guessed claim set and never an empty list pretending to be one. */
  envelope: AgentEvidenceEnvelopeWire | null;
}

function ComparedRun({
  client,
  identityKey,
  runRef,
}: {
  client: AgentsClient;
  identityKey: string;
  runRef: RunCompareRef;
}) {
  const load = useCallback(async (): Promise<ComparedRunFacts> => {
    const run = (await client.getAgentRun(runRef.agentId, runRef.runId)) as RunDetail;
    const [agent, envelope] = await Promise.all([
      client.getAgent(runRef.agentId).catch(() => null),
      client.getAgentRunEvidence
        ? client.getAgentRunEvidence(runRef.agentId, runRef.runId).catch(() => null)
        : Promise.resolve(null),
    ]);
    return { run, agent, envelope };
  }, [client, runRef.agentId, runRef.runId]);
  const [read, retry] = useAgentsRead<ComparedRunFacts>(
    load,
    `${identityKey}:${runRef.agentId}:${runRef.runId}`,
  );

  if (read.phase === "loading") return <Loading things="this Run’s record" />;
  if (read.phase === "unavailable") return <Empty message="This Run isn’t available." />;
  if (read.phase === "error") return <LoadError things="this Run’s record" onRetry={retry} />;

  const { run, agent, envelope } = read.value;
  const presentation = runSettlementPresentation(run);
  const anchors = envelope === null ? null : verifiedRowAnchors(envelope);
  const anchorByEvidence =
    anchors === null ? null : new Map(anchors.map((row) => [row.evidenceId, row.anchor]));

  return (
    <article className="ag-compare__run">
      <header className="ag-compare__runhead">
        <h2 className="ag-compare__h2">{agent === null ? runRef.agentId : agent.label}</h2>
        <p className="ag-compare__runstamp">
          {run.display_label} · {absoluteTime(run.created_at)}
          {agent === null ? "" : ` · ${strategyWord(agent.default_strategy)}`}
        </p>
        <p className="ag-compare__settle">
          <span className={presentation.tone} aria-hidden="true">
            {presentation.glyph}
          </span>{" "}
          {presentation.word}
          <OutputVerdictPill verdict={run.output_verdict} />
          {run.served_model === null ? null : (
            <span className="ag-compare__model">Model · {run.served_model}</span>
          )}
        </p>
      </header>

      <section aria-label="Output">
        <h3 className="ag-compare__h3">Output</h3>
        {run.output_preview === null ? (
          <p className="ag-compare__absent">
            <span className="ne-absent">—</span> {runOutputAbsentNote(run)}
          </p>
        ) : (
          <p className="ag-compare__output">{run.output_preview}</p>
        )}
      </section>

      <section aria-label={ROW_ANCHOR_LABEL}>
        <h3 className="ag-compare__h3" title={ROW_ANCHOR_GLOSS}>
          {ROW_ANCHOR_LABEL}
        </h3>
        {anchors === null ? (
          <p className="ag-compare__absent">{RUN_COMPARE_EVIDENCE_UNAVAILABLE}</p>
        ) : anchors.length === 0 ? (
          <p className="ag-compare__absent">{RUN_COMPARE_NO_ANCHORS}</p>
        ) : (
          <p className="ag-compare__anchors">
            {anchors.map((row) => (
              <HashChip key={row.evidenceId} hash={row.anchor} label={ROW_ANCHOR_COPY_LABEL} />
            ))}
            {anchors.length > 1 ? (
              <span className="ag-rowanchor__count">{anchors.length} rows</span>
            ) : null}
          </p>
        )}
      </section>

      <section aria-label="Claims">
        <h3 className="ag-compare__h3">Claims</h3>
        {envelope === null ? (
          <p className="ag-compare__absent">{RUN_COMPARE_EVIDENCE_UNAVAILABLE}</p>
        ) : envelope.claims.length === 0 ? (
          <p className="ag-compare__absent">No evidence-backed claims were served.</p>
        ) : (
          <ol className="ag-compare__claims">
            {envelope.claims.map((claim) => {
              const anchor =
                claim.evidence_id === null || anchorByEvidence === null
                  ? undefined
                  : anchorByEvidence.get(claim.evidence_id);
              return (
                <li key={claim.claim_id} className="ag-compare__claim">
                  <span>{claim.state === "available" ? claim.display : claim.message}</span>
                  {anchor === undefined ? null : (
                    <HashChip hash={anchor} label={ROW_ANCHOR_COPY_LABEL} />
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <footer className="ag-compare__exits">
        <Link
          className="ne-btn ne-btn--quiet"
          to={`/agents/${encodeURIComponent(runRef.agentId)}/runs/${encodeURIComponent(runRef.runId)}`}
        >
          Open this Run
        </Link>
      </footer>
    </article>
  );
}
