# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tier-2 graph errors.

Fail-closed at the write boundary: an un-ACL'd node/edge upsert is a
:class:`GraphAclError` (the ``AclMapper.resolve`` never-empty guarantee mirrored
onto the store), never a silent unlabeled write. Store/DDL faults are
:class:`GraphStoreError`.
"""

from __future__ import annotations


class GraphStoreError(Exception):
    """A graph store / adapter fault (connection, DDL, cypher execution)."""


class GraphAclError(GraphStoreError):
    """A node or edge was written without a non-empty ``acl`` (fail-closed)."""
