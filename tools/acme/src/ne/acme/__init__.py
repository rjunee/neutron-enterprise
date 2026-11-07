# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-acme`` — the Acme Corp synthetic corpus generator + recorded CI fixtures
(NE-074). See ``tools/acme/README.md`` for the full artifact/ACL/determinism
contract.
"""

from __future__ import annotations

from ne.acme.generator import check, generate, serialize
from ne.acme.pools import CORPUS_VERSION, DEFAULT_SEED

__version__ = "0.1.0"

__all__ = [
    "CORPUS_VERSION",
    "DEFAULT_SEED",
    "__version__",
    "check",
    "generate",
    "serialize",
]
