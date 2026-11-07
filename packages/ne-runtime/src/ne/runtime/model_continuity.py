# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Content-free identities and durable receipts for between-Run model continuity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

import sqlalchemy as sa

from ne.runtime.store import metadata

AGENT_CONTEXT_DOMAIN = b"ne.api.agent-context-ledger.v1\x00"
# Conservative content-free admission reserve for instructions, the governed
# Tool catalog and message serialization. UTF-8 byte count itself upper-bounds
# ordinary tokenizer counts; this reserve covers the non-history prompt head.
CONTINUITY_PROMPT_RESERVE_TOKENS = 8192


def canonical_hash(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(domain + encoded).hexdigest()


def context_snapshot_hash(
    *, agent_id: str, conversation_id: str, component_ids: Iterable[str]
) -> str:
    """Hash the complete ordered content identity, never selection/cache facts."""
    chain = canonical_hash(
        AGENT_CONTEXT_DOMAIN + b"chain-start\x00",
        {"agent_id": agent_id, "conversation_id": conversation_id},
    )
    total = 0
    for component_id in component_ids:
        total += 1
        chain = canonical_hash(
            AGENT_CONTEXT_DOMAIN + b"chain-link\x00",
            {"component_id": component_id, "prior": chain},
        )
    return canonical_hash(
        AGENT_CONTEXT_DOMAIN + b"snapshot\x00",
        {
            "context_chain_hash": chain,
            "conversation_id": conversation_id,
            "total_components": total,
        },
    )


def context_component_id(
    *,
    run_id: str,
    attempt_id: str,
    turn_id: str,
    turn_sequence: int,
    attachment_id: str,
    protected_hash: str,
    content_status: str,
) -> tuple[str, str]:
    target: Mapping[str, object] = {
        "attachment_id": attachment_id,
        "attempt_id": attempt_id,
        "conversation_turn_sequence": turn_sequence,
        "run_id": run_id,
        "turn_id": turn_id,
    }
    input_target_id = canonical_hash(AGENT_CONTEXT_DOMAIN + b"input-target\x00", target)
    component = {
        "content_status": content_status,
        "input_target_id": input_target_id,
        "terminal_hash": protected_hash,
        "turn_sequence": turn_sequence,
    }
    return input_target_id, canonical_hash(AGENT_CONTEXT_DOMAIN + b"component\x00", component)


ne_agent_model_boundary_receipts = sa.Table(
    "ne_agent_model_boundary_receipts",
    metadata,
    sa.Column("receipt_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, unique=True),
    sa.Column("previous_run_id", sa.String, nullable=True),
    sa.Column("conversation_id", sa.String, nullable=False, index=True),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("department", sa.String, nullable=False),
    sa.Column("agent_id", sa.String, nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("agent_definition_hash", sa.String(64), nullable=False),
    sa.Column("model_policy_id", sa.String, nullable=False),
    sa.Column("before_requested_model", sa.String, nullable=True),
    sa.Column("before_served_model", sa.String, nullable=True),
    sa.Column("after_requested_model", sa.String, nullable=True),
    sa.Column("after_authorized_model", sa.String, nullable=False),
    sa.Column("after_authorized_lane", sa.String, nullable=False),
    sa.Column("context_snapshot_hash", sa.String(64), nullable=False),
    sa.Column("context_fit", sa.String, nullable=False),
    sa.Column("estimated_input_tokens", sa.Integer, nullable=False),
    sa.Column("target_input_budget", sa.Integer, nullable=False),
    sa.Column("compaction", sa.String, nullable=False),
    sa.Column("cache_impact", sa.String, nullable=False),
    sa.Column("authorization_receipt_hash", sa.String(64), nullable=False),
    sa.Column("before_config_hash", sa.String(64), nullable=True),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(["run_id"], ["ne_agent_runs.run_id"]),
    sa.ForeignKeyConstraint(["previous_run_id"], ["ne_agent_runs.run_id"]),
    sa.CheckConstraint("agent_version >= 1", name="ck_model_boundary_agent_version"),
    sa.CheckConstraint(
        "context_fit IN ('fits','compaction_required','unknown')",
        name="ck_model_boundary_context_fit",
    ),
    sa.CheckConstraint(
        "estimated_input_tokens >= 0 AND target_input_budget >= 1",
        name="ck_model_boundary_fit_counts",
    ),
    sa.CheckConstraint(
        "compaction IN ('not_required','required_unavailable','unknown')",
        name="ck_model_boundary_compaction",
    ),
    sa.CheckConstraint(
        "cache_impact IN ('reset_expected','reuse_possible','unknown')",
        name="ck_model_boundary_cache_impact",
    ),
)


ne_agent_run_model_steps = sa.Table(
    "ne_agent_run_model_steps",
    metadata,
    sa.Column("model_step_id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, nullable=False, index=True),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("run_event_id", sa.String, nullable=False, unique=True),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("model_policy_id", sa.String, nullable=False),
    sa.Column("request_mode", sa.String, nullable=False),
    sa.Column("requested_model", sa.String, nullable=True),
    sa.Column("served_model", sa.String, nullable=False),
    sa.Column("served_lane", sa.String, nullable=False),
    # WHY THE SERVED MODEL IS THE SERVED MODEL. ``served_model`` alone cannot
    # tell "the route's first attempt answered" apart from "the first attempt
    # failed and the next candidate answered instead" -- and the second is an
    # INVOLUNTARY PROVIDER FALLBACK, a governance event the product claims can
    # never be silent. The gateway has always recorded it (ne-gateway
    # ``_routing_provenance``); it stopped at the trace wire and never reached
    # the Run. These two columns are that fact's durable home.
    #
    # NAMES AND POSITIONS ONLY. ``fallback_from`` is a comma-separated list of
    # DECLARED MODEL NAMES and the writer enforces that shape character by
    # character (``record_model_step``). Provider error text may echo the prompt
    # back (design 10.3 rule 5) and an ``api_base`` can carry a credential;
    # neither may ever ride a column a console renders.
    #
    # NULLABLE, and never defaulted: a step recorded before this column existed
    # knows no route position, and writing 'primary' onto it would fabricate the
    # exact governance claim the column was added to make honest. NULL projects
    # as "unavailable", not as "served as requested".
    sa.Column("route_position", sa.String, nullable=True),
    sa.Column("fallback_from", sa.String(256), nullable=True),
    sa.Column("authorization_receipt_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("run_id", "attempt_id", "ordinal", name="uq_run_model_step_ordinal"),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id"],
        ["ne_agent_run_attempt_links.attempt_id", "ne_agent_run_attempt_links.run_id"],
    ),
    sa.ForeignKeyConstraint(
        ["run_event_id", "run_id", "attempt_id"],
        [
            "ne_agent_run_events.event_id",
            "ne_agent_run_events.run_id",
            "ne_agent_run_events.attempt_id",
        ],
    ),
    sa.CheckConstraint("ordinal >= 0", name="ck_run_model_step_ordinal"),
    sa.CheckConstraint("request_mode IN ('auto','explicit')", name="ck_run_model_step_mode"),
    sa.CheckConstraint(
        "(request_mode = 'auto' AND requested_model IS NULL) OR "
        "(request_mode = 'explicit' AND requested_model IS NOT NULL)",
        name="ck_run_model_step_request_truth",
    ),
    sa.CheckConstraint(
        "served_lane IN ('frontier-api','private-vllm')", name="ck_run_model_step_lane"
    ),
    # A NULL route position is the "recorded before this fact existed" case and
    # passes both checks by SQL's own NULL semantics -- which is what we want:
    # unknown is not a third position, it is the absence of the record.
    sa.CheckConstraint(
        "route_position IN ('primary','fallback')", name="ck_run_model_step_route_position"
    ),
    # ``fallback_from`` names what was PASSED OVER, so it exists exactly when
    # something was: a 'primary' row carrying names, or a 'fallback' row
    # carrying none, is a contradiction and never reaches the wire.
    sa.CheckConstraint(
        "(route_position = 'fallback') = (fallback_from IS NOT NULL)",
        name="ck_run_model_step_fallback_truth",
    ),
)


__all__ = [
    "AGENT_CONTEXT_DOMAIN",
    "CONTINUITY_PROMPT_RESERVE_TOKENS",
    "canonical_hash",
    "context_component_id",
    "context_snapshot_hash",
    "ne_agent_model_boundary_receipts",
    "ne_agent_run_model_steps",
]
