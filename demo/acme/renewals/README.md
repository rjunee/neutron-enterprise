<!-- Copyright (c) 2026 Ryan Junee. SPDX-License-Identifier: Apache-2.0 -->
# Acme renewals tracker — a FIXTURE, and honestly messy

**Everything in this directory is fixture data.** No live customer system is behind it —
exactly like the rest of the Acme corpus. It exists so the demo can show the write-path
quarantine doing its real job against the most universal enterprise source there is: a
hand-maintained spreadsheet export.

- `acme-renewals.csv` — the sheet the (fictional) Acme ops team keeps of contract
  renewals. 19 data rows: 13 clean, 6 carrying the defects that actually accumulate in a
  maintained sheet — a date typed `ASAP`, a value entered `N/A`, a row whose id was never
  assigned, a row with deleted cells, a stale duplicate of `RNW-1003`, and one cp1252
  byte (`0xF1`) pasted into the UTF-8 export. One clean row (`RNW-1013`) carries a
  trailing space on its status **on purpose**: surrounding whitespace is table-stakes
  CSV normalisation, so the connector trims field edges before validation and the row
  ingests with status `renewing` — it is the exhibit that edge whitespace is handled,
  not quarantined. **Do not "fix" this file** — the defects (and that trailing space)
  are the exhibit. The demo's own copy (staged by `ne demo up` into the demo workdir)
  is the one an operator edits live.
- `renewals.schema.json` — the ODCS row contract the connector validates against. The
  quarantine reasons a viewer reads are this schema applied by the platform's ordinary
  contract validator (`OdcsDataContract.validate`) — never scripted strings.

The connector is the ordinary `csv` pull source (`ne.connect.csvfile`), registered in
`NE_CONNECT_SYNC_SOURCES` and driven by the same cron as every other connector: the sheet
is POLLED, not imported. Re-polling an unchanged file is a no-op; fixing a row in the
staged copy clears its quarantine entry on the next poll; deleting a row reconciles its
entry away.
