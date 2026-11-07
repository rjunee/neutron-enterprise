// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13's row detail pane — every field of the record, then the payload, then the
// two things a reviewer does next: replay the decision, or open the run.
//
// THIN CLIENT (C7). The hashes are displayed. They are never compared, and
// there is deliberately no code path here that could compare them: the chain
// verdict on the strip above is the server's, and it is the only one.

import { useCallback, useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import type { ApiClient } from "../api/client";
import type { AuditRecordView, ReplayResultView } from "../api/types";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { HashChip } from "../primitives/HashChip";
import { PrincipalLabel } from "../primitives/PrincipalLabel";
import { ToolbarButton } from "../primitives/Toolbar";
import { Refused } from "../primitives/states";
import { ABSENT, relativeTime } from "../primitives/format";
import { paneTime, payloadText } from "./ledger";

type ReplayStatus = "idle" | "loading" | "ok" | "refused" | "error";

/** The replay result's own event list, in the §2.1 table grammar. Narrow — it
 * sits inside a 420px pane — so it carries the three columns that identify a
 * record and leaves the rest to the row the pane is already open on. */
const REPLAY_COLUMNS: ReadonlyArray<Column<AuditRecordView>> = [
  {
    key: "seq",
    header: "Seq",
    numeric: true,
    width: "64px",
    cell: (row) => row.seq,
    sortValue: (row) => row.seq,
    firstSort: "desc",
  },
  {
    key: "time",
    header: "Time",
    numeric: true,
    width: "88px",
    cell: (row) => (
      <time dateTime={row.occurred_at} title={paneTime(row.occurred_at)}>
        {relativeTime(row.occurred_at)}
      </time>
    ),
    sortValue: (row) => Date.parse(row.occurred_at),
    firstSort: "desc",
  },
  {
    key: "action",
    header: "Action",
    mono: true,
    cell: (row) => row.action ?? row.event_type,
    sortValue: (row) => row.action ?? row.event_type,
  },
];

export function RecordPane({
  record,
  client,
  onClose,
}: {
  record: AuditRecordView;
  client: ApiClient;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<ReplayStatus>("idle");
  const [replay, setReplay] = useState<ReplayResultView | null>(null);

  const runReplay = useCallback(async () => {
    setStatus("loading");
    try {
      const result = await client.replayAudit({ event_id: record.event_id });
      setReplay(result);
      setStatus("ok");
    } catch (err) {
      setReplay(null);
      // A refusal is ONLY a refusal when the server said 403. A network failure
      // rendered as a role boundary is a false statement about authorization.
      setStatus(err instanceof ApiError && err.status === 403 ? "refused" : "error");
    }
  }, [client, record.event_id]);

  return (
    <DetailPane
      title={`Entry ${record.seq}`}
      subtitle={record.action ?? record.event_type}
      onClose={onClose}
      footer={
        <>
          <ToolbarButton label="Replay decision" onClick={() => void runReplay()} />
          {record.trace_id === null ? null : (
            <Link className="ne-btn ne-btn--quiet" to={`/trace/${record.trace_id}`}>
              Open trace
            </Link>
          )}
        </>
      }
    >
      {/* §13 render: "record pane in head/fields/payload/foot anatomy,
          prev/this hash chips" — the chain's own place in it, ahead of the
          record's fields, so the two hashes that prove continuity read as a
          segment of the chain rather than two more rows in a flat list. */}
      <div className="ne-audit__chainseg">
        <div className="ne-audit__chainsegrow">
          <span className="ne-audit__chainseglabel">Previous</span>
          <HashChip hash={record.prev_hash} label="Copy previous hash" />
        </div>
        <div className="ne-audit__chainsegrow">
          <span className="ne-audit__chainseglabel">This record</span>
          <HashChip hash={record.record_hash} label="Copy record hash" />
        </div>
      </div>

      <DetailFields>
        <DetailField label="Time">
          <time dateTime={record.occurred_at}>{paneTime(record.occurred_at)}</time>
        </DetailField>
        <DetailField label="Actor">
          <PrincipalLabel
            id={record.actor}
            reference={{ scope: "audit", reference: record.event_id }}
          />
        </DetailField>
        <DetailField label="Resource" mono>
          {record.resource ?? ABSENT}
        </DetailField>
        <DetailField label="Outcome">{record.outcome ?? ABSENT}</DetailField>
        <DetailField label="Event id" mono>
          {record.event_id}
        </DetailField>
        <DetailField label="Event type" mono>
          {record.event_type}
        </DetailField>
        <DetailField label="KPI tag">{record.kpi_tag ?? ABSENT}</DetailField>
        <DetailField label="Trace id" mono>
          {record.trace_id ?? ABSENT}
        </DetailField>
        <DetailField label="Configuration">
          <HashChip hash={record.config_hash} label="Copy configuration hash" />
        </DetailField>
      </DetailFields>

      <section className="ne-audit__payload">
        <h3 className="ne-audit__paneheading">Payload</h3>
        <pre className="ne-audit__json">{payloadText(record.payload)}</pre>
      </section>

      {status === "idle" ? null : (
        <section className="ne-audit__replay">
          <h3 className="ne-audit__paneheading">Replay</h3>
          {/* A refusal here is the SERVER's, and it is a role boundary — never
              the fault colour, and never rendered over a network failure. */}
          {status === "refused" ? <Refused thing="replaying a decision" page="Replay" /> : null}
          {status === "error" ? (
            <div className="ne-state ne-state--error" role="alert">
              <p className="ne-state__line">Couldn&rsquo;t replay this decision.</p>
              <ToolbarButton label="Retry" onClick={() => void runReplay()} />
            </div>
          ) : null}
          {status === "loading" || status === "ok" ? (
            <>
              <DataTable<AuditRecordView>
                columns={REPLAY_COLUMNS}
                rows={replay?.events ?? []}
                rowKey={(row) => row.event_id}
                label="Replayed events"
                things="the replay"
                defaultSort={{ key: "seq", dir: "desc" }}
                status={status === "loading" ? "loading" : "ok"}
                empty={<p className="ne-state__line">No records behind this decision.</p>}
              />
              {status === "ok" && replay !== null ? (
                <dl className="ne-audit__replaylines">
                  <div className="ne-audit__replayline">
                    <dt>Config hashes:</dt>
                    <dd>
                      {replay.config_hashes.length === 0
                        ? ABSENT
                        : replay.config_hashes.map((hash) => (
                            <HashChip key={hash} hash={hash} label="Copy configuration hash" />
                          ))}
                    </dd>
                  </div>
                  <div className="ne-audit__replayline">
                    <dt>Context:</dt>
                    <dd className="ne-mono">
                      {replay.context_ids.length === 0 ? ABSENT : replay.context_ids.join(", ")}
                    </dd>
                  </div>
                </dl>
              ) : null}
            </>
          ) : null}
        </section>
      )}
    </DetailPane>
  );
}
