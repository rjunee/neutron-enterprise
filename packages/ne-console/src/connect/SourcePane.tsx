// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9's detail pane — "the same fields absolute".
//
// The table gives the relative form because a column of "6 min ago" is what a
// weekly sweep reads down. The pane gives the instant, because the moment
// someone opens a row they are about to write the time into a ticket.
//
// The pane offers NO ACTION. §9: "This screen is read-only in v1: the API
// exposes no sync-trigger or connector-config write." A Sync now button that
// 404s is worse than no button, and a console that writes connector credentials
// is a second privileged path into the platform.

import { Link } from "react-router";
import type { ConnectorStatusView } from "../api/types";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatusPill } from "../primitives/StatePill";
import { absoluteTime, formatCount } from "../primitives/format";
import { lastCheckedAt, newestRecordAt } from "./sources";

export function SourcePane({
  source,
  loadedAt,
  /** True only when the server has POSITIVELY refused this key the ops surface.
   * §1.2's rule, applied to a link instead of a nav item: unknown is not denied,
   * the link still reaches the server, and the server still refuses it. */
  opsRefused,
  onClose,
}: {
  source: ConnectorStatusView;
  loadedAt: number;
  opsRefused: boolean;
  onClose: () => void;
}) {
  const checkedAt = lastCheckedAt(source, loadedAt);
  const recordAt = newestRecordAt(source, loadedAt);
  const seq = source.last_sync_seq;

  return (
    <DetailPane title={source.id} onClose={onClose}>
      <DetailFields>
        <DetailField label="Source" mono>
          {source.id}
        </DetailField>
        <DetailField label="Kind">{source.kind}</DetailField>
        <DetailField label="Posture">
          {source.posture === "snapshot" ? (
            <span className="ne-sources__snapshot" aria-label="Frozen snapshot">
              Frozen snapshot
            </span>
          ) : (
            "Live"
          )}
        </DetailField>
        {/* Two freshness facts, each under its own name (SPEC §12 item 9): when
            the sync cycle last RAN, and how fresh the newest record we hold is.
            A quiet source is "checked two minutes ago, newest record yesterday"
            — both true, and neither may borrow the other's label. */}
        <DetailField label="Last checked">
          {checkedAt === null ? (
            <span className="ne-absent" title="No sync cycle has run for this source">
              Never checked
            </span>
          ) : (
            <time dateTime={checkedAt}>{absoluteTime(checkedAt)}</time>
          )}
        </DetailField>
        <DetailField label="Newest record">
          {recordAt === null ? (
            <span className="ne-absent" title="No record cursor recorded">
              —
            </span>
          ) : (
            <time dateTime={recordAt}>{absoluteTime(recordAt)}</time>
          )}
        </DetailField>
        <DetailField label="Permissions check">
          {source.acl_mirror === "ok" ? (
            <StatusPill word="OK" detail="at last check" />
          ) : source.acl_mirror === "degraded" ? (
            <StatusPill word="DEGRADED" detail="at last check" />
          ) : (
            <span className="ne-absent" title="No sync recorded yet">
              —
            </span>
          )}
        </DetailField>
        <DetailField label="Quarantined">
          {source.quarantined_count === null ? (
            <span className="ne-absent" title="Quarantine store unreadable">
              —
            </span>
          ) : (
            formatCount(source.quarantined_count)
          )}
        </DetailField>
      </DetailFields>

      {/* The sync record is the entry the sync wrote. It is omitted when there
          is none: a line reading "entry —" is a record that does not exist,
          dressed as one, and "Never checked" above already says so. */}
      {seq === null ? null : (
        <p className="ne-sources__syncrecord">
          {opsRefused ? (
            `Sync record: entry ${seq}`
          ) : (
            <Link to={`/audit?seq=${seq}`}>Sync record: entry {seq}</Link>
          )}
        </p>
      )}

      {source.acl_mirror === "degraded" ? (
        <p className="ne-sources__degraded">
          The permissions mirror failed its last check. Results from this source are withheld until
          it passes.
        </p>
      ) : null}
    </DetailPane>
  );
}
