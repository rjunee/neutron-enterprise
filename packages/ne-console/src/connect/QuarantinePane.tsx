// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The quarantined-record detail pane (NE-CONNECT-CSV): the row, the field, the
// offending value, the validator's reason — the beat the quarantine count
// exists to back up. Everything rendered here is a field of the ops-gated
// drill-down read; the console adds no interpretation.
//
// The payload is the parked ROW as the source sheet had it (string values
// bounded server-side). It renders verbatim in source order: an operator
// comparing this pane to the sheet must see the sheet's words, including the
// trailing space or the mojibake escape that IS the defect.

import type { QuarantinedItemView } from "../api/types";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { absoluteTime } from "../primitives/format";

export function QuarantinePane({
  item,
  onClose,
}: {
  item: QuarantinedItemView;
  onClose: () => void;
}) {
  return (
    <DetailPane title={item.record_id ?? item.item_id} onClose={onClose}>
      <DetailFields>
        <DetailField label="Record" mono>
          {item.record_id ?? item.item_id}
        </DetailField>
        <DetailField label="Source" mono>
          {item.source ?? "—"}
        </DetailField>
        <DetailField label="Contract" mono>
          {item.shape || "—"}
        </DetailField>
        <DetailField label="Parked">
          {item.created_at === "" ? (
            "—"
          ) : (
            <time dateTime={item.created_at}>{absoluteTime(item.created_at)}</time>
          )}
        </DetailField>
      </DetailFields>

      <h3 className="ne-sources__qpanetitle">Reason</h3>
      <p className="ne-sources__qpanereason">{item.reason}</p>

      <h3 className="ne-sources__qpanetitle">Record as received</h3>
      <DetailFields>
        {Object.entries(item.payload).map(([field, value]) => (
          <DetailField key={field} label={field} mono>
            {typeof value === "string" ? value : JSON.stringify(value)}
          </DetailField>
        ))}
      </DetailFields>
      {item.payload_truncated ? (
        <p className="ne-sources__qpanenote">Long values are truncated in this view.</p>
      ) : null}
    </DetailPane>
  );
}
