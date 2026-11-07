# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The workflow signature — the content-addressed identity of a workflow shape.

:func:`workflow_signature` is a sha256 hex over the ORDERED, names-only tuple of
``step.action`` values in a :class:`~ne.contracts.skills.WorkflowSnapshot`. It is
the workflow's identity: two snapshots with the same ordered action sequence
produce the same signature, so the forge de-duplicates them into ONE
``skill_candidate`` proposal (via the content-addressed ``proposal_id``).

Order-sensitivity is DELIBERATE: ``[a, b]`` and ``[b, a]`` are different
workflows and hash differently. Tool/action names are canonical identifiers and
are NOT lowercased — a name is what the executor registered. No argument values
enter the signature (there are none in a ``WorkflowStep`` by construction, C12).
"""

from __future__ import annotations

import hashlib
import json

from ne.contracts.skills import WorkflowSnapshot


def workflow_signature(snapshot: WorkflowSnapshot) -> str:
    """Return the sha256 hex of the ordered, names-only ``step.action`` tuple.

    Deterministic and order-sensitive (the action order IS the workflow
    identity). An empty step list hashes the empty JSON array — distinct from any
    non-empty workflow.
    """
    canonical = json.dumps(
        [step.action for step in snapshot.steps], separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
