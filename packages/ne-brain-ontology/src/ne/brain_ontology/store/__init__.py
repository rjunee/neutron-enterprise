# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Package-private triple-store backends (NE-085-spike).

Backend classes are deliberately NOT re-exported from ``ne.brain_ontology``:
the backend-privacy invariant (spec §5.1) binds when the ValidatingTripleStore
gateway lands in 085a — the raw store must only ever be reachable through the
governed writer, never as a plugin or a package export.
"""
