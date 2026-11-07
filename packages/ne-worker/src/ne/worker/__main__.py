# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``python -m ne.worker`` — the durable worker entry point (+ ``--health``)."""

from __future__ import annotations

from ne.worker.main import main

if __name__ == "__main__":
    main()
