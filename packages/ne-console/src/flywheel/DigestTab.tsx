// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT › DIGEST. What the steward surface did this week.
//
// Every number here is SERVER-COMPUTED and rendered as sent. Nothing on this tab
// aggregates, re-counts or interpolates: the only arithmetic is seconds → hours
// for display, and a null stays a null — zero decided proposals produce no
// median, and an absent median renders the em dash, never "0.0 h".
//
// The digest and the latency reads carry INDEPENDENT status. A latency outage
// must not blank the week's counts, and a refusal on either is the server's 403
// and is drawn for nothing else.

import { useCallback, useEffect, useState } from "react";
import type {
  StewardDigestView,
  StewardLatencyView,
  StewardSkillsDigestView,
  StewardLaneDigest,
  StewardSkillsFailure,
  StewardSkillsNearDup,
  StewardSkillsOverlap,
  StewardSkillsRetirement,
  StewardSkillsStaleProposal,
  StewardSkillsUnknownTools,
} from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { Empty, Loading, LoadError, Refused } from "../primitives/states";
import { formatCount } from "../primitives/format";
import { enumLabel, formatHours, shiftWeek } from "./queue";

type ReadStatus = "loading" | "ok" | "refused" | "error";

function readStatus(error: unknown): ReadStatus {
  return error instanceof ApiError && error.status === 403 ? "refused" : "error";
}

export function DigestTab() {
  const client = useApiClient();
  const runDigest = useLatestRequest();
  const runLatency = useLatestRequest();

  /** The week the NEXT read asks for. `null` is "the week the server calls
   * current" — the console does not compute which week that is. */
  const [weekStart, setWeekStart] = useState<string | null>(null);
  const [digest, setDigest] = useState<StewardDigestView | null>(null);
  const [digestStatus, setDigestStatus] = useState<ReadStatus>("loading");
  const [latency, setLatency] = useState<StewardLatencyView | null>(null);
  const [latencyStatus, setLatencyStatus] = useState<ReadStatus>("loading");

  // Each fetch SETTLES its stream and never resets it to loading: "loading" is
  // the state a stream starts in and the state an event handler puts it back
  // into (a week change, a Retry). Resetting it from inside the fetch would set
  // state synchronously on every mount, which is a cascading render and, worse,
  // a second place that decides what "loading" means.
  const fetchDigest = useCallback(async () => {
    const settled = await runDigest(() => client.stewardDigest(weekStart ?? undefined));
    if (settled.kind === "superseded") return;
    if (settled.kind === "success") {
      setDigest(settled.value);
      setDigestStatus("ok");
      return;
    }
    setDigest(null);
    setDigestStatus(readStatus(settled.error));
  }, [client, runDigest, weekStart]);

  const fetchLatency = useCallback(async () => {
    const settled = await runLatency(() => client.stewardLatency());
    if (settled.kind === "superseded") return;
    if (settled.kind === "success") {
      setLatency(settled.value);
      setLatencyStatus("ok");
      return;
    }
    setLatency(null);
    setLatencyStatus(readStatus(settled.error));
  }, [client, runLatency]);

  /** The week nav moves the window and puts the stream back into loading — in
   * the handler, where the intent is, not in the read. */
  const goToWeek = useCallback((next: string | null) => {
    if (next === null) return;
    setDigest(null);
    setDigestStatus("loading");
    setWeekStart(next);
  }, []);

  const retryLatency = useCallback(() => {
    setLatencyStatus("loading");
    void fetchLatency();
  }, [fetchLatency]);

  const retryDigest = useCallback(() => {
    setDigestStatus("loading");
    void fetchDigest();
  }, [fetchDigest]);

  useEffect(() => {
    void (async () => {
      await fetchDigest();
    })();
  }, [fetchDigest]);

  useEffect(() => {
    void (async () => {
      await fetchLatency();
    })();
  }, [fetchLatency]);

  if (digestStatus === "refused") {
    return <Refused thing="Improvement" page="Improvement" />;
  }

  return (
    <div className="ne-fly__digest">
      <nav className="ne-fly__weeknav" aria-label="Week">
        <button
          type="button"
          className="ne-btn ne-btn--quiet"
          disabled={digest === null}
          onClick={() => {
            if (digest !== null) goToWeek(shiftWeek(digest.week_start, -1));
          }}
        >
          ‹ previous week
        </button>
        {digest === null ? null : (
          // These two are CALENDAR DATES (`week_start: date` on the wire), not
          // instants, so they stay in the ISO form the server sent and the form
          // the rest of the console prints dates in. Measured: `absoluteTime`
          // parses "2026-08-24" as UTC midnight and reads it back with LOCAL
          // getters, so west of UTC it renders "2026-08-23 17:00:00" — a
          // fabricated time-of-day on the wrong day. `relativeTime` answers a
          // question a window label is not asking. Do not "format" these.
          <span className="ne-fly__weekwindow">
            <time dateTime={digest.week_start}>{digest.week_start}</time> →{" "}
            <time dateTime={digest.week_end}>{digest.week_end}</time>
          </span>
        )}
        <button
          type="button"
          className="ne-btn ne-btn--quiet"
          disabled={digest === null}
          onClick={() => {
            if (digest !== null) goToWeek(shiftWeek(digest.week_start, 1));
          }}
        >
          next week ›
        </button>
      </nav>

      <section className="ne-fly__section">
        <h2 className="ne-fly__sectiontitle">Week totals</h2>
        {digest?.scope === "principal_visible" ? (
          // 015A0-ENFORCEMENT: the server aggregated what THIS principal may
          // currently see. Rendering that as the deployment's week would state
          // a total nobody computed — and an all-withheld week is deliberately
          // byte-equivalent to an empty one, so the label is the only thing
          // that distinguishes them and it is the server's own word.
          <p className="ne-fly__scope" role="status">
            Counts what you can currently see, not the whole deployment.
          </p>
        ) : null}
        {digestStatus === "loading" ? <Loading things="the digest" /> : null}
        {digestStatus === "error" ? <LoadError things="the digest" onRetry={retryDigest} /> : null}
        {digest === null ? null : (
          <div className="ne-fly__tiles">
            <Tile label="Proposed" value={digest.totals.proposed} />
            <Tile label="Approved" value={digest.totals.approved} />
            <Tile label="Rejected" value={digest.totals.rejected} />
            <Tile label="Applied" value={digest.totals.applied} />
          </div>
        )}
      </section>

      {digest === null ? null : (
        <section className="ne-fly__section">
          <h2 className="ne-fly__sectiontitle">By lane</h2>
          <DataTable<StewardLaneDigest>
            columns={LANE_COLUMNS}
            rows={digest.by_lane}
            rowKey={(row) => row.lane}
            label="By lane"
            things="lanes"
            defaultSort={{ key: "lane", dir: "asc" }}
            empty={<Empty message="No steward activity this week." />}
          />
        </section>
      )}

      <section className="ne-fly__section">
        <h2 className="ne-fly__sectiontitle">Review latency</h2>
        {latencyStatus === "loading" ? <Loading things="review latency" /> : null}
        {latencyStatus === "refused" ? <Refused thing="Improvement" page="Improvement" /> : null}
        {latencyStatus === "error" ? (
          <LoadError things="review latency" onRetry={retryLatency} />
        ) : null}
        {latency === null ? null : (
          <p className="ne-fly__latency">
            Median wait {formatHours(latency.median_wait_s)} · p90 {formatHours(latency.p90_wait_s)}{" "}
            · {formatCount(latency.pending_count)} waiting · oldest{" "}
            {formatHours(latency.oldest_pending_age_s)}
          </p>
        )}
      </section>

      {/* 015A0-ENFORCEMENT: a principal-visible envelope may not carry the Skill
          card. The Gardener report is one GLOBAL fold, so attaching it to a
          scoped envelope would publish skill names this principal's own
          projection never authorized. The console drops it rather than
          rendering whatever arrived. */}
      {digest?.skills && digest.scope !== "principal_visible" ? (
        <SkillHealth skills={digest.skills} />
      ) : null}
    </div>
  );
}

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <div className="ne-fly__tile">
      <span className="ne-fly__tilelabel">{label}</span>
      <span className="ne-fly__tilevalue">{formatCount(value)}</span>
    </div>
  );
}

function countColumn<Row>(key: string, header: string, read: (row: Row) => number): Column<Row> {
  return {
    key,
    header,
    numeric: true,
    width: "96px",
    cell: (row) => formatCount(read(row)),
    sortValue: read,
    firstSort: "desc",
  };
}

const LANE_COLUMNS: ReadonlyArray<Column<StewardLaneDigest>> = [
  {
    key: "lane",
    header: "Lane",
    cell: (row) => enumLabel(row.lane),
    sortValue: (row) => row.lane,
  },
  countColumn<StewardLaneDigest>("proposed", "Proposed", (row) => row.proposed),
  countColumn<StewardLaneDigest>("approved", "Approved", (row) => row.approved),
  countColumn<StewardLaneDigest>("rejected", "Rejected", (row) => row.rejected),
  countColumn<StewardLaneDigest>("applied", "Applied", (row) => row.applied),
];

/**
 * §11's skill health: the rot-card tables as the server computed them. Each is
 * omitted-by-absence — when the server attached no run, the whole section is
 * absent, which is why it is a rendered card and never an empty placeholder.
 */
function SkillHealth({ skills }: { skills: StewardSkillsDigestView }) {
  return (
    <section className="ne-fly__section">
      <h2 className="ne-fly__sectiontitle">Skill health</h2>

      <h3 className="ne-fly__blocktitle">Retirement candidates</h3>
      <DataTable<StewardSkillsRetirement>
        columns={RETIREMENT_COLUMNS}
        rows={skills.retirement_candidates}
        rowKey={(row) => row.name}
        label="Retirement candidates"
        things="retirement candidates"
        defaultSort={{ key: "fired", dir: "desc" }}
        empty={<Empty message="No retirement candidates." />}
      />

      <h3 className="ne-fly__blocktitle">Failure flags</h3>
      <DataTable<StewardSkillsFailure>
        columns={FAILURE_COLUMNS}
        rows={skills.failure_flags}
        rowKey={(row) => row.name}
        label="Failure flags"
        things="failure flags"
        defaultSort={{ key: "ratio", dir: "desc" }}
        empty={<Empty message="No failure flags." />}
      />

      <h3 className="ne-fly__blocktitle">Trigger overlaps</h3>
      <DataTable<StewardSkillsOverlap>
        columns={OVERLAP_COLUMNS}
        rows={skills.trigger_overlaps}
        rowKey={(row) => row.phrase}
        label="Trigger overlaps"
        things="trigger overlaps"
        defaultSort={{ key: "phrase", dir: "asc" }}
        empty={<Empty message="No trigger overlaps." />}
      />

      <h3 className="ne-fly__blocktitle">Near-duplicates</h3>
      <DataTable<StewardSkillsNearDup>
        columns={NEARDUP_COLUMNS}
        rows={skills.near_dups}
        rowKey={(row) => `${row.a}|${row.b}`}
        label="Near-duplicates"
        things="near-duplicates"
        defaultSort={{ key: "score", dir: "desc" }}
        empty={<Empty message="No near-duplicates." />}
      />

      <h3 className="ne-fly__blocktitle">Unknown required tools</h3>
      <DataTable<StewardSkillsUnknownTools>
        columns={UNKNOWN_TOOL_COLUMNS}
        rows={skills.unknown_requires_tools}
        rowKey={(row) => row.name}
        label="Unknown required tools"
        things="unknown required tools"
        defaultSort={{ key: "skill", dir: "asc" }}
        empty={<Empty message="No unknown required tools." />}
      />

      <h3 className="ne-fly__blocktitle">Stale proposals</h3>
      <DataTable<StewardSkillsStaleProposal>
        columns={STALE_COLUMNS}
        rows={skills.stale_proposals}
        rowKey={(row) => row.proposal_id}
        label="Stale proposals"
        things="stale proposals"
        defaultSort={{ key: "age", dir: "desc" }}
        empty={<Empty message="No stale proposals." />}
      />
    </section>
  );
}

const RETIREMENT_COLUMNS: ReadonlyArray<Column<StewardSkillsRetirement>> = [
  {
    key: "name",
    header: "Name",
    mono: true,
    cell: (row) => row.name,
    sortValue: (row) => row.name,
  },
  { key: "reason", header: "Reason", cell: (row) => row.reason, sortValue: (row) => row.reason },
  countColumn<StewardSkillsRetirement>("fired", "Fired", (row) => row.fired_count),
];

// `ratio` is the server's own `failed / known_fires` — the two columns
// immediately to its left and nothing else: ne/skills/retirement.py:151-155
// computes it as `fail_count / (success_count + fail_count)`, deliberately
// excluding unknown-outcome fires. The header names that division, because
// "Ratio" left the reader to guess both the numerator and the scale.
const FAILURE_COLUMNS: ReadonlyArray<Column<StewardSkillsFailure>> = [
  {
    key: "name",
    header: "Name",
    mono: true,
    cell: (row) => row.name,
    sortValue: (row) => row.name,
  },
  countColumn<StewardSkillsFailure>("fires", "Fires", (row) => row.known_fires),
  countColumn<StewardSkillsFailure>("failed", "Failed", (row) => row.failed),
  {
    key: "ratio",
    header: "Failed / fires",
    numeric: true,
    width: "96px",
    cell: (row) => row.ratio.toFixed(2),
    sortValue: (row) => row.ratio,
    firstSort: "desc",
  },
];

const OVERLAP_COLUMNS: ReadonlyArray<Column<StewardSkillsOverlap>> = [
  {
    key: "phrase",
    header: "Phrase",
    cell: (row) => row.phrase,
    sortValue: (row) => row.phrase,
  },
  {
    key: "skills",
    header: "Skills",
    cell: (row) => row.names.join(", "),
    sortValue: (row) => row.names.join(", "),
  },
];

// `score` is a SIMILARITY — higher is more alike, bounded above by 1 — and the
// adjacent Method column is what gives it its unit: `trigger_phrase` is a flat
// 1.0 for an exact shared trigger phrase, `jaccard` is token-set Jaccard, and
// `embedder` is cosine (ne/skills/near_dup.py:102-141 and :144-179). "Score"
// stated neither the direction nor the kind, so 0.87 could as easily have been
// a distance. The pair columns carry two full skill names, not initials.
const NEARDUP_COLUMNS: ReadonlyArray<Column<StewardSkillsNearDup>> = [
  { key: "a", header: "Skill A", mono: true, cell: (row) => row.a, sortValue: (row) => row.a },
  { key: "b", header: "Skill B", mono: true, cell: (row) => row.b, sortValue: (row) => row.b },
  { key: "method", header: "Method", cell: (row) => row.method, sortValue: (row) => row.method },
  {
    key: "score",
    header: "Similarity",
    numeric: true,
    width: "96px",
    cell: (row) => row.score.toFixed(2),
    sortValue: (row) => row.score,
    firstSort: "desc",
  },
];

const UNKNOWN_TOOL_COLUMNS: ReadonlyArray<Column<StewardSkillsUnknownTools>> = [
  {
    key: "skill",
    header: "Skill",
    mono: true,
    cell: (row) => row.name,
    sortValue: (row) => row.name,
  },
  {
    key: "tools",
    header: "Tools",
    cell: (row) => row.unknown.join(", "),
    sortValue: (row) => row.unknown.join(", "),
  },
];

const STALE_COLUMNS: ReadonlyArray<Column<StewardSkillsStaleProposal>> = [
  {
    key: "id",
    header: "Id",
    mono: true,
    cell: (row) => row.proposal_id,
    sortValue: (row) => row.proposal_id,
  },
  { key: "kind", header: "Kind", cell: (row) => enumLabel(row.kind), sortValue: (row) => row.kind },
  { key: "name", header: "Name", cell: (row) => row.name, sortValue: (row) => row.name },
  {
    key: "age",
    header: "Age",
    numeric: true,
    width: "96px",
    cell: (row) => `${formatCount(row.age_days)} d`,
    sortValue: (row) => row.age_days,
    firstSort: "desc",
  },
];
