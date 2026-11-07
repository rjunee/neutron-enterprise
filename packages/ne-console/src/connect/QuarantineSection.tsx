// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9's quarantine section: a heading carrying the total, the Reason · Count
// category table, and — for an ops principal — the parked RECORDS themselves
// (NE-CONNECT-CSV): which record failed, from which source, for exactly what
// reason. A count with no drill-down is a control rendered as decoration; the
// rows are the inspectability the quarantine lane exists for.
//
// A TERMINAL NULL TOTAL IS UNREADABLE, NEVER ZERO. While the read is still in
// flight the heading stays neutral and the table owns the one loading
// announcement. Once the read terminates, null says the store could not be read
// and its `title` says why in the words §9 gives. A zero there would be a
// measurement nobody took.
//
// The reason labels are the server's sanitized categories, rendered as sent.
// The record rows are the ops-gated drill-down read; a refused or failed
// drill-down renders NO rows — never a locally-fabricated empty quarantine.

import { useId } from "react";
import type { QuarantinedItemView, QuarantineReasonView } from "../api/types";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { Empty } from "../primitives/states";
import { formatCount, relativeTime } from "../primitives/format";
import { quarantineEmptyLine, quarantineHeading } from "./sources";

const COLUMNS: ReadonlyArray<Column<QuarantineReasonView>> = [
  {
    key: "reason",
    header: "Reason",
    cell: (row) => row.reason,
    sortValue: (row) => row.reason,
  },
  {
    key: "count",
    header: "Count",
    numeric: true,
    width: "120px",
    cell: (row) => formatCount(row.count),
    sortValue: (row) => row.count,
    firstSort: "desc",
  },
];

/** The record identity an operator reads: the source system's own id, else the
 * park's item id (a hash — still an identity, honestly labelled by the column). */
function recordLabel(row: QuarantinedItemView): string {
  return row.record_id ?? row.item_id;
}

const ITEM_COLUMNS: ReadonlyArray<Column<QuarantinedItemView>> = [
  {
    key: "record",
    header: "Record",
    width: "160px",
    cell: (row) => <span className="ne-mono">{recordLabel(row)}</span>,
    sortValue: (row) => recordLabel(row),
  },
  {
    key: "source",
    header: "Source",
    width: "120px",
    cell: (row) => row.source ?? "",
    sortValue: (row) => row.source ?? "",
  },
  {
    key: "reason",
    header: "Reason",
    cell: (row) => <span className="ne-sources__qreason">{row.reason}</span>,
    sortValue: (row) => row.reason,
  },
  {
    key: "when",
    header: "Parked",
    width: "120px",
    cell: (row) => relativeTime(row.created_at),
    sortValue: (row) => row.created_at,
    firstSort: "desc",
  },
];

export function QuarantineSection({
  total,
  reasons,
  status,
  items,
  onOpenItem,
  selectedItemId,
}: {
  total: number | null;
  reasons: readonly QuarantineReasonView[];
  status: TableStatus;
  /** The ops-gated drill-down rows, or null when that read was refused/failed
   * — null renders NO records table (the categories above still stand). */
  items: readonly QuarantinedItemView[] | null;
  onOpenItem: (item: QuarantinedItemView) => void;
  selectedItemId: string | null;
}) {
  const headingId = useId();
  const loading = status === "loading";
  const unreadable = !loading && total === null;
  const heading = loading ? "Quarantine" : quarantineHeading(total);

  return (
    <section className="ne-sources__quarantine" aria-labelledby={headingId}>
      <h2
        className="ne-sources__sectiontitle"
        id={headingId}
        title={unreadable ? "The quarantine store could not be read" : undefined}
      >
        {heading}
      </h2>
      <DataTable<QuarantineReasonView>
        columns={COLUMNS}
        rows={reasons}
        rowKey={(row) => row.reason}
        label="Quarantine reasons"
        things="quarantine reasons"
        defaultSort={{ key: "count", dir: "desc" }}
        status={status}
        empty={<Empty message={quarantineEmptyLine(total)} />}
      />
      {loading || items === null || items.length === 0 ? null : (
        <DataTable<QuarantinedItemView>
          columns={ITEM_COLUMNS}
          rows={items}
          rowKey={(row) => row.item_id}
          label="Quarantined records"
          things="quarantined records"
          defaultSort={{ key: "when", dir: "desc" }}
          status={status}
          onRowActivate={onOpenItem}
          selectedKey={selectedItemId}
          empty={<Empty message="Nothing is quarantined." />}
        />
      )}
    </section>
  );
}
