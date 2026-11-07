# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``python -m ne.memory_graph`` delegates to the CLI."""

from __future__ import annotations

from ne.memory_graph.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
