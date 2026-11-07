# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``BrainGraphSettings`` — the ``NE_BRAIN_GRAPH_`` house-style Tier-2 configuration.

An :class:`~ne.contracts.config.NeSettings` subclass (the ``PermissionsSettings``
pattern): values layer init > ``NE_BRAIN_GRAPH_`` env (``__`` nesting) > TOML >
defaults. The
declarative ``sources`` mapping (§4.3) is validated fail-fast at load against the
generated ``ENTITY_CLASSES``/``EDGE_DEFS`` allowlists so a graph/schema drift is a
settings-construction error, never a silent bad edge at sync time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.schema import EDGE_DEFS, ENTITY_CLASSES, ENTITY_CONTRACTS

__all__ = [
    "BrainGraphSettings",
    "ErClassConfig",
    "GraphEdgeMapping",
    "GraphEntityMapping",
    "GraphSourceMapping",
]

# (subject_class, edge_kind, object_class) -> allowed by the generated schema.
_EDGE_INDEX: frozenset[tuple[str, str, str]] = frozenset(
    (e.subject_class, e.edge_kind, e.object_class) for e in EDGE_DEFS
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GraphEdgeMapping(_FrozenModel):
    """Declares one FK column of a record projects to a typed edge.

    ``target_source_id_template`` substitutes ``{value}`` with the FK cell value to
    compute the target record's ``source_id`` (e.g. ``"{value}"`` for a Salesforce
    Account id, ``"public.accounts:{value}"`` for a SQL table row).
    """

    payload_key: str
    edge_kind: str
    target_class: str
    target_source_id_template: str = "{value}"
    target_record_id_template: str = "{value}"
    expected_value: str | None = None


class GraphEntityMapping(_FrozenModel):
    """Maps records under a ``source_id`` prefix to one entity class + its edges.

    ``fields`` maps contract-field name -> payload key (the projection validated
    through ``ENTITY_CONTRACTS[entity_class]``).
    """

    source_id_prefix: str
    entity_class: str
    fields: dict[str, str] = Field(default_factory=dict)
    edges: tuple[GraphEdgeMapping, ...] = ()

    def matches(self, source_id: str) -> bool:
        """A record belongs to this mapping iff its source_id equals the prefix
        or begins with ``prefix + ':'`` (the ``table_ref:id`` shape)."""
        return source_id == self.source_id_prefix or source_id.startswith(
            f"{self.source_id_prefix}:"
        )


class GraphSourceMapping(_FrozenModel):
    """All entity mappings for one connector source (``contract.source``)."""

    source_system: str
    entities: tuple[GraphEntityMapping, ...] = ()

    def resolve(self, source_id: str) -> GraphEntityMapping | None:
        """Longest-prefix-wins mapping for ``source_id`` (None if unmapped).

        Equal-length ambiguity is made unrepresentable at settings load, so the
        longest match here is always unique.
        """
        candidates = [m for m in self.entities if m.matches(source_id)]
        if not candidates:
            return None
        return max(candidates, key=lambda m: len(m.source_id_prefix))

    @model_validator(mode="after")
    def _validate(self) -> GraphSourceMapping:
        seen: set[str] = set()
        for mapping in self.entities:
            if mapping.entity_class not in ENTITY_CLASSES:
                raise ValueError(
                    f"unknown entity_class {mapping.entity_class!r} for source "
                    f"{self.source_system!r} (not in ENTITY_CLASSES)"
                )
            if mapping.source_id_prefix in seen:
                raise ValueError(
                    f"ambiguous equal-length source_id_prefix "
                    f"{mapping.source_id_prefix!r} in source {self.source_system!r} — "
                    "a record would match two mappings; prefixes must be unique"
                )
            seen.add(mapping.source_id_prefix)
            for edge in mapping.edges:
                triple = (mapping.entity_class, edge.edge_kind, edge.target_class)
                if triple not in _EDGE_INDEX:
                    raise ValueError(
                        f"edge {edge.edge_kind!r} "
                        f"({mapping.entity_class} -> {edge.target_class}) is not a valid "
                        "EDGE_DEFS relation for the operational schema"
                    )
        return self


class ErClassConfig(_FrozenModel):
    """Splink ER blocking + comparison config for one entity class (NE-082 §5.1).

    ``compare_fields`` feed the Splink comparison model; ``blocking_fields``
    feed the blocking rules. Both are validated against the entity contract's
    field set at settings load so a typo is a construction error, not a silent
    empty ER run.
    """

    compare_fields: tuple[str, ...]
    blocking_fields: tuple[str, ...] = ()


class BrainGraphSettings(NeSettings):
    """Configuration for the Tier-2 entity graph (opt-in, additive deployment)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_BRAIN_GRAPH_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    enabled: bool = False
    database_url: str = "postgresql://localhost:5432/postgres"
    graph_name: str = "ne_graph"
    max_hops: int = Field(default=3, ge=1, le=6)
    default_read_limit: int = Field(default=200, ge=1)
    sources: tuple[GraphSourceMapping, ...] = ()

    ledger_url: str | None = None
    """Optional SQLAlchemy URL for the sync ledger (defaults to ``database_url``)."""

    audit_db_path: Path | None = None
    """Optional local sqlite path for the CLI's dual-append audit engine."""

    # -- NE-082 Splink ER (out-of-process, review-gated) ---------------------
    er_enabled: bool = False
    er_proposal_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    er_classes: dict[str, ErClassConfig] = Field(default_factory=dict)
    er_env_dir: Path | None = None
    """Override for the Splink sidecar env dir (defaults to the in-repo splink_env)."""

    # -- NE-082 graph-aware retrieval + LightRAG (native default) ------------
    lightrag_enabled: bool = False
    lightrag_env_dir: Path | None = None
    graph_expansion_backend: Literal["native", "lightrag"] = "native"
    retrieval_expansion_hops: int = Field(default=1, ge=1, le=3)
    retrieval_max_paths: int = Field(default=5, ge=0)

    # -- NE-T2X extraction (opt-in, review-gated) -----------------------------
    extraction_enabled: bool = False
    """Master gate (design §7). ``False`` (the default) is byte-identical to
    today: no extraction module composed, no gateway task required, no
    ``graph_extraction_candidate`` proposal ever emitted, no new audit events,
    and the ``extraction run|purge`` CLI verbs refuse (design §2.5)."""

    extraction_mode: Literal["ingest_review", "read_flagged", "autonomous"] = "ingest_review"
    """The human-vouching point (design §2.2): ``ingest_review`` gates the
    WRITE (this unit ships only this mode); ``read_flagged``/``autonomous``
    write immediately and ride the SAME proposal as a confirmation/retroactive
    review surface (NE-T2X-4). Enabling extraction defaults to the STRICTEST
    mode — an upgrade/enable never lands in YOLO by omission."""

    extraction_risk_accepted: bool = False
    """Must be ``True`` for ``extraction_mode="autonomous"`` — the auditable
    YOLO consent bit (design B-P1-3): every element written under that mode is
    stamped ``assurance="extracted"`` regardless, so a later downgrade to
    ``read_flagged`` retroactively re-flags the whole corpus for free, but
    ENABLING YOLO in the first place is never a quiet flag flip."""

    extraction_sources: tuple[str, ...] = ()
    """Connector ``source_system``s whose chunks are extraction-eligible
    (per-source trust, design A-P0-2). Empty (the default) means NOTHING is
    eligible even when ``extraction_enabled=True`` — fail-closed."""

    extraction_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    """Below this, a mention is discarded and counted (``discarded_below_threshold``,
    design §3.1). The mode-1 backfill queue this feeds is proportional to
    NEW-entity flux, not raw mention volume (design §2.4): fine at steady
    state, a real multi-day steward campaign on a large first backfill.
    Deployments that cannot staff that campaign should run the backfill in
    ``read_flagged`` and switch to ``ingest_review`` for steady state — an
    explicit, config_hash-visible posture change, not a workaround."""

    extraction_auto_link_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    """A ``link`` assertion at/above this resolves without ever becoming a
    proposal row (design §2.4's queue-shrink mechanism); validated
    ``>= extraction_min_confidence`` below."""

    @model_validator(mode="after")
    def _validate_extraction(self) -> BrainGraphSettings:
        """Combo fail-louds (design §7/D-P1-1), enforced UNCONDITIONALLY — even
        when ``extraction_enabled=False`` — so a config artifact can never even
        EXPRESS an unconsented YOLO mode or an inverted threshold ordering,
        deliberately stricter than gating these checks on the master flag."""
        if self.extraction_auto_link_threshold < self.extraction_min_confidence:
            raise ValueError(
                "extraction_auto_link_threshold must be >= extraction_min_confidence "
                f"(got auto_link={self.extraction_auto_link_threshold}, "
                f"min_confidence={self.extraction_min_confidence})"
            )
        if self.extraction_mode == "autonomous" and not self.extraction_risk_accepted:
            raise ValueError(
                "extraction_mode='autonomous' (YOLO) requires extraction_risk_accepted=True — "
                "an auditable, explicit consent bit (design B-P1-3)"
            )
        if self.extraction_enabled and not self.enabled:
            raise ValueError(
                "extraction_enabled=True requires enabled=True — there is no Tier-2 graph to "
                "extract into (design §7)"
            )
        return self

    @model_validator(mode="after")
    def _validate_er_classes(self) -> BrainGraphSettings:
        for entity_class, cfg in self.er_classes.items():
            if entity_class not in ENTITY_CLASSES:
                raise ValueError(
                    f"er_classes: unknown entity_class {entity_class!r} (not in ENTITY_CLASSES)"
                )
            fields = set(ENTITY_CONTRACTS[entity_class].model_fields)
            for field_name in (*cfg.compare_fields, *cfg.blocking_fields):
                if field_name not in fields:
                    raise ValueError(
                        f"er_classes[{entity_class!r}]: field {field_name!r} is not a "
                        f"contract field of {entity_class}"
                    )
        return self

    def source_for(self, source_system: str) -> GraphSourceMapping | None:
        for mapping in self.sources:
            if mapping.source_system == source_system:
                return mapping
        return None
