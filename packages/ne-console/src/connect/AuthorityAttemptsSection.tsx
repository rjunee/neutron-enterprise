// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Thin ops surface over the server-filtered current authority-attempt queue.

import { useMemo } from "react";
import type { AuthorityAttemptView } from "../api/types";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { Empty, LoadError } from "../primitives/states";
import { useAuthorityAttempts } from "./useAuthorityAttempts";

function stageLabel(stage: AuthorityAttemptView["stage"]): string {
  switch (stage) {
    case "claimed":
      return "Claimed";
    case "deletion_validated":
      return "Deletion validated";
    case "authority_tombstoned":
      return "Authority tombstoned";
  }
}

export function AuthorityAttemptsSection({
  knownRefused,
  deferred,
}: {
  knownRefused: boolean;
  deferred: boolean;
}) {
  const attempts = useAuthorityAttempts(knownRefused, deferred);
  const rows = attempts.data?.attempts ?? [];
  const columns = useMemo<readonly Column<AuthorityAttemptView>[]>(
    () => [
      {
        key: "attempt",
        header: "Attempt",
        mono: true,
        cell: (row) => row.attempt_id,
      },
      {
        key: "source",
        header: "Source",
        cell: (row) => row.source_kind,
      },
      {
        key: "stage",
        header: "Stage",
        cell: (row) => stageLabel(row.stage),
      },
      {
        key: "revision",
        header: "Source revision",
        mono: true,
        cell: (row) => row.source_revision,
      },
      {
        key: "retry",
        header: "Retry",
        cell: (row) => (
          <button
            type="button"
            className="ne-btn ne-btn--quiet"
            aria-label={`Retry authority attempt: ${row.attempt_id}`}
            onClick={() => attempts.retry(row.attempt_id)}
          >
            Retry
          </button>
        ),
      },
    ],
    [attempts],
  );

  // A server 403 means this deployment does not expose this ops queue to the
  // caller. It is absent, never rendered as an empty queue or a broken page.
  if (deferred || attempts.status === "refused") return null;
  const tableStatus: TableStatus = attempts.status;

  return (
    <section className="ne-sources__authority" aria-labelledby="authority-attempts-title">
      <h2 id="authority-attempts-title" className="ne-sources__sectiontitle">
        Authority recovery
      </h2>
      {attempts.retrying === null ? null : (
        <p className="ne-sources__authoritynote" role="status">
          Queuing governed retry…
        </p>
      )}
      {attempts.retryFailed === null ? null : (
        <p className="ne-sources__authoritynote" role="alert">
          Retry was not accepted. The current queue was reloaded.
        </p>
      )}
      <DataTable<AuthorityAttemptView>
        columns={columns}
        rows={rows}
        rowKey={(row) => row.attempt_id}
        label="Authority recovery attempts"
        things="authority recovery attempts"
        status={tableStatus}
        empty={<Empty message="No authority retries pending." />}
        error={<LoadError things="authority recovery attempts" onRetry={attempts.refetch} />}
      />
    </section>
  );
}
