// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 SERVING — the panel that turns "private-vllm" from a label into an
// observation. serving.ts owns the row semantics; this component owns when the
// section exists at all and how fresh the numbers on screen are.
//
// WHEN THE SECTION EXISTS. The reader sees rows exactly when the server sent
// observations: no client method → nothing (older deployment); ops surface
// refused → nothing (the Home-tile precedent: a section for a job that is not
// this person's is omitted, not apologized for — and a knownRefused read is
// never even asked, so browsing Models does not salt the audit log with
// denials); zero probeable lanes → nothing (a deployment with no private lane
// has nothing to prove here — a placeholder row would be the fabrication §10
// forbids). A FAILED read renders its failure: an outage on the freshness
// surface must not look like an empty deployment.
//
// FRESHNESS IS PART OF THE CLAIM. The server's verdict carries `checked_at`;
// this panel re-reads every 30 s and re-renders the ages every 5 s, so "just
// now" on screen tracks the clock instead of freezing at mount. The displayed
// age is always computed from the SERVED timestamp — a cached verdict arrives
// with an older stamp and honestly reads older.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { LaneServingView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useSurfaceAccess } from "../auth/useSurfaceAccess";
import { useGovernedRead } from "../landing/useGovernedRead";
import { StatePill } from "../primitives/StatePill";
import { LoadError } from "../primitives/states";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import { availabilityLine, endpointHost, pillWord, reportedModel, servingReader } from "./serving";

const RELOAD_MS = 30_000;
const TICK_MS = 5_000;

function CheckedAt({ iso, now }: { iso: string | null; now: number }) {
  if (iso === null) return null;
  return (
    <span className="ne-models__servingmeta">
      Checked{" "}
      <time dateTime={iso} title={absoluteTime(iso)}>
        {relativeTime(iso, now)}
      </time>
    </span>
  );
}

function Row({ lane, now }: { lane: LaneServingView; now: number }) {
  const reported = reportedModel(lane);
  const availability = availabilityLine(lane, now);
  const host = endpointHost(lane.endpoint);
  return (
    <li className="ne-models__servingrow">
      <div className="ne-models__servinghead">
        <span className="ne-mono">{lane.model_id}</span>
        <span className="ne-models__sub">{lane.lane}</span>
        <StatePill word={pillWord(lane.state, lane.serves_configured_model)} />
      </div>
      <dl className="ne-models__servingfacts">
        <dt>Endpoint</dt>
        <dd>
          {host === "" ? (
            <span className="ne-absent">—</span>
          ) : (
            <span className="ne-models__servingendpoint ne-mono">{host}</span>
          )}
        </dd>
        {reported === null ? null : (
          <>
            <dt>Model</dt>
            <dd className={reported.mismatch ? "ne-models__servingmismatch" : undefined}>
              <span className="ne-mono">{reported.text}</span>
            </dd>
          </>
        )}
        {lane.observed_context_window === null ? null : (
          <>
            <dt>Window</dt>
            <dd>{formatCount(lane.observed_context_window)} tokens</dd>
          </>
        )}
        {lane.engine_version === null ? null : (
          <>
            <dt>Engine</dt>
            <dd>vLLM {lane.engine_version}</dd>
          </>
        )}
      </dl>
      <div className="ne-models__servingfoot">
        <CheckedAt iso={lane.checked_at} now={now} />
        {lane.latency_ms === null ? null : (
          <span className="ne-models__servingmeta">{lane.latency_ms} ms</span>
        )}
        {availability === null ? null : (
          <span className="ne-models__servingnote">{availability}</span>
        )}
      </div>
    </li>
  );
}

export function ServingPanel() {
  const client = useApiClient();
  const access = useSurfaceAccess();
  const reader = useMemo(() => servingReader(client), [client]);
  const read = useGovernedRead(
    useCallback(async () => (reader === null ? null : await reader()), [reader]),
    access.refused("ops"),
  );

  // Re-observe on a cadence; useGovernedRead keeps the last data while the
  // re-read is in flight, so the panel never blinks out between polls.
  const { reload } = read;
  useEffect(() => {
    if (reader === null) return;
    const id = window.setInterval(reload, RELOAD_MS);
    return () => window.clearInterval(id);
  }, [reader, reload]);

  // Ages on screen track the clock between polls.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  if (reader === null || read.status === "refused" || read.status === "loading") return null;
  if (read.status === "error" || read.data === null) {
    return (
      <section className="ne-models__serving" aria-label="Serving health">
        <ServingIntro />
        <LoadError things="serving status" onRetry={read.reload} />
      </section>
    );
  }
  if (read.data.lanes.length === 0) return null;
  return (
    <section className="ne-models__serving" aria-label="Serving health">
      <ServingIntro />
      <ul className="ne-models__servinglist">
        {read.data.lanes.map((lane) => (
          <Row key={`${lane.lane} ${lane.model_id}`} lane={lane} now={now} />
        ))}
      </ul>
    </section>
  );
}

function ServingIntro() {
  return (
    <div className="ne-models__servingintro">
      <h2 className="ne-models__servingtitle">Private serving health</h2>
      <p className="ne-models__servingcontext">
        A healthy endpoint is not the same as &ldquo;available for this run in Agents.&rdquo; The
        server decides availability for each run.
      </p>
    </div>
  );
}
