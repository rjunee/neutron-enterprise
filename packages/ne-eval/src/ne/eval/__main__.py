# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``python -m ne.eval`` entrypoint."""

from __future__ import annotations

import sys

from ne.eval.cli import main

if __name__ == "__main__":
    sys.exit(main())
