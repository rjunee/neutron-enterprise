# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CSV-file connector (NE-CONNECT-CSV): a polled spreadsheet export as a source.

A hand-maintained sheet is the most universal enterprise data source there is —
and the one that actually produces contract violations. This connector treats a
CSV file as a POLLED source (the same ``*/15 * * * *`` cron lane as every other
pull sync), never a one-time import: every run re-reads the file as a full
snapshot, the content-hash dedup ledger makes an unchanged file a no-op, a fixed
row leaves quarantine on the next poll, and a row that fails the operator-owned
ODCS data contract is parked inspectably with the CONTRACT VALIDATOR'S OWN
message as the reason.

The package layout mirrors :mod:`ne.connect.sql`: ``config`` (typed fail-closed
settings), ``connector`` (the frozen ``Connector`` protocol impl), ``contract``
(the ODCS contract builder), ``sync`` (the governed ``run_csv_sync`` entrypoint
registered in :data:`ne.connect.runner.SYNC_SOURCES`).
"""

from ne.connect.csvfile.config import CsvConnectorSettings
from ne.connect.csvfile.connector import CsvConnector
from ne.connect.csvfile.contract import csv_data_contract
from ne.connect.csvfile.sync import CsvSyncOutcome, run_csv_sync

__all__ = [
    "CsvConnector",
    "CsvConnectorSettings",
    "CsvSyncOutcome",
    "csv_data_contract",
    "run_csv_sync",
]
