# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BrainGraphSettings: env layering, fail-fast mapping validation, config-hash join."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from ne.brain_graph.config import (
    BrainGraphSettings,
    GraphEdgeMapping,
    GraphEntityMapping,
    GraphSourceMapping,
)
from ne.contracts.config import compute_platform_config_hash


def test_defaults() -> None:
    s = BrainGraphSettings()
    assert s.enabled is False
    assert s.graph_name == "ne_graph"
    assert s.max_hops == 3
    assert s.default_read_limit == 200


def test_env_layering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("NE_BRAIN_GRAPH_GRAPH_NAME", "custom_graph")
    monkeypatch.setenv("NE_BRAIN_GRAPH_MAX_HOPS", "5")
    s = BrainGraphSettings()
    assert s.enabled is True
    assert s.graph_name == "custom_graph"
    assert s.max_hops == 5


def test_old_bare_env_no_longer_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collision-prone bare ``NE_`` namespace is disconnected from this class."""
    monkeypatch.setenv("NE_ENABLED", "true")
    monkeypatch.setenv("NE_GRAPH_NAME", "custom_graph")
    monkeypatch.setenv("NE_MAX_HOPS", "5")
    s = BrainGraphSettings()
    assert s.enabled is False
    assert s.graph_name == "ne_graph"
    assert s.max_hops == 3


def test_env_prefix_not_in_config_hash_preimage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default config_hash is byte-identical across the env_prefix change.

    ``env_prefix`` lives in ``model_config`` and never enters the ``model_dump``
    preimage, so with no ``NE_*`` env set the new class and an old-prefix subclass
    produce IDENTICAL platform config_hashes — the T2-wiring/freeze governance proof.
    """
    for name in [k for k in os.environ if k.startswith("NE_")]:
        monkeypatch.delenv(name)

    class _OldBarePrefix(BrainGraphSettings):
        model_config = SettingsConfigDict(
            env_prefix="NE_",
            env_nested_delimiter="__",
            extra="forbid",
            toml_file=Path("ne.toml"),
        )

    assert compute_platform_config_hash(
        {"ne-brain-graph": BrainGraphSettings()}
    ) == compute_platform_config_hash({"ne-brain-graph": _OldBarePrefix()})


def test_toml_keys_unaffected_by_env_prefix(tmp_path: Path) -> None:
    """TOML keys stay bare field names — ``env_prefix`` is not load-bearing for TOML."""
    cfg = tmp_path / "ne.toml"
    cfg.write_text('enabled = true\ngraph_name = "toml_graph"\n')
    s = BrainGraphSettings.from_config_file(cfg)
    assert s.enabled is True
    assert s.graph_name == "toml_graph"


def test_max_hops_bounds() -> None:
    with pytest.raises(ValidationError):
        BrainGraphSettings(max_hops=7)
    with pytest.raises(ValidationError):
        BrainGraphSettings(max_hops=0)


def test_mapping_rejects_unknown_entity_class() -> None:
    with pytest.raises(ValidationError):
        GraphSourceMapping(
            source_system="s",
            entities=(GraphEntityMapping(source_id_prefix="X", entity_class="Bogus"),),
        )


def test_mapping_rejects_invalid_edge_triple() -> None:
    with pytest.raises(ValidationError):
        GraphSourceMapping(
            source_system="s",
            entities=(
                GraphEntityMapping(
                    source_id_prefix="Acc",
                    entity_class="Account",
                    edges=(
                        # Account has no BELONGS_TO edge (that is Ticket->Account).
                        GraphEdgeMapping(
                            payload_key="fk", edge_kind="BELONGS_TO", target_class="Account"
                        ),
                    ),
                ),
            ),
        )


def test_mapping_rejects_duplicate_prefix() -> None:
    with pytest.raises(ValidationError):
        GraphSourceMapping(
            source_system="s",
            entities=(
                GraphEntityMapping(source_id_prefix="Dup", entity_class="Account"),
                GraphEntityMapping(source_id_prefix="Dup", entity_class="Person"),
            ),
        )


def test_valid_edge_triple_accepted() -> None:
    mapping = GraphSourceMapping(
        source_system="s",
        entities=(
            GraphEntityMapping(
                source_id_prefix="Case",
                entity_class="Ticket",
                edges=(
                    GraphEdgeMapping(
                        payload_key="AccountId", edge_kind="BELONGS_TO", target_class="Account"
                    ),
                ),
            ),
        ),
    )
    assert mapping.entities[0].edges[0].edge_kind == "BELONGS_TO"


def test_longest_prefix_wins() -> None:
    mapping = GraphSourceMapping(
        source_system="s",
        entities=(
            GraphEntityMapping(source_id_prefix="public", entity_class="Account"),
            GraphEntityMapping(source_id_prefix="public.tickets", entity_class="Ticket"),
        ),
    )
    resolved = mapping.resolve("public.tickets:42")
    assert resolved is not None
    assert resolved.entity_class == "Ticket"


def test_config_hash_join() -> None:
    settings = BrainGraphSettings(graph_name="g1")
    digest = compute_platform_config_hash({"brain_graph": settings})
    assert isinstance(digest, str)
    # A config change changes the platform hash.
    other = compute_platform_config_hash({"brain_graph": BrainGraphSettings(graph_name="g2")})
    assert digest != other


def test_er_and_retrieval_defaults() -> None:
    s = BrainGraphSettings()
    assert s.er_enabled is False
    assert s.er_proposal_threshold == 0.95
    assert s.er_classes == {}
    assert s.lightrag_enabled is False
    assert s.graph_expansion_backend == "native"
    assert s.retrieval_expansion_hops == 1
    assert s.retrieval_max_paths == 5


def test_er_classes_valid_config_accepted() -> None:
    from ne.brain_graph.config import ErClassConfig

    s = BrainGraphSettings(
        er_classes={
            "Person": ErClassConfig(
                compare_fields=("given_name", "family_name", "email"),
                blocking_fields=("email",),
            )
        }
    )
    assert "Person" in s.er_classes


def test_er_classes_rejects_unknown_entity_class() -> None:
    from ne.brain_graph.config import ErClassConfig

    with pytest.raises(ValidationError):
        BrainGraphSettings(er_classes={"Nope": ErClassConfig(compare_fields=("x",))})


def test_er_classes_rejects_unknown_field() -> None:
    from ne.brain_graph.config import ErClassConfig

    with pytest.raises(ValidationError):
        BrainGraphSettings(er_classes={"Person": ErClassConfig(compare_fields=("not_a_field",))})


def test_retrieval_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        BrainGraphSettings(retrieval_expansion_hops=4)
    with pytest.raises(ValidationError):
        BrainGraphSettings(er_proposal_threshold=0.4)


# -- NE-T2X-2: extraction settings -------------------------------------------


def test_extraction_defaults() -> None:
    s = BrainGraphSettings()
    assert s.extraction_enabled is False
    assert s.extraction_mode == "ingest_review"
    assert s.extraction_risk_accepted is False
    assert s.extraction_sources == ()
    assert s.extraction_min_confidence == 0.7
    assert s.extraction_auto_link_threshold == 0.9


def test_extraction_autonomous_requires_risk_acceptance() -> None:
    with pytest.raises(ValidationError):
        BrainGraphSettings(extraction_mode="autonomous")
    # Enforced UNCONDITIONALLY — even with extraction_enabled left at default False.
    with pytest.raises(ValidationError):
        BrainGraphSettings(extraction_mode="autonomous", extraction_risk_accepted=False)
    s = BrainGraphSettings(
        enabled=True,
        extraction_enabled=True,
        extraction_mode="autonomous",
        extraction_risk_accepted=True,
    )
    assert s.extraction_mode == "autonomous"


def test_extraction_requires_graph_enabled() -> None:
    with pytest.raises(ValidationError):
        BrainGraphSettings(extraction_enabled=True)
    s = BrainGraphSettings(enabled=True, extraction_enabled=True)
    assert s.extraction_enabled is True


def test_extraction_threshold_ordering_enforced() -> None:
    with pytest.raises(ValidationError):
        BrainGraphSettings(extraction_min_confidence=0.8, extraction_auto_link_threshold=0.5)
    s = BrainGraphSettings(extraction_min_confidence=0.5, extraction_auto_link_threshold=0.5)
    assert s.extraction_auto_link_threshold == s.extraction_min_confidence


def test_extraction_fields_move_platform_config_hash() -> None:
    """The ONE declared default-visible delta (design B-P1-2/§2.5): adding these
    fields to BrainGraphSettings moves the platform config_hash once for every
    deployment that composes brain_graph — verified here directly, and the
    real join sites are grep-verified in the builder's final report."""
    baseline = compute_platform_config_hash({"brain_graph": BrainGraphSettings(enabled=True)})
    variant = compute_platform_config_hash(
        {
            "brain_graph": BrainGraphSettings(
                enabled=True, extraction_enabled=True, extraction_sources=("x",)
            )
        }
    )
    assert baseline != variant
